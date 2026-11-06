"""AP-3: the mechanical gates of the red-first adversarial review loop.

The loop that produced kibisis PR #29 by hand: a fresh reviewer with a new cut writes findings as tests, the
tests are committed as evidence BEFORE a fixer exists, a fixer works on its own branch, the fix is admitted only
if the finding tests go green and the suite stays green, and the next round starts from the merged head.

This module holds the parts that must not be left to an agent's self-report:

* `open_job`      isolated worktrees from the target's head; cut chosen by rotation; briefs written.
* `gate_findings` a finding test is admitted only if it FAILS on the unpatched head (pytest rc 1); a test that
                  passes (rc 0) or does not run (rc >= 2) is refused. Admitted evidence is committed on the
                  integration branch before any fixer is briefed.
* `open_fix`      the fixer's branch, from the integration head.
* `gate_fix`      the finding tests must be byte-identical to the committed evidence, must pass, and the whole
                  suite must pass with the configured environment variables stripped; only then is the branch
                  merged into the integration branch.
* `should_continue` bounded rounds; stop at zero highs.
* `close_job`     push the integration branch by SHA and open a PR (a human merges), or close with no PR.

Each gate appends one receipt to the Argus ledger when one is configured (payload ints/strings only).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

GIT_IDENTITY = ["-c", "user.name=Hermes", "-c", "user.email=hermes@perseuscomputing.com"]
SEVERITY_RE = re.compile(r"^[\s\"']*severity:\s*(high|medium|low)\s*$", re.I | re.M)
DEFAULT_ENV_STRIP = ["KIBISIS_PASSPHRASE", "KIBISIS_PASSPHRASE_FILE", "KIBISIS_SRC", "CI"]


@dataclasses.dataclass
class RedTeamConfig:
    repo_path: str
    test_command: List[str]                       # e.g. [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    cuts: List[str]
    rotation_path: str
    jobs_root: str
    python_path: Optional[str] = "src"            # PYTHONPATH relative to the worktree, or None
    ledger_path: Optional[str] = None
    max_rounds: int = 2
    env_strip: List[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_ENV_STRIP))
    base_branch: str = "main"
    test_dir: str = "tests"
    notes_dir: str = "review_notes"
    gh_token_cmd: Optional[str] = None            # command printing a GitHub token, for close_job
    actor: str = "autopoiesis.redteam"

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RedTeamConfig":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class Job:
    job_id: str
    path: str
    integration: str
    review: str
    fix: Optional[str]
    base_head: str
    cut: str
    repo: str


# --------------------------------------------------------------------------------------------- helpers
def _git(cwd: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *GIT_IDENTITY, *args], cwd=str(cwd), check=check, capture_output=True, text=True)


def _out(cwd: str | Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _state_path(job: Job) -> Path:
    return Path(job.path) / "job.json"


def _load(job: Job) -> Dict[str, Any]:
    return json.loads(_state_path(job).read_text())


def _save(job: Job, state: Dict[str, Any]) -> None:
    tmp = _state_path(job).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, _state_path(job))


def _receipt(cfg: RedTeamConfig, event_type: str, payload: Dict[str, Any]) -> Optional[int]:
    """One Argus receipt per gate; ints and strings only. Returns the sequence, or None without a ledger."""
    if not cfg.ledger_path:
        return None
    try:
        from argus_ledger.ledger import AppendOnlyLedger  # type: ignore
    except ImportError:  # the argus checkout on this box, when the package is not installed
        argus_src = Path("/opt/data/work/telarch-suite/argus/src")
        if argus_src.is_dir() and str(argus_src) not in sys.path:
            sys.path.insert(0, str(argus_src))
        from argus_ledger.ledger import AppendOnlyLedger  # type: ignore
    clean = {k: (v if isinstance(v, int) and not isinstance(v, bool) else str(v)) for k, v in payload.items()}
    rc = AppendOnlyLedger(cfg.ledger_path).append(event_type=event_type, actor=cfg.actor, payload=clean)
    return int(rc.sequence)


def _test_env(cfg: RedTeamConfig, worktree: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in set(cfg.env_strip)}
    if cfg.python_path:
        env["PYTHONPATH"] = str(Path(worktree) / cfg.python_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_tests(cfg: RedTeamConfig, worktree: str, targets: List[str], log: Path) -> int:
    """Run the configured test command on `targets` (or the suite's test dir). Returns the exit code."""
    cmd = [*cfg.test_command, *(targets or [cfg.test_dir])]
    with open(log, "w") as fh:
        p = subprocess.run(cmd, cwd=worktree, env=_test_env(cfg, worktree), stdout=fh, stderr=subprocess.STDOUT)
    return p.returncode


def _severity(path: Path) -> str:
    m = SEVERITY_RE.search(path.read_text(errors="replace")[:2000])
    return m.group(1).lower() if m else "medium"


def _finding_name(path: Path, round_n: int) -> str:
    return path.name[len(f"review{round_n}_"):-3]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------------------------- rotation
def next_cut(rotation_path: str | Path, repo: str, cuts: List[str]) -> str:
    """The first cut this repo has not had this lap; wraps when every cut has been used. History is kept."""
    path = Path(rotation_path)
    hist: Dict[str, List[str]] = json.loads(path.read_text()) if path.is_file() else {}
    used = hist.get(repo, [])
    lap = used[-(len(used) % len(cuts)):] if len(used) % len(cuts) else []
    cut = next(c for c in cuts if c not in lap)
    hist.setdefault(repo, []).append(cut)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hist, indent=1, sort_keys=True))
    return cut


# --------------------------------------------------------------------------------------------- job lifecycle
def open_job(cfg: RedTeamConfig, job_id: str, cut: Optional[str] = None) -> Job:
    repo = Path(cfg.repo_path).resolve()
    if _out(repo, "status", "--porcelain"):
        raise RuntimeError(f"target worktree is dirty: {repo}")
    base_head = _out(repo, "rev-parse", cfg.base_branch)
    job_dir = Path(cfg.jobs_root) / job_id
    if job_dir.exists():
        raise RuntimeError(f"job exists: {job_dir}")
    job_dir.mkdir(parents=True)
    integ, review = job_dir / "integration", job_dir / "review"
    _git(repo, "worktree", "add", "-q", "-b", f"redteam/{job_id}", str(integ), base_head)
    _git(repo, "worktree", "add", "-q", "--detach", str(review), base_head)
    cut = cut or next_cut(cfg.rotation_path, repo.name, cfg.cuts)
    job = Job(job_id, str(job_dir), str(integ), str(review), None, base_head, cut, str(repo))
    _save(job, {"job_id": job_id, "repo": str(repo), "base_head": base_head, "cut": cut, "status": "open",
                "opened_at": int(time.time()), "rounds": [], "branch": f"redteam/{job_id}"})
    (job_dir / "reviewer_brief.md").write_text(reviewer_brief(cfg, job, 1))
    seq = _receipt(cfg, "redteam.job_opened", {"job_id": job_id, "repo": repo.name, "base_head": base_head, "cut": cut,
                                               "max_rounds": cfg.max_rounds})
    if seq is not None:
        st = _load(job); st["receipts"] = [seq]; _save(job, st)
    return job


def load_job(cfg: RedTeamConfig, job_id: str) -> Job:
    job_dir = Path(cfg.jobs_root) / job_id
    st = json.loads((job_dir / "job.json").read_text())
    fix = job_dir / "fix"
    return Job(job_id, str(job_dir), str(job_dir / "integration"), str(job_dir / "review"),
               str(fix) if fix.is_dir() else None, st["base_head"], st["cut"], st["repo"])


def reviewer_brief(cfg: RedTeamConfig, job: Job, round_n: int) -> str:
    head = _out(job.integration, "rev-parse", "HEAD")
    return f"""# Reviewer brief — round {round_n}, cut: {job.cut}

Worktree: `{job.review}` (detached at `{head[:12]}`). Repository: `{Path(job.repo).name}`.

Review this code adversarially through the cut **{job.cut}**. Read SECURITY.md and the threat model first.

Prove every finding with a test that FAILS on this head: `{cfg.test_dir}/review{round_n}_<name>.py`, module
docstring first lines `severity: high|medium|low` and `finding: <one line>`. The harness runs each file on the
unpatched head and keeps only the ones that fail (pytest exit 1); a test that passes or does not run is discarded,
so run yours before you finish. Do not fix anything, do not edit existing source or tests. Write the narrative
with file:line references to `{cfg.notes_dir}/R{round_n}.md`. Severity high = integrity, confidentiality,
atomicity or lease/fencing guarantees broken, or data loss; medium = wrong result under realistic misuse;
low = everything else.

Test invocation: `{' '.join(cfg.test_command)} {cfg.test_dir}/review{round_n}_<name>.py` from the worktree,
with PYTHONPATH `{cfg.python_path or ''}` and these variables unset: {', '.join(cfg.env_strip)}.
"""


def fixer_brief(cfg: RedTeamConfig, job: Job, round_n: int) -> str:
    st = _load(job)
    rnd = st["rounds"][round_n - 1]
    files = "\n".join(f"- `{cfg.test_dir}/review{round_n}_{f['name']}.py` ({f['severity']})" for f in rnd["accepted"])
    return f"""# Fixer brief — round {round_n}, cut: {job.cut}

Worktree: `{job.fix}` on branch `redteam/{job.job_id}-fix{round_n}`. Findings, each proven by a test that fails on
this head (notes in `{cfg.notes_dir}/R{round_n}.md`):
{files}

Fix the class of defect, not the witness. Do not edit any `review{round_n}_*.py` file: the harness refuses a
branch where their bytes differ from the committed evidence. The harness admits the branch only when every
finding test passes and the whole suite (`{' '.join(cfg.test_command)} {cfg.test_dir}`) passes with
{', '.join(cfg.env_strip)} unset. Commit your work on this branch (any number of commits). Do not run build
tools that write outside the worktree.
"""


def gate_findings(cfg: RedTeamConfig, job: Job, round_n: int) -> Dict[str, Any]:
    """Admit only finding tests that fail on the unpatched head; commit the evidence on the integration branch."""
    review = Path(job.review)
    logs = Path(job.path) / "logs"; logs.mkdir(exist_ok=True)
    accepted, rejected = [], []
    for test in sorted((review / cfg.test_dir).glob(f"review{round_n}_*.py")):
        name = _finding_name(test, round_n)
        rc = _run_tests(cfg, job.review, [str(test.relative_to(review))], logs / f"r{round_n}_head_{name}.log")
        if rc == 1:
            accepted.append({"name": name, "severity": _severity(test), "sha256": _sha(test), "file": test.name})
        else:
            rejected.append({"name": name, "reason": "passes_on_head" if rc == 0 else "does_not_run", "rc": rc})
    integ = Path(job.integration)
    for f in accepted:
        shutil.copy2(review / cfg.test_dir / f["file"], integ / cfg.test_dir / f["file"])
    notes = review / cfg.notes_dir / f"R{round_n}.md"
    if notes.is_file():
        (integ / cfg.notes_dir).mkdir(exist_ok=True)
        shutil.copy2(notes, integ / cfg.notes_dir / notes.name)
    if accepted or notes.is_file():
        _git(integ, "add", "-A")
        _git(integ, "commit", "-qm", f"review {round_n} ({job.cut}): {len(accepted)} finding(s) proven on "
                                      f"{job.base_head[:12]}; {len(rejected)} refused")
    highs = sum(1 for f in accepted if f["severity"] == "high")
    st = _load(job)
    st["rounds"].append({"round": round_n, "accepted": accepted, "rejected": rejected, "highs": highs,
                         "evidence_head": _out(integ, "rev-parse", "HEAD"), "gated_at": int(time.time())})
    st["status"] = "findings_gated" if accepted else "no_findings"
    _save(job, st)
    seq = _receipt(cfg, "redteam.findings_gated", {"job_id": job.job_id, "round": round_n, "cut": job.cut,
                                                   "accepted": len(accepted), "rejected": len(rejected), "highs": highs,
                                                   "evidence_head": st["rounds"][-1]["evidence_head"]})
    if seq is not None:
        st = _load(job); st.setdefault("receipts", []).append(seq); _save(job, st)
    return {"accepted": accepted, "rejected": rejected, "highs": highs, "receipt": seq}


def open_fix(cfg: RedTeamConfig, job: Job, round_n: int) -> str:
    """The fixer's worktree on its own branch from the integration head (which carries the evidence)."""
    fix = Path(job.path) / "fix"
    if fix.exists():
        _git(job.repo, "worktree", "remove", "--force", str(fix))
    head = _out(job.integration, "rev-parse", "HEAD")
    _git(job.repo, "branch", "-q", "-D", f"redteam/{job.job_id}-fix{round_n}", check=False)
    _git(job.repo, "worktree", "add", "-q", "-b", f"redteam/{job.job_id}-fix{round_n}", str(fix), head)
    job.fix = str(fix)
    (Path(job.path) / f"fixer_brief_r{round_n}.md").write_text(fixer_brief(cfg, job, round_n))
    return str(fix)


def gate_fix(cfg: RedTeamConfig, job: Job, round_n: int) -> Dict[str, Any]:
    """Admit the fix branch only if the evidence is untouched, the findings pass, and the suite passes."""
    st = _load(job)
    rnd = st["rounds"][round_n - 1]
    fix = Path(job.fix or (Path(job.path) / "fix"))
    logs = Path(job.path) / "logs"; logs.mkdir(exist_ok=True)
    if _out(fix, "status", "--porcelain"):
        _git(fix, "add", "-A"); _git(fix, "commit", "-qm", f"fix round {round_n}: uncommitted work")

    def refuse(reason: str, **extra: Any) -> Dict[str, Any]:
        rnd["fix"] = "rejected"; rnd["fix_reason"] = reason; st["status"] = "fix_rejected"; _save(job, st)
        seq = _receipt(cfg, "redteam.fix_gated", {"job_id": job.job_id, "round": round_n, "verdict": "rejected",
                                                  "reason": reason, "fix_head": _out(fix, "rev-parse", "HEAD")})
        return {"verdict": "rejected", "reason": reason, "receipt": seq, **extra}

    modified = [f["file"] for f in rnd["accepted"] if not (fix / cfg.test_dir / f["file"]).is_file()
                or _sha(fix / cfg.test_dir / f["file"]) != f["sha256"]]
    if modified:
        return refuse("finding_tests_modified", files=modified)
    targets = [f"{cfg.test_dir}/{f['file']}" for f in rnd["accepted"]]
    if _run_tests(cfg, str(fix), targets, logs / f"r{round_n}_fix_findings.log") != 0:
        return refuse("findings_still_red")
    if _run_tests(cfg, str(fix), [], logs / f"r{round_n}_fix_suite.log") != 0:
        return refuse("suite_red")
    fix_head = _out(fix, "rev-parse", "HEAD")
    _git(job.integration, "merge", "-q", "--no-ff", "-m", f"fix round {round_n} ({job.cut})", fix_head)
    rnd["fix"] = "merged"; rnd["fix_head"] = fix_head; rnd["merged_head"] = _out(job.integration, "rev-parse", "HEAD")
    st["status"] = "fix_merged"; _save(job, st)
    # the next reviewer starts from the merged head, on a clean slate (the previous reviewer's refused files go)
    _git(job.review, "checkout", "-q", "-f", "--detach", rnd["merged_head"])
    _git(job.review, "clean", "-qfd")
    (Path(job.path) / "reviewer_brief.md").write_text(reviewer_brief(cfg, job, round_n + 1))
    seq = _receipt(cfg, "redteam.fix_gated", {"job_id": job.job_id, "round": round_n, "verdict": "merged",
                                              "fix_head": fix_head, "merged_head": rnd["merged_head"],
                                              "findings": len(rnd["accepted"])})
    if seq is not None:
        st = _load(job); st.setdefault("receipts", []).append(seq); _save(job, st)
    return {"verdict": "merged", "fix_head": fix_head, "merged_head": rnd["merged_head"], "receipt": seq}


def should_continue(cfg: RedTeamConfig, job: Job) -> bool:
    """Another round only while budget remains and the last review found at least one high."""
    rounds = _load(job)["rounds"]
    if not rounds or len(rounds) >= cfg.max_rounds:
        return False
    return rounds[-1]["highs"] > 0


def close_job(cfg: RedTeamConfig, job: Job, open_pr: bool = True) -> Dict[str, Any]:
    """Push the integration branch by SHA and open a PR for a human to merge; or close with nothing to show."""
    st = _load(job)
    merged = [r for r in st["rounds"] if r.get("fix") == "merged"]
    head = _out(job.integration, "rev-parse", "HEAD")
    result: Dict[str, Any] = {"job_id": job.job_id, "head": head, "rounds": len(st["rounds"]),
                              "merged_rounds": len(merged), "pr": None}
    if merged and open_pr and cfg.gh_token_cmd:
        token = subprocess.check_output(cfg.gh_token_cmd, shell=True, text=True).strip()
        url = _out(job.repo, "remote", "get-url", "origin")
        m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", url)
        if not m:
            raise RuntimeError(f"not a GitHub remote: {url}")
        org, name = m.group(1), m.group(2)
        push_url = f"https://x-access-token:{token}@github.com/{org}/{name}.git"
        _git(job.repo, "push", "-q", push_url, f"{head}:refs/heads/{st['branch']}")
        remote = json.load(urllib.request.urlopen(urllib.request.Request(
            f"https://api.github.com/repos/{org}/{name}/git/commits/{head}",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "autopoiesis"})))
        if remote.get("sha") != head:
            raise RuntimeError("pushed head not found on remote")
        body = pr_body(cfg, job)
        req = urllib.request.Request(f"https://api.github.com/repos/{org}/{name}/pulls",
                                     data=json.dumps({"title": f"redteam {job.job_id}: {job.cut} "
                                                               f"({sum(len(r['accepted']) for r in merged)} findings fixed)",
                                                      "head": st["branch"], "base": cfg.base_branch, "body": body}).encode(),
                                     headers={"Authorization": f"Bearer {token}", "User-Agent": "autopoiesis",
                                              "Content-Type": "application/json"}, method="POST")
        pr = json.load(urllib.request.urlopen(req))
        result["pr"] = pr["html_url"]
    st["status"] = "closed_pr" if result["pr"] else "closed"
    st["closed_at"] = int(time.time()); st["final_head"] = head; st["pr"] = result["pr"]
    _save(job, st)
    seq = _receipt(cfg, "redteam.job_closed", {"job_id": job.job_id, "cut": job.cut, "rounds": len(st["rounds"]),
                                               "merged_rounds": len(merged), "final_head": head,
                                               "pr": result["pr"] or "none"})
    result["receipt"] = seq
    for wt in ("review", "fix", "integration"):   # the branch ref stays in the target repo; the worktrees are scratch
        p = Path(job.path) / wt
        if p.is_dir():
            _git(job.repo, "worktree", "remove", "--force", str(p), check=False)
    return result


def pr_body(cfg: RedTeamConfig, job: Job) -> str:
    st = _load(job)
    lines = [f"Automated red-first review, cut **{job.cut}**, base `{job.base_head[:12]}`. "
             f"Each finding is a test committed before the fix; the fix was admitted only with the finding tests "
             f"untouched and green and the suite green with {', '.join(cfg.env_strip)} unset. A human merges.", ""]
    for r in st["rounds"]:
        lines.append(f"## Round {r['round']} — {len(r['accepted'])} proven, {len(r['rejected'])} refused, "
                     f"{r['highs']} high — fix: {r.get('fix', 'none')}")
        for f in r["accepted"]:
            lines.append(f"- **{f['severity']}** `{cfg.test_dir}/{f['file']}`")
        lines.append(f"\nNotes: `{cfg.notes_dir}/R{r['round']}.md`. Receipts: {st.get('receipts', [])}\n")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------- CLI
def cli(argv: List[str], cfg_dict: Dict[str, Any]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="autopoiesis redteam")
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open"); o.add_argument("--job", required=True); o.add_argument("--cut")
    g = sub.add_parser("gate-findings"); g.add_argument("--job", required=True); g.add_argument("--round", type=int, required=True)
    f = sub.add_parser("open-fix"); f.add_argument("--job", required=True); f.add_argument("--round", type=int, required=True)
    x = sub.add_parser("gate-fix"); x.add_argument("--job", required=True); x.add_argument("--round", type=int, required=True)
    c = sub.add_parser("continue"); c.add_argument("--job", required=True)
    z = sub.add_parser("close"); z.add_argument("--job", required=True); z.add_argument("--no-pr", action="store_true")
    s = sub.add_parser("status"); s.add_argument("--job", required=True)
    a = p.parse_args(argv)
    cfg = RedTeamConfig.from_dict(cfg_dict)
    if a.cmd == "open":
        job = open_job(cfg, a.job, a.cut)
        out: Any = {**dataclasses.asdict(job), "reviewer_brief": str(Path(job.path) / "reviewer_brief.md")}
    else:
        job = load_job(cfg, a.job)
        if a.cmd == "gate-findings":
            out = gate_findings(cfg, job, a.round)
        elif a.cmd == "open-fix":
            out = {"fix": open_fix(cfg, job, a.round), "fixer_brief": str(Path(job.path) / f"fixer_brief_r{a.round}.md")}
        elif a.cmd == "gate-fix":
            out = gate_fix(cfg, job, a.round)
        elif a.cmd == "continue":
            out = {"continue": should_continue(cfg, job)}
        elif a.cmd == "close":
            out = close_job(cfg, job, open_pr=not a.no_pr)
        else:
            out = _load(job)
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0

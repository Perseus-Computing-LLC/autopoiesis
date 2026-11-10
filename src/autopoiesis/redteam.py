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
* `gate_fix`      judges the COMMIT at the tip of the fix branch, checked out fresh from git objects (never the
                  fixer's working directory): the evidence of every round so far must be byte-identical to
                  what was committed and must pass; the whole suite must pass with the configured environment
                  variables stripped; every test the suite ran on the evidence head must still be collected
                  and not skipped; the diff must change something outside the test and notes directories and
                  must not touch pytest's control files. Only then is that commit merged.
* `should_continue` bounded rounds; stop at zero highs.
* `close_job`     push the integration branch by SHA and open a PR (a human merges), or close with no PR.

Each gate appends one receipt to the Argus ledger when one is configured (payload ints/strings only).

The test command must be pytest-compatible: the gates read exit codes (0 green, 1 red, >= 2 did not run) and the
JUnit report (`--junitxml`) for which tests ran and how they ended.

What the gates do NOT establish: the tests are agent-written code executed without a sandbox, so a product
module that tampers with the test run from inside (rewriting reports, patching pytest at import) is not
detected; an existing test whose body the fixer rewrote keeps its id and is only disclosed (job.json, PR body),
not refused; severity is the reviewer's own declaration.
"""
from __future__ import annotations

import configparser
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

GIT_IDENTITY = ["-c", "user.name=Hermes", "-c", "user.email=hermes@perseuscomputing.com"]
SEVERITY_RE = re.compile(r"^[\s\"']*severity:\s*(high|medium|low)\s*$", re.I | re.M)
DEFAULT_ENV_STRIP = ["KIBISIS_PASSPHRASE", "KIBISIS_PASSPHRASE_FILE", "KIBISIS_SRC", "CI"]
# Files that steer the test run itself. A fix branch may not change them (gate_fix: test_infrastructure_modified).
TEST_CONTROL_FILES = frozenset({"conftest.py", "pytest.ini", ".pytest.ini", "sitecustomize.py", "usercustomize.py"})
# Shared config files: only their pytest section is protected, the rest (dependencies, metadata) may change.
TEST_CONTROL_SHARED = frozenset({"pyproject.toml", "setup.cfg", "tox.ini"})


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
    allow_test_config_changes: bool = False       # let a fix branch edit conftest.py / pytest configuration
    api_timeout: float = 30.0                     # seconds, per GitHub API call in close_job

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
def _git(cwd: str | Path, *args: str, check: bool = True,
         env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *GIT_IDENTITY, *args], cwd=str(cwd), check=check, capture_output=True, text=True,
                          env=env)


def _out(cwd: str | Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _blob(repo: str | Path, commit: str, rel: str) -> Optional[bytes]:
    """The bytes git holds for `rel` in `commit` (None when it is not a blob there). Never reads a working tree."""
    p = subprocess.run(["git", "cat-file", "blob", f"{commit}:{rel}"], cwd=str(repo), capture_output=True)
    return p.stdout if p.returncode == 0 else None


def _diff_status(repo: str | Path, a: str, b: str) -> Dict[str, str]:
    """path -> A/M/D/T for the net change between two commits (renames shown as delete + add)."""
    raw = subprocess.run(["git", "diff", "--name-status", "--no-renames", "-z", a, b], cwd=str(repo), check=True,
                         capture_output=True).stdout.decode("utf-8", "surrogateescape")
    parts = raw.split("\0")
    return {parts[i + 1]: parts[i] for i in range(0, len(parts) - 1, 2)}


@contextlib.contextmanager
def _clean_checkout(job: Job, name: str, commit: str) -> Iterator[Path]:
    """A fresh detached checkout of `commit` made from git objects: no untracked, ignored or edited files."""
    path = Path(job.path) / "gate" / name

    def drop() -> None:
        _git(job.repo, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
        _git(job.repo, "worktree", "prune", check=False)

    drop()                                                   # a checkout left behind by a killed gate
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(job.repo, "worktree", "add", "-q", "--detach", str(path), commit)
    try:
        yield path
    finally:
        drop()


def _pristine(checkout: Path) -> None:
    """Undo whatever a test run left in a gate checkout before the next run uses it."""
    _git(checkout, "reset", "-q", "--hard")
    _git(checkout, "clean", "-qfdx")


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


def _run_tests(cfg: RedTeamConfig, worktree: str | Path, targets: List[str], log: Path,
               junit: Optional[Path] = None) -> int:
    """Run the configured test command on `targets` (or the suite's test dir). Returns the exit code.

    With `junit`, a JUnit report is written there (outside the tree under test) for `_junit_outcomes`.
    """
    cmd = [*cfg.test_command, *(targets or [cfg.test_dir])]
    if junit is not None:
        junit.unlink(missing_ok=True)
        cmd.append(f"--junitxml={junit}")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as fh:
        p = subprocess.run(cmd, cwd=str(worktree), env=_test_env(cfg, str(worktree)), stdout=fh,
                           stderr=subprocess.STDOUT)
    return p.returncode


_OUTCOME_RANK = {"passed": 0, "skipped": 1, "error": 2, "failed": 3}


def _junit_outcomes(junit: Path) -> Optional[Dict[str, Dict[str, str]]]:
    """test id -> {outcome: passed|skipped|error|failed, message}. None when there is no readable report."""
    try:
        root = ET.parse(junit).getroot()
    except (OSError, ET.ParseError):
        return None
    out: Dict[str, Dict[str, str]] = {}
    for case in root.iter("testcase"):
        tid = f"{case.get('classname', '')}::{case.get('name', '')}"
        outcome, message = "passed", ""
        for child in case:
            kind = {"failure": "failed", "error": "error", "skipped": "skipped"}.get(child.tag)
            if kind and _OUTCOME_RANK[kind] > _OUTCOME_RANK[outcome]:
                outcome, message = kind, child.get("message") or ""
        if tid not in out or _OUTCOME_RANK[outcome] > _OUTCOME_RANK[out[tid]["outcome"]]:
            out[tid] = {"outcome": outcome, "message": message}       # setup/call/teardown rows: the worst wins
    return out


def _module_id(cfg: RedTeamConfig, file_name: str) -> str:
    """The JUnit classname prefix pytest gives the tests of `<test_dir>/<file_name>`."""
    return ".".join([*Path(cfg.test_dir).parts, file_name[:-3]])


def _in_module(tid: str, module: str) -> bool:
    cls = tid.split("::", 1)[0]
    return cls == module or cls.startswith(module + ".")


def _under(rel: str, directory: str) -> bool:
    d = directory.strip("/")
    return rel == d or rel.startswith(d + "/")


def _pytest_section(name: str, data: Optional[bytes]) -> Any:
    """The part of a shared config file that steers pytest (compared between evidence head and candidate)."""
    if data is None:
        return None
    text = data.decode("utf-8", "replace")
    if name == "pyproject.toml":
        try:
            return tomllib.loads(text).get("tool", {}).get("pytest")
        except (tomllib.TOMLDecodeError, AttributeError):
            return ("unparseable", text)
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error:
        return ("unparseable", text)
    return {s: dict(parser.items(s)) for s in parser.sections() if s in ("pytest", "tool:pytest")}


def _test_control_changes(repo: str | Path, base: str, cand: str, changed: Dict[str, str]) -> List[str]:
    """Changed paths that decide what the test run collects, skips or reports."""
    hits = []
    for rel in sorted(changed):
        name = rel.rsplit("/", 1)[-1]
        if name in TEST_CONTROL_FILES:
            hits.append(rel)
        elif name in TEST_CONTROL_SHARED and \
                _pytest_section(name, _blob(repo, base, rel)) != _pytest_section(name, _blob(repo, cand, rel)):
            hits.append(rel)
    return hits


def _round(state: Dict[str, Any], round_n: int) -> Dict[str, Any]:
    """The record of round `round_n`; refuses a number that is not a gated round (rounds are 1..n in order)."""
    rounds = state["rounds"]
    if not isinstance(round_n, int) or not 1 <= round_n <= len(rounds) or rounds[round_n - 1].get("round") != round_n:
        raise RuntimeError(f"round {round_n} has not been gated (gated rounds: {[r.get('round') for r in rounds]})")
    return rounds[round_n - 1]


def _require_open(state: Dict[str, Any]) -> None:
    if str(state.get("status", "")).startswith("closed"):
        raise RuntimeError(f"job {state.get('job_id')} is closed")


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
    rnd = _round(st, round_n)
    files = "\n".join(f"- `{cfg.test_dir}/review{round_n}_{f['name']}.py` ({f['severity']})" for f in rnd["accepted"])
    return f"""# Fixer brief — round {round_n}, cut: {job.cut}

Worktree: `{job.fix}` on branch `redteam/{job.job_id}-fix{round_n}`. Findings, each proven by a test that fails on
this head (notes in `{cfg.notes_dir}/R{round_n}.md`):
{files}

Fix the class of defect, not the witness. Commit your work on this branch (any number of commits): the harness
judges the commit at the tip of the branch from a fresh checkout, so uncommitted, untracked and git-ignored files
in this worktree are not part of the fix and are never merged. The branch is admitted only when

- no `review<N>_*.py` evidence file and no `{cfg.notes_dir}/R<N>.md` of this or an earlier round differs from
  what was committed, and every one of those finding tests passes (not skipped);
- the whole suite (`{' '.join(cfg.test_command)} {cfg.test_dir}`) passes with {', '.join(cfg.env_strip)} unset,
  and every test it ran before your change is still collected and not skipped (do not delete, rename, skip or
  deselect existing tests);
- the diff changes at least one file outside `{cfg.test_dir}/` and `{cfg.notes_dir}/`, and does not touch
  conftest.py, pytest.ini, sitecustomize.py or the pytest section of pyproject.toml / setup.cfg / tox.ini.

Existing tests you edit are listed in the pull request for the human reviewer. Do not run build tools that write
outside the worktree.
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


def _evidence(state: Dict[str, Any], cfg: RedTeamConfig, upto: int) -> List[Dict[str, Any]]:
    """Every accepted finding of rounds 1..upto, with its path relative to the repo."""
    return [{**f, "round": r["round"], "rel": f"{cfg.test_dir}/{f['file']}"}
            for r in state["rounds"][:upto] for f in r["accepted"]]


def gate_fix(cfg: RedTeamConfig, job: Job, round_n: int) -> Dict[str, Any]:
    """Admit the fix branch only if its tip commit, checked out clean, keeps all evidence intact and green,
    keeps the suite green without dropping or skipping tests, and changes product code."""
    st = _load(job)
    _require_open(st)
    if st.get("pending"):
        raise RuntimeError("an evidence commit is unfinished; re-run gate-findings first")
    rnd = _round(st, round_n)
    if round_n != len(st["rounds"]):
        raise RuntimeError(f"round {round_n} is not the current round ({len(st['rounds'])})")
    if rnd.get("fix") == "merged":
        raise RuntimeError(f"round {round_n} is already merged")
    if not rnd["accepted"]:
        raise RuntimeError(f"round {round_n} has no accepted findings; there is nothing to fix")
    integ = Path(job.integration)
    logs = Path(job.path) / "logs"; logs.mkdir(exist_ok=True)
    evidence_head = rnd["evidence_head"]
    if _out(integ, "rev-parse", "HEAD") != evidence_head or _out(integ, "status", "--porcelain"):
        raise RuntimeError(f"integration worktree is not at the clean evidence head {evidence_head[:12]}")
    branch = f"redteam/{job.job_id}-fix{round_n}"
    # The candidate is the commit the branch points at. Nothing is read from, or added out of, the fixer's
    # working directory: uncommitted, untracked and git-ignored files there are not part of the fix.
    cand = _git(job.repo, "rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}", check=False).stdout.strip()

    def refuse(reason: str, **extra: Any) -> Dict[str, Any]:
        rnd["fix"] = "rejected"; rnd["fix_reason"] = reason; rnd["fix_head"] = cand or "none"
        st["status"] = "fix_rejected"; _save(job, st)
        seq = _receipt(cfg, "redteam.fix_gated", {"job_id": job.job_id, "round": round_n, "verdict": "rejected",
                                                  "reason": reason, "fix_head": cand or "none"})
        if seq is not None:
            st.setdefault("receipts", []).append(seq); _save(job, st)
        return {"verdict": "rejected", "reason": reason, "receipt": seq, **extra}

    if not cand:
        return refuse("no_fix_branch", branch=branch)
    if cand == evidence_head:
        fix = Path(job.fix or (Path(job.path) / "fix"))
        left = _git(fix, "status", "--porcelain", check=False).stdout.splitlines() if fix.is_dir() else []
        return refuse("no_commits", uncommitted=len(left))
    if _git(job.repo, "merge-base", "--is-ancestor", evidence_head, cand, check=False).returncode != 0:
        return refuse("not_based_on_evidence")

    # 1. evidence of every round so far, compared as git blobs of the candidate commit
    evidence = _evidence(st, cfg, round_n)
    modified = []
    for f in evidence:
        data = _blob(job.repo, cand, f["rel"])
        if data is None or hashlib.sha256(data).hexdigest() != f["sha256"]:
            modified.append(f["rel"])
    if modified:
        return refuse("finding_tests_modified", files=modified)
    notes_changed = []
    for r in st["rounds"][:round_n]:
        if r.get("notes_sha256"):
            rel = f"{cfg.notes_dir}/R{r['round']}.md"
            data = _blob(job.repo, cand, rel)
            if data is None or hashlib.sha256(data).hexdigest() != r["notes_sha256"]:
                notes_changed.append(rel)
    if notes_changed:
        return refuse("review_notes_modified", files=notes_changed)

    # 2. what the branch changes
    changed = _diff_status(job.repo, evidence_head, cand)
    if not cfg.allow_test_config_changes:
        control = _test_control_changes(job.repo, evidence_head, cand, changed)
        if control:
            return refuse("test_infrastructure_modified", files=control)
    product = sorted(rel for rel in changed if not _under(rel, cfg.test_dir) and not _under(rel, cfg.notes_dir))
    if not product:
        return refuse("no_product_change", files=sorted(changed))
    tests_changed = sorted(rel for rel, how in changed.items() if _under(rel, cfg.test_dir) and how != "A")

    # 3. which tests the suite runs on the evidence head (clean checkout)
    with _clean_checkout(job, f"r{round_n}_base", evidence_head) as base_tree:
        base_xml = logs / f"r{round_n}_base_suite.xml"
        _run_tests(cfg, base_tree, [], logs / f"r{round_n}_base_suite.log", junit=base_xml)
        base = _junit_outcomes(base_xml)
    if base is None:
        raise RuntimeError("the suite run on the evidence head produced no JUnit report; "
                           "test_command must be pytest-compatible (--junitxml)")

    # 4. the candidate commit, clean checkout: evidence green, suite green, no test dropped or skipped
    with _clean_checkout(job, f"r{round_n}_fix", cand) as tree:
        find_xml = logs / f"r{round_n}_fix_findings.xml"
        rc = _run_tests(cfg, tree, [f["rel"] for f in evidence], logs / f"r{round_n}_fix_findings.log", junit=find_xml)
        if rc != 0:
            return refuse("findings_still_red", rc=rc)
        ran = _junit_outcomes(find_xml)
        if ran is None:
            return refuse("no_test_report")
        not_run = []
        for f in evidence:
            module = _module_id(cfg, f["file"])
            mine = {t: o["outcome"] for t, o in ran.items() if _in_module(t, module)}
            wanted = f.get("tests") or sorted(mine)
            if not wanted or any(mine.get(t) != "passed" for t in wanted) or "skipped" in mine.values():
                not_run.append(f["rel"])
        if not_run:
            return refuse("findings_not_run", files=not_run)
        _pristine(tree)
        suite_xml = logs / f"r{round_n}_fix_suite.xml"
        if _run_tests(cfg, tree, [], logs / f"r{round_n}_fix_suite.log", junit=suite_xml) != 0:
            return refuse("suite_red")
        now = _junit_outcomes(suite_xml)
        if now is None:
            return refuse("no_test_report")
        removed = sorted(t for t in base if t not in now)
        if removed:
            return refuse("tests_removed", tests=removed[:50], count=len(removed))
        skipped = sorted(t for t, o in base.items() if o["outcome"] != "skipped" and now[t]["outcome"] == "skipped")
        if skipped:
            return refuse("tests_skipped", tests=skipped[:50], count=len(skipped))

    # 5. merge exactly that commit; the merged tree must be the tree that was tested
    fix_head = cand
    merge = _git(integ, "merge", "-q", "--no-ff", "-m", f"fix round {round_n} ({job.cut})", fix_head, check=False)
    if merge.returncode != 0 or _out(integ, "rev-parse", "HEAD^{tree}") != _out(job.repo, "rev-parse", f"{fix_head}^{{tree}}"):
        _git(integ, "merge", "--abort", check=False)
        _git(integ, "reset", "-q", "--hard", evidence_head)
        _git(integ, "clean", "-qfd")
        return refuse("merge_failed", detail=(merge.stderr or merge.stdout).strip()[-500:])
    rnd["fix"] = "merged"; rnd["fix_head"] = fix_head; rnd["merged_head"] = _out(integ, "rev-parse", "HEAD")
    rnd.pop("fix_reason", None)
    rnd["fix_diff"] = {"product_paths": len(product), "existing_tests_changed": tests_changed}
    st["status"] = "fix_merged"; _save(job, st)
    # the next reviewer starts from the merged head, on a clean slate (the previous reviewer's refused files go)
    _git(job.review, "checkout", "-q", "-f", "--detach", rnd["merged_head"])
    _git(job.review, "clean", "-qfd")
    (Path(job.path) / "reviewer_brief.md").write_text(reviewer_brief(cfg, job, round_n + 1))
    seq = _receipt(cfg, "redteam.fix_gated", {"job_id": job.job_id, "round": round_n, "verdict": "merged",
                                              "fix_head": fix_head, "merged_head": rnd["merged_head"],
                                              "findings": len(rnd["accepted"]), "product_paths": len(product),
                                              "existing_tests_changed": len(tests_changed)})
    if seq is not None:
        st = _load(job); st.setdefault("receipts", []).append(seq); _save(job, st)
    return {"verdict": "merged", "fix_head": fix_head, "merged_head": rnd["merged_head"], "receipt": seq,
            "existing_tests_changed": tests_changed}


def should_continue(cfg: RedTeamConfig, job: Job) -> bool:
    """Another round only while budget remains and the last review found at least one high."""
    rounds = _load(job)["rounds"]
    if not rounds or len(rounds) >= cfg.max_rounds:
        return False
    return rounds[-1]["highs"] > 0


def _scrub(text: Any, secrets: List[str]) -> str:
    """`text` with every secret replaced; used on anything that may reach an exception, a log or job.json."""
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


def _git_env(config: List[tuple], **extra: str) -> Dict[str, str]:
    """The process environment plus per-command git config (GIT_CONFIG_COUNT/KEY/VALUE): nothing goes in argv."""
    env = dict(os.environ)
    try:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{n + i}"] = key
        env[f"GIT_CONFIG_VALUE_{n + i}"] = value
    env["GIT_CONFIG_COUNT"] = str(n + len(config))
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update(extra)
    return env


# A credential helper that answers from the environment. Its text (in git's config, hence in `sh -c` argv)
# names the variable, never its value.
_CREDENTIAL_HELPER = ("!f() { test \"$1\" = get && "
                      "printf 'username=x-access-token\\npassword=%s\\n' \"$AUTOPOIESIS_GH_TOKEN\"; }; f")


def _push(job: Job, org: str, name: str, token: str, head: str, branch: str) -> None:
    """Push `head` to refs/heads/<branch> of the GitHub repo and check the remote ref.

    The token never appears in an argv, an exception or a log:
    * the destination is resolved first, with the credentialed URL form handed to git through the environment
      (`ls-remote --get-url` contacts nothing), so `url.<base>.insteadOf` rules written against that form keep
      working;
    * the push itself goes to a credential-free URL and git asks `_CREDENTIAL_HELPER`, which reads the
      environment (so the https transport helper's argv is clean too);
    * every message built from git's output is scrubbed.
    """
    secrets = [token, urllib.parse.quote(token, safe="")]
    credentialed = "https://x-access-token:" + secrets[1] + "@" + f"github.com/{org}/{name}.git"
    alias = f"autopoiesis-push-{os.getpid()}"
    probe = _git(job.repo, "ls-remote", "--get-url", alias, check=False,
                 env=_git_env([(f"remote.{alias}.url", credentialed)]))
    target = probe.stdout.strip()
    if probe.returncode != 0 or not target or target == alias:
        raise RuntimeError("cannot resolve the push URL: " + _scrub(probe.stderr.strip()[-500:], secrets))
    if any(secret in target for secret in secrets):
        parts = urllib.parse.urlsplit(target)
        host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
        target = urllib.parse.urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
        if not host or any(secret in target for secret in secrets):
            raise RuntimeError("cannot build a credential-free push URL")
    env = _git_env([("credential.helper", ""), ("credential.helper", _CREDENTIAL_HELPER)],
                   AUTOPOIESIS_GH_TOKEN=token)
    push = _git(job.repo, "push", "-q", target, f"{head}:refs/heads/{branch}", check=False, env=env)
    if push.returncode != 0:
        raise RuntimeError(f"git push failed (exit {push.returncode}): " + _scrub(push.stderr.strip()[-800:], secrets))
    seen = _git(job.repo, "ls-remote", target, f"refs/heads/{branch}", check=False, env=env)
    if seen.returncode != 0 or seen.stdout.split()[:1] != [head]:
        raise RuntimeError(f"after the push, refs/heads/{branch} on the remote does not point at {head[:12]}")


def _github(url: str, token: str, timeout: float, payload: Optional[Dict[str, Any]] = None) -> Any:
    """One GitHub API call with a timeout. Raises urllib.error.HTTPError as is (it carries no request headers)."""
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "autopoiesis"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _drop_worktrees(job: Job) -> None:
    for wt in ("review", "fix", "integration"):   # the branch ref stays in the target repo; the worktrees are scratch
        p = Path(job.path) / wt
        if p.is_dir():
            _git(job.repo, "worktree", "remove", "--force", str(p), check=False)
    shutil.rmtree(Path(job.path) / "gate", ignore_errors=True)
    _git(job.repo, "worktree", "prune", check=False)


def close_job(cfg: RedTeamConfig, job: Job, open_pr: bool = True) -> Dict[str, Any]:
    """Push the integration branch by SHA and open a PR for a human to merge; or close with nothing to show.

    Closing a closed job changes nothing and returns what the first close recorded.
    """
    st = _load(job)
    merged = [r for r in st["rounds"] if r.get("fix") == "merged"]
    if str(st.get("status", "")).startswith("closed"):
        _drop_worktrees(job)
        return {"job_id": job.job_id, "head": st["final_head"], "rounds": len(st["rounds"]),
                "merged_rounds": len(merged), "pr": st.get("pr"), "receipt": st.get("close_receipt"),
                "already_closed": True}
    if st.get("pending"):
        raise RuntimeError("an evidence commit is unfinished; re-run gate-findings first")
    head = _out(job.integration, "rev-parse", "HEAD")
    result: Dict[str, Any] = {"job_id": job.job_id, "head": head, "rounds": len(st["rounds"]),
                              "merged_rounds": len(merged), "pr": None}
    if merged and open_pr and cfg.gh_token_cmd:
        got = subprocess.run(cfg.gh_token_cmd, shell=True, capture_output=True, text=True)
        token = got.stdout.strip()
        if got.returncode != 0 or not token:        # the command's output is not echoed: it may hold a credential
            raise RuntimeError(f"gh_token_cmd gave no token (exit {got.returncode})")
        secrets = [token, urllib.parse.quote(token, safe="")]
        url = _out(job.repo, "remote", "get-url", "origin")
        m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", url)
        if not m:
            raise RuntimeError("not a GitHub remote: " + _scrub(url, secrets))
        org, name = m.group(1), m.group(2)
        api = f"https://api.github.com/repos/{org}/{name}"
        _push(job, org, name, token, head, st["branch"])
        st["pushed_head"] = head; _save(job, st)     # a retry after a failed API call knows the push happened
        try:
            remote = _github(f"{api}/git/commits/{head}", token, cfg.api_timeout)
            if remote.get("sha") != head:
                raise RuntimeError("pushed head not found on remote")
            title = f"redteam {job.job_id}: {job.cut} ({sum(len(r['accepted']) for r in merged)} findings fixed)"
            try:
                pr = _github(f"{api}/pulls", token, cfg.api_timeout,
                             {"title": title, "head": st["branch"], "base": cfg.base_branch, "body": pr_body(cfg, job)})
            except urllib.error.HTTPError as exc:
                if exc.code != 422:
                    raise
                # 422: most likely an earlier POST succeeded and its response was lost; adopt that PR
                existing = _github(f"{api}/pulls?state=open&head={org}:{st['branch']}", token, cfg.api_timeout)
                if not existing:
                    raise
                pr = existing[0]
        except RuntimeError:
            raise
        except Exception as exc:                     # nothing below the API helper may surface the token
            raise RuntimeError("GitHub API call failed: " + _scrub(f"{type(exc).__name__}: {exc}", secrets)) from None
        result["pr"] = pr["html_url"]
    last = st["rounds"][-1] if st["rounds"] else {}
    result["unreviewed_high_fix"] = bool(last.get("fix") == "merged" and last.get("highs", 0) > 0)
    st["status"] = "closed_pr" if result["pr"] else "closed"
    st["closed_at"] = int(time.time()); st["final_head"] = head; st["pr"] = result["pr"]
    _save(job, st)
    seq = _receipt(cfg, "redteam.job_closed", {"job_id": job.job_id, "cut": job.cut, "rounds": len(st["rounds"]),
                                               "merged_rounds": len(merged), "final_head": head,
                                               "pr": result["pr"] or "none",
                                               "unreviewed_high_fix": int(result["unreviewed_high_fix"])})
    result["receipt"] = seq
    if seq is not None:
        st["close_receipt"] = seq; st.setdefault("receipts", []).append(seq); _save(job, st)
    _drop_worktrees(job)
    return result


def pr_body(cfg: RedTeamConfig, job: Job) -> str:
    st = _load(job)
    lines = [f"Automated red-first review, cut **{job.cut}**, base `{job.base_head[:12]}`. "
             f"Each finding is a test committed before the fix; a fix was admitted only as a commit that, on a "
             f"fresh checkout, leaves all finding tests untouched and green and the suite green with "
             f"{', '.join(cfg.env_strip)} unset, without dropping or skipping a test. Severity is the reviewer's "
             f"own declaration. A human merges.", ""]
    for r in st["rounds"]:
        lines.append(f"## Round {r['round']} — {len(r['accepted'])} proven, {len(r['rejected'])} refused, "
                     f"{r['highs']} high — fix: {r.get('fix', 'none')}")
        for f in r["accepted"]:
            lines.append(f"- **{f['severity']}** `{cfg.test_dir}/{f['file']}`")
        changed = (r.get("fix_diff") or {}).get("existing_tests_changed") or []
        if changed:
            lines.append(f"\nExisting test files changed by this fix (check that none was weakened): "
                         + ", ".join(f"`{c}`" for c in changed))
        lines.append(f"\nNotes: `{cfg.notes_dir}/R{r['round']}.md`. Receipts: {st.get('receipts', [])}\n")
    last = st["rounds"][-1] if st["rounds"] else None
    if last and last.get("highs", 0) > 0:
        if last.get("fix") == "merged":
            why = (f"the round budget (max_rounds={cfg.max_rounds}) is used up" if len(st["rounds"]) >= cfg.max_rounds
                   else "the job was closed before another round")
            lines.append(f"**Not re-reviewed:** round {last['round']} proved {last['highs']} high-severity finding(s); "
                         f"the fix for them was merged without a further review round ({why}). "
                         f"Review that fix by hand.\n")
        else:
            lines.append(f"**Open:** round {last['round']} proved {last['highs']} high-severity finding(s) and no fix "
                         f"for them was admitted (fix: {last.get('fix', 'none')}); their tests are on this branch "
                         f"and fail.\n")
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
    try:
        if a.cmd == "open":
            job = open_job(cfg, a.job, a.cut)
            out: Any = {**dataclasses.asdict(job), "reviewer_brief": str(Path(job.path) / "reviewer_brief.md")}
        else:
            job = load_job(cfg, a.job)
            if a.cmd == "gate-findings":
                out = gate_findings(cfg, job, a.round)
            elif a.cmd == "open-fix":
                out = {"fix": open_fix(cfg, job, a.round),
                       "fixer_brief": str(Path(job.path) / f"fixer_brief_r{a.round}.md")}
            elif a.cmd == "gate-fix":
                out = gate_fix(cfg, job, a.round)
            elif a.cmd == "continue":
                out = {"continue": should_continue(cfg, job)}
            elif a.cmd == "close":
                out = close_job(cfg, job, open_pr=not a.no_pr)
            else:
                out = _load(job)
    except RuntimeError as exc:                   # a refusal by the harness itself: one line, no traceback
        print(json.dumps({"error": str(exc)}, indent=1), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0

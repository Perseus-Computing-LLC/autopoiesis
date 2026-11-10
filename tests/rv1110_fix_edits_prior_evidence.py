"""severity: high
finding: gate_fix neither pins nor runs earlier rounds' evidence; a round-2 fixer can re-introduce the round-1 defect (with or without rewriting review1_*.py) and the branch is merged

redteam.py:296-299 compares sha256 only for `rnd["accepted"]` where `rnd = st["rounds"][round_n - 1]`, and
redteam.py:300-301 runs only this round's files. Earlier evidence is left to the suite run (redteam.py:303,
`pytest <test_dir>`), but `review<N>_*.py` does not match pytest's default `python_files` (test_*.py, *_test.py),
so a directory run never collects it: round-1 findings are not re-checked in round 2, nor by the target's CI
after the PR merges.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from autopoiesis import redteam

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
SRC_BUGGY = "def add(a, b):\n    return a - b  # bug 1\n\ndef double(x):\n    return x * 3  # bug 2\n\ndef one():\n    return 1\n"
SRC_ADD_FIXED = SRC_BUGGY.replace("return a - b  # bug 1", "return a + b")
SRC_BOTH_FIXED = SRC_ADD_FIXED.replace("return x * 3  # bug 2", "return x * 2")
SRC_ADD_REGRESSED = SRC_BUGGY.replace("return x * 3  # bug 2", "return x * 2")     # double fixed, add broken again


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make_target(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "__init__.py").write_text(SRC_BUGGY)
    (repo / "tests" / "test_green.py").write_text("from pkg import one\n\ndef test_one():\n    assert one() == 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    return repo


def cfg_for(repo: Path, tmp_path: Path) -> redteam.RedTeamConfig:
    return redteam.RedTeamConfig(
        repo_path=str(repo), test_command=[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], python_path="src",
        cuts=["a", "b"], rotation_path=str(tmp_path / "rotation.json"), jobs_root=str(tmp_path / "jobs"),
        ledger_path=None, max_rounds=2)


def finding(review: Path, round_n: int, name: str, body: str) -> None:
    (review / "tests" / f"review{round_n}_{name}.py").write_text(f'"""severity: high\nfinding: {name}\n"""\n{body}')


def two_rounds(tmp_path: Path, job_id: str):
    """Round 1 (add) honestly fixed and merged; round 2 (double) gated and its fix worktree opened."""
    cfg = cfg_for(make_target(tmp_path), tmp_path)
    job = redteam.open_job(cfg, job_id)
    finding(Path(job.review), 1, "add", "from pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    assert [f["name"] for f in redteam.gate_findings(cfg, job, 1)["accepted"]] == ["add"]
    fix = Path(redteam.open_fix(cfg, job, 1))
    (fix / "src" / "pkg" / "__init__.py").write_text(SRC_ADD_FIXED)
    git(fix, "commit", "-qam", "fix add")
    assert redteam.gate_fix(cfg, job, 1)["verdict"] == "merged"
    finding(Path(job.review), 2, "double", "from pkg import double\n\ndef test_double():\n    assert double(2) == 4\n")
    assert [f["name"] for f in redteam.gate_findings(cfg, job, 2)["accepted"]] == ["double"]
    fix = Path(redteam.open_fix(cfg, job, 2))
    return cfg, job, fix


def test_control_honest_round_two_fix_is_merged(tmp_path: Path):
    cfg, job, fix = two_rounds(tmp_path, "c1")
    (fix / "src" / "pkg" / "__init__.py").write_text(SRC_BOTH_FIXED)
    git(fix, "commit", "-qam", "fix double")
    assert redteam.gate_fix(cfg, job, 2)["verdict"] == "merged"


def test_round_two_fix_that_weakens_round_one_evidence_is_refused(tmp_path: Path):
    cfg, job, fix = two_rounds(tmp_path, "e1")
    st = json.loads(Path(job.path, "job.json").read_text())
    pinned = st["rounds"][0]["accepted"][0]["sha256"]
    # the round-2 fixer fixes `double`, breaks `add` again, and guts the round-1 evidence so nothing notices
    (fix / "src" / "pkg" / "__init__.py").write_text(SRC_ADD_REGRESSED)
    (fix / "tests" / "review1_add.py").write_text("def test_add():\n    assert True\n")
    git(fix, "commit", "-qam", "fix double")
    v = redteam.gate_fix(cfg, job, 2)
    integ_evidence = Path(job.integration) / "tests" / "review1_add.py"
    assert v["verdict"] == "rejected", f"round-1 evidence was rewritten and the branch was still admitted: {v}"
    assert hashlib.sha256(integ_evidence.read_bytes()).hexdigest() == pinned


def test_round_two_fix_that_regresses_round_one_without_touching_evidence_is_refused(tmp_path: Path):
    cfg, job, fix = two_rounds(tmp_path, "e2")
    (fix / "src" / "pkg" / "__init__.py").write_text(SRC_ADD_REGRESSED)      # evidence files left byte-identical
    git(fix, "commit", "-qam", "fix double")
    v = redteam.gate_fix(cfg, job, 2)
    assert v["verdict"] == "rejected", f"round-1 finding is red again on the admitted head: {v}"

"""severity: high
finding: gate_findings runs the finding tests in a review worktree it never verifies; a reviewer who edits product code there gets a "finding" admitted that passes on the real unpatched head

redteam.py:236-245. The contract (module docstring, reviewer brief) is "admitted only if it FAILS on the
unpatched head". The test is executed in `job.review`, where the reviewer agent may have changed tracked source,
added a conftest.py or helper modules; only `review<N>_*.py` and the notes are copied to the integration branch,
and the evidence is never re-run there. The fixer then has nothing to fix and an empty fix "merges".
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from autopoiesis import redteam

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def open_job(tmp_path: Path, job_id: str):
    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a - b  # bug\n\ndef one():\n    return 1\n")
    (repo / "tests" / "test_green.py").write_text("from pkg import one\n\ndef test_one():\n    assert one() == 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    cfg = redteam.RedTeamConfig(
        repo_path=str(repo), test_command=[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], python_path="src",
        cuts=["a", "b"], rotation_path=str(tmp_path / "rotation.json"), jobs_root=str(tmp_path / "jobs"),
        ledger_path=None, max_rounds=2)
    return cfg, redteam.open_job(cfg, job_id)


def rc_on_integration(cfg, job, files) -> dict:
    """Exit code of each evidence file on the integration tree (= pristine base head + committed evidence)."""
    logs = Path(job.path) / "logs"
    return {f: redteam._run_tests(cfg, job.integration, [f"tests/{f}"], logs / f"rv1110_{f}.log") for f in files}


def test_control_real_finding_is_red_on_the_integration_head(tmp_path: Path):
    cfg, job = open_job(tmp_path, "c1")
    (Path(job.review) / "tests" / "review1_add.py").write_text(
        '"""severity: high\nfinding: add\n"""\nfrom pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    g = redteam.gate_findings(cfg, job, 1)
    assert rc_on_integration(cfg, job, [f["file"] for f in g["accepted"]]) == {"review1_add.py": 1}


def test_finding_that_fails_only_on_a_reviewer_modified_tree_is_refused(tmp_path: Path):
    cfg, job = open_job(tmp_path, "m1")
    review = Path(job.review)
    # the reviewer breaks a function that is correct on the head, then "finds" the breakage
    (review / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a - b  # bug\n\ndef one():\n    return 2\n")
    (review / "tests" / "review1_one.py").write_text(
        '"""severity: high\nfinding: one() returns 2\n"""\nfrom pkg import one\n\ndef test_one():\n    assert one() == 1\n')
    g = redteam.gate_findings(cfg, job, 1)
    accepted = [f["file"] for f in g["accepted"]]
    green_on_head = [f for f, rc in rc_on_integration(cfg, job, accepted).items() if rc != 1]
    assert not green_on_head, f"admitted as proven on the unpatched head, but not red there: {green_on_head}"


def test_empty_fix_for_a_fabricated_finding_is_not_reported_as_a_merged_fix(tmp_path: Path):
    cfg, job = open_job(tmp_path, "m2")
    review = Path(job.review)
    (review / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a - b  # bug\n\ndef one():\n    return 2\n")
    (review / "tests" / "review1_one.py").write_text(
        '"""severity: high\nfinding: one() returns 2\n"""\nfrom pkg import one\n\ndef test_one():\n    assert one() == 1\n')
    if not redteam.gate_findings(cfg, job, 1)["accepted"]:
        return                                              # refused at the first gate: correct
    redteam.open_fix(cfg, job, 1)                           # the fixer commits nothing at all
    v = redteam.gate_fix(cfg, job, 1)
    assert v["verdict"] == "rejected", f"a fix branch identical to the evidence head was recorded as merged: {v}"

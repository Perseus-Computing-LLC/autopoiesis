"""severity: high
finding: gate_fix tests the fix worktree, not the commit it merges: a git-ignored file present only in the fixer's worktree makes the findings green, and the merged head has the finding red

redteam.py:287-306. `git status --porcelain` / `git add -A` do not see ignored files, the tests run in the
fixer's directory (redteam.py:301,303), and after `git merge` (redteam.py:306) the integration tree is never
tested. Any path matched by the target's .gitignore (generated modules, build/, local settings, *.pyc) works.
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


def round_one(tmp_path: Path, job_id: str):
    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n_generated.py\n")      # a generated module, as many repos have
    (repo / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a - b  # bug\n\ndef one():\n    return 1\n")
    (repo / "tests" / "test_green.py").write_text("from pkg import one\n\ndef test_one():\n    assert one() == 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    cfg = redteam.RedTeamConfig(
        repo_path=str(repo), test_command=[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], python_path="src",
        cuts=["a", "b"], rotation_path=str(tmp_path / "rotation.json"), jobs_root=str(tmp_path / "jobs"),
        ledger_path=None, max_rounds=2)
    job = redteam.open_job(cfg, job_id)
    (Path(job.review) / "tests" / "review1_add.py").write_text(
        '"""severity: high\nfinding: add subtracts\n"""\nfrom pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    assert [f["name"] for f in redteam.gate_findings(cfg, job, 1)["accepted"]] == ["add"]
    return cfg, job, Path(redteam.open_fix(cfg, job, 1))


def finding_rc_on_merged_head(cfg, job) -> int:
    return redteam._run_tests(cfg, job.integration, ["tests/review1_add.py"], Path(job.path) / "logs" / "rv1110.log")


def test_control_honest_fix_is_green_on_the_merged_head(tmp_path: Path):
    cfg, job, fix = round_one(tmp_path, "c1")
    (fix / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a + b\n\ndef one():\n    return 1\n")
    git(fix, "commit", "-qam", "fix add")
    assert redteam.gate_fix(cfg, job, 1)["verdict"] == "merged"
    assert finding_rc_on_merged_head(cfg, job) == 0


def test_fix_that_depends_on_an_ignored_file_is_not_merged_red(tmp_path: Path):
    cfg, job, fix = round_one(tmp_path, "i1")
    (fix / "src" / "pkg" / "__init__.py").write_text("from pkg._generated import add\n\ndef one():\n    return 1\n")
    (fix / "src" / "pkg" / "_generated.py").write_text("def add(a, b):\n    return a + b\n")   # ignored: never committed
    git(fix, "commit", "-qam", "fix add")
    v = redteam.gate_fix(cfg, job, 1)
    if v["verdict"] == "rejected":
        return                                              # refusing is correct
    rc = finding_rc_on_merged_head(cfg, job)
    assert rc == 0, f"verdict {v['verdict']!r}, but the finding test exits {rc} on the merged integration head"

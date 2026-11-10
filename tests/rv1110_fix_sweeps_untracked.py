"""severity: medium
finding: gate_fix commits whatever is lying in the fix worktree (`git add -A`) and merges it, so untracked scratch files of the fixer end up on the branch that close_job pushes

redteam.py:287-288. The fixer brief says "Commit your work on this branch"; anything the fixer did NOT commit
(debug dumps, editor backups, local env files, tool caches) is swept into a harness-authored commit
"fix round N: uncommitted work" and then into the PR.
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
    fix = Path(redteam.open_fix(cfg, job, 1))
    (fix / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a + b\n\ndef one():\n    return 1\n")
    git(fix, "commit", "-qam", "fix add")                   # the fixer's actual, committed work
    return cfg, job, fix


def test_control_committed_fix_is_merged(tmp_path: Path):
    cfg, job, fix = round_one(tmp_path, "c1")
    assert redteam.gate_fix(cfg, job, 1)["verdict"] == "merged"
    assert "scratch/debug_env.txt" not in git(Path(job.integration), "ls-files").splitlines()


def test_untracked_scratch_files_do_not_reach_the_integration_branch(tmp_path: Path):
    cfg, job, fix = round_one(tmp_path, "u1")
    (fix / "scratch").mkdir()
    (fix / "scratch" / "debug_env.txt").write_text("LOCAL_DEBUG_VALUE=not-for-publication\n")   # never committed
    redteam.gate_fix(cfg, job, 1)                           # refusing a dirty worktree would also be correct
    tracked = git(Path(job.integration), "ls-files").splitlines()
    assert "scratch/debug_env.txt" not in tracked, "an uncommitted scratch file was committed and merged by the gate"

"""severity: medium
finding: open_job is not atomic: if next_cut (or the brief write) raises after the worktrees are created, the job dir, both worktrees and the branch stay behind and a retry with the same job id fails with "job exists"

redteam.py:168-176. `job_dir.mkdir`, two `git worktree add` (one creating branch redteam/<id>) happen before
`next_cut` (redteam.py:172) and before job.json exists. next_cut raises on an unreadable/corrupt rotation file
(it writes that file non-atomically with `path.write_text`, redteam.py:155), on `cuts: []` (ZeroDivisionError at
redteam.py:151) and on an unwritable rotation directory. There is no job.json, so `close` cannot clean up either.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from autopoiesis import redteam

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make(tmp_path: Path):
    repo = tmp_path / "target"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_green.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    cfg = redteam.RedTeamConfig(
        repo_path=str(repo), test_command=[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], python_path=None,
        cuts=["a", "b"], rotation_path=str(tmp_path / "state" / "rotation.json"), jobs_root=str(tmp_path / "jobs"),
        ledger_path=None)
    return repo, cfg


def leftovers(repo: Path, cfg, job_id: str) -> dict:
    return {
        "job_dir": (Path(cfg.jobs_root) / job_id).exists(),
        "worktrees": git(repo, "worktree", "list", "--porcelain").count("worktree ") - 1,
        "branch": bool(git(repo, "branch", "--list", f"redteam/{job_id}")),
    }


def test_control_open_job_works_with_a_sane_rotation_file(tmp_path: Path):
    repo, cfg = make(tmp_path)
    job = redteam.open_job(cfg, "ok")
    assert job.cut == "a" and leftovers(repo, cfg, "ok") == {"job_dir": True, "worktrees": 2, "branch": True}


def test_failed_open_leaves_nothing_behind_and_can_be_retried(tmp_path: Path):
    repo, cfg = make(tmp_path)
    rotation = Path(cfg.rotation_path)
    rotation.parent.mkdir(parents=True)
    rotation.write_text('{"target": ["a"')                 # truncated by an interrupted write
    with pytest.raises(Exception):
        redteam.open_job(cfg, "j1")
    left = leftovers(repo, cfg, "j1")
    rotation.write_text('{"target": ["a"]}')               # operator repairs the file and retries
    try:
        retry = redteam.open_job(cfg, "j1").cut
    except Exception as exc:
        retry = f"retry failed: {exc}"
    assert left == {"job_dir": False, "worktrees": 0, "branch": False}, f"left behind after a failed open: {left}"
    assert retry == "b", retry


def test_empty_cut_list_is_refused_before_anything_is_created(tmp_path: Path):
    repo, cfg = make(tmp_path)
    cfg.cuts = []
    with pytest.raises(Exception):
        redteam.open_job(cfg, "j2")
    left = leftovers(repo, cfg, "j2")
    assert left == {"job_dir": False, "worktrees": 0, "branch": False}, f"left behind after a failed open: {left}"

"""severity: low
finding: gate_findings copies evidence into the integration tree before committing; when the commit fails (e.g. a pre-commit hook of the target repo) the tree is left with staged files and job.json has no record of the round

redteam.py:243-253 then 255-259. The copy and `git add -A` are not undone when `git commit` raises, and the state
write comes after the commit. Hooks live in the target's common git dir, so they apply to the harness worktrees.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from autopoiesis import redteam

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def open_job(tmp_path: Path, job_id: str, hook: bool):
    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    (repo / "tests" / "test_green.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    if hook:                                                # e.g. a lint hook that rejects the reviewer's file
        hooks = Path(git(repo, "rev-parse", "--git-common-dir"))
        hooks = (hooks if hooks.is_absolute() else repo / hooks) / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\necho 'lint failed' >&2\nexit 1\n")
        os.chmod(hooks / "pre-commit", 0o755)
    cfg = redteam.RedTeamConfig(
        repo_path=str(repo), test_command=[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], python_path="src",
        cuts=["a", "b"], rotation_path=str(tmp_path / "rotation.json"), jobs_root=str(tmp_path / "jobs"),
        ledger_path=None)
    job = redteam.open_job(cfg, job_id)
    (Path(job.review) / "tests" / "review1_add.py").write_text(
        '"""severity: high\nfinding: add subtracts\n"""\nfrom pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    return cfg, job


def test_control_without_a_hook_the_tree_is_clean_after_gating(tmp_path: Path):
    cfg, job = open_job(tmp_path, "c1", hook=False)
    redteam.gate_findings(cfg, job, 1)
    assert git(Path(job.integration), "status", "--porcelain") == ""


def test_failed_evidence_commit_does_not_leave_the_integration_tree_dirty(tmp_path: Path):
    cfg, job = open_job(tmp_path, "h1", hook=True)
    raised = None
    try:
        redteam.gate_findings(cfg, job, 1)
    except Exception as exc:
        raised = type(exc).__name__
    dirty = git(Path(job.integration), "status", "--porcelain")
    rounds = json.loads(Path(job.path, "job.json").read_text())["rounds"]
    assert raised is not None or rounds, "the commit was refused by the hook, yet the gate reported nothing"
    assert dirty == "", f"integration tree left dirty after {raised}: {dirty!r} (rounds recorded: {len(rounds)})"

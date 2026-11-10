"""severity: high
finding: close_job embeds the GitHub token in the `git push` argv; a failed push raises CalledProcessError whose message/cmd (and so any traceback or log) contains the token

redteam.py:343-344 builds `https://x-access-token:<token>@github.com/...` and hands it to `_git(..., check=True)`
(redteam.py:78-79). `str(CalledProcessError)` prints the whole command list. The token is also visible in the
process list for the lifetime of the push.

No network and no real credential: the token is a fixed fake string, git is restricted to the file transport,
and the credentialed URL is rewritten (url.<local>.insteadOf) to a local path that does not exist.
"""
from __future__ import annotations

import json
import subprocess
import sys
import traceback
import urllib.request
from pathlib import Path

import pytest

from autopoiesis import redteam

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
FAKE = "rv1110FAKEtokenNOTaSECRET"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def merged_job(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")                       # git may not touch the network
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

    def no_network(*a, **kw):
        raise AssertionError("urlopen must not be reached in this test")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)

    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "tests" / "test_green.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    git(repo, "remote", "add", "origin", "https://github.com/example-org/example-repo.git")
    missing = tmp_path / "no-such-remote"
    git(repo, "config", f"url.{missing}/.insteadOf", f"https://x-access-token:{FAKE}@github.com/")
    cfg = redteam.RedTeamConfig(
        repo_path=str(repo), test_command=[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], python_path="src",
        cuts=["a"], rotation_path=str(tmp_path / "rotation.json"), jobs_root=str(tmp_path / "jobs"),
        ledger_path=None, gh_token_cmd=f"echo {FAKE}")
    job = redteam.open_job(cfg, "t1")
    (Path(job.review) / "tests" / "review1_add.py").write_text(
        '"""severity: high\nfinding: add\n"""\nfrom pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    redteam.gate_findings(cfg, job, 1)
    fix = Path(redteam.open_fix(cfg, job, 1))
    (fix / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a + b\n")
    git(fix, "commit", "-qam", "fix")
    assert redteam.gate_fix(cfg, job, 1)["verdict"] == "merged"
    return cfg, job


def test_control_push_is_attempted_and_fails_locally(tmp_path: Path, monkeypatch):
    cfg, job = merged_job(tmp_path, monkeypatch)
    with pytest.raises(Exception):
        redteam.close_job(cfg, job)
    assert json.loads(Path(job.path, "job.json").read_text())["status"] == "fix_merged"     # not closed


def test_failed_push_does_not_expose_the_token(tmp_path: Path, monkeypatch):
    cfg, job = merged_job(tmp_path, monkeypatch)
    try:
        rendered = json.dumps(redteam.close_job(cfg, job), default=str)
    except Exception as exc:                                               # what a caller's log/traceback would hold
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        rendered += str(exc) + repr(exc) + repr(getattr(exc, "cmd", "")) + str(getattr(exc, "stderr", ""))
    rendered += Path(job.path, "job.json").read_text()
    leaked = FAKE in rendered
    assert not leaked, "the push credential appears in the error raised by close_job"

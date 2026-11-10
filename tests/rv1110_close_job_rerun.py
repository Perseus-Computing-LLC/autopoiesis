"""severity: medium
finding: close_job cannot be re-run (a second close crashes on the removed worktree and would overwrite closed_at/final_head) and its GitHub API calls are made without a timeout

redteam.py:331-333 reads HEAD from `job.integration`, which redteam.py:366-369 removed on the first close, and
never looks at `st["status"]`. redteam.py:345 and :357 call `urllib.request.urlopen(req)` with no `timeout`, so a
stalled connection blocks the harness forever after the branch has already been pushed (redteam.py:344).

No network: the credentialed github URL is rewritten (url.<dir>.insteadOf) to a local bare repo, git is limited
to the file transport, urlopen is replaced by a recorder, and the token is a fixed fake string.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from autopoiesis import redteam

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
FAKE = "rv1110FAKEtokenNOTaSECRET"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def merged_job(tmp_path: Path, monkeypatch, job_id: str):
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "tests" / "test_green.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    bare = tmp_path / "remotes" / "example-org" / "example-repo.git"
    bare.mkdir(parents=True)
    git(bare, "init", "-q", "--bare")
    git(repo, "remote", "add", "origin", "https://github.com/example-org/example-repo.git")
    git(repo, "config", f"url.{tmp_path / 'remotes'}/.insteadOf", f"https://x-access-token:{FAKE}@github.com/")
    cfg = redteam.RedTeamConfig(
        repo_path=str(repo), test_command=[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], python_path="src",
        cuts=["a"], rotation_path=str(tmp_path / "rotation.json"), jobs_root=str(tmp_path / "jobs"),
        ledger_path=None, gh_token_cmd=f"echo {FAKE}")
    job = redteam.open_job(cfg, job_id)
    (Path(job.review) / "tests" / "review1_add.py").write_text(
        '"""severity: high\nfinding: add\n"""\nfrom pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    redteam.gate_findings(cfg, job, 1)
    fix = Path(redteam.open_fix(cfg, job, 1))
    (fix / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a + b\n")
    git(fix, "commit", "-qam", "fix")
    assert redteam.gate_fix(cfg, job, 1)["verdict"] == "merged"
    return cfg, job, bare


def fake_github(monkeypatch, job):
    calls = []
    head = git(Path(job.integration), "rev-parse", "HEAD")

    def urlopen(req, *args, **kwargs):
        timeout = kwargs.get("timeout", args[1] if len(args) > 1 else None)
        calls.append({"url": req.full_url, "method": req.get_method(), "timeout": timeout})
        body = {"html_url": "https://example.invalid/pr/1"} if req.get_method() == "POST" else {"sha": head}
        return io.BytesIO(json.dumps(body).encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls, head


def test_control_close_pushes_to_the_local_bare_remote_and_records_the_pr(tmp_path: Path, monkeypatch):
    cfg, job, bare = merged_job(tmp_path, monkeypatch, "c1")
    calls, head = fake_github(monkeypatch, job)
    out = redteam.close_job(cfg, job)
    assert out["pr"] == "https://example.invalid/pr/1"
    assert git(bare, "rev-parse", "refs/heads/redteam/c1") == head
    assert [c["method"] for c in calls] == ["GET", "POST"]


def test_api_calls_carry_a_timeout(tmp_path: Path, monkeypatch):
    cfg, job, _ = merged_job(tmp_path, monkeypatch, "t1")
    calls, _ = fake_github(monkeypatch, job)
    redteam.close_job(cfg, job)
    no_timeout = [f"{c['method']} {c['url']}" for c in calls if not c["timeout"]]
    assert not no_timeout, f"urlopen called without a timeout: {no_timeout}"


def test_closing_a_closed_job_again_is_a_no_op(tmp_path: Path, monkeypatch):
    cfg, job, _ = merged_job(tmp_path, monkeypatch, "r1")
    first = redteam.close_job(cfg, job, open_pr=False)
    state1 = json.loads(Path(job.path, "job.json").read_text())
    try:
        second = redteam.close_job(cfg, redteam.load_job(cfg, "r1"), open_pr=False)
    except Exception as exc:
        second = {"error": f"{type(exc).__name__}: {exc}"}
    state2 = json.loads(Path(job.path, "job.json").read_text())
    assert second.get("head") == first["head"], f"second close: {second}"
    assert (state2["final_head"], state2["closed_at"]) == (state1["final_head"], state1["closed_at"])

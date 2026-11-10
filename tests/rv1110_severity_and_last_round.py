"""severity: medium
finding: severity is whatever the reviewer's file says, parsed loosely: a `severity: high` line with any trailing text is silently recorded as "medium", so highs == 0, the loop stops, and the PR body gives no hint that a high-severity fix went out without a re-review

redteam.py:38 (`SEVERITY_RE` requires the line to END after high|medium|low) and redteam.py:132-134 (`_severity`
falls back to "medium" without recording that nothing matched). `should_continue` (redteam.py:321-326) is driven
only by that count. When the round budget is exhausted the last fix is merged and shipped unreviewed, and
`pr_body` (redteam.py:373-384) says only "fix: merged".
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from autopoiesis import redteam

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
BODY = "from pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def open_job(tmp_path: Path, job_id: str, max_rounds: int = 2):
    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    (repo / "tests" / "test_green.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    cfg = redteam.RedTeamConfig(
        repo_path=str(repo), test_command=[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], python_path="src",
        cuts=["a", "b"], rotation_path=str(tmp_path / "rotation.json"), jobs_root=str(tmp_path / "jobs"),
        ledger_path=None, max_rounds=max_rounds)
    return cfg, redteam.open_job(cfg, job_id)


def test_control_exact_severity_line_counts_as_high(tmp_path: Path):
    cfg, job = open_job(tmp_path, "c1")
    (Path(job.review) / "tests" / "review1_add.py").write_text(f'"""severity: high\nfinding: add\n"""\n{BODY}')
    g = redteam.gate_findings(cfg, job, 1)
    assert g["highs"] == 1 and redteam.should_continue(cfg, job) is True


def test_high_with_a_trailing_remark_is_not_silently_downgraded(tmp_path: Path):
    cfg, job = open_job(tmp_path, "s1")
    (Path(job.review) / "tests" / "review1_add.py").write_text(
        f'"""severity: high (silent data loss)\nfinding: add\n"""\n{BODY}')
    g = redteam.gate_findings(cfg, job, 1)
    recorded = {f["name"]: f["severity"] for f in g["accepted"]}
    refused = {f["name"] for f in g["rejected"]}            # refusing a malformed header would also be correct
    assert "add" in refused or recorded.get("add") == "high", f"declared high, recorded as {recorded}"
    assert "add" in refused or redteam.should_continue(cfg, job) is True


def test_pr_body_says_when_the_last_high_fix_was_not_re_reviewed(tmp_path: Path):
    cfg, job = open_job(tmp_path, "p1", max_rounds=1)
    (Path(job.review) / "tests" / "review1_add.py").write_text(f'"""severity: high\nfinding: add\n"""\n{BODY}')
    assert redteam.gate_findings(cfg, job, 1)["highs"] == 1
    fix = Path(redteam.open_fix(cfg, job, 1))
    (fix / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a + b\n")
    git(fix, "commit", "-qam", "fix")
    assert redteam.gate_fix(cfg, job, 1)["verdict"] == "merged"
    assert redteam.should_continue(cfg, job) is False       # budget exhausted with a high in the last round
    body = redteam.pr_body(cfg, job)
    flagged = re.search(r"\b(not|no|without)\b.{0,40}review|unreviewed|round (budget|limit)|max_rounds", body, re.I)
    assert flagged, "PR body does not disclose that the fix for a high finding was merged without a further review round"

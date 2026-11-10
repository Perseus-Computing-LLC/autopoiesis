"""severity: high
finding: gate_findings never checks round_n against the recorded rounds: a repeated or out-of-order call appends a second entry, so rounds[round_n-1] (used by gate_fix/fixer_brief) pins the wrong evidence set and the round budget is miscounted

redteam.py:255-257 does `st["rounds"].append({"round": round_n, ...})` unconditionally, while redteam.py:215 and
redteam.py:284 index `st["rounds"][round_n - 1]` and redteam.py:324 counts `len(rounds)`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from autopoiesis import redteam

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
SRC_BUGGY = "def add(a, b):\n    return a - b  # bug 1\n\ndef double(x):\n    return x * 3  # bug 2\n\ndef one():\n    return 1\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def open_job(tmp_path: Path, job_id: str):
    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "__init__.py").write_text(SRC_BUGGY)
    (repo / "tests" / "test_green.py").write_text("from pkg import one\n\ndef test_one():\n    assert one() == 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    cfg = redteam.RedTeamConfig(
        repo_path=str(repo), test_command=[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], python_path="src",
        cuts=["a", "b"], rotation_path=str(tmp_path / "rotation.json"), jobs_root=str(tmp_path / "jobs"),
        ledger_path=None, max_rounds=2)
    return cfg, redteam.open_job(cfg, job_id)


def finding(review: Path, round_n: int, name: str, body: str) -> None:
    (review / "tests" / f"review{round_n}_{name}.py").write_text(f'"""severity: high\nfinding: {name}\n"""\n{body}')


def rounds(job) -> list:
    return json.loads(Path(job.path, "job.json").read_text())["rounds"]


ADD = "from pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
DOUBLE = "from pkg import double\n\ndef test_double():\n    assert double(2) == 4\n"


def test_control_single_call_records_one_round(tmp_path: Path):
    cfg, job = open_job(tmp_path, "c1")
    finding(Path(job.review), 1, "add", ADD)
    redteam.gate_findings(cfg, job, 1)
    assert [r["round"] for r in rounds(job)] == [1]
    assert redteam.should_continue(cfg, job) is True


def test_gating_the_same_round_twice_keeps_one_round_record(tmp_path: Path):
    cfg, job = open_job(tmp_path, "r1")
    finding(Path(job.review), 1, "add", ADD)
    redteam.gate_findings(cfg, job, 1)
    finding(Path(job.review), 1, "double", DOUBLE)          # the reviewer adds one more file, the operator re-gates
    try:
        redteam.gate_findings(cfg, job, 1)
    except Exception:
        pass                                               # refusing the repeat outright is also correct
    recorded = [r["round"] for r in rounds(job)]
    committed = sorted(p.name for p in (Path(job.integration) / "tests").glob("review1_*.py"))
    pinned = sorted(f["file"] for f in rounds(job)[0]["accepted"])      # what gate_fix(round 1) will hash
    assert recorded == [1], f"round 1 recorded {len(recorded)} times: {recorded}"
    assert pinned == committed, f"evidence committed but not pinned for gate_fix: {set(committed) - set(pinned)}"
    assert redteam.should_continue(cfg, job) is True       # one round done, one high, budget of two


def test_gating_round_two_before_round_one_is_refused(tmp_path: Path):
    cfg, job = open_job(tmp_path, "r2")
    finding(Path(job.review), 2, "add", ADD)
    try:
        redteam.gate_findings(cfg, job, 2)
    except Exception:
        pass
    recorded = [r["round"] for r in rounds(job)]
    assert all(r == i + 1 for i, r in enumerate(recorded)), f"rounds list is not 1..n in order: {recorded}"

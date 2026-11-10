"""AP-3 red-team loop harness: the mechanical gates of the adversarial review loop.

A finding counts only if its test FAILS on the unpatched head and PASSES after the fix; the fixer may not touch
the finding tests; rounds are bounded and stop at zero highs; every gate leaves a receipt. The agents (reviewer,
fixer) are outside this module; it only admits or refuses what they produce.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from autopoiesis import redteam

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def target(tmp_path: Path) -> Path:
    """A git repo with one buggy function and one green test."""
    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a - b  # bug\n\ndef one():\n    return 1\n")
    (repo / "tests" / "test_green.py").write_text("from pkg import one\n\ndef test_one():\n    assert one() == 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    return repo


def cfg_for(repo: Path, tmp_path: Path, **kw) -> redteam.RedTeamConfig:
    base = dict(
        repo_path=str(repo),
        test_command=[PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        python_path="src",
        cuts=["laundering", "atomicity", "concurrency"],
        rotation_path=str(tmp_path / "rotation.json"),
        jobs_root=str(tmp_path / "jobs"),
        ledger_path=None,
        max_rounds=2,
    )
    base.update(kw)
    return redteam.RedTeamConfig(**base)


def test_cut_rotation_is_per_repo_and_wraps(tmp_path: Path):
    path = tmp_path / "rotation.json"
    cuts = ["a", "b"]
    assert redteam.next_cut(path, "r1", cuts) == "a"
    assert redteam.next_cut(path, "r1", cuts) == "b"
    assert redteam.next_cut(path, "r2", cuts) == "a"           # other repo starts fresh
    assert redteam.next_cut(path, "r1", cuts) == "a"           # wraps, and the history is kept
    assert json.loads(path.read_text())["r1"] == ["a", "b", "a"]


def test_open_job_makes_isolated_worktrees_on_the_head(target: Path, tmp_path: Path):
    cfg = cfg_for(target, tmp_path)
    job = redteam.open_job(cfg, job_id="j1")
    assert job.cut == "laundering"
    assert Path(job.integration).is_dir() and Path(job.review).is_dir()
    assert git(Path(job.integration), "rev-parse", "HEAD") == git(target, "rev-parse", "main") == job.base_head
    assert git(Path(job.integration), "rev-parse", "--abbrev-ref", "HEAD") == "redteam/j1"
    assert git(Path(job.review), "rev-parse", "HEAD") == job.base_head
    assert git(target, "status", "--porcelain") == ""                                  # target untouched
    assert json.loads(Path(job.path, "job.json").read_text())["status"] == "open"
    brief = Path(job.path, "reviewer_brief.md").read_text()
    assert "laundering" in brief and "review1_" in brief and "fails on" in brief.lower()


def write_finding(review: Path, name: str, body: str, severity: str = "high") -> Path:
    p = review / "tests" / f"review1_{name}.py"
    p.write_text(f'"""severity: {severity}\nfinding: {name}\n"""\n{body}')
    return p


def test_gate_findings_admits_only_tests_that_fail_on_head(target: Path, tmp_path: Path):
    cfg = cfg_for(target, tmp_path)
    job = redteam.open_job(cfg, job_id="j2")
    review = Path(job.review)
    write_finding(review, "add", "from pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    write_finding(review, "passes", "from pkg import one\n\ndef test_one():\n    assert one() == 1\n", "medium")
    write_finding(review, "broken", "import nonexistent_module\n\ndef test_x():\n    assert False\n", "high")
    (review / "review_notes").mkdir()
    (review / "review_notes" / "R1.md").write_text("# round 1\n")

    g = redteam.gate_findings(cfg, job, round_n=1)
    assert sorted(f["name"] for f in g["accepted"]) == ["add"]
    rejected = {f["name"]: f["reason"] for f in g["rejected"]}
    assert rejected["passes"] == "passes_on_head"
    assert rejected["broken"] == "does_not_run"
    assert g["highs"] == 1
    # accepted evidence is committed on the integration branch before any fixer exists
    integ = Path(job.integration)
    assert (integ / "tests" / "review1_add.py").is_file()
    assert not (integ / "tests" / "review1_passes.py").exists()
    assert (integ / "review_notes" / "R1.md").is_file()
    assert git(integ, "status", "--porcelain") == ""
    assert "review 1" in git(integ, "log", "-1", "--format=%s")
    assert json.loads(Path(job.path, "job.json").read_text())["rounds"][0]["highs"] == 1


def test_gate_findings_with_nothing_accepted_stops_the_job(target: Path, tmp_path: Path):
    cfg = cfg_for(target, tmp_path)
    job = redteam.open_job(cfg, job_id="j3")
    write_finding(Path(job.review), "passes", "from pkg import one\n\ndef test_one():\n    assert one() == 1\n")
    g = redteam.gate_findings(cfg, job, round_n=1)
    assert g["accepted"] == [] and g["highs"] == 0
    assert redteam.should_continue(cfg, job) is False


def _accepted_round(cfg, job_id, target, tmp_path):
    job = redteam.open_job(cfg, job_id=job_id)
    write_finding(Path(job.review), "add", "from pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    redteam.gate_findings(cfg, job, round_n=1)
    fix = Path(redteam.open_fix(cfg, job, round_n=1))
    assert git(fix, "rev-parse", "--abbrev-ref", "HEAD") == "redteam/%s-fix1" % job_id
    assert (fix / "tests" / "review1_add.py").is_file()
    return job, fix


def test_gate_fix_requires_findings_green_and_suite_green_then_merges(target: Path, tmp_path: Path):
    cfg = cfg_for(target, tmp_path)
    job, fix = _accepted_round(cfg, "j4", target, tmp_path)
    (fix / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a + b\n\ndef one():\n    return 1\n")
    git(fix, "commit", "-qam", "fix add")
    v = redteam.gate_fix(cfg, job, round_n=1)
    assert v["verdict"] == "merged", v
    integ = Path(job.integration)
    assert "return a + b" in (integ / "src" / "pkg" / "__init__.py").read_text()
    assert git(integ, "status", "--porcelain") == ""
    assert job_state(job)["rounds"][0]["fix"] == "merged"


def test_gate_fix_rejects_a_fix_that_edits_the_finding_test(target: Path, tmp_path: Path):
    cfg = cfg_for(target, tmp_path)
    job, fix = _accepted_round(cfg, "j5", target, tmp_path)
    (fix / "tests" / "review1_add.py").write_text("def test_add():\n    assert True\n")       # weakened
    git(fix, "commit", "-qam", "weaken")
    v = redteam.gate_fix(cfg, job, round_n=1)
    assert v["verdict"] == "rejected" and v["reason"] == "finding_tests_modified"
    assert "return a - b" in (Path(job.integration) / "src" / "pkg" / "__init__.py").read_text()   # nothing merged


def test_gate_fix_rejects_a_fix_that_breaks_the_suite(target: Path, tmp_path: Path):
    cfg = cfg_for(target, tmp_path)
    job, fix = _accepted_round(cfg, "j6", target, tmp_path)
    (fix / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a + b\n\ndef one():\n    return 2\n")
    git(fix, "commit", "-qam", "fix add, break one")
    v = redteam.gate_fix(cfg, job, round_n=1)
    assert v["verdict"] == "rejected" and v["reason"] == "suite_red"


def test_gate_fix_rejects_a_fix_that_leaves_a_finding_red(target: Path, tmp_path: Path):
    cfg = cfg_for(target, tmp_path)
    job, fix = _accepted_round(cfg, "j7", target, tmp_path)
    (fix / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a * b\n\ndef one():\n    return 1\n")
    git(fix, "commit", "-qam", "wrong fix")
    v = redteam.gate_fix(cfg, job, round_n=1)
    assert v["verdict"] == "rejected" and v["reason"] == "findings_still_red"


def test_rounds_are_bounded_and_stop_at_zero_highs(target: Path, tmp_path: Path):
    cfg = cfg_for(target, tmp_path, max_rounds=2)
    job = redteam.open_job(cfg, job_id="j8")
    write_finding(Path(job.review), "add", "from pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    redteam.gate_findings(cfg, job, round_n=1)
    assert redteam.should_continue(cfg, job) is True        # one high, one round left
    state = job_state(job); state["rounds"][0]["highs"] = 0
    Path(job.path, "job.json").write_text(json.dumps(state))
    assert redteam.should_continue(cfg, job) is False        # zero highs stops
    state["rounds"][0]["highs"] = 1; state["rounds"].append({"round": 2, "highs": 1})
    Path(job.path, "job.json").write_text(json.dumps(state))
    assert redteam.should_continue(cfg, job) is False        # budget exhausted


# rv1110 F12 (rv1110_findings_unrelated_failure.py): a finding file that never imports the code under review is
# refused ("does_not_import_product"), because no product fix can turn it green. This test is about the stripped
# environment, not about that rule, so its finding now imports and uses the product (`one`) and still fails
# only because the variable was stripped. The old body (os.environ alone) is exactly what F12 refuses.
def test_tests_run_with_the_environment_stripped(target: Path, tmp_path: Path, monkeypatch):
    cfg = cfg_for(target, tmp_path, env_strip=["SECRET_PASSPHRASE"])
    monkeypatch.setenv("SECRET_PASSPHRASE", "x")
    job = redteam.open_job(cfg, job_id="j9")
    write_finding(Path(job.review), "env",
                  "import os\nfrom pkg import one\n\ndef test_env():\n"
                  "    assert one() == 1 and 'SECRET_PASSPHRASE' in os.environ\n")
    g = redteam.gate_findings(cfg, job, round_n=1)
    assert [f["name"] for f in g["accepted"]] == ["env"]     # it fails only because the env was stripped


def test_receipts_are_written_when_a_ledger_is_configured(target: Path, tmp_path: Path):
    pytest.importorskip("argus_ledger")
    cfg = cfg_for(target, tmp_path)
    cfg.ledger_path = str(tmp_path / "ledger.jsonl")
    job = redteam.open_job(cfg, job_id="j10")
    write_finding(Path(job.review), "add", "from pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    redteam.gate_findings(cfg, job, round_n=1)
    kinds = [json.loads(l)["event_type"] for l in Path(cfg.ledger_path).read_text().splitlines()]
    assert kinds == ["redteam.job_opened", "redteam.findings_gated"]


def job_state(job) -> dict:
    return json.loads(Path(job.path, "job.json").read_text())

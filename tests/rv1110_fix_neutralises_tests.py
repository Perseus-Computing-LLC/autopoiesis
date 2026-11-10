"""severity: high
finding: gate_fix merges a branch that changes no product code: a conftest.py that skips every test (or deleting a red suite test) turns "findings pass + suite passes" green

redteam.py:296-304. The gate hashes the finding files and then trusts pytest's exit code. Everything else that
decides that exit code is fixer-controlled and unchecked: conftest.py, pytest config (addopts/-k/testpaths in
pyproject/pytest.ini/tox.ini), and the pre-existing suite tests themselves. A skipped test exits 0.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from autopoiesis import redteam

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
SRC_BUGGY = "def add(a, b):\n    return a - b  # bug\n\ndef one():\n    return 1\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def round_one(tmp_path: Path, job_id: str):
    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "__init__.py").write_text(SRC_BUGGY)
    (repo / "tests" / "test_green.py").write_text("from pkg import one\n\ndef test_one():\n    assert one() == 1\n")
    (repo / "tests" / "test_misc.py").write_text("import pkg\n\ndef test_importable():\n    assert pkg is not None\n")
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


def test_control_honest_fix_is_merged(tmp_path: Path):
    cfg, job, fix = round_one(tmp_path, "c1")
    (fix / "src" / "pkg" / "__init__.py").write_text(SRC_BUGGY.replace("a - b  # bug", "a + b"))
    git(fix, "commit", "-qam", "fix add")
    assert redteam.gate_fix(cfg, job, 1)["verdict"] == "merged"


def test_fix_that_only_adds_a_skipping_conftest_is_refused(tmp_path: Path):
    cfg, job, fix = round_one(tmp_path, "s1")
    (fix / "tests" / "conftest.py").write_text(
        "import pytest\n\ndef pytest_collection_modifyitems(items):\n"
        "    for item in items:\n        item.add_marker(pytest.mark.skip(reason='later'))\n")
    git(fix, "add", "-A")
    git(fix, "commit", "-qm", "test infrastructure")
    v = redteam.gate_fix(cfg, job, 1)
    product = (Path(job.integration) / "src" / "pkg" / "__init__.py").read_text()
    assert v["verdict"] == "rejected", f"no product change, finding test never executed, yet admitted: {v}"
    assert "a - b" in product            # (and the defect is indeed still there)


def test_fix_that_deletes_a_pre_existing_suite_test_is_refused(tmp_path: Path):
    cfg, job, fix = round_one(tmp_path, "d1")
    # fixes add, breaks one(); the suite test that would catch it is removed in the same branch
    (fix / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a + b\n\ndef one():\n    return 2\n")
    git(fix, "rm", "-q", "tests/test_green.py")
    git(fix, "commit", "-qam", "fix add")
    v = redteam.gate_fix(cfg, job, 1)
    assert v["verdict"] == "rejected", f"a pre-existing suite test was deleted to get a green suite: {v}"

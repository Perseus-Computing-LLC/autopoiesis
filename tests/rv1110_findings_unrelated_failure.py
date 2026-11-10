"""severity: medium
finding: gate_findings admits a finding test that cannot be about the product (`assert False`, no import of the code under review); redteam never applies the project's own trivial-test check, and the round can then never be fixed without touching evidence

redteam.py:238-240 admits on `rc == 1` alone. evaluator.py:17-48 has `_is_trivial_test_file` (used only by
SubprocessFitnessEvaluator, and it flags only `assert True`/`pass`, not `assert False`); redteam.py does not
import it. An unfixable "finding" is committed as evidence, counted in `highs`, and every honest fix is refused
with `findings_still_red`, because the fixer may not edit the file.
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


def open_job(tmp_path: Path, job_id: str):
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
        ledger_path=None)
    return cfg, redteam.open_job(cfg, job_id)


def test_control_real_finding_is_admitted(tmp_path: Path):
    cfg, job = open_job(tmp_path, "c1")
    (Path(job.review) / "tests" / "review1_add.py").write_text(
        '"""severity: high\nfinding: add\n"""\nfrom pkg import add\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    assert [f["name"] for f in redteam.gate_findings(cfg, job, 1)["accepted"]] == ["add"]


def test_constant_false_finding_is_refused(tmp_path: Path):
    cfg, job = open_job(tmp_path, "t1")
    (Path(job.review) / "tests" / "review1_bogus.py").write_text(
        '"""severity: high\nfinding: everything is broken\n"""\n\ndef test_bogus():\n    assert False\n')
    g = redteam.gate_findings(cfg, job, 1)
    assert [f["name"] for f in g["accepted"]] == [], f"admitted {g['accepted']} with highs={g['highs']}"


def test_finding_that_fails_on_a_missing_optional_import_inside_the_test_is_refused(tmp_path: Path):
    cfg, job = open_job(tmp_path, "t2")
    (Path(job.review) / "tests" / "review1_dep.py").write_text(
        '"""severity: high\nfinding: needs a helper that is not installed\n"""\n\n'
        "def test_dep():\n    import rv1110_module_that_is_not_installed  # noqa: F401\n")
    g = redteam.gate_findings(cfg, job, 1)
    assert [f["name"] for f in g["accepted"]] == [], f"admitted a test that never reached the product: {g['accepted']}"

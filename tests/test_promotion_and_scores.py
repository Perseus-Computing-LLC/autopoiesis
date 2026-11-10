"""Regression tests for promotion, configuration flags, mutation scores and the evaluator.

Cases next to the ones the rv1110 finding files use: another way a promotion can fail to land, another spelling
of "false", another reason a kill score is unknown.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from autopoiesis.config import _flag, load_config
from autopoiesis.deployment import GitPromotionManager, PromotionError
from autopoiesis.evaluator import SubprocessFitnessEvaluator
from autopoiesis.models import MutationProposal, ParetoVerdict
from autopoiesis.mutation_test import DifferentialMutationTester

PY = sys.executable
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=check, capture_output=True, text=True)


def candidate(tmp_path: Path, monkeypatch, dominant: bool = True):
    """A repo on `main` and a candidate worktree with one uncommitted change."""
    for var, val in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.com"),
                     ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.com")):
        monkeypatch.setenv(var, val)                          # promote_mutation commits with the ambient identity
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.txt").write_text("v1\n")
    (repo / ".gitignore").write_text(".autopoiesis/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "candidate", str(wt), "main")
    (wt / "app.txt").write_text("v2\n")
    mutation = MutationProposal(proposal_id="mut_t", cluster_id="c1", target_repo="repo", target_files=["app.txt"],
                                diff="x", branch_name="candidate", worktree_path=str(wt), rationale="r")
    verdict = ParetoVerdict(candidate_id="mut_t", is_pareto_dominant=dominant, zero_regression=True,
                            defect_eliminated=True, performance_acceptable=True, security_verified=True)
    return repo, wt, mutation, verdict


def tip(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref).stdout.strip()


def cards(repo: Path) -> list:
    return sorted(p.name for p in (repo / ".autopoiesis" / "attestations").glob("*.json"))


# ------------------------------------------------------------------------------------------- promotion
def test_release_branch_not_checked_out_anywhere_is_moved_by_ref(tmp_path: Path, monkeypatch):
    repo, _, mutation, verdict = candidate(tmp_path, monkeypatch)
    git(repo, "checkout", "-q", "-b", "dev")                  # nobody has `main` checked out
    (repo / "wip.txt").write_text("the operator's uncommitted work\n")
    receipt = GitPromotionManager(repo, release_branch="main").promote_mutation(mutation, verdict)
    assert tip(repo, "main") == receipt.commit_sha
    assert tip(repo, "dev") != receipt.commit_sha             # the branch that happened to be checked out stays
    assert (repo / "app.txt").read_text() == "v1\n" and (repo / "wip.txt").exists()
    assert cards(repo) == [f"{receipt.release_id}.json"]


def test_release_branch_checked_out_in_the_repo_gets_its_files_updated(tmp_path: Path, monkeypatch):
    repo, _, mutation, verdict = candidate(tmp_path, monkeypatch)
    receipt = GitPromotionManager(repo, release_branch="main").promote_mutation(mutation, verdict)
    assert tip(repo, "main") == receipt.commit_sha and (repo / "app.txt").read_text() == "v2\n"
    assert git(repo, "status", "--porcelain").stdout.strip() == ""


@pytest.mark.parametrize("why", ["diverged", "dirty_conflict", "missing_branch", "nothing_to_commit"])
def test_a_promotion_that_cannot_land_raises_and_attests_nothing(tmp_path: Path, monkeypatch, why: str):
    repo, wt, mutation, verdict = candidate(tmp_path, monkeypatch)
    branch = "main"
    if why == "diverged":
        (repo / "other.txt").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "concurrent")
    elif why == "dirty_conflict":
        (repo / "app.txt").write_text("local edit the fast-forward would overwrite\n")
    elif why == "missing_branch":
        branch = "release"
    else:
        (wt / "app.txt").write_text("v1\n")                   # the candidate changes nothing
    before = tip(repo, "main")
    with pytest.raises(PromotionError):
        GitPromotionManager(repo, release_branch=branch, ledger_path=tmp_path / "audit.jsonl").promote_mutation(
            mutation, verdict)
    assert tip(repo, "main") == before
    assert cards(repo) == [] and not (tmp_path / "audit.jsonl").exists()


def test_a_non_dominant_candidate_is_still_refused_before_anything_is_committed(tmp_path: Path, monkeypatch):
    repo, wt, mutation, verdict = candidate(tmp_path, monkeypatch, dominant=False)
    with pytest.raises(ValueError):
        GitPromotionManager(repo, release_branch="main").promote_mutation(mutation, verdict)
    assert tip(repo, "candidate") == tip(repo, "main") and git(wt, "status", "--porcelain").stdout.strip() != ""


# ------------------------------------------------------------------------------------------- configuration
@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("false", False), ("False", False),
                                            ("no", False), ("off", False), ("0", False), (0, False),
                                            ("true", True), ("yes", True), ("on", True), (1, True)])
def test_boolean_settings_are_read_by_meaning(value, expected):
    assert _flag(value, "deployment.auto_merge") is expected


@pytest.mark.parametrize("value", ["maybe", "", None, 2, [], 0.0])
def test_a_setting_that_is_not_a_boolean_is_refused(value):
    with pytest.raises(ValueError, match="deployment.auto_merge"):
        _flag(value, "deployment.auto_merge")


def test_auto_merge_written_as_a_quoted_string_is_not_read_as_true(tmp_path: Path):
    path = tmp_path / "autopoiesis.yaml"
    path.write_text('version: 1\nproject:\n  name: p\n  repo_path: "."\ndeployment:\n  auto_merge: "false"\n')
    assert load_config(path).deployment.auto_merge is False


# ------------------------------------------------------------------------------------------- mutation score
def scorer(tmp_path: Path, source: str, test_body: str, name: str = "calc") -> DifferentialMutationTester:
    (tmp_path / f"{name}.py").write_text(source)
    (tmp_path / f"test_{name}.py").write_text(
        f"import sys\nsys.path.insert(0, {str(tmp_path)!r})\nimport {name}\n\n{test_body}")
    return DifferentialMutationTester(tmp_path, test_command=f"{PY} -m pytest -q -p no:cacheprovider test_{name}.py")


def test_unmeasured_results_say_why(tmp_path: Path):
    red = scorer(tmp_path, "def f(x):\n    return x + 5\n", "def test_f():\n    assert calc.f(1) == 0\n").evaluate(["calc.py"])
    assert (red.measured, red.mutation_kill_score, red.total_mutants) == (False, 0.0, 0)
    assert "do not pass" in red.reason

    none = scorer(tmp_path, "def f(x):\n    return abs(x)\n", "def test_f():\n    assert calc.f(-1) == 1\n").evaluate(["calc.py"])
    assert (none.measured, none.mutation_kill_score) == (False, 0.0) and "no mutant" in none.reason

    missing = scorer(tmp_path, "def f(x):\n    return x + 5\n", "def test_f():\n    assert calc.f(1) == 6\n").evaluate(["gone.py"])
    assert missing.measured is False and missing.mutation_kill_score == 0.0


def test_a_measured_score_counts_surviving_mutants(tmp_path: Path):
    # the test never looks at the result, so every mutant survives: measured, and zero
    res = scorer(tmp_path, "def f(x):\n    return x + 5\n", "def test_f():\n    calc.f(1)\n").evaluate(["calc.py"])
    assert res.measured is True and res.reason == ""
    assert res.total_mutants == 2 and res.killed_mutants == 0 and res.mutation_kill_score == 0.0


def test_two_operators_of_one_type_on_one_line_each_get_their_own_mutant(tmp_path: Path):
    src = "def f(a, b, c):\n    return a + b + c\n"
    res = scorer(tmp_path, src, "def test_f():\n    assert calc.f(1, 2, 4) == 7\n").evaluate(["calc.py"])
    original = ast.unparse(ast.parse(src))
    programs = {m.mutated_code for m in res.mutants}
    assert original not in programs
    assert any("a - b + c" in p for p in programs) and any("a + b - c" in p for p in programs)
    assert res.measured and res.mutation_kill_score == 1.0


def test_the_sandbox_file_is_restored_after_scoring(tmp_path: Path):
    src = "def f(x):\n    return x * 2 + 1\n"
    t = scorer(tmp_path, src, "def test_f():\n    assert calc.f(3) == 7\n")
    t.evaluate(["calc.py"])
    assert (tmp_path / "calc.py").read_text() == src


# ------------------------------------------------------------------------------------------- evaluator
def test_a_golden_command_that_cannot_finish_is_a_failed_candidate_with_a_reason(tmp_path: Path):
    challenge = tmp_path / "test_challenge.py"
    challenge.write_text("def test_ok():\n    assert True\n")
    mutation = MutationProposal(proposal_id="m", cluster_id="c", target_repo="r", target_files=[], diff="",
                                branch_name="b", worktree_path=str(tmp_path), rationale="r")
    ev = SubprocessFitnessEvaluator(golden_test_cmd=f"{PY} -c 'import time; time.sleep(30)'", timeout_seconds=1)
    result = ev.evaluate(mutation, [challenge])
    assert (result.golden_tests_failed, result.test_pass_rate) == (1, 0.0)
    assert "timeout" in result.details["golden_error"]

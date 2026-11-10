"""severity: medium
finding: SubprocessFitnessEvaluator.evaluate crashes with UnboundLocalError when the golden test command times out (or cannot be started) instead of reporting a failed candidate

evaluator.py:93-107 catches the exception from `subprocess.run` and sets golden_failed = 1, but evaluator.py:147
then reads `res_golden.stdout` guarded only by `total_golden > 0`, which is always true (1 passed or 1 failed).
`res_golden` was never bound, so the evaluator raises and the cycle dies with a traceback instead of a verdict.
"""
from __future__ import annotations

from pathlib import Path

from autopoiesis.evaluator import SubprocessFitnessEvaluator
from autopoiesis.models import MutationProposal


def proposal(worktree: Path) -> MutationProposal:
    return MutationProposal(proposal_id="mut_rv1110", cluster_id="c1", target_repo="r", target_files=["a.py"],
                            diff="x", branch_name="b", worktree_path=str(worktree), rationale="r")


def test_control_failing_golden_command_is_reported_as_failed(tmp_path: Path):
    report = SubprocessFitnessEvaluator(golden_test_cmd="exit 1", timeout_seconds=30).evaluate(proposal(tmp_path), [])
    assert (report.golden_tests_failed, report.test_pass_rate) == (1, 0.0)


def test_golden_timeout_is_reported_as_failed_not_raised(tmp_path: Path):
    evaluator = SubprocessFitnessEvaluator(golden_test_cmd="sleep 3", timeout_seconds=1)
    try:
        report = evaluator.evaluate(proposal(tmp_path), [])
        outcome = (report.golden_tests_failed, report.test_pass_rate)
    except Exception as exc:
        outcome = f"{type(exc).__name__}: {exc}"
    assert outcome == (1, 0.0), f"timeout of the golden suite: {outcome}"

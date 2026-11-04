"""Unit and integration tests for Autopoiesis."""

import json
import subprocess
from pathlib import Path

import pytest

from autopoiesis.curriculum import CurriculumGenerator, FileBasedCurriculumGenerator
from autopoiesis.deployment import GitPromotionManager
from autopoiesis.engine import AutopoiesisEngine
from autopoiesis.evaluator import SubprocessFitnessEvaluator
from autopoiesis.gate import ParetoGate
from autopoiesis.models import FailureCluster, FitnessReport, MutationProposal, TelemetryEvent
from autopoiesis.mutation import PatchApplicatorMutationEngine, WorktreeManager
from autopoiesis.telemetry import ClusterEngine, JsonlTelemetrySource


def test_cluster_engine_groups_errors():
    events = [
        TelemetryEvent(
            sequence=1, timestamp="2026-11-04T00:00:00Z", tool="terminal", status="error",
            error_message="exit 127: command not found: foo", exit_code=127
        ),
        TelemetryEvent(
            sequence=2, timestamp="2026-11-04T00:01:00Z", tool="terminal", status="error",
            error_message="exit 127: command not found: bar", exit_code=127
        ),
        TelemetryEvent(
            sequence=3, timestamp="2026-11-04T00:02:00Z", tool="terminal", status="ok",
            error_message=None, exit_code=0
        ),
    ]
    engine = ClusterEngine()
    clusters = engine.cluster_events(events)

    assert len(clusters) == 1
    assert clusters[0].occurrences == 2
    assert clusters[0].tool == "terminal"
    assert "command not found" in clusters[0].template


def test_pareto_gate_strict_rejection_on_regression():
    gate = ParetoGate()

    regressed = FitnessReport(
        candidate_id="mut_bad",
        test_pass_rate=0.95,
        golden_tests_passed=19,
        golden_tests_failed=1,
        synthetic_tests_passed=5,
        synthetic_tests_failed=0,
        target_defect_rate=0.0,
        p95_latency_ms=10.0,
        memory_peak_mb=50.0,
        opsec_passed=True,
        audit_chain_valid=True,
    )
    verdict = gate.evaluate_candidate(regressed)
    assert not verdict.is_pareto_dominant
    assert not verdict.zero_regression
    assert any("Regression detected" in r for r in verdict.rejection_reasons)
    assert verdict.reclaimed_prompt_tokens == 0


def test_pareto_gate_accepts_dominant_candidate():
    gate = ParetoGate(prompt_token_dividend_per_rule=45)

    dominant = FitnessReport(
        candidate_id="mut_good",
        test_pass_rate=1.0,
        golden_tests_passed=20,
        golden_tests_failed=0,
        synthetic_tests_passed=5,
        synthetic_tests_failed=0,
        target_defect_rate=0.0,
        p95_latency_ms=10.0,
        memory_peak_mb=50.0,
        opsec_passed=True,
        audit_chain_valid=True,
    )
    verdict = gate.evaluate_candidate(dominant)
    assert verdict.is_pareto_dominant
    assert verdict.zero_regression
    assert verdict.defect_eliminated
    assert verdict.reclaimed_prompt_tokens == 45


def test_end_to_end_autopoiesis_rejects_noop_stub_and_cleans_branches(tmp_path: Path):
    repo_dir = tmp_path / "mock_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "agent@perseus.test"], cwd=str(repo_dir), check=True)

    initial_file = repo_dir / "app.py"
    initial_file.write_text("def run():\n    return 'hello'\n", encoding="utf-8")
    test_file = repo_dir / "test_app.py"
    test_file.write_text("import app\ndef test_app():\n    assert app.run() == 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), check=True)

    log_file = tmp_path / "telemetry.jsonl"
    log_file.write_text(json.dumps({
        "sequence": 1,
        "timestamp": "2026-11-04T00:00:00Z",
        "tool": "terminal",
        "status": "error",
        "error_message": "exit 127: command not found: foo",
        "exit_code": 127
    }) + "\n", encoding="utf-8")

    engine = AutopoiesisEngine(
        telemetry_source=JsonlTelemetrySource(log_file),
        cluster_engine=ClusterEngine(),
        worktree_manager=WorktreeManager(repo_dir, tmp_path / "worktrees"),
        mutation_engine=PatchApplicatorMutationEngine(repo_name="mock_repo"),
        curriculum_generator=FileBasedCurriculumGenerator(),
        fitness_evaluator=SubprocessFitnessEvaluator(golden_test_cmd="pytest -q test_app.py"),
        pareto_gate=ParetoGate(prompt_token_dividend_per_rule=40),
        deployment_manager=GitPromotionManager(repo_dir, release_branch="main"),
    )

    result = engine.run_cycle(dry_run=False)
    assert result["status"] == "evaluated_not_promoted"
    assert result["promoted"] is False
    assert any("No-op mutation" in r for r in result["verdict"]["rejection_reasons"])
    assert any("Trivial curriculum" in r for r in result["verdict"]["rejection_reasons"])

    # Verify temporary autopoiesis-eval-* branch was cleaned up
    branches = subprocess.check_output(["git", "-C", str(repo_dir), "branch"], text=True)
    assert "autopoiesis-eval-" not in branches


class _RealCurriculumGenerator(CurriculumGenerator):
    def generate_curriculum(
        self, cluster: FailureCluster, mutation: MutationProposal, sandbox_path: Path
    ):
        tests_dir = sandbox_path / "tests" / "autopoiesis_synthetic"
        tests_dir.mkdir(parents=True, exist_ok=True)
        test_file = tests_dir / f"test_cluster_{cluster.cluster_id}.py"
        test_file.write_text(
            "import app\ndef test_remediated_command():\n    assert app.resolve_cmd('foo') == '/usr/bin/foo'\n",
            encoding="utf-8",
        )
        return [test_file]


def test_end_to_end_autopoiesis_cycle_promotes_real_mutation(tmp_path: Path):
    repo_dir = tmp_path / "mock_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "agent@perseus.test"], cwd=str(repo_dir), check=True)

    initial_file = repo_dir / "app.py"
    initial_file.write_text("def run():\n    return 'hello'\n", encoding="utf-8")
    test_file = repo_dir / "test_app.py"
    test_file.write_text("import app\ndef test_app():\n    assert app.run() == 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), check=True)

    log_file = tmp_path / "telemetry.jsonl"
    log_file.write_text(json.dumps({
        "sequence": 1,
        "timestamp": "2026-11-04T00:00:00Z",
        "tool": "terminal",
        "status": "error",
        "error_message": "exit 127: command not found: foo",
        "exit_code": 127
    }) + "\n", encoding="utf-8")

    def _apply_fix(cluster: FailureCluster, sandbox_path: Path):
        target = sandbox_path / "app.py"
        target.write_text(
            "def run():\n    return 'hello'\n\ndef resolve_cmd(name: str) -> str:\n    return f'/usr/bin/{name}'\n",
            encoding="utf-8",
        )
        return ["app.py"]

    engine = AutopoiesisEngine(
        telemetry_source=JsonlTelemetrySource(log_file),
        cluster_engine=ClusterEngine(),
        worktree_manager=WorktreeManager(repo_dir, tmp_path / "worktrees"),
        mutation_engine=PatchApplicatorMutationEngine(repo_name="mock_repo", mutator_fn=_apply_fix),
        curriculum_generator=_RealCurriculumGenerator(),
        fitness_evaluator=SubprocessFitnessEvaluator(golden_test_cmd="pytest -q test_app.py"),
        pareto_gate=ParetoGate(prompt_token_dividend_per_rule=40),
        deployment_manager=GitPromotionManager(repo_dir, release_branch="main"),
    )

    result = engine.run_cycle(dry_run=False)
    assert result["status"] == "promoted"
    assert result["promoted"] is True
    assert result["reclaimed_tokens"] == 40
    assert "release_id" in result["receipt"]

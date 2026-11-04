"""Unit and integration tests for Autopoiesis."""

import json
import subprocess
from pathlib import Path

import pytest

from autopoiesis.curriculum import FileBasedCurriculumGenerator
from autopoiesis.deployment import GitPromotionManager
from autopoiesis.engine import AutopoiesisEngine
from autopoiesis.evaluator import SubprocessFitnessEvaluator
from autopoiesis.gate import ParetoGate
from autopoiesis.models import FitnessReport, MutationProposal, TelemetryEvent
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

    # Candidate with 1 golden failure must be rejected
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


def test_end_to_end_autopoiesis_cycle(tmp_path: Path):
    # Setup mock git repo
    repo_dir = tmp_path / "mock_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "agent@perseus.test"], cwd=str(repo_dir), check=True)

    # Initial file & commit
    initial_file = repo_dir / "app.py"
    initial_file.write_text("def run():\n    return 'hello'\n", encoding="utf-8")
    test_file = repo_dir / "test_app.py"
    test_file.write_text("import app\ndef test_app():\n    assert app.run() == 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), check=True)

    # Telemetry source
    log_file = tmp_path / "telemetry.jsonl"
    log_file.write_text(json.dumps({
        "sequence": 1,
        "timestamp": "2026-11-04T00:00:00Z",
        "tool": "terminal",
        "status": "error",
        "error_message": "exit 127: command not found: foo",
        "exit_code": 127
    }) + "\n", encoding="utf-8")

    source = JsonlTelemetrySource(log_file)
    cluster_engine = ClusterEngine()
    worktree_mgr = WorktreeManager(repo_dir, tmp_path / "worktrees")
    mutation_engine = PatchApplicatorMutationEngine(repo_name="mock_repo")
    curriculum_gen = FileBasedCurriculumGenerator()
    evaluator = SubprocessFitnessEvaluator(golden_test_cmd="pytest -q test_app.py")
    gate = ParetoGate(prompt_token_dividend_per_rule=40)
    deployer = GitPromotionManager(repo_dir, release_branch="main")

    engine = AutopoiesisEngine(
        telemetry_source=source,
        cluster_engine=cluster_engine,
        worktree_manager=worktree_mgr,
        mutation_engine=mutation_engine,
        curriculum_generator=curriculum_gen,
        fitness_evaluator=evaluator,
        pareto_gate=gate,
        deployment_manager=deployer,
    )

    result = engine.run_cycle(dry_run=False)

    assert result["status"] == "promoted"
    assert result["promoted"] is True
    assert result["reclaimed_tokens"] == 40
    assert "release_id" in result["receipt"]

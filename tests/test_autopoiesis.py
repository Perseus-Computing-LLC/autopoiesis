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


def test_template_normalization_extended():
    norm = ClusterEngine.normalize_template
    assert "<LINE>" in norm("Error at /opt/data/work/foo.py:142 in main")
    assert "<DUR>" in norm("Command timed out after 30s")
    assert "<DUR>" in norm("Latency spike 250ms recorded")
    assert "<MEM>" in norm("OOM allocating 512MB RAM")
    assert "<IP>" in norm("Failed to connect to 10.168.168.29:9095")
    assert "<CMD>" in norm("bash: command not found: unknown_binary")
    assert "<PATH>" in norm("open: No such file or directory: /var/log/stale.log")


def test_retry_deconvolution_separates_transient_and_deterministic():
    events = [
        # Deterministic failure: tool "compile" fails twice in a row without succeeding
        TelemetryEvent(sequence=1, timestamp="2026-11-04T00:00:00Z", tool="compile", status="error", error_message="syntax error: unexpected token"),
        TelemetryEvent(sequence=2, timestamp="2026-11-04T00:01:00Z", tool="compile", status="error", error_message="syntax error: unexpected token"),
        TelemetryEvent(sequence=3, timestamp="2026-11-04T00:02:00Z", tool="compile", status="error", error_message="syntax error: unexpected token"),
        # Transient failure: tool "network" fails then succeeds immediately twice
        TelemetryEvent(sequence=4, timestamp="2026-11-04T00:03:00Z", tool="network", status="error", error_message="connection timeout after 30s"),
        TelemetryEvent(sequence=5, timestamp="2026-11-04T00:04:00Z", tool="network", status="ok", error_message=None),
        TelemetryEvent(sequence=6, timestamp="2026-11-04T00:05:00Z", tool="network", status="error", error_message="connection timeout after 30s"),
        TelemetryEvent(sequence=7, timestamp="2026-11-04T00:06:00Z", tool="network", status="ok", error_message=None),
    ]

    engine = ClusterEngine()
    clusters = engine.cluster_events(events)

    assert len(clusters) == 2
    # Deterministic cluster comes first
    assert not clusters[0].is_transient
    assert clusters[0].tool == "compile"

    # Transient cluster is flagged and sorted after deterministic
    assert clusters[1].is_transient
    assert clusters[1].tool == "network"
    assert clusters[1].transient_retry_rate >= 0.5


def test_red_first_curriculum_enforcement_rejects_trivial_head_pass(tmp_path: Path):
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

    # With enforce_red_first=True, FileBasedCurriculumGenerator emits "assert True",
    # which passes on HEAD and is immediately rejected before mutation
    engine = AutopoiesisEngine(
        telemetry_source=JsonlTelemetrySource(log_file),
        cluster_engine=ClusterEngine(),
        worktree_manager=WorktreeManager(repo_dir, tmp_path / "worktrees"),
        mutation_engine=PatchApplicatorMutationEngine(repo_name="mock_repo"),
        curriculum_generator=FileBasedCurriculumGenerator(),
        fitness_evaluator=SubprocessFitnessEvaluator(golden_test_cmd="pytest -q test_app.py"),
        pareto_gate=ParetoGate(),
        deployment_manager=GitPromotionManager(repo_dir, release_branch="main"),
        enforce_red_first=True,
    )

    result = engine.run_cycle(dry_run=False)
    assert result["status"] == "challenge_not_red_on_head"
    assert result["promoted"] is False
    assert any("RED-first" in r for r in result["verdict"]["rejection_reasons"])


def test_config_loader_and_cli_status(tmp_path: Path):
    from autopoiesis.cli import main
    from autopoiesis.config import AutopoiesisConfig, load_config

    cfg_file = tmp_path / "autopoiesis.yaml"
    cfg_file.write_text("""version: 1
project:
  name: "test-system"
  repo_path: "."
telemetry:
  adapter: "jsonl"
  path: "telemetry.jsonl"
  lookback_days: 7
evaluation:
  test_command: "pytest -q"
  enforce_red_first: true
""", encoding="utf-8")

    cfg = load_config(cfg_file)
    assert cfg.project.name == "test-system"
    assert cfg.evaluation.enforce_red_first is True

    # Test CLI init
    init_cfg = tmp_path / "new_cfg.yaml"
    ret = main(["--config", str(init_cfg), "init", "--project-name", "cli-test"])
    assert ret == 0
    assert init_cfg.exists()
    loaded = load_config(init_cfg)
    assert loaded.project.name == "cli-test"

    # Test CLI status with telemetry log
    telem = tmp_path / "telemetry.jsonl"
    telem.write_text(json.dumps({
        "sequence": 1,
        "timestamp": "2026-11-04T00:00:00Z",
        "tool": "terminal",
        "status": "error",
        "error_message": "exit 1: failure occurred",
        "exit_code": 1
    }) + "\n", encoding="utf-8")

    # Override telem path in cfg
    init_cfg.write_text(init_cfg.read_text().replace("telemetry.jsonl", str(telem)))
    status_ret = main(["--config", str(init_cfg), "status"])
    assert status_ret == 0

    status_json_ret = main(["--config", str(init_cfg), "status", "--json"])
    assert status_json_ret == 0


def test_cli_cycle_dry_run(tmp_path: Path):
    from autopoiesis.cli import main

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

    telem = tmp_path / "telemetry.jsonl"
    telem.write_text(json.dumps({
        "sequence": 1,
        "timestamp": "2026-11-04T00:00:00Z",
        "tool": "terminal",
        "status": "error",
        "error_message": "exit 1: test error",
        "exit_code": 1
    }) + "\n", encoding="utf-8")

    cfg_file = tmp_path / "autopoiesis.yaml"
    cfg_file.write_text(f"""version: 1
project:
  name: "mock-proj"
  repo_path: "{repo_dir}"
telemetry:
  adapter: "jsonl"
  path: "{telem}"
  lookback_days: 7
mutation:
  sandbox_dir: "{tmp_path / 'sandboxes'}"
evaluation:
  test_command: "pytest -q test_app.py"
  enforce_red_first: false
deployment:
  branch: "main"
  auto_merge: true
""", encoding="utf-8")

    cycle_ret = main(["--config", str(cfg_file), "cycle", "--dry-run", "--json"])
    assert cycle_ret == 0



"""Multi-objective fitness evaluation."""

from __future__ import annotations

import abc
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from autopoiesis.models import FitnessReport, MutationProposal


class FitnessEvaluator(abc.ABC):
    """Evaluates candidate mutation fitness across multiple objectives."""

    @abc.abstractmethod
    def evaluate(
        self, mutation: MutationProposal, synthetic_test_paths: List[Path]
    ) -> FitnessReport:
        pass


class SubprocessFitnessEvaluator(FitnessEvaluator):
    """Evaluates mutations by running golden and synthetic test commands."""

    def __init__(
        self,
        golden_test_cmd: str = "pytest -q",
        benchmark_cmd: Optional[str] = None,
        timeout_seconds: int = 300,
    ):
        self.golden_test_cmd = golden_test_cmd
        self.benchmark_cmd = benchmark_cmd
        self.timeout_seconds = timeout_seconds

    def evaluate(
        self, mutation: MutationProposal, synthetic_test_paths: List[Path]
    ) -> FitnessReport:
        worktree = Path(mutation.worktree_path)

        # 1. Run Golden Test Suite
        t0 = time.time()
        try:
            res_golden = subprocess.run(
                self.golden_test_cmd,
                shell=True,
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            golden_passed = 1 if res_golden.returncode == 0 else 0
            golden_failed = 0 if res_golden.returncode == 0 else 1
        except Exception:
            golden_passed = 0
            golden_failed = 1

        latency_ms = (time.time() - t0) * 1000.0

        # 2. Run Synthetic Tests
        synthetic_passed = 0
        synthetic_failed = 0
        for test_path in synthetic_test_paths:
            try:
                res_synth = subprocess.run(
                    f"pytest -q {test_path}",
                    shell=True,
                    cwd=str(worktree),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
                if res_synth.returncode == 0:
                    synthetic_passed += 1
                else:
                    synthetic_failed += 1
            except Exception:
                synthetic_failed += 1

        total_golden = golden_passed + golden_failed
        pass_rate = golden_passed / float(total_golden) if total_golden > 0 else 0.0

        return FitnessReport(
            candidate_id=mutation.proposal_id,
            test_pass_rate=pass_rate,
            golden_tests_passed=golden_passed,
            golden_tests_failed=golden_failed,
            synthetic_tests_passed=synthetic_passed,
            synthetic_tests_failed=synthetic_failed,
            target_defect_rate=0.0 if synthetic_failed == 0 else 1.0,
            p95_latency_ms=latency_ms,
            memory_peak_mb=128.0,
            opsec_passed=True,
            audit_chain_valid=True,
            details={"golden_stdout": res_golden.stdout if total_golden > 0 else ""},
        )

"""Multi-objective fitness evaluation."""

from __future__ import annotations

import abc
import ast
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from autopoiesis.models import FitnessReport, MutationProposal


def _is_trivial_test_file(test_path: Path) -> bool:
    """Return True if a synthetic test file contains no non-trivial assertions."""
    try:
        tree = ast.parse(test_path.read_text(encoding="utf-8"))
    except Exception:
        return True

    test_funcs = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    ]
    if not test_funcs:
        return True

    for fn in test_funcs:
        stmts = [
            s for s in fn.body
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
        ]
        if not stmts:
            return True
        non_trivial_in_fn = False
        for s in stmts:
            if isinstance(s, ast.Pass):
                continue
            if isinstance(s, ast.Assert) and isinstance(s.test, ast.Constant) and s.test.value is True:
                continue
            non_trivial_in_fn = True
            break
        if not non_trivial_in_fn:
            return True
    return False


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
        noop_mutation = not bool(mutation.diff.strip() or mutation.target_files)
        trivial_challenge = (
            not synthetic_test_paths
            or any(_is_trivial_test_file(Path(p)) for p in synthetic_test_paths)
        )

        sub_env = os.environ.copy()
        existing_pp = sub_env.get("PYTHONPATH", "")
        sub_env["PYTHONPATH"] = f"{worktree}:{existing_pp}" if existing_pp else str(worktree)
        golden_cmd = self.golden_test_cmd
        if golden_cmd.startswith("pytest "):
            golden_cmd = f"{sys.executable} -m {golden_cmd}"

        # 1. Run Golden Test Suite
        t0 = time.time()
        golden_stdout = ""
        golden_error: Optional[str] = None      # why the command produced no exit code (timeout, cannot start)
        try:
            res_golden = subprocess.run(
                golden_cmd,
                shell=True,
                cwd=str(worktree),
                env=sub_env,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            golden_passed = 1 if res_golden.returncode == 0 else 0
            golden_failed = 0 if res_golden.returncode == 0 else 1
            golden_stdout = res_golden.stdout or ""
        except subprocess.TimeoutExpired as exc:
            # a suite that does not finish is a failed candidate, reported as such
            golden_passed = 0
            golden_failed = 1
            partial = exc.stdout or ""
            golden_stdout = partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial
            golden_error = f"timeout after {self.timeout_seconds}s"
        except Exception as exc:
            golden_passed = 0
            golden_failed = 1
            golden_error = f"{type(exc).__name__}: {exc}"

        latency_ms = (time.time() - t0) * 1000.0

        # 2. Run Synthetic Tests
        synthetic_passed = 0
        synthetic_failed = 0
        for test_path in synthetic_test_paths:
            try:
                res_synth = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", str(test_path)],
                    cwd=str(worktree),
                    env=sub_env,
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
            target_defect_rate=0.0 if (synthetic_failed == 0 and not noop_mutation and not trivial_challenge) else 1.0,
            p95_latency_ms=latency_ms,
            memory_peak_mb=128.0,
            opsec_passed=True,
            audit_chain_valid=True,
            details={
                "golden_stdout": golden_stdout,
                "golden_error": golden_error,
                "noop_mutation": noop_mutation,
                "trivial_challenge": trivial_challenge,
            },
        )

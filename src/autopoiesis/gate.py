"""Pareto dominance evaluation and regression prevention."""

from __future__ import annotations

from typing import List, Optional

from autopoiesis.models import FitnessReport, ParetoVerdict


class ParetoGate:
    """Evaluates candidate fitness against parent baseline using strict Pareto dominance."""

    def __init__(
        self,
        max_allowed_latency_degradation_pct: float = 2.0,
        prompt_token_dividend_per_rule: int = 35,
        min_mutation_kill_score: float = 0.0,
    ):
        self.max_allowed_latency_degradation_pct = max_allowed_latency_degradation_pct
        self.prompt_token_dividend_per_rule = prompt_token_dividend_per_rule
        self.min_mutation_kill_score = min_mutation_kill_score

    def evaluate_candidate(
        self,
        candidate_report: FitnessReport,
        baseline_report: Optional[FitnessReport] = None,
    ) -> ParetoVerdict:
        rejections: List[str] = []

        # 0. Non-Triviality & Non-Empty Mutation Guard
        noop_mutation = bool(candidate_report.details.get("noop_mutation", False))
        trivial_challenge = bool(candidate_report.details.get("trivial_challenge", False))
        if noop_mutation:
            rejections.append("No-op mutation: candidate diff and target_files are empty.")
        if trivial_challenge:
            rejections.append("Trivial curriculum: synthetic challenge test contains only constant assert True.")

        # 1. Zero Regression Check (Golden Suite 100% Pass)
        zero_regression = candidate_report.golden_tests_failed == 0 and candidate_report.test_pass_rate == 1.0
        if not zero_regression:
            rejections.append(
                f"Regression detected: {candidate_report.golden_tests_failed} golden tests failed."
            )

        # 2. Defect Remediation Check
        defect_eliminated = (
            not noop_mutation
            and not trivial_challenge
            and candidate_report.synthetic_tests_failed == 0
            and candidate_report.target_defect_rate == 0.0
        )
        if not defect_eliminated and not noop_mutation and not trivial_challenge:
            rejections.append(
                f"Defect not resolved: {candidate_report.synthetic_tests_failed} synthetic challenge tests failed."
            )

        # 3. Performance Envelope Check
        perf_acceptable = True
        if baseline_report and baseline_report.p95_latency_ms > 0:
            pct_change = (
                (candidate_report.p95_latency_ms - baseline_report.p95_latency_ms)
                / baseline_report.p95_latency_ms
            ) * 100.0
            if pct_change > self.max_allowed_latency_degradation_pct:
                perf_acceptable = False
                rejections.append(
                    f"Performance degradation: p95 latency increased by {pct_change:.2f}% (max allowed: {self.max_allowed_latency_degradation_pct}%)."
                )

        # 4. Security & Cryptographic Integrity Check
        security_verified = candidate_report.opsec_passed and candidate_report.audit_chain_valid
        if not security_verified:
            rejections.append("Security failure: OpSec violation or audit chain discontinuity.")

        # 5. Differential Mutation Testing Check (Causal Minimality)
        mutation_testing_passed = True
        if candidate_report.mutation_kill_score is not None:
            if candidate_report.mutation_kill_score < self.min_mutation_kill_score:
                mutation_testing_passed = False
                rejections.append(
                    f"Mutation Kill Score failure: {candidate_report.mutation_kill_score:.2f} < {self.min_mutation_kill_score:.2f} (patch delta contains surviving mutants / dead code)."
                )

        is_dominant = (
            zero_regression and defect_eliminated and perf_acceptable and security_verified and mutation_testing_passed
        )

        reclaimed_tokens = self.prompt_token_dividend_per_rule if is_dominant else 0

        return ParetoVerdict(
            candidate_id=candidate_report.candidate_id,
            is_pareto_dominant=is_dominant,
            zero_regression=zero_regression,
            defect_eliminated=defect_eliminated,
            performance_acceptable=perf_acceptable,
            security_verified=security_verified,
            rejection_reasons=rejections,
            reclaimed_prompt_tokens=reclaimed_tokens,
        )

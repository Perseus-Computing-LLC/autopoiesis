"""Autopoiesis Closed-Loop Engine."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from autopoiesis.config import AutopoiesisConfig
from autopoiesis.curriculum import CurriculumGenerator, RedFirstCurriculumGenerator
from autopoiesis.deployment import DeploymentManager, GitPromotionManager, PromotionError
from autopoiesis.evaluator import FitnessEvaluator, SubprocessFitnessEvaluator
from autopoiesis.gate import ParetoGate
from autopoiesis.models import FailureCluster, MutationProposal, ParetoVerdict, PromotionReceipt
from autopoiesis.mutation import MutationEngine, PatchApplicatorMutationEngine, WorktreeManager
from autopoiesis.telemetry import ArgusTelemetrySource, ClusterEngine, JsonlTelemetrySource, TelemetrySource

logger = logging.getLogger("autopoiesis")


class AutopoiesisEngine:
    """Orchestrates the 5-stage closed-loop recursive self-evolution cycle."""

    def __init__(
        self,
        telemetry_source: TelemetrySource,
        cluster_engine: ClusterEngine,
        worktree_manager: WorktreeManager,
        mutation_engine: MutationEngine,
        curriculum_generator: CurriculumGenerator,
        fitness_evaluator: FitnessEvaluator,
        pareto_gate: ParetoGate,
        deployment_manager: DeploymentManager,
        enforce_red_first: bool = False,
        auto_merge: bool = True,
    ):
        self.telemetry_source = telemetry_source
        self.cluster_engine = cluster_engine
        self.worktree_manager = worktree_manager
        self.mutation_engine = mutation_engine
        self.curriculum_generator = curriculum_generator
        self.fitness_evaluator = fitness_evaluator
        self.pareto_gate = pareto_gate
        self.deployment_manager = deployment_manager
        self.enforce_red_first = enforce_red_first
        # False: evaluate and report, never call the deployment manager (config: deployment.auto_merge)
        self.auto_merge = auto_merge

    @classmethod
    def from_config(
        cls,
        config: AutopoiesisConfig,
        mutation_engine: Optional[MutationEngine] = None,
        curriculum_generator: Optional[CurriculumGenerator] = None,
    ) -> AutopoiesisEngine:
        """Construct a configured engine from an AutopoiesisConfig object."""
        repo_path = Path(config.project.repo_path).resolve()
        sandbox_base = repo_path / config.mutation.sandbox_dir

        if config.telemetry.adapter == "argus":
            telemetry_source: TelemetrySource = ArgusTelemetrySource(Path(config.telemetry.path))
        else:
            telemetry_source = JsonlTelemetrySource(Path(config.telemetry.path))

        cluster_engine = ClusterEngine()
        worktree_manager = WorktreeManager(repo_path, sandbox_base)

        if mutation_engine is None:
            mutation_engine = PatchApplicatorMutationEngine(repo_name=config.project.name)

        if curriculum_generator is None:
            curriculum_generator = RedFirstCurriculumGenerator()

        fitness_evaluator = SubprocessFitnessEvaluator(
            golden_test_cmd=config.evaluation.test_command
        )
        pareto_gate = ParetoGate(
            max_allowed_latency_degradation_pct=config.evaluation.max_latency_degradation_pct,
            prompt_token_dividend_per_rule=config.evaluation.prompt_token_dividend,
        )
        deployment_manager = GitPromotionManager(
            repo_path=repo_path,
            release_branch=config.deployment.branch,
        )

        return cls(
            telemetry_source=telemetry_source,
            cluster_engine=cluster_engine,
            worktree_manager=worktree_manager,
            mutation_engine=mutation_engine,
            curriculum_generator=curriculum_generator,
            fitness_evaluator=fitness_evaluator,
            pareto_gate=pareto_gate,
            deployment_manager=deployment_manager,
            enforce_red_first=config.evaluation.enforce_red_first,
            auto_merge=config.deployment.auto_merge,
        )

    def run_cycle(self, dry_run: bool = False) -> Dict[str, Any]:
        """Execute a single end-to-end evolutionary cycle."""
        report: Dict[str, Any] = {
            "status": "idle",
            "clusters_discovered": 0,
            "selected_cluster": None,
            "mutation_proposed": None,
            "verdict": None,
            "promoted": False,
            "reclaimed_tokens": 0,
        }

        # 1. SENSE: Read telemetry & cluster failures
        events = list(self.telemetry_source.iter_events())
        clusters = self.cluster_engine.cluster_events(events)
        report["clusters_discovered"] = len(clusters)

        if not clusters:
            report["status"] = "no_clusters"
            return report

        target_cluster = clusters[0]
        report["selected_cluster"] = {
            "cluster_id": target_cluster.cluster_id,
            "tool": target_cluster.tool,
            "occurrences": target_cluster.occurrences,
            "template": target_cluster.template,
            "is_transient": target_cluster.is_transient,
        }

        # 2. CREATE SANDBOX
        sandbox_path: Optional[Path] = None
        branch_name = f"autopoiesis-eval-{target_cluster.cluster_id}"
        try:
            sandbox_path = self.worktree_manager.create_sandbox(branch_name)

            # 3. CURRICULUM: Synthesize challenge tests
            synthetic_tests = self.curriculum_generator.generate_curriculum(
                target_cluster, None, sandbox_path
            )

            # 3b. RED-FIRST VERIFICATION: Ensure challenge test fails on unpatched HEAD
            if self.enforce_red_first:
                is_red = self.curriculum_generator.verify_red_on_head(
                    synthetic_tests, sandbox_path
                )
                if not is_red:
                    report["status"] = "challenge_not_red_on_head"
                    report["verdict"] = {
                        "is_pareto_dominant": False,
                        "zero_regression": True,
                        "defect_eliminated": False,
                        "rejection_reasons": [
                            "Challenge test passed on unpatched HEAD (violates RED-first invariant)."
                        ],
                        "reclaimed_tokens": 0,
                    }
                    return report

            # 4. MUTATE: Apply proposed mutation
            proposal = self.mutation_engine.propose_mutation(target_cluster, sandbox_path)
            if not proposal:
                report["status"] = "mutation_failed"
                return report

            report["mutation_proposed"] = proposal.proposal_id

            # 5. EVALUATE: Multi-objective Pareto evaluation
            fitness = self.fitness_evaluator.evaluate(proposal, synthetic_tests)
            verdict = self.pareto_gate.evaluate_candidate(fitness)
            report["verdict"] = {
                "is_pareto_dominant": verdict.is_pareto_dominant,
                "zero_regression": verdict.zero_regression,
                "defect_eliminated": verdict.defect_eliminated,
                "rejection_reasons": verdict.rejection_reasons,
                "reclaimed_tokens": verdict.reclaimed_prompt_tokens,
            }

            # 6. PROMOTE & DISTILL: If Pareto-dominant, merge and distill prompt rules
            if verdict.is_pareto_dominant and not dry_run and not self.auto_merge:
                # the operator's configuration says: do not merge. The candidate is reported, not promoted.
                report["status"] = "dominant_not_merged"
                report["promotion_skipped"] = "deployment.auto_merge is false"
            elif verdict.is_pareto_dominant and not dry_run:
                try:
                    receipt = self.deployment_manager.promote_mutation(proposal, verdict)
                except PromotionError as exc:
                    report["status"] = "promotion_failed"
                    report["promotion_error"] = str(exc)
                    return report
                report["status"] = "promoted"
                report["promoted"] = True
                report["receipt"] = {
                    "release_id": receipt.release_id,
                    "commit_sha": receipt.commit_sha,
                    "distilled_rules": receipt.distilled_prompt_invariants,
                }
                report["reclaimed_tokens"] = verdict.reclaimed_prompt_tokens
            else:
                report["status"] = "evaluated_not_promoted" if not dry_run else "dry_run_evaluated"

            return report

        finally:
            if sandbox_path and sandbox_path.exists():
                self.worktree_manager.remove_sandbox(sandbox_path, branch_name=branch_name)

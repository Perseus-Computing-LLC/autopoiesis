"""Autopoiesis Closed-Loop Engine."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from autopoiesis.curriculum import CurriculumGenerator
from autopoiesis.deployment import DeploymentManager
from autopoiesis.evaluator import FitnessEvaluator
from autopoiesis.gate import ParetoGate
from autopoiesis.models import FailureCluster, MutationProposal, ParetoVerdict, PromotionReceipt
from autopoiesis.mutation import MutationEngine, WorktreeManager
from autopoiesis.telemetry import ClusterEngine, TelemetrySource

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
    ):
        self.telemetry_source = telemetry_source
        self.cluster_engine = cluster_engine
        self.worktree_manager = worktree_manager
        self.mutation_engine = mutation_engine
        self.curriculum_generator = curriculum_generator
        self.fitness_evaluator = fitness_evaluator
        self.pareto_gate = pareto_gate
        self.deployment_manager = deployment_manager

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
        }

        # 2. MUTATE: Create sandbox worktree & synthesize mutation
        sandbox_path: Optional[Path] = None
        branch_name = f"autopoiesis-eval-{target_cluster.cluster_id}"
        try:
            sandbox_path = self.worktree_manager.create_sandbox(branch_name)
            proposal = self.mutation_engine.propose_mutation(target_cluster, sandbox_path)
            if not proposal:
                report["status"] = "mutation_failed"
                return report

            report["mutation_proposed"] = proposal.proposal_id

            # 3. CURRICULUM: Synthesize challenge tests
            synthetic_tests = self.curriculum_generator.generate_curriculum(
                target_cluster, proposal, sandbox_path
            )

            # 4. EVALUATE: Multi-objective Pareto evaluation
            fitness = self.fitness_evaluator.evaluate(proposal, synthetic_tests)
            verdict = self.pareto_gate.evaluate_candidate(fitness)
            report["verdict"] = {
                "is_pareto_dominant": verdict.is_pareto_dominant,
                "zero_regression": verdict.zero_regression,
                "defect_eliminated": verdict.defect_eliminated,
                "rejection_reasons": verdict.rejection_reasons,
                "reclaimed_tokens": verdict.reclaimed_prompt_tokens,
            }

            # 5. PROMOTE & DISTILL: If Pareto-dominant, merge and distill prompt rules
            if verdict.is_pareto_dominant and not dry_run:
                receipt = self.deployment_manager.promote_mutation(proposal, verdict)
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

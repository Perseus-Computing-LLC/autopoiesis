"""Self-hosting promotion and prompt distillation."""

from __future__ import annotations

import abc
import datetime
import subprocess
from pathlib import Path
from typing import List

from autopoiesis.models import MutationProposal, ParetoVerdict, PromotionReceipt


class DeploymentManager(abc.ABC):
    """Abstract interface for promoting Pareto-dominant mutations into production."""

    @abc.abstractmethod
    def promote_mutation(
        self, mutation: MutationProposal, verdict: ParetoVerdict
    ) -> PromotionReceipt:
        pass


class GitPromotionManager(DeploymentManager):
    """Promotes mutations via git merge, releases, and prompt rule distillation."""

    def __init__(self, repo_path: Path, release_branch: str = "main"):
        self.repo_path = Path(repo_path).resolve()
        self.release_branch = release_branch

    def promote_mutation(
        self, mutation: MutationProposal, verdict: ParetoVerdict
    ) -> PromotionReceipt:
        if not verdict.is_pareto_dominant:
            raise ValueError(f"Cannot promote non-dominant candidate {mutation.proposal_id}")

        worktree = Path(mutation.worktree_path)

        # 1. Commit in worktree
        subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True)
        commit_msg = (
            f"chore(autopoiesis): autogenous promotion of {mutation.proposal_id}\n\n"
            f"Remediates failure cluster {mutation.cluster_id}.\n"
            f"Pareto verdict: zero_regression={verdict.zero_regression}, "
            f"defect_eliminated={verdict.defect_eliminated}."
        )
        subprocess.run(["git", "-C", str(worktree), "commit", "-m", commit_msg], check=True)

        rev_res = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True
        )
        commit_sha = rev_res.stdout.strip()

        # 2. Merge to release branch
        subprocess.run(
            ["git", "-C", str(self.repo_path), "merge", "--ff-only", commit_sha],
            capture_output=True, text=True
        )

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        receipt = PromotionReceipt(
            release_id=f"rel_{commit_sha[:8]}",
            commit_sha=commit_sha,
            merged_at=now_iso,
            pareto_verdict=verdict,
            promoted_artifacts=mutation.target_files,
            distilled_prompt_invariants=[f"invariant.{mutation.cluster_id}"],
        )
        return receipt

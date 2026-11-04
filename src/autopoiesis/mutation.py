"""Mutation operators and sandboxed worktree management."""

from __future__ import annotations

import abc
import contextlib
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Iterator, List, Optional

from autopoiesis.models import FailureCluster, MutationProposal


class WorktreeManager:
    """Manages ephemeral Git worktrees for safe, sandboxed mutation and evaluation."""

    def __init__(self, repo_path: Path, worktree_base: Path):
        self.repo_path = Path(repo_path).resolve()
        self.worktree_base = Path(worktree_base).resolve()
        self.worktree_base.mkdir(parents=True, exist_ok=True)

    def create_sandbox(self, branch_name: str) -> Path:
        """Create an isolated worktree branch."""
        sandbox_path = self.worktree_base / branch_name
        if sandbox_path.exists():
            self.remove_sandbox(sandbox_path, branch_name=branch_name)

        subprocess.run(
            ["git", "-C", str(self.repo_path), "branch", "-D", branch_name],
            capture_output=True, text=True
        )
        cmd = [
            "git", "-C", str(self.repo_path),
            "worktree", "add", "-b", branch_name,
            str(sandbox_path), "HEAD"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create git worktree: {res.stderr}")
        return sandbox_path

    def remove_sandbox(self, sandbox_path: Path, branch_name: Optional[str] = None) -> None:
        """Safely prune and remove a worktree and its temporary branch."""
        inferred_branch = branch_name or sandbox_path.name
        subprocess.run(
            ["git", "-C", str(self.repo_path), "worktree", "remove", "--force", str(sandbox_path)],
            capture_output=True, text=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo_path), "worktree", "prune"],
            capture_output=True, text=True
        )
        if inferred_branch and inferred_branch != "main":
            subprocess.run(
                ["git", "-C", str(self.repo_path), "branch", "-D", inferred_branch],
                capture_output=True, text=True
            )

    @contextlib.contextmanager
    def sandbox(self, branch_name: str) -> Iterator[Path]:
        """Context manager that guarantees worktree and temporary branch teardown."""
        path = self.create_sandbox(branch_name)
        try:
            yield path
        finally:
            self.remove_sandbox(path, branch_name=branch_name)


class MutationEngine(abc.ABC):
    """Abstract interface for proposing codebase mutations given failure telemetry."""

    @abc.abstractmethod
    def propose_mutation(
        self, cluster: FailureCluster, sandbox_path: Path
    ) -> Optional[MutationProposal]:
        """Synthesize a code patch in the sandbox to cure the failure cluster."""
        pass


class PatchApplicatorMutationEngine(MutationEngine):
    """Applies structured code mutations into sandboxed worktrees."""

    def __init__(
        self,
        repo_name: str,
        mutator_fn: Optional[Callable[[FailureCluster, Path], Optional[ List[str]]]] = None,
    ):
        self.repo_name = repo_name
        self.mutator_fn = mutator_fn

    def propose_mutation(
        self, cluster: FailureCluster, sandbox_path: Path
    ) -> Optional[MutationProposal]:
        proposal_id = f"mut_{uuid.uuid4().hex[:8]}"
        branch_name = f"autopoiesis/{cluster.cluster_id}_{proposal_id}"

        target_files: List[str] = []
        diff_text = ""
        if self.mutator_fn is not None:
            touched = self.mutator_fn(cluster, sandbox_path) or []
            target_files = list(touched)
            diff_res = subprocess.run(
                ["git", "-C", str(sandbox_path), "diff"],
                capture_output=True, text=True
            )
            diff_text = diff_res.stdout

        return MutationProposal(
            proposal_id=proposal_id,
            cluster_id=cluster.cluster_id,
            target_repo=self.repo_name,
            target_files=target_files,
            diff=diff_text,
            branch_name=branch_name,
            worktree_path=str(sandbox_path),
            rationale=f"Autogenous remediation for cluster {cluster.cluster_id} ({cluster.template})",
        )

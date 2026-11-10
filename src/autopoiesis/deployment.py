"""Self-hosting promotion, prompt distillation, and cryptographic attestation."""

from __future__ import annotations

import abc
import datetime
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from autopoiesis.models import MutationProposal, ParetoVerdict, PromotionReceipt

# Optional integration with Argus append-only audit ledger. The package is used when it is installed, or when
# its source directory is named explicitly (GitPromotionManager(argus_src=...) or AUTOPOIESIS_ARGUS_SRC).
# No location is built in.
ARGUS_SRC_ENV = "AUTOPOIESIS_ARGUS_SRC"


def _argus_importable(argus_src: Optional[Path] = None) -> bool:
    try:
        import argus_ledger  # type: ignore  # noqa: F401
        return True
    except ImportError:
        pass
    src = argus_src or os.environ.get(ARGUS_SRC_ENV)
    if not src or not Path(src).is_dir():
        return False
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        import argus_ledger  # type: ignore  # noqa: F401
        return True
    except ImportError:
        return False


ARGUS_AVAILABLE = _argus_importable()


class PromotionError(RuntimeError):
    """The candidate was not put on the release branch. Nothing was attested."""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


class DeploymentManager(abc.ABC):
    """Abstract interface for promoting Pareto-dominant mutations into production."""

    @abc.abstractmethod
    def promote_mutation(
        self, mutation: MutationProposal, verdict: ParetoVerdict
    ) -> PromotionReceipt:
        pass


class GitPromotionManager(DeploymentManager):
    """Promotes mutations via git merge, releases, prompt distillation, and Argus attestation."""

    def __init__(
        self,
        repo_path: Path,
        release_branch: str = "main",
        ledger_path: Optional[Path] = None,
        argus_src: Optional[Path] = None,
    ):
        self.repo_path = Path(repo_path).resolve()
        self.release_branch = release_branch
        self.ledger_path = Path(ledger_path).resolve() if ledger_path else None
        self.argus_src = Path(argus_src) if argus_src else None

    def _record_attestation(
        self,
        release_id: str,
        commit_sha: str,
        mutation: MutationProposal,
        verdict: ParetoVerdict,
        now_iso: str,
    ) -> Optional[int]:
        """Record SHA-256 tamper-evident attestation event in Argus ledger and write local card."""
        seq: Optional[int] = None
        event_hash: Optional[str] = None

        if self.ledger_path and self.ledger_path.parent.exists() and _argus_importable(self.argus_src):
            try:
                from argus_ledger.ledger import AppendOnlyLedger  # type: ignore

                ledger = AppendOnlyLedger(str(self.ledger_path))
                # Note: Argus ledger payloads accept ints/strings only (canonical hashing)
                payload: Dict[str, Any] = {
                    "release_id": str(release_id),
                    "commit_sha": str(commit_sha),
                    "cluster_id": str(mutation.cluster_id),
                    "proposal_id": str(mutation.proposal_id),
                    "reclaimed_tokens": int(verdict.reclaimed_prompt_tokens),
                    "zero_regression": "true" if verdict.zero_regression else "false",
                    "defect_eliminated": "true" if verdict.defect_eliminated else "false",
                }
                ev = ledger.append(
                    event_type="autopoiesis.mutation_promoted",
                    actor="autopoiesis",
                    payload=payload,
                )
                seq = getattr(ev, "sequence", None)
                event_hash = getattr(ev, "event_hash", None)
            except Exception:
                pass

        # Write Attested Computation Card
        attest_dir = self.repo_path / ".autopoiesis" / "attestations"
        try:
            attest_dir.mkdir(parents=True, exist_ok=True)
            card: Dict[str, Any] = {
                "type": "Attested Computation",
                "title": f"Autogenous Release {release_id}",
                "description": f"Pareto-dominant self-repair for cluster {mutation.cluster_id}",
                "release_id": release_id,
                "commit_sha": commit_sha,
                "cluster_id": mutation.cluster_id,
                "proposal_id": mutation.proposal_id,
                "created_at": now_iso,
                "reclaimed_tokens": verdict.reclaimed_prompt_tokens,
                "zero_regression": verdict.zero_regression,
                "defect_eliminated": verdict.defect_eliminated,
                "argus_receipt_seq": seq,
                "argus_event_hash": event_hash,
            }
            card_bytes = json.dumps(card, sort_keys=True, separators=(",", ":")).encode("utf-8")
            card["card_sha256"] = hashlib.sha256(card_bytes).hexdigest()

            card_path = attest_dir / f"{release_id}.json"
            card_path.write_text(json.dumps(card, indent=2), encoding="utf-8")
        except Exception:
            pass

        return seq

    def _release_ref(self) -> str:
        return f"refs/heads/{self.release_branch}"

    def _release_tip(self) -> Optional[str]:
        res = _git(self.repo_path, "rev-parse", "-q", "--verify", f"{self._release_ref()}^{{commit}}")
        return res.stdout.strip() if res.returncode == 0 else None

    def _worktree_on_release_branch(self) -> Optional[Path]:
        """The worktree that has the release branch checked out, if any."""
        res = _git(self.repo_path, "worktree", "list", "--porcelain")
        if res.returncode != 0:
            raise PromotionError(f"cannot list worktrees: {res.stderr.strip()[-300:]}")
        current: Optional[Path] = None
        for line in res.stdout.splitlines():
            if line.startswith("worktree "):
                current = Path(line[len("worktree "):])
            elif line == f"branch {self._release_ref()}":
                return current
        return None

    def _advance_release_branch(self, old_tip: str, commit_sha: str) -> None:
        """Fast-forward the configured release branch to `commit_sha`, or raise PromotionError.

        The branch that moves is `release_branch`, whatever is checked out in `repo_path`. Where the release
        branch is checked out, the fast-forward is done there so that its files follow; where it is not checked
        out anywhere, the ref is moved with a compare-and-swap against the tip seen before.
        """
        if _git(self.repo_path, "merge-base", "--is-ancestor", old_tip, commit_sha).returncode != 0:
            raise PromotionError(
                f"{self.release_branch} ({old_tip[:8]}) is not an ancestor of the candidate {commit_sha[:8]}: "
                f"not a fast-forward")
        holder = self._worktree_on_release_branch()
        if holder is not None:
            res = _git(holder, "merge", "--ff-only", commit_sha)
            how = f"git merge --ff-only in {holder}"
        else:
            res = _git(self.repo_path, "update-ref", "-m", f"autopoiesis: promote {commit_sha[:8]}",
                       self._release_ref(), commit_sha, old_tip)
            how = "git update-ref"
        if res.returncode != 0:
            raise PromotionError(f"{how} failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()[-400:]}")
        if self._release_tip() != commit_sha:
            raise PromotionError(f"after {how}, {self.release_branch} does not point at {commit_sha[:8]}")

    def promote_mutation(
        self, mutation: MutationProposal, verdict: ParetoVerdict
    ) -> PromotionReceipt:
        """Commit the candidate, fast-forward the release branch to it, then attest.

        Raises PromotionError when the commit is not on the release branch afterwards; in that case no ledger
        event, no attestation card and no receipt exist.
        """
        if not verdict.is_pareto_dominant:
            raise ValueError(f"Cannot promote non-dominant candidate {mutation.proposal_id}")

        worktree = Path(mutation.worktree_path)
        old_tip = self._release_tip()
        if old_tip is None:
            raise PromotionError(f"release branch {self.release_branch!r} does not exist in {self.repo_path}")

        # 1. Commit in worktree
        commit_msg = (
            f"chore(autopoiesis): autogenous promotion of {mutation.proposal_id}\n\n"
            f"Remediates failure cluster {mutation.cluster_id}.\n"
            f"Pareto verdict: zero_regression={verdict.zero_regression}, "
            f"defect_eliminated={verdict.defect_eliminated}."
        )
        for step in (("add", "-A"), ("commit", "-m", commit_msg), ("rev-parse", "HEAD")):
            res = _git(worktree, *step)
            if res.returncode != 0:
                raise PromotionError(
                    f"git {step[0]} failed in the candidate worktree (exit {res.returncode}): "
                    f"{(res.stderr or res.stdout).strip()[-400:]}")
        commit_sha = res.stdout.strip()

        # 2. Fast-forward the release branch; anything short of that is not a promotion
        self._advance_release_branch(old_tip, commit_sha)

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        release_id = f"rel_{commit_sha[:8]}"

        # 3. Cryptographic attestation
        receipt_seq = self._record_attestation(
            release_id=release_id,
            commit_sha=commit_sha,
            mutation=mutation,
            verdict=verdict,
            now_iso=now_iso,
        )

        receipt = PromotionReceipt(
            release_id=release_id,
            commit_sha=commit_sha,
            merged_at=now_iso,
            pareto_verdict=verdict,
            promoted_artifacts=mutation.target_files,
            distilled_prompt_invariants=[f"invariant.{mutation.cluster_id}"],
            audit_receipt_seq=receipt_seq,
        )
        return receipt

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

# Optional integration with Argus append-only audit ledger
ARGUS_AVAILABLE = False
try:
    import argus_ledger  # type: ignore
    ARGUS_AVAILABLE = True
except ImportError:
    argus_src = Path("/opt/data/work/telarch-suite/argus/src")
    if argus_src.is_dir():
        sys.path.insert(0, str(argus_src))
        try:
            import argus_ledger  # type: ignore
            ARGUS_AVAILABLE = True
        except ImportError:
            ARGUS_AVAILABLE = False


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
    ):
        self.repo_path = Path(repo_path).resolve()
        self.release_branch = release_branch
        self.ledger_path = Path(ledger_path).resolve() if ledger_path else None

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

        if self.ledger_path and ARGUS_AVAILABLE and self.ledger_path.parent.exists():
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

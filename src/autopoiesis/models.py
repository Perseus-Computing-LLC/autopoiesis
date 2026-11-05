"""Data models for Autopoiesis: Recursive Self-Evolution Engine."""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any, Dict, List, Optional


@dataclasses.dataclass(frozen=True)
class TelemetryEvent:
    """A single execution trace event from production telemetry."""
    sequence: int
    timestamp: str
    tool: str
    status: str  # "ok", "error"
    error_message: Optional[str] = None
    exit_code: Optional[int] = None
    latency_ms: Optional[float] = None
    metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class FailureCluster:
    """A cluster of recurring failures or latency spikes."""
    cluster_id: str
    tool: str
    failure_class: str
    template: str
    occurrences: int
    recent_24h_occurrences: int
    sample_events: List[int] = dataclasses.field(default_factory=list)
    baseline_rate: float = 0.0
    is_transient: bool = False
    transient_retry_rate: float = 0.0


@dataclasses.dataclass
class MutationProposal:
    """A proposed code modification to resolve a cluster or optimize performance."""
    proposal_id: str
    cluster_id: str
    target_repo: str
    target_files: List[str]
    diff: str
    branch_name: str
    worktree_path: str
    rationale: str
    generated_at: str = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )


@dataclasses.dataclass
class FitnessReport:
    """Multi-dimensional evaluation results for a candidate mutation."""
    candidate_id: str
    test_pass_rate: float            # 1.0 = 100% tests passed
    golden_tests_passed: int
    golden_tests_failed: int
    synthetic_tests_passed: int
    synthetic_tests_failed: int
    target_defect_rate: float         # Post-mutation rate on target cluster
    p95_latency_ms: float
    memory_peak_mb: float
    opsec_passed: bool
    audit_chain_valid: bool
    mutation_kill_score: Optional[float] = None
    details: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ParetoVerdict:
    """Verdict comparing candidate mutation against parent baseline."""
    candidate_id: str
    is_pareto_dominant: bool
    zero_regression: bool
    defect_eliminated: bool
    performance_acceptable: bool
    security_verified: bool
    rejection_reasons: List[str] = dataclasses.field(default_factory=list)
    reclaimed_prompt_tokens: int = 0


@dataclasses.dataclass
class PromotionReceipt:
    """Immutable record of an autogenous software release promotion."""
    release_id: str
    commit_sha: str
    merged_at: str
    pareto_verdict: ParetoVerdict
    promoted_artifacts: List[str]
    distilled_prompt_invariants: List[str]
    audit_receipt_seq: Optional[int] = None

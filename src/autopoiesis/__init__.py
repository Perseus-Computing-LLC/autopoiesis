"""Autopoiesis: Recursive Self-Evolution Engine for Software Suites."""

__version__ = "0.1.0"

from autopoiesis.curriculum import CurriculumGenerator, FileBasedCurriculumGenerator
from autopoiesis.deployment import DeploymentManager, GitPromotionManager
from autopoiesis.engine import AutopoiesisEngine
from autopoiesis.evaluator import FitnessEvaluator, SubprocessFitnessEvaluator
from autopoiesis.gate import ParetoGate
from autopoiesis.models import (
    FailureCluster,
    FitnessReport,
    MutationProposal,
    ParetoVerdict,
    PromotionReceipt,
    TelemetryEvent,
)
from autopoiesis.mutation import MutationEngine, PatchApplicatorMutationEngine, WorktreeManager
from autopoiesis.telemetry import (
    ArgusTelemetrySource,
    ClusterEngine,
    JsonlTelemetrySource,
    TelemetrySource,
)

__all__ = [
    "__version__",
    "AutopoiesisEngine",
    "TelemetryEvent",
    "FailureCluster",
    "MutationProposal",
    "FitnessReport",
    "ParetoVerdict",
    "PromotionReceipt",
    "TelemetrySource",
    "JsonlTelemetrySource",
    "ArgusTelemetrySource",
    "ClusterEngine",
    "WorktreeManager",
    "MutationEngine",
    "PatchApplicatorMutationEngine",
    "CurriculumGenerator",
    "FileBasedCurriculumGenerator",
    "FitnessEvaluator",
    "SubprocessFitnessEvaluator",
    "ParetoGate",
    "DeploymentManager",
    "GitPromotionManager",
]

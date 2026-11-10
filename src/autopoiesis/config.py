"""Configuration models and loader for Autopoiesis."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


@dataclasses.dataclass
class ProjectConfig:
    name: str = "software-suite"
    repo_path: str = "."


@dataclasses.dataclass
class TelemetryConfig:
    adapter: str = "jsonl"  # "jsonl" or "argus"
    path: str = "telemetry.jsonl"
    lookback_days: int = 7


@dataclasses.dataclass
class MutationConfig:
    sandbox_dir: str = ".autopoiesis/sandboxes"


@dataclasses.dataclass
class EvaluationConfig:
    test_command: str = "pytest -q"
    max_latency_degradation_pct: float = 2.0
    prompt_token_dividend: int = 35
    enforce_red_first: bool = True


def _flag(value: Any, key: str) -> bool:
    """A boolean setting. `bool("false")` is True, so strings are read by meaning and anything else is refused."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "on", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0"):
        return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"{key} must be true or false, got {value!r}")


@dataclasses.dataclass
class DeploymentConfig:
    branch: str = "main"
    auto_merge: bool = True


@dataclasses.dataclass
class AutopoiesisConfig:
    """Root configuration for an Autopoiesis self-evolution loop."""
    version: int = 1
    project: ProjectConfig = dataclasses.field(default_factory=ProjectConfig)
    telemetry: TelemetryConfig = dataclasses.field(default_factory=TelemetryConfig)
    mutation: MutationConfig = dataclasses.field(default_factory=MutationConfig)
    evaluation: EvaluationConfig = dataclasses.field(default_factory=EvaluationConfig)
    deployment: DeploymentConfig = dataclasses.field(default_factory=DeploymentConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AutopoiesisConfig:
        proj_data = data.get("project", {})
        telem_data = data.get("telemetry", {})
        mut_data = data.get("mutation", {})
        eval_data = data.get("evaluation", {})
        deploy_data = data.get("deployment", {})

        return cls(
            version=int(data.get("version", 1)),
            project=ProjectConfig(
                name=proj_data.get("name", "software-suite"),
                repo_path=proj_data.get("repo_path", "."),
            ),
            telemetry=TelemetryConfig(
                adapter=telem_data.get("adapter", "jsonl"),
                path=telem_data.get("path", "telemetry.jsonl"),
                lookback_days=int(telem_data.get("lookback_days", 7)),
            ),
            mutation=MutationConfig(
                sandbox_dir=mut_data.get("sandbox_dir", ".autopoiesis/sandboxes"),
            ),
            evaluation=EvaluationConfig(
                test_command=eval_data.get("test_command", "pytest -q"),
                max_latency_degradation_pct=float(eval_data.get("max_latency_degradation_pct", 2.0)),
                prompt_token_dividend=int(eval_data.get("prompt_token_dividend", 35)),
                enforce_red_first=_flag(eval_data.get("enforce_red_first", True), "evaluation.enforce_red_first"),
            ),
            deployment=DeploymentConfig(
                branch=deploy_data.get("branch", "main"),
                auto_merge=_flag(deploy_data.get("auto_merge", True), "deployment.auto_merge"),
            ),
        )


def load_config(config_path: Union[str, Path] = "autopoiesis.yaml") -> AutopoiesisConfig:
    """Load configuration from a YAML or JSON file."""
    path = Path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    content = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        if not HAS_YAML:
            raise ImportError("PyYAML is required to parse YAML config files.")
        data = yaml.safe_load(content) or {}
    elif path.suffix == ".json":
        data = json.loads(content) or {}
    else:
        # Try YAML first, then JSON
        if HAS_YAML:
            data = yaml.safe_load(content) or {}
        else:
            data = json.loads(content) or {}

    return AutopoiesisConfig.from_dict(data)

"""severity: high
finding: `deployment.auto_merge: false` is parsed and then ignored: AutopoiesisEngine.from_config / run_cycle promote (commit + merge into the checked-out release branch) whenever the gate says dominant and dry_run is False

config.py:46,89 define and parse `auto_merge`; engine.py:78-81 builds GitPromotionManager without it and
engine.py:174-175 promotes on `verdict.is_pareto_dominant and not dry_run` alone. The shipped autopoiesis.yaml
sets `auto_merge: false`, and `autopoiesis cycle` (cli.py:167-168) without `--dry-run` therefore merges.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autopoiesis.config import AutopoiesisConfig
from autopoiesis.curriculum import CurriculumGenerator
from autopoiesis.engine import AutopoiesisEngine
from autopoiesis.mutation import PatchApplicatorMutationEngine


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class Challenge(CurriculumGenerator):
    """A real challenge: red on the head, green once app.resolve_cmd exists."""

    def generate_curriculum(self, cluster, mutation, sandbox_path: Path):
        d = sandbox_path / "tests" / "autopoiesis_synthetic"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"test_cluster_{cluster.cluster_id}.py"
        f.write_text("import app\n\ndef test_remediated():\n    assert app.resolve_cmd('foo') == '/usr/bin/foo'\n")
        return [f]


def apply_fix(cluster, sandbox_path: Path):
    (sandbox_path / "app.py").write_text(
        "def run():\n    return 'hello'\n\ndef resolve_cmd(name):\n    return f'/usr/bin/{name}'\n")
    return ["app.py"]


def run_cycle(tmp_path: Path, auto_merge: bool):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    (repo / "app.py").write_text("def run():\n    return 'hello'\n")
    (repo / "test_app.py").write_text("import app\n\ndef test_app():\n    assert app.run() == 'hello'\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text(json.dumps({"sequence": 1, "tool": "terminal", "status": "error", "exit_code": 127,
                                     "error_message": "exit 127: command not found: foo"}) + "\n")
    config = AutopoiesisConfig.from_dict({
        "project": {"name": "repo", "repo_path": str(repo)},
        "telemetry": {"adapter": "jsonl", "path": str(telemetry)},
        "mutation": {"sandbox_dir": str(tmp_path / "sandboxes")},
        "evaluation": {"test_command": "pytest -q -p no:cacheprovider test_app.py", "enforce_red_first": True},
        "deployment": {"branch": "main", "auto_merge": auto_merge},
    })
    assert config.deployment.auto_merge is auto_merge
    engine = AutopoiesisEngine.from_config(
        config, mutation_engine=PatchApplicatorMutationEngine("repo", mutator_fn=apply_fix),
        curriculum_generator=Challenge())
    before = git(repo, "rev-parse", "main")
    report = engine.run_cycle(dry_run=False)
    return report, before, git(repo, "rev-parse", "main")


def test_control_with_auto_merge_true_the_cycle_promotes(tmp_path: Path):
    report, before, after = run_cycle(tmp_path, auto_merge=True)
    assert report["verdict"]["is_pareto_dominant"] is True
    assert report["promoted"] is True and after != before


def test_auto_merge_false_keeps_the_release_branch_untouched(tmp_path: Path):
    report, before, after = run_cycle(tmp_path, auto_merge=False)
    assert report["verdict"]["is_pareto_dominant"] is True          # the candidate is good; the operator said "do not merge"
    assert after == before, f"auto_merge is false, yet main moved {before[:8]} -> {after[:8]} (status {report['status']})"
    assert report["promoted"] is False

"""CLI interface for Autopoiesis."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autopoiesis.config import AutopoiesisConfig, load_config
from autopoiesis.engine import AutopoiesisEngine
from autopoiesis.telemetry import ArgusTelemetrySource, ClusterEngine, JsonlTelemetrySource, TelemetrySource


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autopoiesis",
        description="Autopoiesis: Recursive Self-Evolution Engine for Software Suites",
    )
    parser.add_argument(
        "--config", default="autopoiesis.yaml", help="Path to autopoiesis.yaml configuration file"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Init
    init_parser = subparsers.add_parser("init", help="Initialize autopoiesis configuration")
    init_parser.add_argument("--repo-path", default=".", help="Target repository path")
    init_parser.add_argument("--project-name", default="software-suite", help="Project name")

    # Cycle
    cycle_parser = subparsers.add_parser("cycle", help="Run an autogenous self-evolution cycle")
    cycle_parser.add_argument("--dry-run", action="store_true", help="Evaluate without promoting/merging")
    cycle_parser.add_argument("--telemetry", help="Override telemetry file path")
    cycle_parser.add_argument("--json", action="store_true", help="Output machine-readable JSON report")

    # Status
    status_parser = subparsers.add_parser("status", help="Inspect current failure clusters and readiness")
    status_parser.add_argument("--telemetry", help="Override telemetry file path")
    status_parser.add_argument("--json", action="store_true", help="Output machine-readable JSON status")

    # Red team (AP-3): the mechanical gates of the adversarial review loop; agents call these step by step.
    rt_parser = subparsers.add_parser("redteam", help="AP-3 red-first review loop gates (open, gate-findings, open-fix, gate-fix, continue, close, status)")
    rt_parser.add_argument("--target", required=True, help="Target name under the config's `redteam.targets`")
    rt_parser.add_argument("rest", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)

    if args.command == "redteam":
        import yaml
        from autopoiesis import redteam

        raw = yaml.safe_load(Path(args.config).read_text()) or {}
        section = raw.get("redteam") or {}
        targets = section.get("targets") or {}
        if args.target not in targets:
            print(f"unknown redteam target {args.target!r}; known: {sorted(targets)}", file=sys.stderr)
            return 2
        cfg_dict = {k: v for k, v in section.items() if k != "targets"}
        cfg_dict.update(targets[args.target])
        return redteam.cli(args.rest, cfg_dict)

    if args.command == "init":
        config_path = Path(args.config)
        if config_path.exists():
            print(f"{config_path} already exists.")
            return 0
        content = f"""version: 1
project:
  name: "{args.project_name}"
  repo_path: "{args.repo_path}"

telemetry:
  adapter: "jsonl"
  path: "telemetry.jsonl"
  lookback_days: 7

mutation:
  sandbox_dir: ".autopoiesis/sandboxes"

evaluation:
  test_command: "pytest -q"
  max_latency_degradation_pct: 2.0
  prompt_token_dividend: 35
  enforce_red_first: true

deployment:
  branch: "main"
  auto_merge: true
"""
        config_path.write_text(content, encoding="utf-8")
        print(f"Initialized {config_path} successfully.")
        return 0

    # Ensure config exists for status and cycle
    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"Error: Configuration file '{config_path}' not found. Run 'autopoiesis init' first.")
        return 1

    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"Error loading configuration: {e}")
        return 1

    if args.telemetry:
        config.telemetry.path = args.telemetry

    if args.command == "status":
        telem_path = Path(config.telemetry.path)
        if not telem_path.is_absolute():
            telem_path = Path(config.project.repo_path) / telem_path

        if config.telemetry.adapter == "argus":
            source: TelemetrySource = ArgusTelemetrySource(telem_path)
        else:
            source = JsonlTelemetrySource(telem_path)

        events = list(source.iter_events())
        engine = ClusterEngine()
        clusters = engine.cluster_events(events)

        deterministic = [c for c in clusters if not c.is_transient]
        transient = [c for c in clusters if c.is_transient]

        status_data = {
            "project": config.project.name,
            "repo_path": str(config.project.repo_path),
            "telemetry_source": str(telem_path),
            "events_ingested": len(events),
            "total_clusters": len(clusters),
            "deterministic_clusters": len(deterministic),
            "transient_clusters": len(transient),
            "top_clusters": [
                {
                    "cluster_id": c.cluster_id,
                    "tool": c.tool,
                    "occurrences": c.occurrences,
                    "is_transient": c.is_transient,
                    "template": c.template,
                }
                for c in clusters[:5]
            ],
        }

        if args.json:
            print(json.dumps(status_data, indent=2))
        else:
            print("==================================================")
            print(f"Autopoiesis Status — {config.project.name}")
            print("==================================================")
            print(f"Repository:         {config.project.repo_path}")
            print(f"Telemetry Source:   {telem_path} ({len(events)} events)")
            print(f"Active Clusters:    {len(clusters)} ({len(deterministic)} deterministic, {len(transient)} transient)")
            print("--------------------------------------------------")
            if clusters:
                print("Top Failure Targets:")
                for c in clusters[:5]:
                    t_flag = "[TRANSIENT]" if c.is_transient else "[DETERMINISTIC]"
                    print(f"  • {t_flag} {c.cluster_id} ({c.tool}, {c.occurrences}x): {c.template[:60]}")
            else:
                print("No failure clusters discovered. System Pareto-stable.")
            print("==================================================")
        return 0

    elif args.command == "cycle":
        engine = AutopoiesisEngine.from_config(config)
        report = engine.run_cycle(dry_run=args.dry_run)

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print("==================================================")
            print(f"Autopoiesis Cycle Complete — Status: {report.get('status')}")
            print("==================================================")
            print(f"Clusters Discovered: {report.get('clusters_discovered')}")
            sel = report.get("selected_cluster")
            if sel:
                print(f"Selected Target:     {sel.get('cluster_id')} ({sel.get('tool')}, {sel.get('occurrences')}x)")
            if report.get("mutation_proposed"):
                print(f"Mutation Proposal:   {report.get('mutation_proposed')}")
            verdict = report.get("verdict")
            if verdict:
                print(f"Pareto Dominant:     {verdict.get('is_pareto_dominant')}")
                if verdict.get("rejection_reasons"):
                    print("Rejection Reasons:")
                    for r in verdict["rejection_reasons"]:
                        print(f"  ✗ {r}")
            if report.get("promoted"):
                print("PROMOTION SUCCESS:")
                receipt = report.get("receipt", {})
                print(f"  Release ID: {receipt.get('release_id')}")
                print(f"  Commit SHA: {receipt.get('commit_sha')}")
                print(f"  Tokens Reclaimed: {report.get('reclaimed_tokens')}")
            if report.get("promotion_skipped"):
                print(f"NOT PROMOTED: {report['promotion_skipped']}")
            if report.get("promotion_error"):
                print(f"PROMOTION FAILED: {report['promotion_error']}")
            print("==================================================")
        # "dominant_not_merged": the cycle did what the configuration asked (deployment.auto_merge: false)
        ok_statuses = ("no_clusters", "dry_run_evaluated", "dominant_not_merged")
        return 0 if (report.get("promoted") or args.dry_run or report.get("status") in ok_statuses) else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

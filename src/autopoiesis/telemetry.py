"""Telemetry ingestion and failure clustering."""

from __future__ import annotations

import abc
import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from autopoiesis.models import FailureCluster, TelemetryEvent


class TelemetrySource(abc.ABC):
    """Abstract interface for ingesting execution telemetry."""

    @abc.abstractmethod
    def iter_events(self, since_seq: int = 0) -> Iterator[TelemetryEvent]:
        """Yield telemetry events strictly ordered by sequence."""
        pass


class JsonlTelemetrySource(TelemetrySource):
    """Ingests telemetry from a standard JSONL log file."""

    def __init__(self, log_path: Path):
        self.log_path = Path(log_path)

    def iter_events(self, since_seq: int = 0) -> Iterator[TelemetryEvent]:
        if not self.log_path.exists():
            return
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    seq = int(data.get("sequence", 0))
                    if seq <= since_seq:
                        continue
                    yield TelemetryEvent(
                        sequence=seq,
                        timestamp=data.get("timestamp", ""),
                        tool=data.get("tool", "unknown"),
                        status=data.get("status", "ok"),
                        error_message=data.get("error_message") or data.get("error"),
                        exit_code=data.get("exit_code"),
                        latency_ms=data.get("latency_ms"),
                        metadata=data.get("metadata", {}),
                    )
                except Exception:
                    continue


class ArgusTelemetrySource(TelemetrySource):
    """Ingests telemetry from an immutable Argus audit ledger."""

    def __init__(self, ledger_path: Path):
        self.ledger_path = Path(ledger_path)

    def iter_events(self, since_seq: int = 0) -> Iterator[TelemetryEvent]:
        if not self.ledger_path.exists():
            return
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    seq = int(data.get("sequence", 0))
                    if seq <= since_seq:
                        continue
                    ev_type = data.get("event_type", "")
                    if ev_type not in ("tool_execution", "tool_failure", "process_execution"):
                        continue
                    payload = data.get("payload", {})
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    yield TelemetryEvent(
                        sequence=seq,
                        timestamp=data.get("timestamp", ""),
                        tool=payload.get("tool", "unknown"),
                        status="error" if payload.get("is_error") or payload.get("status") == "error" else "ok",
                        error_message=payload.get("error") or payload.get("error_detail"),
                        exit_code=payload.get("exit_code"),
                        latency_ms=payload.get("latency_ms"),
                        metadata=payload,
                    )
                except Exception:
                    continue


class ClusterEngine:
    """Clusters telemetry errors into actionable failure targets."""

    @staticmethod
    def normalize_template(error_text: str) -> str:
        """Strip variable strings, numbers, and hex IDs to produce a template."""
        if not error_text:
            return "empty_error"
        s = error_text.strip()
        # Replace hex hashes / UUIDs
        s = re.sub(r"\b[0-9a-fA-F]{8,64}\b", "<HEX>", s)
        # Replace integers
        s = re.sub(r"\b\d+\b", "<N>", s)
        # Replace quoted paths / strings
        s = re.sub(r"'[^']*'", "'<S>'", s)
        s = re.sub(r'"[^"]*"', '"<S>"', s)
        # Replace specific dynamic tokens
        s = re.sub(r"(command not found:\s*)\S+", r"\1<CMD>", s)
        s = re.sub(r"(No such file or directory:\s*)\S+", r"\1<PATH>", s)
        return s[:200]

    def cluster_events(self, events: List[TelemetryEvent]) -> List[FailureCluster]:
        clusters: Dict[str, FailureCluster] = {}
        total_calls = len(events)
        if total_calls == 0:
            return []

        for ev in events:
            if ev.status != "error" and (ev.exit_code is None or ev.exit_code == 0):
                continue
            template = self.normalize_template(ev.error_message or f"exit_{ev.exit_code}")
            failure_class = f"{ev.tool}.exit_{ev.exit_code}" if ev.exit_code else f"{ev.tool}.error"
            
            raw_id = f"{ev.tool}:{failure_class}:{template}"
            cid = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]

            if cid not in clusters:
                clusters[cid] = FailureCluster(
                    cluster_id=cid,
                    tool=ev.tool,
                    failure_class=failure_class,
                    template=template,
                    occurrences=0,
                    recent_24h_occurrences=0,
                    sample_events=[],
                    baseline_rate=0.0,
                )
            c = clusters[cid]
            c.occurrences += 1
            c.recent_24h_occurrences += 1
            if len(c.sample_events) < 5:
                c.sample_events.append(ev.sequence)

        for c in clusters.values():
            c.baseline_rate = c.occurrences / float(total_calls)

        return sorted(clusters.values(), key=lambda x: x.occurrences, reverse=True)

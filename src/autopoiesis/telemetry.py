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
        # Replace line references in stack traces (e.g., file.py:142 -> file.py:<LINE>)
        s = re.sub(r"([a-zA-Z0-9_\-\./]+\.py):\d+", r"\1:<LINE>", s)
        # Replace duration tokens (e.g., 30s, 150ms, 2.5h)
        s = re.sub(r"\b\d+(\.\d+)?(ms|s|m|h)\b", "<DUR>", s)
        # Replace memory tokens (e.g., 50MB, 4.2GB, 1024bytes)
        s = re.sub(r"\b\d+(\.\d+)?(MB|GB|KB|bytes|B)\b", "<MEM>", s)
        # Replace IP addresses
        s = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?\b", "<IP>", s)
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

        # Track sequential retries per tool for transient deconvolution
        retry_attempts: Dict[str, int] = {}
        retry_successes: Dict[str, int] = {}
        last_error_for_tool: Dict[str, str] = {}  # tool -> cid

        for ev in events:
            is_err = (ev.status == "error") or (ev.exit_code is not None and ev.exit_code != 0)
            
            # Check if this event follows a previous error for the same tool (retry deconvolution)
            if ev.tool in last_error_for_tool:
                prev_cid = last_error_for_tool.pop(ev.tool)
                retry_attempts[prev_cid] = retry_attempts.get(prev_cid, 0) + 1
                if not is_err:
                    retry_successes[prev_cid] = retry_successes.get(prev_cid, 0) + 1

            if not is_err:
                continue

            template = self.normalize_template(ev.error_message or f"exit_{ev.exit_code}")
            failure_class = f"{ev.tool}.exit_{ev.exit_code}" if ev.exit_code else f"{ev.tool}.error"
            
            raw_id = f"{ev.tool}:{failure_class}:{template}"
            cid = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]
            last_error_for_tool[ev.tool] = cid

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
                    is_transient=False,
                    transient_retry_rate=0.0,
                )
            c = clusters[cid]
            c.occurrences += 1
            c.recent_24h_occurrences += 1
            if len(c.sample_events) < 5:
                c.sample_events.append(ev.sequence)

        for cid, c in clusters.items():
            c.baseline_rate = c.occurrences / float(total_calls)
            attempts = retry_attempts.get(cid, 0)
            if attempts > 0:
                succ = retry_successes.get(cid, 0)
                rate = float(succ) / float(attempts)
                c.transient_retry_rate = rate
                if attempts >= 2 and rate >= 0.5:
                    c.is_transient = True

        # Sort deterministic clusters first, then by occurrence count
        return sorted(clusters.values(), key=lambda x: (x.is_transient, -x.occurrences))

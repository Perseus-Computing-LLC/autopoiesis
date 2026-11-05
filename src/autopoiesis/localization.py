"""Spectrum-Based Fault Localization (SBFL) using the Ochiai metric."""

from __future__ import annotations

import ast
import dataclasses
import math
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple


@dataclasses.dataclass
class SuspiciousnessRecord:
    file_path: str
    line_number: int
    score: float
    failed_executions: int
    passed_executions: int


class LineTracer:
    """Lightweight line coverage tracer using sys.settrace."""

    def __init__(self, target_files: Set[str]):
        # Normalize target files to absolute canonical paths
        self.target_files = {str(Path(f).resolve()) for f in target_files}
        self.executed_lines: Set[Tuple[str, int]] = set()

    def trace_func(self, frame, event, arg):
        if event == "line":
            filename = frame.f_code.co_filename
            abs_fn = str(Path(filename).resolve())
            if abs_fn in self.target_files:
                self.executed_lines.add((abs_fn, frame.f_lineno))
        return self.trace_func

    def __enter__(self):
        self.executed_lines.clear()
        self._orig_trace = sys.gettrace()
        sys.settrace(self.trace_func)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.settrace(self._orig_trace)


class OchiaiSBFL:
    """Calculates Ochiai suspiciousness ranking across executed code lines.

    Formula:
        Score(s) = e_f / sqrt(total_f * (e_f + e_p))
    """

    def __init__(self, target_files: Optional[List[Path]] = None):
        self.target_files = {str(f.resolve()) for f in (target_files or [])}
        self.total_passing: int = 0
        self.total_failing: int = 0
        # (file, line) -> count
        self.passed_counts: Dict[Tuple[str, int], int] = {}
        self.failed_counts: Dict[Tuple[str, int], int] = {}

    def record_run(self, passed: bool, executed_lines: Set[Tuple[str, int]]) -> None:
        if passed:
            self.total_passing += 1
            for loc in executed_lines:
                self.passed_counts[loc] = self.passed_counts.get(loc, 0) + 1
        else:
            self.total_failing += 1
            for loc in executed_lines:
                self.failed_counts[loc] = self.failed_counts.get(loc, 0) + 1

    def run_and_record(self, test_callable: Callable[[], None]) -> bool:
        """Execute a test callable under tracing and record pass/fail."""
        tracer = LineTracer(self.target_files)
        passed = True
        try:
            with tracer:
                test_callable()
        except Exception:
            passed = False
        self.record_run(passed, tracer.executed_lines)
        return passed

    def compute_suspiciousness(self) -> List[SuspiciousnessRecord]:
        """Rank all executed statements by Ochiai suspiciousness score."""
        if self.total_failing == 0:
            return []

        all_locations = set(self.passed_counts.keys()) | set(self.failed_counts.keys())
        records: List[SuspiciousnessRecord] = []

        total_f = float(self.total_failing)

        for file_path, lineno in all_locations:
            e_f = float(self.failed_counts.get((file_path, lineno), 0))
            e_p = float(self.passed_counts.get((file_path, lineno), 0))

            if e_f == 0.0:
                score = 0.0
            else:
                denom = math.sqrt(total_f * (e_f + e_p))
                score = e_f / denom if denom > 0.0 else 0.0

            records.append(
                SuspiciousnessRecord(
                    file_path=file_path,
                    line_number=lineno,
                    score=round(score, 4),
                    failed_executions=int(e_f),
                    passed_executions=int(e_p),
                )
            )

        # Sort descending by score, then ascending by line number
        records.sort(key=lambda r: (-r.score, r.file_path, r.line_number))
        return records

"""severity: low
finding: _receipt falls back to a hard-coded, machine-specific checkout (/opt/data/work/telarch-suite/argus/src) and puts it on sys.path when argus_ledger is not installed

redteam.py:104-110. In a public package this path means nothing on any other machine (the user gets a bare
ImportError with no hint that a ledger package is needed), and on a machine where the directory exists the
harness silently imports ledger code from an unpinned working copy. The location is not configurable.

The test never touches that directory: Path.is_dir is wrapped to record what gets probed and to answer False
for everything outside tmp_path, and the argus_ledger import is blocked through sys.modules.
"""
from __future__ import annotations

import pathlib
import sys
from pathlib import Path

from autopoiesis import redteam


def probe(tmp_path: Path, monkeypatch, ledger_path):
    probed = []
    real_is_dir = pathlib.Path.is_dir

    def is_dir(self, *a, **kw):
        probed.append(str(self))
        return real_is_dir(self, *a, **kw) if str(self).startswith(str(tmp_path)) else False
    monkeypatch.setattr(pathlib.Path, "is_dir", is_dir)
    monkeypatch.setitem(sys.modules, "argus_ledger", None)          # "package not installed"
    monkeypatch.setitem(sys.modules, "argus_ledger.ledger", None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    cfg = redteam.RedTeamConfig(repo_path=str(tmp_path), test_command=["true"], cuts=["a"],
                                rotation_path=str(tmp_path / "rotation.json"), jobs_root=str(tmp_path / "jobs"),
                                ledger_path=ledger_path)
    try:
        outcome = redteam._receipt(cfg, "rv1110.probe", {"n": 1})
    except ImportError as exc:
        outcome = f"ImportError: {exc}"
    return outcome, probed


def test_control_no_ledger_means_no_receipt_and_no_probe(tmp_path: Path, monkeypatch):
    outcome, probed = probe(tmp_path, monkeypatch, ledger_path=None)
    assert outcome is None and probed == []


def test_missing_ledger_package_does_not_probe_a_machine_specific_path(tmp_path: Path, monkeypatch):
    outcome, probed = probe(tmp_path, monkeypatch, ledger_path=str(tmp_path / "audit.jsonl"))
    foreign = [p for p in probed if not p.startswith(str(tmp_path))]
    assert isinstance(outcome, str) and outcome.startswith("ImportError")     # fixture: the package is "absent"
    assert not foreign, f"fallback import location is hard-coded: {foreign}"

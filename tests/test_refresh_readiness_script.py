"""``scripts/boards/refresh_readiness.py`` refuses to re-pin on a failed gate (Issue #6076)."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "boards" / "refresh_readiness.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("refresh_readiness_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_deterministic_zip_is_order_independent(mod, tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    mod.write_deterministic_zip(a, {"z.txt": b"z", "a/b.txt": b"ab"})
    mod.write_deterministic_zip(b, {"a/b.txt": b"ab", "z.txt": b"z"})
    assert a.read_bytes() == b.read_bytes()
    with zipfile.ZipFile(a) as z:
        assert z.namelist() == ["a/b.txt", "z.txt"]
        assert {i.date_time for i in z.infolist()} == {mod.ZIP_DATE_TIME}


def test_compare_metrics_flags_only_recorded_differences(mod):
    recorded = {"drc_violations": 0, "nets_routed_pct": 100.0, "lvs_clean": True}
    assert mod.compare_metrics(recorded, {**recorded, "lvs_mismatches": 3}) == []
    assert mod.compare_metrics(recorded, {**recorded, "nets_routed_pct": 97.3}) == [
        "nets_routed_pct"
    ]


def _fake_board(root: Path) -> Path:
    board = root / "99-fake"
    mfg = board / "output" / "manufacturing"
    mfg.mkdir(parents=True)
    (board / "output" / "board.kicad_sch").write_text("sch")
    (board / "output" / "board_routed.kicad_pcb").write_text("pcb")
    (board / "output" / "erc_report.json").write_text("{}")
    for name in ("kicad_project.zip", "design-source.zip"):
        with zipfile.ZipFile(mfg / name, "w") as z:
            z.writestr("output/board.kicad_sch", "old")
    (mfg / "manifest.json").write_text(json.dumps({"version": "1.0", "files": {}}) + "\n")
    with zipfile.ZipFile(board / "output" / "manufacturing.zip", "w") as z:
        z.writestr("manifest.json", "old")
    report = {
        "status": "ready",
        "blockers": [],
        "metrics": {"drc_violations": 0, "nets_routed_pct": 100.0, "lvs_clean": True},
        "inputs": {"output/board.kicad_sch": "0" * 64},
        "checked_at": "2026-01-01T00:00:00+00:00",
    }
    (board / "output" / "readiness.json").write_text(json.dumps(report, indent=2) + "\n")
    return board


def test_metric_regression_publishes_nothing(mod, tmp_path, monkeypatch):
    board = _fake_board(tmp_path)
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    monkeypatch.setitem(
        mod.BOARDS,
        board.name,
        mod.BoardConfig(
            routed_pcb="output/board_routed.kicad_pcb",
            schematic="output/board.kicad_sch",
            tier="jlcpcb",
            erc=(mod.ErcJob("x.json", "output", "board.kicad_sch", ("output/erc_report.json",)),),
            archives=(
                ("output/manufacturing/kicad_project.zip", (".",)),
                ("output/manufacturing/design-source.zip", (".",)),
            ),
        ),
    )
    clean_erc = json.dumps({"sheets": [{"violations": []}], "date": "now"}).encode()
    monkeypatch.setattr(mod, "native_erc", lambda *a, **k: clean_erc)
    monkeypatch.setattr(mod, "gate_native_drc", lambda *a: 0)
    monkeypatch.setattr(mod, "gate_kct_check", lambda *a: (True, 0))
    monkeypatch.setattr(mod, "gate_net_status", lambda *a: 97.3)  # one net lost
    monkeypatch.setattr(mod, "gate_sync", lambda *a: None)

    with pytest.raises(mod.GateFailure, match="nets_routed_pct"):
        mod.refresh(board, "2026-10-09T00:00:00+00:00")
    after = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    assert after == before


def test_failed_gate_publishes_nothing(mod, tmp_path, monkeypatch):
    board = _fake_board(tmp_path)
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    monkeypatch.setitem(
        mod.BOARDS,
        board.name,
        mod.BoardConfig(
            routed_pcb="output/board_routed.kicad_pcb",
            schematic="output/board.kicad_sch",
            tier="jlcpcb",
            erc=(),
            archives=(),
        ),
    )

    def drc_fails(*_):
        raise mod.GateFailure("native DRC has errors or unconnected items")

    monkeypatch.setattr(mod, "gate_native_drc", drc_fails)
    with pytest.raises(mod.GateFailure, match="native DRC"):
        mod.refresh(board, "2026-10-09T00:00:00+00:00")
    assert {p: p.read_bytes() for p in board.rglob("*") if p.is_file()} == before

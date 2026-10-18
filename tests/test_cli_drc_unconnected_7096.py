"""``kct drc`` must not pass a board with error-severity unconnected items (Issue #7096)."""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.cli import drc_cmd

ITEM = {
    "description": "Pad 1 [N1] of R1 on F.Cu",
    "uuid": "aaaaaaaa-0000-0000-0000-000000000001",
    "pos": {"x": 1.0, "y": 1.0},
}


def _report(unconnected: list[dict], violations: list[dict] | None = None) -> dict:
    return {
        "$schema": "https://schemas.kicad.org/drc.v1.json",
        "source": "board.kicad_pcb",
        "date": "2026-10-18T00:00:00+00:00",
        "kicad_version": "10.0.5",
        "coordinate_units": "mm",
        "violations": violations or [],
        "unconnected_items": unconnected,
        "schematic_parity": [],
    }


def _open(severity: str = "error") -> dict:
    return {
        "type": "unconnected_items",
        "severity": severity,
        "description": "Missing connection between items",
        "items": [ITEM, {**ITEM, "uuid": "bbbbbbbb-0000-0000-0000-000000000002"}],
    }


def _write(tmp_path: Path, data: dict) -> str:
    path = tmp_path / "board-drc.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_unrouted_report_fails(tmp_path, capsys):
    rc = drc_cmd.main([_write(tmp_path, _report([_open()])), "--allow-incomplete"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "PASSED" not in out


def test_routed_report_passes(tmp_path):
    assert drc_cmd.main([_write(tmp_path, _report([])), "--allow-incomplete"]) == 0


def test_ignore_unconnected_restores_clearance_only(tmp_path):
    path = _write(tmp_path, _report([_open()]))
    assert drc_cmd.main([path, "--allow-incomplete", "--ignore-unconnected"]) == 0


def test_warning_severity_unconnected_does_not_fail(tmp_path):
    path = _write(tmp_path, _report([_open("warning")]))
    assert drc_cmd.main([path, "--allow-incomplete"]) == 0


def test_json_carries_unconnected_count(tmp_path, capsys):
    path = _write(tmp_path, _report([_open(), _open()]))
    rc = drc_cmd.main([path, "--allow-incomplete", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["summary"]["unconnected"] == 2
    assert data["summary"]["errors"] == 2


def test_json_ignore_unconnected_counts_zero(tmp_path, capsys):
    path = _write(tmp_path, _report([_open()]))
    rc = drc_cmd.main([path, "--allow-incomplete", "--format", "json", "--ignore-unconnected"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["summary"]["unconnected"] == 0


def test_summary_format_fails(tmp_path, capsys):
    path = _write(tmp_path, _report([_open()]))
    assert drc_cmd.main([path, "--allow-incomplete", "--format", "summary"]) == 1
    assert "unconnected_items" in capsys.readouterr().out


def test_errors_only_still_fails(tmp_path):
    path = _write(tmp_path, _report([_open()]))
    assert drc_cmd.main([path, "--allow-incomplete", "--errors-only"]) == 1


def test_filter_config_does_not_drop_unconnected(tmp_path):
    cfg = tmp_path / "filters.toml"
    cfg.write_text('[[drc.filters]]\ntype = "clearance"\naction = "ignore"\n')
    path = _write(tmp_path, _report([_open()]))
    assert drc_cmd.main([path, "--allow-incomplete", "--filter-config", str(cfg)]) == 1


def test_mfr_mode_fails_on_unconnected(tmp_path, capsys):
    path = _write(tmp_path, _report([_open()]))
    assert drc_cmd.main([path, "--mfr", "jlcpcb"]) == 1
    assert "UNCONNECTED ITEMS" in capsys.readouterr().out
    assert drc_cmd.main([path, "--mfr", "jlcpcb", "--ignore-unconnected"]) == 0

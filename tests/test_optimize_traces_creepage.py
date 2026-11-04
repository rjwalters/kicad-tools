"""Issue #8300: optimize-traces rejects individual edits that break pairwise creepage."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.optimize_cmd import main
from kicad_tools.cli.runner import KiCadCLIResult
from kicad_tools.router.optimizer.pcb import parse_segments

REFILL = "kicad_tools.cli.runner.run_refill_zones"


def _seg(x1, y1, x2, y2, net, layer="F.Cu"):
    return (
        f'\t(segment (start {x1} {y1}) (end {x2} {y2}) (width 0.2) (layer "{layer}") (net {net}))\n'
    )


def _board() -> str:
    """LV detours under an HV stub; a second, safe LV detour sits far away.

    The straight shortcut of the first U passes 0.8 mm (edge to edge) from the
    HV stub: clean for a ~0.2 mm fab floor, short of a 1.6 mm requirement.
    """
    head = (
        '(kicad_pcb\n\t(version 20240108)\n\t(generator "test")\n'
        '\t(general (thickness 1.6))\n\t(paper "A4")\n'
        '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '\t(setup (pad_to_mask_clearance 0))\n\t(net 0 "")\n'
        '\t(net 1 "HV")\n\t(net 2 "LV")\n'
        "\t(gr_rect (start 90 80) (end 160 120) (stroke (width 0.1) (type default))"
        ' (fill none) (layer "Edge.Cuts") (uuid "edge"))\n'
    )
    # The detour pass only rewrites a chain whose ends are known pads.
    pads = "".join(
        f'\t\t(pad "{i}" smd rect (at {x} 95) (size 0.4 0.4) (layers "F.Cu") (net 2 "LV"))\n'
        for i, x in enumerate((105, 115, 135, 145), 1)
    )
    body = (
        '\t(footprint "TP"\n\t\t(layer "F.Cu")\n\t\t(uuid "fp")\n\t\t(at 0 0)\n'
        '\t\t(property "Reference" "TP1" (at 0 0) (layer "F.SilkS") (uuid "r"))\n' + pads + "\t)\n"
    )
    body += _seg(110, 96, 110, 110, 1)  # HV stub
    # LV detour 1 (shortcut would pass the HV stub)
    body += _seg(105, 95, 105, 90, 2) + _seg(105, 90, 115, 90, 2) + _seg(115, 90, 115, 95, 2)
    # LV detour 2 (shortcut is far from any HV copper)
    body += _seg(135, 95, 135, 90, 2) + _seg(135, 90, 145, 90, 2) + _seg(145, 90, 145, 95, 2)
    return head + body + ")\n"


@pytest.fixture
def pcb(tmp_path: Path) -> Path:
    p = tmp_path / "hv.kicad_pcb"
    p.write_text(_board())
    return p


@pytest.fixture
def vmap(tmp_path: Path) -> Path:
    p = tmp_path / "vm.json"
    p.write_text(json.dumps({"HV": 150.0, "LV": 0.0}))  # 150 V -> 1.6 mm (PD2/IIIa)
    return p


def _run(pcb: Path, tmp_path: Path, *extra: str) -> tuple[dict, list]:
    out = tmp_path / "out.kicad_pcb"
    with patch(REFILL, return_value=KiCadCLIResult(success=True)):
        assert main([str(pcb), "-o", str(out), "--format", "json", *extra]) == 0
    segs = parse_segments(out.read_text())["LV"]
    return segs


def _spans_x(segs, x_from, x_to) -> bool:
    """True when some LV segment runs straight across ``x_from``..``x_to``."""
    return any(s.y1 == s.y2 and min(s.x1, s.x2) <= x_from and max(s.x1, s.x2) >= x_to for s in segs)


def test_baseline_takes_shortcut_without_voltage_map(pcb, tmp_path, capsys):
    segs = _run(pcb, tmp_path)
    capsys.readouterr()
    # Both detours collapse to a direct run (no voltage map -> nothing stops it).
    assert not any(s.y1 == 90 or s.y2 == 90 for s in segs)


def test_voltage_map_rejects_unsafe_edit_keeps_safe_one(pcb, vmap, tmp_path, capsys):
    segs = _run(pcb, tmp_path, "--voltage-map", str(vmap))
    data = json.loads(capsys.readouterr().out)
    # Detour 1 must not be shortened across the HV stub ...
    assert any(s.y1 == 90 or s.y2 == 90 for s in segs if s.x1 < 120 and s.x2 < 120)
    # ... but the independent detour 2 on the same net is still optimised.
    assert not any(s.y1 == 90 or s.y2 == 90 for s in segs if s.x1 > 120 and s.x2 > 120)
    skipped = data["creepage_skipped"]
    assert skipped and skipped[0]["net_a"] == "HV" and skipped[0]["net_b"] == "LV"
    assert skipped[0]["count"] >= 1


def test_text_summary_reports_skips(pcb, vmap, tmp_path, capsys):
    out = tmp_path / "out.kicad_pcb"
    with patch(REFILL, return_value=KiCadCLIResult(success=True)):
        assert main([str(pcb), "-o", str(out), "--voltage-map", str(vmap)]) == 0
    text = capsys.readouterr().out
    assert "Edits skipped for creepage" in text
    assert "HV / LV" in text


def test_no_flag_output_has_no_creepage_keys(pcb, tmp_path, capsys):
    _run(pcb, tmp_path)
    data = json.loads(capsys.readouterr().out)
    assert "creepage_skipped" not in data and "voltage_map" not in data


def test_missing_voltage_map_exits_nonzero(pcb, tmp_path, capsys):
    rc = main([str(pcb), "--voltage-map", str(tmp_path / "nope.json")])
    assert rc == 1
    assert "voltage-map file not found" in capsys.readouterr().err


def test_invalid_voltage_map_exits_nonzero(pcb, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert main([str(pcb), "--voltage-map", str(bad)]) == 1
    assert "invalid voltage-map" in capsys.readouterr().err


def test_missing_voltage_map_json_mode(pcb, tmp_path, capsys):
    rc = main([str(pcb), "--voltage-map", str(tmp_path / "nope.json"), "--format", "json"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["success"] is False


def _pad_board() -> str:
    """As :func:`_board`, but the HV copper is a PAD of another footprint.

    No HV trace exists, so a trace-only pairwise check cannot see it: the
    detour-1 shortcut along y=95 would pass ~1.2 mm (edge to edge) from the HV
    pad, short of the 1.6 mm requirement (Issue #8300 review).
    """
    board = _board().replace(_seg(110, 96, 110, 110, 1), "")
    hv_fp = (
        '\t(footprint "HVPAD"\n\t\t(layer "F.Cu")\n\t\t(uuid "fp-hv")\n\t\t(at 110 96.5)\n'
        '\t\t(property "Reference" "J1" (at 0 0) (layer "F.SilkS") (uuid "r-hv"))\n'
        '\t\t(pad "1" smd rect (at 0 0) (size 0.4 0.4) (layers "F.Cu") (net 1 "HV"))\n'
        "\t)\n"
    )
    return board[: board.rindex(")")] + hv_fp + ")\n"


@pytest.fixture
def pad_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "hv_pad.kicad_pcb"
    p.write_text(_pad_board())
    return p


def test_hv_pad_baseline_takes_shortcut(pad_pcb, tmp_path, capsys):
    segs = _run(pad_pcb, tmp_path)
    capsys.readouterr()
    assert not any(s.y1 == 90 or s.y2 == 90 for s in segs)


def test_hv_pad_blocks_shortcut_with_voltage_map(pad_pcb, vmap, tmp_path, capsys):
    segs = _run(pad_pcb, tmp_path, "--voltage-map", str(vmap))
    data = json.loads(capsys.readouterr().out)
    # The detour next to the HV pad survives; the far one is still optimised.
    assert any(s.y1 == 90 or s.y2 == 90 for s in segs if s.x1 < 120 and s.x2 < 120)
    assert not any(s.y1 == 90 or s.y2 == 90 for s in segs if s.x1 > 120 and s.x2 > 120)
    assert {(r["net_a"], r["net_b"]) for r in data["creepage_skipped"]} == {("HV", "LV")}

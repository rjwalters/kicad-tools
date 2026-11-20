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


def test_no_map_json_reports_inactive_gate(pcb, tmp_path, capsys):
    _run(pcb, tmp_path)
    data = json.loads(capsys.readouterr().out)
    assert data["creepage_gate"] == "inactive"
    assert any("creepage gate inactive" in w for w in data["warnings"])


def test_map_json_reports_active_gate_no_warning(pcb, vmap, tmp_path, capsys):
    _run(pcb, tmp_path, "--voltage-map", str(vmap))
    data = json.loads(capsys.readouterr().out)
    assert data["creepage_gate"] == "active"
    assert "warnings" not in data


def test_no_map_text_warns_on_stderr(pcb, tmp_path, capsys):
    out = tmp_path / "out.kicad_pcb"
    with patch(REFILL, return_value=KiCadCLIResult(success=True)):
        assert main([str(pcb), "-o", str(out)]) == 0
    cap = capsys.readouterr()
    assert "creepage gate inactive" in cap.err
    assert "creepage gate inactive" not in cap.out


def test_no_map_quiet_suppresses_text_warning(pcb, tmp_path, capsys):
    out = tmp_path / "out.kicad_pcb"
    with patch(REFILL, return_value=KiCadCLIResult(success=True)):
        assert main([str(pcb), "-o", str(out), "--quiet"]) == 0
    assert "creepage gate inactive" not in capsys.readouterr().err


def test_map_text_has_no_warning(pcb, vmap, tmp_path, capsys):
    out = tmp_path / "out.kicad_pcb"
    with patch(REFILL, return_value=KiCadCLIResult(success=True)):
        assert main([str(pcb), "-o", str(out), "--voltage-map", str(vmap)]) == 0
    assert "creepage gate inactive" not in capsys.readouterr().err


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


# ---------------------------------------------------------------------------
# Issue #9640: a range map must not let a rewrite deepen an already-failing pair
# ---------------------------------------------------------------------------


def _shaped_board() -> str:
    """As :func:`_board`, plus a second LV stub that already sits short of 1.6 mm.

    Under the envelope map both nets swing ``0..150 V``: the per-edit table
    collapses each to its largest endpoint (150 V), differences them to 0 V and
    gates nothing, while the census scores ``dV = 150 V`` (1.6 mm).  The extra
    stub runs 1.0 mm (edge to edge) from the HV stub, so ``HV|LV|F.Cu`` already
    fails in the input; the detour-1 shortcut would take it to 0.8 mm.  The
    failing *key* is unchanged -- only its shortfall grows.
    """
    board = _board()
    near = _seg(111.3, 98, 111.3, 108, 2)
    return board[: board.rindex(")")] + "\t" + near.lstrip("\t") + ")\n"


@pytest.fixture
def shaped_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "shaped.kicad_pcb"
    p.write_text(_shaped_board())
    return p


@pytest.fixture
def range_vmap(tmp_path: Path) -> Path:
    p = tmp_path / "range.json"
    p.write_text(
        json.dumps(
            {
                "HV": {"min": 0.0, "max": 150.0},
                "LV": {"min": 0.0, "max": 150.0},
                "_pair_bounds": [{"nets": ["HV", "LV"], "max_dv": 150.0, "why": "test"}],
            }
        )
    )
    return p


def _detour1_shortcut_taken(segs) -> bool:
    return not any(s.y1 == 90 or s.y2 == 90 for s in segs if s.x1 < 120 and s.x2 < 120)


def test_range_map_baseline_takes_shortcut_without_gate(shaped_pcb, tmp_path, capsys):
    segs = _run(shaped_pcb, tmp_path)
    capsys.readouterr()
    assert _detour1_shortcut_taken(segs)


def test_range_map_rolls_back_rewrite_that_deepens_failing_pair(
    shaped_pcb, range_vmap, tmp_path, capsys
):
    segs = _run(shaped_pcb, tmp_path, "--voltage-map", str(range_vmap))
    data = json.loads(capsys.readouterr().out)
    # The shaped detour survives: the shortcut would put the pair 0.2 mm deeper.
    assert not _detour1_shortcut_taken(segs)
    assert data["creepage_audit"] == "ran"
    # Main's interval-aware per-edit table now refuses the shortcut up front
    # (``creepage_skipped``); the whole-board audit stays as the backstop and
    # must at worst roll the net back.
    blocked_per_edit = any(
        {r["net_a"], r["net_b"]} == {"HV", "LV"} for r in data.get("creepage_skipped", [])
    )
    assert blocked_per_edit or data["output_verify_rolled_back"].get("creepage") == ["LV"]
    assert data["creepage_unwaived_failures_after"] <= data["creepage_unwaived_failures_before"]


def test_range_map_json_lists_pairs_checked(shaped_pcb, range_vmap, tmp_path, capsys):
    _run(shaped_pcb, tmp_path, "--voltage-map", str(range_vmap))
    data = json.loads(capsys.readouterr().out)
    checked = data["creepage_pairs_checked"]
    assert any({r["net_a"], r["net_b"]} == {"HV", "LV"} for r in checked)
    row = next(r for r in checked if {r["net_a"], r["net_b"]} == {"HV", "LV"})
    assert row["margin_mm_before"] is not None
    # Rolled back: the written board is no worse than the input.
    assert row["margin_mm_after"] >= row["margin_mm_before"] - 1e-6
    assert data["creepage_pairs_unchecked"] == []


def test_json_lists_voltage_map_pair_absent_from_board(pcb, tmp_path, capsys):
    vm = tmp_path / "ghost.json"
    vm.write_text(json.dumps({"HV": 150.0, "LV": 0.0, "GHOST": 300.0}))
    _run(pcb, tmp_path, "--voltage-map", str(vm))
    data = json.loads(capsys.readouterr().out)
    ghost = {frozenset((r["net_a"], r["net_b"])) for r in data["creepage_pairs_unchecked"]}
    assert frozenset(("GHOST", "HV")) in ghost


def _lock_stub(board_text: str) -> str:
    """Mark the shaped LV detour-1 run as locked (KiCad 8+ ``(locked yes)``)."""
    return board_text.replace(
        _seg(105, 90, 115, 90, 2),
        _seg(105, 90, 115, 90, 2).replace("(net 2))", "(net 2) (locked yes))"),
    )


def test_locked_track_net_is_left_untouched(tmp_path, capsys):
    p = tmp_path / "locked.kicad_pcb"
    p.write_text(_lock_stub(_board()))
    segs = _run(p, tmp_path)
    data = json.loads(capsys.readouterr().out)
    # Without any voltage map the shortcut would be taken, but the net is locked.
    assert not _detour1_shortcut_taken(segs)
    # The whole net is left alone, so the unrelated detour 2 stays too.
    assert any(s.y1 == 90 or s.y2 == 90 for s in segs if s.x1 > 120 and s.x2 > 120)
    assert data["locked_nets_skipped"] == ["LV"]


def test_unlocked_board_reports_no_locked_nets(pcb, tmp_path, capsys):
    _run(pcb, tmp_path)
    assert json.loads(capsys.readouterr().out)["locked_nets_skipped"] == []


# --- Issue #9640 review round 3 ------------------------------------------


def test_locked_arc_locks_its_net():
    from kicad_tools.router.optimizer.pcb import locked_segment_nets

    text = (
        '(kicad_pcb (net 0 "") (net 1 "LV")\n'
        '(arc (start 0 0) (mid 1 1) (end 2 0) (width 0.2) (layer "F.Cu") (net 1) (locked yes)))\n'
    )
    assert locked_segment_nets(text) == {"LV"}


def test_net_name_containing_locked_does_not_lock():
    from kicad_tools.router.optimizer.pcb import locked_segment_nets

    text = (
        '(kicad_pcb (net 0 "") (net 1 "sense locked rail")\n'
        '(segment (start 0 0) (end 2 0) (width 0.2) (layer "F.Cu") (net "sense locked rail")))\n'
    )
    assert locked_segment_nets(text) == set()


def test_public_optimize_traces_help_mentions_locked(capsys):
    from kicad_tools.cli import main as kct_main

    with pytest.raises(SystemExit):
        kct_main(["optimize-traces", "--help"])
    assert "locked" in capsys.readouterr().out


def test_shortfall_resolution_distinguishes_one_micron():
    from kicad_tools.creepage.output_audit import failing_units

    def row(m):
        return [{"net_a": "A", "net_b": "B", "layer": "F.Cu", "pass": False, "margin_mm": m}]

    assert failing_units(row(-0.8)) != failing_units(row(-0.799))
    assert failing_units(row(-0.8))["A|B|F.Cu"] > failing_units(row(-0.799))["A|B|F.Cu"]


def test_ranged_ghost_net_pairs_are_reported_unchecked(pcb, tmp_path, capsys):
    vm = tmp_path / "ghost_range.json"
    vm.write_text(
        json.dumps(
            {
                "HV": {"min": 0.0, "max": 150.0},
                "LV": {"min": 0.0, "max": 150.0},
                "GHOST": {"min": 0.0, "max": 150.0},
                "_pair_bounds": [{"nets": ["HV", "GHOST"], "max_dv": 150.0, "why": "t"}],
            }
        )
    )
    _run(pcb, tmp_path, "--voltage-map", str(vm))
    data = json.loads(capsys.readouterr().out)
    ghost = {frozenset((r["net_a"], r["net_b"])) for r in data["creepage_pairs_unchecked"]}
    assert {frozenset(("GHOST", "HV")), frozenset(("GHOST", "LV"))} <= ghost


def test_finished_board_that_regresses_after_refill_reverts_to_input(pcb, vmap, tmp_path, capsys):
    """A post-write step (zone refill) that breaks creepage must not ship."""
    out = tmp_path / "out.kicad_pcb"
    original = pcb.read_text()

    def fake_refill(path):
        text = Path(path).read_text()
        # An LV stub 0.5 mm from the HV stub: a new failing pair at 1.6 mm.
        Path(path).write_text(text[: text.rindex(")")] + _seg(110.5, 98, 110.5, 108, 2) + ")\n")
        return True, None

    with patch("kicad_tools.cli.optimize_cmd._refill_written_board", side_effect=fake_refill):
        assert main([str(pcb), "-o", str(out), "--format", "json", "--voltage-map", str(vmap)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert out.read_text() == original
    assert data["creepage_final_reverted_to_input"] is True
    assert data["output_verify_rolled_back"]["creepage_final"]
    assert data["creepage_unwaived_failures_after"] == data["creepage_unwaived_failures_before"]

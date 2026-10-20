"""Issue #7322: stitch vias keep their drill out of an SMD pad's mask-dam zone.

PR #7308 (#7296) gave the router's A* a mask-dam floor
(``DesignRules.min_via_pad_edge_gap``) but the stitcher still dropped GND vias
into the band just outside SMD pads, which ``kct check`` reports as
``via_at_pad_edge``.  The stitcher now consults the same
``via_drill_at_pad_edge`` predicate for every candidate site.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.stitch_cmd import (
    PadEdgeGuard,
    PadInfo,
    _pad_copper_gap,
    build_pad_edge_guard,
    calculate_dogleg_via_position,
    calculate_extended_escape_position,
    calculate_via_stub,
    find_all_pad_bboxes,
    run_blanket_stitch,
    run_stitch,
    run_thermal_stitch,
)
from kicad_tools.router.via_in_pad_eligibility import via_drill_at_pad_edge
from kicad_tools.sexp import parse_string

from .test_stitch_mfr import TWO_LAYER_GND_ZONE_PCB

VIA = 0.45
DRILL = 0.3
CLEARANCE = 0.1
FLOOR = 0.6  # generous, so the zone outreaches the copper clearance


def _write(tmp_path: Path, extra: str = "") -> Path:
    text = TWO_LAYER_GND_ZONE_PCB.rstrip()
    text = text[:-1] + extra + ")\n"
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(text)
    return pcb


def _foreign_smd(x: float, y: float, kind: str = "smd") -> str:
    drill = " (drill 0.3)" if kind == "thru_hole" else ""
    return (
        '  (net 2 "SIG")\n'
        '  (footprint "X:P" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000200")\n'
        f"    (at {x} {y})\n"
        '    (property "Reference" "X1" (at 0 -1 0) (layer "F.SilkS") (uuid "ref-x"))\n'
        f'    (pad "1" {kind} rect (at 0 0) (size 0.4 0.4){drill} '
        '(layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SIG"))\n'
        "  )\n"
    )


def _stitch(pcb: Path, floor: float):
    return run_stitch(
        pcb,
        ["GND"],
        via_size=VIA,
        drill=DRILL,
        clearance=CLEARANCE,
        dry_run=True,
        pad_edge_gap=floor,
    )


def _zone_hits(pcb: Path, result, floor: float) -> int:
    bboxes = find_all_pad_bboxes(parse_string(pcb.read_text()), smd_only=True)
    return sum(
        via_drill_at_pad_edge(
            _pad_copper_gap(v.via_x, v.via_y, v.via_x, v.via_y, b), v.drill, floor
        )
        for v in result.vias_added
        for b in bboxes
    )


def _pcb_with_pad_on_base_via(tmp_path: Path) -> tuple[Path, float]:
    """A board whose floor-0 stitch via sits 0.5 mm from a foreign SMD pad."""
    base = _stitch(_write(tmp_path), 0.0)
    assert len(base.vias_added) == 1
    v = base.vias_added[0]
    # Pad edge 0.5 mm east of the via centre (centre 0.7): copper-legal under
    # the 0.1 mm clearance, inside the 0.15 + 0.6 band.
    return _write(tmp_path, _foreign_smd(v.via_x + 0.7, v.via_y)), v.via_x


def test_control_without_floor_lands_in_the_zone(tmp_path):
    pcb, _ = _pcb_with_pad_on_base_via(tmp_path)
    result = _stitch(pcb, 0.0)
    assert result.vias_added
    assert _zone_hits(pcb, result, FLOOR) > 0, "control must reach the zone"


def test_stitch_with_floor_places_no_via_in_the_zone(tmp_path):
    pcb, _ = _pcb_with_pad_on_base_via(tmp_path)
    result = _stitch(pcb, FLOOR)
    assert result.vias_added, "a legal site outside the zone exists"
    assert _zone_hits(pcb, result, FLOOR) == 0


def test_floor_zero_disables_the_guard(tmp_path):
    pcb, _ = _pcb_with_pad_on_base_via(tmp_path)
    a = _stitch(pcb, 0.0)
    b = run_stitch(pcb, ["GND"], via_size=VIA, drill=DRILL, clearance=CLEARANCE, dry_run=True)
    assert [(v.via_x, v.via_y) for v in a.vias_added] == [(v.via_x, v.via_y) for v in b.vias_added]


def test_through_hole_pads_are_not_dam_pads(tmp_path):
    base = _stitch(_write(tmp_path), 0.0)
    v = base.vias_added[0]
    pcb = _write(tmp_path, _foreign_smd(v.via_x + 0.7, v.via_y, kind="thru_hole"))
    sexp = parse_string(pcb.read_text())
    assert find_all_pad_bboxes(sexp, smd_only=True) != []  # C1's SMD pads
    assert len(find_all_pad_bboxes(sexp)) == len(find_all_pad_bboxes(sexp, smd_only=True)) + 1


def test_blanket_and_thermal_skip_the_zone(tmp_path):
    pcb = _write(tmp_path, _foreign_smd(115.0, 115.0))
    blanket = run_blanket_stitch(
        pcb,
        ["GND"],
        via_size=VIA,
        drill=DRILL,
        clearance=CLEARANCE,
        spacing=0.5,
        dry_run=True,
        pad_edge_gap=FLOOR,
    )
    assert blanket.vias_added
    assert _zone_hits(pcb, blanket, FLOOR) == 0
    free = run_blanket_stitch(
        pcb,
        ["GND"],
        via_size=VIA,
        drill=DRILL,
        clearance=CLEARANCE,
        spacing=0.5,
        dry_run=True,
    )
    assert _zone_hits(pcb, free, FLOOR) > 0, "control: the grid reaches the zone"

    thermal = run_thermal_stitch(
        pcb,
        ["GND"],
        via_size=VIA,
        drill=DRILL,
        clearance=CLEARANCE,
        dry_run=True,
        pad_edge_gap=FLOOR,
    )
    assert _zone_hits(pcb, thermal, FLOOR) == 0


# -- strategy-level: every placer honours the guard ------------------------

PAD = PadInfo("U1", "1", 1, "GND", 0.0, 0.0, "F.Cu", 0.5, 0.5)
# A foreign 0.4 mm SMD pad east of the straight-line site (x = 0.75).
FOREIGN = (1.2, -0.2, 1.6, 0.2, 2, "rect", 0.2, 0.0)
GUARD = PadEdgeGuard([FOREIGN], FLOOR)


def _kw():
    return {
        "other_net_pads": [(1.4, 0.0, 0.2, 2)],
        "other_net_pad_bboxes": [FOREIGN],
        "via_drill": DRILL,
    }


def test_straight_placer_rejects_the_zone_site():
    free = calculate_via_stub(PAD, 0.5, VIA, [], CLEARANCE, **_kw())
    assert free is not None and GUARD.blocks(free[0], free[1], DRILL)
    guarded = calculate_via_stub(PAD, 0.5, VIA, [], CLEARANCE, pad_edge_guard=GUARD, **_kw())
    assert guarded is None or not GUARD.blocks(guarded[0], guarded[1], DRILL)


def test_dogleg_and_extended_placers_never_return_a_zone_site():
    for fn in (calculate_dogleg_via_position, calculate_extended_escape_position):
        pos = fn(PAD, 0.5, VIA, [], CLEARANCE, pad_edge_guard=GUARD, **_kw())
        assert pos is None or not GUARD.blocks(pos[0], pos[1], DRILL), fn.__name__


def test_guard_is_off_without_a_floor():
    assert build_pad_edge_guard(parse_string(TWO_LAYER_GND_ZONE_PCB), 0.0) is None
    assert not PadEdgeGuard([FOREIGN], 0.0).blocks(0.75, 0.0, DRILL)
    # Inside the copper is the via-in-pad question, not this predicate's.
    assert not GUARD.blocks(1.4, 0.0, DRILL)

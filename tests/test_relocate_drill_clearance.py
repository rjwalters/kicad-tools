"""Unit tests for the generic hole-to-hole via relocation pass (issue #4408).

These tests exercise
:func:`kicad_tools.drc.relocate_drill_clearance.relocate_drill_clearance` on
synthetic boards -- no routing, no board recipe -- so they run in milliseconds
and pin the safety-critical behaviour:

* the middle via of a 0.5 mm-pitch in-pad stack (the board-04 LQFP-48 west
  escape geometry) is relocated onto a clearance-safe location so both drill
  pairs clear the floor;
* the pass reads the **active** manufacturer floor (a wider floor relocates a
  pair a narrower floor tolerates -- it is not hardcoded to 0.5, and it is not
  gated on ``via_in_pad_supported``);
* a boxed-in via is left in place and reported (the safety invariant: the pass
  never mis-places a via into a fresh violation).
"""

from __future__ import annotations

import dataclasses
import math

from kicad_tools.drc.relocate_drill_clearance import (
    _find_target,
    _violating_pairs,
    relocate_drill_clearance,
)
from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB

# The board-04 geometry: 0.3 mm micro-via pad / 0.15 mm drill.
_SIZE = 0.3
_DRILL = 0.15


def _tier1_rules():
    """jlcpcb-tier1 design rules (min_hole_to_hole 0.5 mm, via-in-pad supported)."""
    return get_profile("jlcpcb-tier1").get_design_rules()


def _stack_board() -> PCB:
    """Three in-pad micro-vias stacked at the 0.5 mm pin pitch.

    Mirrors the board-04 OSC_OUT / NRST / GND west-escape cluster: the middle
    via's drill is 0.350 mm edge-to-edge from each neighbour (< 0.5 mm floor).
    The middle via carries an east-going escape track, so it can slide onto its
    own escape node exactly as #4017 did by hand.
    """
    pcb = PCB.create(width=40.0, height=40.0)
    pcb.add_via(20.0, 20.0, size=_SIZE, drill=_DRILL, net="OSC_OUT")
    pcb.add_via(20.0, 20.5, size=_SIZE, drill=_DRILL, net="NRST")
    pcb.add_via(20.0, 21.0, size=_SIZE, drill=_DRILL, net="GND")
    # East escape for the middle (NRST) via -> gives it a slide direction/node.
    pcb.add_trace((20.0, 20.5), (20.5, 20.5), width=0.127, layer="B.Cu", net="NRST")
    return pcb


def test_relocates_middle_via_of_pitch_stack() -> None:
    """The middle via of a 0.5 mm stack is relocated; both pairs clear the floor."""
    pcb = _stack_board()
    rules = _tier1_rules()

    before = _violating_pairs(list(pcb.vias), rules.min_hole_to_hole_mm)
    assert len(before) == 2, "expected the two 0.350 mm pairs to violate the 0.5 mm floor"

    result = relocate_drill_clearance(pcb, rules)

    assert result.changed
    assert len(result.moved) == 1
    assert result.moved[0].net_name == "NRST"
    assert not result.unresolved

    # Post-move: no via/via pair is below the floor.
    after = _violating_pairs(list(pcb.vias), rules.min_hole_to_hole_mm)
    assert after == [], f"relocation left {len(after)} hole-to-hole pair(s) unresolved"

    # And the relocated via genuinely cleared both neighbours by >= the floor.
    moved = result.moved[0]
    for via in pcb.vias:
        if via.uuid == moved.uuid:
            continue
        gap = (
            math.hypot(via.position[0] - moved.new_x, via.position[1] - moved.new_y)
            - _DRILL / 2.0
            - via.drill / 2.0
        )
        assert gap >= rules.min_hole_to_hole_mm - 1e-6


def test_relocated_via_stays_connected_via_stub() -> None:
    """The relocated via re-bonds to its old location with connectivity stubs."""
    pcb = _stack_board()
    result = relocate_drill_clearance(pcb, _tier1_rules())

    moved = result.moved[0]
    # A signal via that slides off its pad must stub on every connected layer so
    # the pad / route stays electrically bonded to the new via location.
    assert moved.stub_layers, "expected connectivity stub layer(s) for the moved signal via"

    # There is now same-net copper running old -> new (either the pre-existing
    # escape leg or an appended stub) so the move preserved connectivity.
    old = (moved.old_x, moved.old_y)
    new = (moved.new_x, moved.new_y)
    bonded = False
    for seg in pcb.segments_in_net(moved.net):
        endpoints = {seg.start, seg.end}
        if any(math.hypot(p[0] - old[0], p[1] - old[1]) < 1e-3 for p in endpoints) and any(
            math.hypot(p[0] - new[0], p[1] - new[1]) < 1e-3 for p in endpoints
        ):
            bonded = True
            break
    assert bonded, "no same-net segment bonds the old via location to the new one"


def test_pass_reads_active_floor_not_hardcoded() -> None:
    """The relocation is driven by the active ``min_hole_to_hole_mm`` floor.

    The same 0.350 mm-gap stack is a violation at a 0.5 mm floor but legal at a
    0.3 mm floor -- so a narrow-floor run must be a no-op and a wide-floor run
    must relocate.  This proves the pass reads the profile's floor rather than a
    hardcoded 0.5 (and is not gated on ``via_in_pad_supported``).
    """
    tier1 = _tier1_rules()

    narrow = dataclasses.replace(tier1, min_hole_to_hole_mm=0.30)
    pcb_narrow = _stack_board()
    result_narrow = relocate_drill_clearance(pcb_narrow, narrow)
    assert not result_narrow.changed, "0.350 mm gap is legal at a 0.30 mm floor -- must not move"

    wide = dataclasses.replace(tier1, min_hole_to_hole_mm=0.50)
    pcb_wide = _stack_board()
    result_wide = relocate_drill_clearance(pcb_wide, wide)
    assert result_wide.changed, "0.350 mm gap violates a 0.50 mm floor -- must relocate"


def test_dry_run_reports_without_mutating() -> None:
    """``dry_run`` reports the move without touching the board."""
    pcb = _stack_board()
    original = [(v.position[0], v.position[1]) for v in pcb.vias]

    result = relocate_drill_clearance(pcb, _tier1_rules(), dry_run=True)

    assert result.changed  # it WOULD move a via
    assert [(v.position[0], v.position[1]) for v in pcb.vias] == original


def test_net_scoping_restricts_moves() -> None:
    """A ``nets`` filter restricts which vias the pass may relocate."""
    pcb = _stack_board()
    # NRST (the only via with an escape) is excluded, so no move is possible
    # without introducing a violation elsewhere -> the pass leaves it and reports.
    result = relocate_drill_clearance(pcb, _tier1_rules(), nets={"GND", "OSC_OUT"})

    assert all(m.net_name in {"GND", "OSC_OUT"} for m in result.moved)


def _boxed_target_board() -> PCB:
    """A via surrounded by other-net vias in all 8 ladder directions.

    Every candidate location the ladder proposes lands on top of a blocking
    drill, so no clearance-legal off-position exists.
    """
    from kicad_tools.drc.relocate_drill_clearance import _PLANE_DIRECTIONS

    pcb = PCB.create(width=40.0, height=40.0)
    cx, cy = 20.0, 20.0
    pcb.add_via(cx, cy, size=_SIZE, drill=_DRILL, net="TARGET")
    # Ring radius chosen so blockers do not violate each other (0.65 mm chord ->
    # 0.5 mm gap, exactly the floor) but block every candidate direction.
    ring_r = 0.85
    for i, (dx, dy) in enumerate(_PLANE_DIRECTIONS):
        pcb.add_via(cx + dx * ring_r, cy + dy * ring_r, size=_SIZE, drill=_DRILL, net=f"BLK{i}")
    return pcb


def test_boxed_in_target_has_no_clearance_legal_location() -> None:
    """``_find_target`` returns None when every ladder candidate is blocked."""
    from kicad_tools.cli.relocate_in_pad_vias import _collect_smd_pads_by_net, _collect_tht_pads

    pcb = _boxed_target_board()
    rules = _tier1_rules()
    # The target is the centre via (programmatic Via objects carry only a net
    # number, so select by position rather than name).
    target = min(pcb.vias, key=lambda v: math.hypot(v.position[0] - 20.0, v.position[1] - 20.0))

    pads_by_net = _collect_smd_pads_by_net(pcb)
    tht_pads = _collect_tht_pads(pcb)

    result = _find_target(
        pcb,
        target,
        None,  # no escape node
        pads_by_net,
        tht_pads,
        rules.min_clearance_mm,
        rules.min_hole_to_hole_mm,
    )
    assert result is None


def test_dense_field_never_introduces_a_new_violation() -> None:
    """The pass is clearance-safe on a crowded field (safety invariant).

    A dense via grid has interior vias that are genuinely boxed in (reported
    unresolved) and edge vias that can escape to clear spots (relocated).  The
    invariant under test: whatever the pass does, it must NEVER increase the
    violation count -- every move is validated against ``_check_clearance`` and
    a via with no clearance-legal location is left in place and reported.
    """
    from kicad_tools.cli.relocate_in_pad_vias import (
        _check_clearance,
        _collect_smd_pads_by_net,
        _collect_tht_pads,
    )

    pcb = PCB.create(width=40.0, height=40.0)
    # 5x5 grid at 0.35 mm spacing (0.20 mm drill gap << 0.5 mm floor).
    n = 0
    for gx in range(5):
        for gy in range(5):
            pcb.add_via(
                18.0 + gx * 0.35,
                18.0 + gy * 0.35,
                size=_SIZE,
                drill=_DRILL,
                net=f"N{n}",
            )
            n += 1

    rules = _tier1_rules()
    before = _violating_pairs(list(pcb.vias), rules.min_hole_to_hole_mm)
    assert before, "sanity: the dense grid must start with violations"

    result = relocate_drill_clearance(pcb, rules)

    # Safety invariant: the pass never makes the board worse.
    after = _violating_pairs(list(pcb.vias), rules.min_hole_to_hole_mm)
    assert len(after) <= len(before)

    # Interior vias are boxed in -> reported, never mis-placed.
    assert result.unresolved

    # Every via the pass moved is itself clearance-safe at its new location.
    pads_by_net = _collect_smd_pads_by_net(pcb)
    tht_pads = _collect_tht_pads(pcb)
    by_uuid = {v.uuid: v for v in pcb.vias}
    for m in result.moved:
        via = by_uuid[m.uuid]
        reason = _check_clearance(
            pcb,
            via,
            via.position[0],
            via.position[1],
            pads_by_net,
            tht_pads,
            rules.min_clearance_mm,
            rules.min_hole_to_hole_mm,
        )
        assert reason is None, f"moved via {m.uuid[:8]} is not clearance-safe: {reason}"

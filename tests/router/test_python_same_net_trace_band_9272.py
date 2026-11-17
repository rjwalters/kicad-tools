"""Pure-Python same-net via-vs-own-trace slit-band guard (Issue #9272).

#9208 made the C++ pathfinder refuse a via seat whose annulus lies in the fab
``(0, same_net_trace_gap_band_edge_mm)`` band beside the via's OWN net's
stored trace.  The pure-Python engines never read that scalar, so on a host
without the C++ extension (or ``--backend python``) the same seat was
accepted -- the pad-band twin fired, the trace band did not.  This suite pins
the two Python via-seat gates that now consume it, mirroring
``test_cpp_same_net_segment_gap_parity_9208.py``:

* ``EscapeRouter._can_place_via`` (own-net tracks it is handed);
* the lattice via legality ``LatticePathfinder._via_ok_at`` (own-net
  COMMITTED segments, any layer).

The shared predicate is ``via_clearance.same_net_trace_band_clear``.  It adds
``SAME_NET_BAND_WRITE_SLACK_MM`` to the edge (positions are written at three
decimals), so the exact-edge seat the C++ guard accepts is refused here by
that slack; seats are therefore probed clear of ``edge`` by more than the
slack.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.core.geometry import point_to_segment_distance
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.escape import EscapeRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.via_clearance import (
    SAME_NET_BAND_WRITE_SLACK_MM,
    via_annulus_trace_band_ok,
)

NET = 7
FOREIGN_NET = 8
EDGE = 0.25
VIA_DIAMETER = 0.5  # annulus half 0.25
VIA_DRILL = 0.25
TRACE_WIDTH = 0.25  # trace half 0.125 -> gap == centre distance - 0.375

# A 4 mm horizontal own-net trace centred at (10, 10).
SEG = (8.0, 10.0, 12.0, 10.0)

# (dy from the trace centreline, accepted?)
VERDICTS = [
    (0.0, True),  # centred on the trace: overlap, one copper union
    (0.25, True),  # annulus overlaps the trace body: union
    (0.375, True),  # gap == 0: touching union
    (0.5, False),  # gap 0.125 inside (0, 0.25): slit
    (0.6, False),  # gap 0.225 still inside
    (0.7, True),  # gap 0.325 beyond edge + slack
    (1.0, True),
]


def _rules(edge: float | None) -> DesignRules:
    return DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=0.15,
        via_drill=VIA_DRILL,
        via_diameter=VIA_DIAMETER,
        via_clearance=0.15,
        grid_resolution=0.125,
        same_net_trace_gap_band_edge_mm=edge,
    )


# ---------------------------------------------------------------------------
# Escape ladder: EscapeRouter._can_place_via
# ---------------------------------------------------------------------------


def _router(edge: float | None) -> EscapeRouter:
    rules = _rules(edge)
    grid = RoutingGrid(20, 20, rules, origin_x=0, origin_y=0)
    return EscapeRouter(grid, rules, component_holes=())


def _segment(net: int = NET, seg=SEG, layer: Layer = Layer.F_CU) -> Segment:
    x1, y1, x2, y2 = seg
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=TRACE_WIDTH, layer=layer, net=net)


@pytest.mark.parametrize(("dy", "expected"), VERDICTS)
def test_escape_via_seat_verdicts(dy, expected):
    router = _router(EDGE)
    assert router._can_place_via(10.0, 10.0 + dy, net=NET, foreign_tracks=[_segment()]) is expected


def test_escape_guard_off_when_edge_unresolved():
    for edge in (None, 0.0):
        router = _router(edge)
        assert router._can_place_via(10.0, 10.5, net=NET, foreign_tracks=[_segment()])


def test_escape_foreign_net_trace_in_band_is_not_this_guards_business():
    """A foreign trace at the same distance is clearance's job, not the band's."""
    router = _router(EDGE)
    on = router._can_place_via(10.0, 10.5, net=NET, foreign_tracks=[_segment(net=FOREIGN_NET)])
    off = _router(0.0)._can_place_via(
        10.0, 10.5, net=NET, foreign_tracks=[_segment(net=FOREIGN_NET)]
    )
    assert on == off


def test_escape_trace_on_other_layer_is_held_to_the_band():
    """The annulus exists on every layer, so a bottom-layer own trace counts."""
    router = _router(EDGE)
    assert not router._can_place_via(
        10.0, 10.5, net=NET, foreign_tracks=[_segment(layer=Layer.B_CU)]
    )


def test_escape_via_at_the_end_of_a_same_net_stub():
    """Board 04's GND shape: a via beside the END cap of an own-net stub."""
    stub = (123.5, 85.5, 124.65, 85.5)
    rules = _rules(EDGE)
    grid = RoutingGrid(140, 100, rules, origin_x=100, origin_y=70)
    router = EscapeRouter(grid, rules, component_holes=())
    seg = _segment(seg=stub)
    # centre distance 0.5 from the end point -> gap 0.125: in band.
    assert not router._can_place_via(124.65, 86.0, net=NET, foreign_tracks=[seg])
    # moved out beyond edge + slack: accepted.
    assert router._can_place_via(124.65, 86.2, net=NET, foreign_tracks=[seg])


# ---------------------------------------------------------------------------
# Lattice via legality: LatticePathfinder._via_ok_at
# ---------------------------------------------------------------------------

_OUTLINE = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]


def _lattice(edge: float | None, layers: int = 2) -> tuple[LatticePathfinder, object]:
    far_pad = Pad(
        x=2.0,
        y=2.0,
        width=0.6,
        height=0.6,
        net=FOREIGN_NET,
        net_name="OTHER",
        layer=Layer.F_CU,
        ref="U1",
        pin="1",
    )
    stack = LayerStack.two_layer() if layers == 2 else LayerStack.four_layer_sig_gnd_pwr_sig()
    pf = LatticePathfinder(_OUTLINE, [far_pad], _rules(edge), stack)
    pf.build()
    return pf, pf._fresh_committed()


@pytest.mark.parametrize(("dy", "expected"), VERDICTS)
def test_lattice_via_seat_verdicts(dy, expected):
    pf, committed = _lattice(EDGE)
    committed.add_run(0, [(SEG[0], SEG[1]), (SEG[2], SEG[3])], NET, TRACE_WIDTH / 2, 0.15)
    assert pf._via_ok_at((10.0, 10.0 + dy), NET, committed) is expected


def test_lattice_guard_off_when_edge_unresolved():
    for edge in (None, 0.0):
        pf, committed = _lattice(edge)
        committed.add_run(0, [(SEG[0], SEG[1]), (SEG[2], SEG[3])], NET, TRACE_WIDTH / 2, 0.15)
        assert pf._via_ok_at((10.0, 10.5), NET, committed)


def test_lattice_foreign_net_committed_trace_is_not_banded():
    pf, committed = _lattice(EDGE)
    committed.add_run(0, [(SEG[0], SEG[1]), (SEG[2], SEG[3])], FOREIGN_NET, TRACE_WIDTH / 2, 0.15)
    # 0.5 off a foreign trace: gap 0.125 < via clearance 0.15 -> clearance refuses
    # it regardless of the band; 0.6 clears clearance, and the band (a same-net
    # guard) must not refuse it.
    pf_off, committed_off = _lattice(0.0)
    committed_off.add_run(
        0, [(SEG[0], SEG[1]), (SEG[2], SEG[3])], FOREIGN_NET, TRACE_WIDTH / 2, 0.15
    )
    for dy in (0.5, 0.6, 0.7):
        assert pf._via_ok_at((10.0, 10.0 + dy), NET, committed) == pf_off._via_ok_at(
            (10.0, 10.0 + dy), NET, committed_off
        )


def test_lattice_trace_on_other_layer_is_held_to_the_band():
    pf, committed = _lattice(EDGE)
    committed.add_run(1, [(SEG[0], SEG[1]), (SEG[2], SEG[3])], NET, TRACE_WIDTH / 2, 0.15)
    assert not pf._via_ok_at((10.0, 10.5), NET, committed)


def test_lattice_via_at_the_end_of_a_same_net_stub():
    pf, committed = _lattice(EDGE)
    committed.add_run(0, [(8.0, 10.0), (9.15, 10.0)], NET, TRACE_WIDTH / 2, 0.15)
    assert not pf._via_ok_at((9.15, 10.5), NET, committed)
    assert pf._via_ok_at((9.15, 10.7), NET, committed)


def test_lattice_only_committed_segments_count():
    """No committed copper -> nothing to band against (route-in-progress is #9273)."""
    pf, committed = _lattice(EDGE)
    assert pf._via_ok_at((10.0, 10.5), NET, committed)


# ---------------------------------------------------------------------------
# The shared helper, and parity with the C++ guard
# ---------------------------------------------------------------------------


def test_helper_disabled_and_empty_inputs_accept():
    seg = (*SEG, TRACE_WIDTH)
    assert via_annulus_trace_band_ok(10.0, 10.5, VIA_DIAMETER, [seg], 0.0)
    assert via_annulus_trace_band_ok(10.0, 10.5, VIA_DIAMETER, [], EDGE)
    assert not via_annulus_trace_band_ok(10.0, 10.5, VIA_DIAMETER, [seg], EDGE)


def test_helper_zero_length_segment_is_annulus_vs_annulus():
    dot = (10.0, 10.0, 10.0, 10.0, TRACE_WIDTH)
    for dy, expected in ((0.25, True), (0.375, True), (0.5, False), (0.7, True), (1.0, True)):
        assert via_annulus_trace_band_ok(10.0, 10.0 + dy, VIA_DIAMETER, [dot], EDGE) is expected


def _exact_gap(x, y, seg) -> float:
    return point_to_segment_distance(x, y, *seg[:4]) - VIA_DIAMETER / 2 - TRACE_WIDTH / 2


@pytest.mark.parametrize("angle_deg", [0, 45, 90, 135, 30, 60])
def test_python_helper_matches_exact_gap_region_over_a_fan(angle_deg):
    """Swept seats around a (diagonal) trace follow the exact-gap two-region rule."""
    half = 2.0
    ca, sa = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    seg = (10.0 - half * ca, 10.0 - half * sa, 10.0 + half * ca, 10.0 + half * sa)
    checked = in_band = 0
    for i in range(-24, 25):
        for j in range(-12, 13):
            x, y = 10.0 + i * 0.125, 10.0 + j * 0.125
            gap = _exact_gap(x, y, seg)
            # Skip seats within the polygon/slack noise of a region boundary.
            if min(abs(gap), abs(gap - EDGE)) < SAME_NET_BAND_WRITE_SLACK_MM + 1e-3:
                continue
            expected = not (0.0 < gap < EDGE)
            got = via_annulus_trace_band_ok(x, y, VIA_DIAMETER, [(*seg, TRACE_WIDTH)], EDGE)
            assert got is expected, (angle_deg, x, y, gap)
            checked += 1
            in_band += not expected
    assert checked > 500
    assert in_band > 20, "the fan must exercise the band"


@pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not available")
@pytest.mark.parametrize("dy", [0.0, 0.25, 0.375, 0.5, 0.6, 0.7, 1.0])
def test_python_gates_agree_with_the_cpp_guard(dy):
    """Same fixture, same verdict: C++ ``is_via_blocked`` vs both Python gates."""
    from kicad_tools.router import router_cpp

    res = 0.125
    grid = router_cpp.Grid3D(160, 160, 2, res, 0.0, 0.0)
    grid.add_stored_segment(*SEG, TRACE_WIDTH, 0, NET)
    rules = router_cpp.DesignRules()
    rules.trace_width = TRACE_WIDTH
    rules.trace_clearance = 0.15
    rules.via_clearance = 0.15
    rules.via_drill = VIA_DRILL
    rules.via_diameter = VIA_DIAMETER
    rules.allow_smd_vias = True
    rules.same_net_trace_gap_band_edge_mm = EDGE
    pf = router_cpp.Pathfinder(grid, rules, False)
    cpp_ok = not pf.is_via_blocked(round(10.0 / res), round((10.0 + dy) / res), NET, True)

    esc_ok = _router(EDGE)._can_place_via(10.0, 10.0 + dy, net=NET, foreign_tracks=[_segment()])
    lpf, committed = _lattice(EDGE)
    committed.add_run(0, [(SEG[0], SEG[1]), (SEG[2], SEG[3])], NET, TRACE_WIDTH / 2, 0.15)
    lat_ok = lpf._via_ok_at((10.0, 10.0 + dy), NET, committed)
    assert esc_ok == lat_ok == cpp_ok

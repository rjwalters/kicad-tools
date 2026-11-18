"""C++ same-net via-annulus-vs-own-trace slit-band guard parity (Issue #9208).

Board 06's last USB2_D- ``physical_copper_gap`` finding is a via ANNULUS
(d0.6) skirting its own net's trace (w0.25) inside the trace population's
0.1016 mm slit band (``fab_band: trace``).  #8994 mirrored the annulus-vs-own-
*pad* band into the C++ via predicates; this suite pins the annulus-vs-own-
*segment* sibling (``DesignRules.same_net_trace_gap_band_edge_mm`` consumed by
``Pathfinder::is_via_blocked_diag`` and the coupled ``smd_vias_ok`` gate):

* the C++ verdict equals the Python predicate's regions over in-band /
  overlapping / touching-union / beyond-edge / exact-edge seats -- the Python
  oracle is ``via_clearance.same_net_segment_trace_gap_acceptable`` (the
  trace-population wrapper of the shared predicate, tangent accepted; an
  axis-aligned segment is a zero-thickness box swept by ``via_r + w/2``)
  and, for diagonals, the same region test on the exact point-to-segment gap;
* only the via's OWN net's segments count (a foreign net's trace is
  clearance's job);
* a through via's annulus exists on every layer, so an own-net trace on ANY
  layer is held to the band (the checker grades each layer separately);
* the guard is inert when no stackup edge was resolved (never a literal).
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.core.geometry import point_to_segment_distance
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.via_clearance import same_net_segment_trace_gap_acceptable

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)

# Dyadic lattice and dimensions so exact-edge probes are exact in float32 AND
# float64 (the 1e-9 slack in both predicates then decides, not rounding).
RES = 0.125
COLS = ROWS = 160  # 20 x 20 mm
NET = 7
FOREIGN_NET = 8

EDGE = 0.25
VIA_DIAMETER = 0.5  # annulus half 0.25
VIA_DRILL = 0.25
TRACE_WIDTH = 0.25  # trace half 0.125 -> gap == dist - 0.375


def _cpp_pathfinder(edge_mm: float, segments, layers: int = 2):
    from kicad_tools.router import router_cpp

    grid = router_cpp.Grid3D(COLS, ROWS, layers, RES, 0.0, 0.0)
    for x1, y1, x2, y2, width, layer_idx, net in segments:
        grid.add_stored_segment(x1, y1, x2, y2, width, layer_idx, net)
    rules = router_cpp.DesignRules()
    rules.trace_width = TRACE_WIDTH
    rules.trace_clearance = 0.15
    rules.via_clearance = 0.15
    rules.via_drill = VIA_DRILL
    rules.via_diameter = VIA_DIAMETER
    rules.allow_smd_vias = True
    rules.same_net_trace_gap_band_edge_mm = edge_mm
    return router_cpp.Pathfinder(grid, rules, False)


def _gx(x_mm: float) -> int:
    gx = round(x_mm / RES)
    assert abs(gx * RES - x_mm) < 1e-9, f"probe {x_mm} off the {RES} lattice"
    return gx


def _band_rejects(edge_mm: float, segments, x_mm: float, y_mm: float, layers: int = 2) -> bool:
    """The BAND's contribution to the C++ via verdict at one world seat."""
    pf_on = _cpp_pathfinder(edge_mm, segments, layers)
    pf_off = _cpp_pathfinder(0.0, segments, layers)
    gx, gy = _gx(x_mm), _gx(y_mm)
    assert not pf_off.is_via_blocked(gx, gy, NET, True), "guard-off baseline must leave it open"
    return pf_on.is_via_blocked(gx, gy, NET, True)


def _oracle_ok(x_mm, y_mm, seg, edge_mm=EDGE) -> bool:
    """Python verdict: annulus at (x, y) vs the trace ``seg`` (exact gap)."""
    x1, y1, x2, y2, width = seg[:5]
    gap = point_to_segment_distance(x_mm, y_mm, x1, y1, x2, y2) - VIA_DIAMETER / 2 - width / 2
    return not (1e-9 < gap < edge_mm - 1e-9)


def _python_axis_aligned_ok(x_mm, y_mm, seg) -> bool:
    """Same verdict through the trace-population Python predicate (axis-aligned seg)."""
    x1, y1, x2, y2, width = seg[:5]
    box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    return same_net_segment_trace_gap_acceptable(
        (x_mm, y_mm), (x_mm, y_mm), VIA_DIAMETER / 2 + width / 2, [box], EDGE
    )


# A 4 mm horizontal own-net trace centred on the board centre.
HORIZ = (8.0, 10.0, 12.0, 10.0, TRACE_WIDTH, 0, NET)


@requires_cpp
@pytest.mark.parametrize(
    ("dy", "expected"),
    [
        (0.0, True),  # centred on the trace: overlap, one copper union
        (0.25, True),  # annulus overlaps the trace body: union
        (0.375, True),  # gap == 0 exactly: touching union, accepted
        (0.5, False),  # gap 0.125 inside (0, 0.25): slit
        (0.625, True),  # gap == edge exactly: accepted (open band)
        (0.75, True),  # gap 0.375 beyond the edge: clear
    ],
)
def test_horizontal_segment_verdicts_match_python(dy, expected):
    """In-band rejected; overlap / touching / beyond-edge / exact-edge accepted."""
    y_mm = 10.0 + dy
    assert _python_axis_aligned_ok(10.0, y_mm, HORIZ) is expected, "oracle self-check"
    assert _oracle_ok(10.0, y_mm, HORIZ) is expected, "geometry oracle self-check"
    assert _band_rejects(EDGE, [HORIZ], 10.0, y_mm) is (not expected)


@requires_cpp
def test_segment_end_cap_region_matches_python():
    """Beside the segment END (round cap): distance is to the endpoint."""
    for dx in (0.25, 0.375, 0.5, 0.625, 0.75):
        x_mm = 12.0 + dx
        expected = _oracle_ok(x_mm, 10.0, HORIZ)
        assert _python_axis_aligned_ok(x_mm, 10.0, HORIZ) is expected
        assert _band_rejects(EDGE, [HORIZ], x_mm, 10.0) is (not expected), dx


@requires_cpp
@pytest.mark.parametrize("angle_deg", [0, 45, 90, 135, 30, 60])
def test_segment_fan_parity(angle_deg):
    """Swept seats around a (diagonal) trace match the exact-gap verdict."""
    half = 2.0
    cx = cy = 10.0
    ca, sa = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    seg = (cx - half * ca, cy - half * sa, cx + half * ca, cy + half * sa, TRACE_WIDTH, 0, NET)
    checked = in_band = 0
    for i in range(-24, 25):
        for j in range(-12, 13):
            x_mm = cx + i * RES
            y_mm = cy + j * RES
            gap = (
                point_to_segment_distance(x_mm, y_mm, *seg[:4]) - VIA_DIAMETER / 2 - TRACE_WIDTH / 2
            )
            # Skip seats whose exact gap sits within float32 noise of a band
            # edge: both sides are right there, they just round differently.
            if min(abs(gap), abs(gap - EDGE)) < 1e-4:
                continue
            expected = _oracle_ok(x_mm, y_mm, seg)
            assert _band_rejects(EDGE, [seg], x_mm, y_mm) is (not expected), (
                angle_deg,
                x_mm,
                y_mm,
                gap,
            )
            checked += 1
            in_band += not expected
    assert checked > 500
    assert in_band > 20, "the fan must exercise the band"


@requires_cpp
def test_zero_length_segment_is_an_annulus_vs_annulus_form():
    """A degenerate segment measures point-to-point (the via-stub form)."""
    dot = (10.0, 10.0, 10.0, 10.0, TRACE_WIDTH, 0, NET)
    # centre distance d -> gap = d - 0.25 - 0.125
    for dy, expected in ((0.25, True), (0.375, True), (0.5, False), (0.625, True), (1.0, True)):
        assert _oracle_ok(10.0, 10.0 + dy, dot) is expected
        assert _band_rejects(EDGE, [dot], 10.0, 10.0 + dy) is (not expected), dy


# ---------------------------------------------------------------------------
# Net / layer scope and the unresolved-edge contract
# ---------------------------------------------------------------------------


@requires_cpp
def test_foreign_net_segment_in_band_does_not_refuse():
    """A foreign net's trace in the band is clearance's job, not this guard's."""
    foreign = (8.0, 10.0, 12.0, 10.0, TRACE_WIDTH, 0, FOREIGN_NET)
    pf_off = _cpp_pathfinder(0.0, [foreign])
    pf_on = _cpp_pathfinder(EDGE, [foreign])
    gx, gy = _gx(10.0), _gx(10.5)
    assert pf_on.is_via_blocked(gx, gy, NET, True) == pf_off.is_via_blocked(gx, gy, NET, True)


@requires_cpp
def test_far_segment_is_ignored():
    """A trace well beyond ``annulus + edge`` never reaches the seat."""
    far = (2.0, 2.0, 4.0, 2.0, TRACE_WIDTH, 0, NET)
    assert _band_rejects(EDGE, [far], 10.0, 10.5) is False


@requires_cpp
@pytest.mark.parametrize("layer_idx", [0, 1, 2, 3])
def test_through_via_annulus_is_held_to_the_band_on_every_layer(layer_idx):
    """The annulus exists on each layer of a through via, so an own-net trace
    on ANY layer is graded against it (``kct check`` measures per layer)."""
    seg = (8.0, 10.0, 12.0, 10.0, TRACE_WIDTH, layer_idx, NET)
    assert _band_rejects(EDGE, [seg], 10.0, 10.5, layers=4) is True  # in band
    assert _band_rejects(EDGE, [seg], 10.0, 10.375, layers=4) is False  # union
    assert _band_rejects(EDGE, [seg], 10.0, 10.75, layers=4) is False  # clear


@requires_cpp
def test_unresolved_edge_leaves_guard_inert():
    """Edge unresolved (None -> 0.0): every band seat stays open (#8974 AC-3)."""
    for y_mm in (10.5, 10.625, 10.4375 + 0.0625):
        pf = _cpp_pathfinder(0.0, [HORIZ])
        assert not pf.is_via_blocked(_gx(10.0), _gx(y_mm), NET, True), y_mm


@requires_cpp
def test_pad_edge_alone_does_not_enable_the_trace_guard():
    """The two populations are independent knobs (never widen the pad edge)."""
    from kicad_tools.router import router_cpp

    grid = router_cpp.Grid3D(COLS, ROWS, 2, RES, 0.0, 0.0)
    grid.add_stored_segment(*HORIZ[:4], TRACE_WIDTH, 0, NET)
    rules = router_cpp.DesignRules()
    rules.via_drill = VIA_DRILL
    rules.via_diameter = VIA_DIAMETER
    rules.via_clearance = 0.15
    rules.trace_clearance = 0.15
    rules.allow_smd_vias = True
    rules.same_net_pad_gap_band_edge_mm = 0.15  # pad edge only
    pf = router_cpp.Pathfinder(grid, rules, False)
    assert not pf.is_via_blocked(_gx(10.0), _gx(10.5), NET, True)


@requires_cpp
def test_python_rules_field_defaults_off_and_marshals():
    """``DesignRules.same_net_trace_gap_band_edge_mm`` defaults to ``None``."""
    from kicad_tools.router.rules import DesignRules

    assert DesignRules().same_net_trace_gap_band_edge_mm is None
    assert DesignRules(same_net_trace_gap_band_edge_mm=0.1016).same_net_trace_gap_band_edge_mm == (
        pytest.approx(0.1016)
    )


def test_trace_edge_resolver_reads_the_trace_spacing_band():
    """The edge is resolved from the dfm table, never a literal (jlcpcb 4L/1oz)."""
    from kicad_tools.cli.stitch_cmd import (
        _resolve_mfr_pad_gap_edge,
        _resolve_mfr_trace_gap_edge,
    )

    assert _resolve_mfr_trace_gap_edge("jlcpcb", 4, 1.0) == pytest.approx(0.1016)
    assert _resolve_mfr_pad_gap_edge("jlcpcb", 4, 1.0) == pytest.approx(0.15)
    assert _resolve_mfr_trace_gap_edge("no-such-fab", 4, 1.0) is None

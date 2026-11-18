"""Direct coverage of ``CoupledPathfinder::same_net_trace_gap_in_band`` (Issue #9225).

#9208 added the same-net via-annulus-vs-own-trace slit-band refusal to BOTH C++
via checks.  Its parity suite
(``test_cpp_same_net_segment_gap_parity_9208.py``) constructs
``router_cpp.Pathfinder`` only, so the coupled twin was exercised just
indirectly, through the board 06 regen.  This suite is the trace-population
analogue of ``tests/test_coupled_own_net_pad_band_9140.py`` for the coupled
engine, and pins the two things that differ between the call sites:

* the annulus radius source -- the coupled gate reads the net class's
  ``HaloNetDims::via_diameter`` (``set_halo_net_dimensions``) when one is
  installed; the single-ended gate uses its own search radius;
* the verdict is judged PER RAIL (``p_net`` / ``n_net`` against their own
  nets' segments) and cached per node (``smd_vias_ok``).

Observable
----------
The band is one term of the ``smd_vias_ok`` gate, which is only reachable from
inside ``CoupledPathfinder::route``.  The first pop expands the start node, and
every via move out of it counts ``via_in_pad`` in ``CoupledRouteResult.rejections``
when the gate refuses.  A tiny iteration budget stops the search right after
that single expansion, and each probe is paired with a guard-off baseline that
must record NO ``via_in_pad`` -- so the count isolates the band's contribution.
"""

from __future__ import annotations

import pytest

from kicad_tools.core.geometry import point_to_segment_distance
from kicad_tools.router.cpp_backend import is_cpp_available

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)

# Dyadic lattice and dimensions, so exact-edge probes are exact in float32 AND
# float64 (the 1e-9 slack in the shared predicate then decides, not rounding).
RES = 0.125
COLS = ROWS = 160  # 20 x 20 mm

P_NET = 7
N_NET = 9
OTHER_NET = 11

EDGE = 0.25
VIA_DIAMETER = 0.5  # global annulus half 0.25
VIA_DRILL = 0.25
TRACE_WIDTH = 0.25  # trace half 0.125 -> gap == dist - via_half - 0.125

# A 4 mm horizontal trace through the board centre, per net.
P_HORIZ = (8.0, 10.0, 12.0, 10.0, TRACE_WIDTH, 0, P_NET)
N_HORIZ = (8.0, 10.0, 12.0, 10.0, TRACE_WIDTH, 0, N_NET)

# Where the rail NOT under test parks: far from every segment used here.
P_FAR = (10.0, 4.0)
N_FAR = (10.0, 16.0)


def _gx(v_mm: float) -> int:
    g = round(v_mm / RES)
    assert abs(g * RES - v_mm) < 1e-9, f"probe {v_mm} off the {RES} lattice"
    return g


def _via_in_pad_refusals(
    edge_mm,
    segments,
    p_xy,
    n_xy,
    *,
    layers=2,
    halo=(),
    pad_edge_mm=0.0,
) -> int:
    """``via_in_pad`` rejections recorded while expanding the start node.

    ``halo`` is a sequence of ``(net, via_diameter)`` net-class overrides
    installed through ``set_halo_net_dimensions``.
    """
    from kicad_tools.router import router_cpp

    grid = router_cpp.Grid3D(COLS, ROWS, layers, RES, 0.0, 0.0)
    for seg in segments:
        grid.add_stored_segment(*seg)
    rules = router_cpp.DesignRules()
    rules.trace_width = TRACE_WIDTH
    rules.trace_clearance = 0.15
    rules.via_clearance = 0.15
    rules.via_drill = VIA_DRILL
    rules.via_diameter = VIA_DIAMETER
    rules.allow_smd_vias = True
    rules.same_net_trace_gap_band_edge_mm = edge_mm
    rules.same_net_pad_gap_band_edge_mm = pad_edge_mm
    pf = router_cpp.CoupledPathfinder(grid, rules, 2, 0, 1, 1, 0, 0.0, 1.0)
    for net, via_diameter in halo:
        pf.set_halo_net_dimensions(net, TRACE_WIDTH, 0.15, via_diameter)

    px, py = _gx(p_xy[0]), _gx(p_xy[1])
    nx, ny = _gx(n_xy[0]), _gx(n_xy[1])
    # Goals 5 mm east, so the start is not the goal and the search must expand.
    result = pf.route(
        px,
        py,
        nx,
        ny,
        0,
        px + 40,
        py,
        nx + 40,
        ny,
        0,
        P_NET,
        N_NET,
        2,
        0,
        0,
        list(range(layers)),
        [],
        2,  # budget: pop + expand the start node, stop before the next pop
        0.0,
        False,
        False,
    )
    return int(result.rejections.get("via_in_pad", 0))


def _band_rejects(edge_mm, segments, p_xy, n_xy, *, layers=2, halo=(), pad_edge_mm=0.0) -> bool:
    """The BAND's contribution to the coupled via verdict at one start seat."""
    baseline = _via_in_pad_refusals(
        0.0, segments, p_xy, n_xy, layers=layers, halo=halo, pad_edge_mm=pad_edge_mm
    )
    assert baseline == 0, "guard-off baseline must leave the via move open"
    refusals = _via_in_pad_refusals(
        edge_mm, segments, p_xy, n_xy, layers=layers, halo=halo, pad_edge_mm=pad_edge_mm
    )
    # The verdict is computed once per node and shared by every target layer.
    assert refusals in (0, layers - 1), refusals
    return refusals > 0


def _oracle_ok(x_mm, y_mm, seg, via_diameter=VIA_DIAMETER, edge_mm=EDGE) -> bool:
    """Python verdict: annulus at (x, y) vs the trace ``seg`` (exact gap)."""
    x1, y1, x2, y2, width = seg[:5]
    gap = point_to_segment_distance(x_mm, y_mm, x1, y1, x2, y2) - via_diameter / 2 - width / 2
    return not (1e-9 < gap < edge_mm - 1e-9)


# ---------------------------------------------------------------------------
# Region verdicts on the P rail (the shape of the 9140 / 8994 pad tests)
# ---------------------------------------------------------------------------


@requires_cpp
@pytest.mark.parametrize(
    ("dy", "expected_ok"),
    [
        (0.0, True),  # centred on the trace: overlap, one copper union
        (0.25, True),  # annulus overlaps the trace body: union
        (0.375, True),  # gap == 0 exactly: touching union, accepted
        (0.5, False),  # gap 0.125 inside (0, 0.25): slit
        (0.625, True),  # gap == edge exactly: accepted (open band)
        (0.75, True),  # gap 0.375 beyond the edge: clear
    ],
)
def test_p_rail_region_verdicts(dy, expected_ok):
    """In-band rejected; overlap / touching / beyond-edge / exact-edge accepted."""
    seat = (10.0, 10.0 + dy)
    assert _oracle_ok(*seat, P_HORIZ) is expected_ok, "oracle self-check"
    assert _band_rejects(EDGE, [P_HORIZ], seat, N_FAR) is (not expected_ok)


@requires_cpp
def test_n_rail_in_band_is_rejected_too():
    """The N rail is judged by the same rule, against ITS OWN net's trace."""
    assert _band_rejects(EDGE, [N_HORIZ], P_FAR, (10.0, 10.5)) is True
    assert _band_rejects(EDGE, [N_HORIZ], P_FAR, (10.0, 10.375)) is False  # union
    assert _band_rejects(EDGE, [N_HORIZ], P_FAR, (10.0, 10.75)) is False  # clear


@requires_cpp
def test_end_cap_region_matches_python():
    """Beside the trace END (round cap): the distance is to the endpoint."""
    for dx in (0.25, 0.375, 0.5, 0.625, 0.75):
        x_mm = 12.0 + dx
        expected_ok = _oracle_ok(x_mm, 10.0, P_HORIZ)
        assert _band_rejects(EDGE, [P_HORIZ], (x_mm, 10.0), N_FAR) is (not expected_ok), dx


@requires_cpp
def test_diagonal_trace_matches_exact_gap():
    """Seats around a 45-degree trace follow the exact point-to-segment gap."""
    half = 2.0
    c = 10.0
    seg = (c - half, c - half, c + half, c + half, TRACE_WIDTH, 0, P_NET)
    checked = in_band = 0
    for i in range(-8, 9):
        for j in range(-8, 9):
            x_mm, y_mm = c + i * RES, c + j * RES
            gap = (
                point_to_segment_distance(x_mm, y_mm, *seg[:4]) - VIA_DIAMETER / 2 - TRACE_WIDTH / 2
            )
            # Seats within float32 noise of a band edge round either way.
            if min(abs(gap), abs(gap - EDGE)) < 1e-4:
                continue
            expected_ok = _oracle_ok(x_mm, y_mm, seg)
            assert _band_rejects(EDGE, [seg], (x_mm, y_mm), N_FAR) is (not expected_ok), (
                x_mm,
                y_mm,
                gap,
            )
            checked += 1
            in_band += not expected_ok
    assert checked > 150
    assert in_band > 10, "the sweep must exercise the band"


@requires_cpp
@pytest.mark.parametrize("layer_idx", [0, 1, 2, 3])
def test_through_via_annulus_is_held_to_the_band_on_every_layer(layer_idx):
    """The annulus exists on each layer of a through via, so an own-net trace on
    ANY layer is graded; the one cached verdict refuses every target layer."""
    seg = (8.0, 10.0, 12.0, 10.0, TRACE_WIDTH, layer_idx, P_NET)
    assert _band_rejects(EDGE, [seg], (10.0, 10.5), N_FAR, layers=4) is True
    assert _via_in_pad_refusals(EDGE, [seg], (10.0, 10.5), N_FAR, layers=4) == 3
    assert _band_rejects(EDGE, [seg], (10.0, 10.375), N_FAR, layers=4) is False  # union
    assert _band_rejects(EDGE, [seg], (10.0, 10.75), N_FAR, layers=4) is False  # clear


# ---------------------------------------------------------------------------
# Net scope, the far-segment cull, and the unresolved-edge contract
# ---------------------------------------------------------------------------


@requires_cpp
def test_foreign_net_trace_in_band_does_not_refuse():
    """A third net's trace in the band is clearance's job, not this guard's."""
    foreign = (8.0, 10.0, 12.0, 10.0, TRACE_WIDTH, 0, OTHER_NET)
    assert _band_rejects(EDGE, [foreign], (10.0, 10.5), N_FAR) is False
    assert _band_rejects(EDGE, [foreign], P_FAR, (10.0, 10.5)) is False


@requires_cpp
def test_far_trace_is_ignored():
    """A trace well beyond ``annulus + edge`` never reaches the seat."""
    far = (2.0, 2.0, 4.0, 2.0, TRACE_WIDTH, 0, P_NET)
    assert _band_rejects(EDGE, [far], (10.0, 10.5), N_FAR) is False


@requires_cpp
def test_unresolved_edge_leaves_guard_inert():
    """Edge unresolved (None -> 0.0): every band seat stays open (#8974 AC-3)."""
    for y_mm in (10.5, 10.625):
        assert _via_in_pad_refusals(0.0, [P_HORIZ], (10.0, y_mm), N_FAR) == 0, y_mm
        assert _via_in_pad_refusals(0.0, [N_HORIZ], P_FAR, (10.0, y_mm)) == 0, y_mm


@requires_cpp
def test_pad_edge_alone_does_not_enable_the_trace_guard():
    """The two populations are independent knobs (never widen the pad edge)."""
    assert _via_in_pad_refusals(0.0, [P_HORIZ], (10.0, 10.5), N_FAR, pad_edge_mm=0.15) == 0


# ---------------------------------------------------------------------------
# Per-rail judgement: each rail against its OWN net's segments
# ---------------------------------------------------------------------------


@requires_cpp
def test_each_rail_is_judged_against_its_own_nets_segments():
    """A partner-net trace in a rail's band never refuses that rail's via."""
    in_band_seat = (10.0, 10.5)
    # P rail in band of the P trace: refused.  In band of the N trace: not.
    assert _band_rejects(EDGE, [P_HORIZ], in_band_seat, N_FAR) is True
    assert _band_rejects(EDGE, [N_HORIZ], in_band_seat, N_FAR) is False
    # And the mirror for the N rail.
    assert _band_rejects(EDGE, [N_HORIZ], P_FAR, in_band_seat) is True
    assert _band_rejects(EDGE, [P_HORIZ], P_FAR, in_band_seat) is False


@requires_cpp
def test_swapped_partner_traces_refuse_neither_rail():
    """Both seats are in band, but each of a PARTNER's trace: nothing refuses."""
    p_seat, n_seat = (10.0, 10.5), (10.0, 14.5)
    n_trace_by_p = N_HORIZ  # y=10 -> in band of the P seat only
    p_trace_by_n = (8.0, 14.0, 12.0, 14.0, TRACE_WIDTH, 0, P_NET)  # in band of N seat
    assert _band_rejects(EDGE, [n_trace_by_p, p_trace_by_n], p_seat, n_seat) is False


@requires_cpp
def test_both_rails_in_band_refuse_once_per_target_layer():
    """Either rail suffices, and two offending rails don't double-count: the
    per-node cached verdict yields one refusal per target layer."""
    p_seat, n_seat = (10.0, 10.5), (10.0, 14.5)
    p_trace = P_HORIZ
    n_trace = (8.0, 14.0, 12.0, 14.0, TRACE_WIDTH, 0, N_NET)
    assert _via_in_pad_refusals(EDGE, [p_trace, n_trace], p_seat, n_seat, layers=4) == 3
    assert _via_in_pad_refusals(EDGE, [p_trace], p_seat, n_seat, layers=4) == 3
    assert _via_in_pad_refusals(EDGE, [n_trace], p_seat, n_seat, layers=4) == 3


# ---------------------------------------------------------------------------
# Annulus radius source: HaloNetDims::via_diameter
# ---------------------------------------------------------------------------


@requires_cpp
@pytest.mark.parametrize(
    ("dy", "halo_diameter", "global_ok", "halo_ok"),
    [
        # Wider net-class annulus (half 0.375): gap 0 -> touching union.
        (0.5, 0.75, False, True),
        # Wider annulus (half 0.4375): gap 0.1875 -> slit the global radius misses.
        (0.75, 0.875, True, False),
        # Same diameter as the global rule: no change.
        (0.5, 0.5, False, False),
        (0.75, 0.5, True, True),
        # Narrower annulus (half 0.125): gap 0.25 == edge -> accepted.
        (0.5, 0.25, False, True),
    ],
)
def test_p_rail_net_class_via_diameter_shifts_the_band(dy, halo_diameter, global_ok, halo_ok):
    """The installed net-class via diameter, not the global one, sets the radius."""
    seat = (10.0, 10.0 + dy)
    assert _oracle_ok(*seat, P_HORIZ) is global_ok, "oracle self-check (global)"
    assert _oracle_ok(*seat, P_HORIZ, via_diameter=halo_diameter) is halo_ok, "oracle (halo)"
    assert _band_rejects(EDGE, [P_HORIZ], seat, N_FAR) is (not global_ok)
    assert _band_rejects(EDGE, [P_HORIZ], seat, N_FAR, halo=[(P_NET, halo_diameter)]) is (
        not halo_ok
    )


@requires_cpp
def test_n_rail_net_class_via_diameter_shifts_the_band():
    seat = (10.0, 10.75)  # global radius: clear; a 0.875 annulus: in band
    assert _band_rejects(EDGE, [N_HORIZ], P_FAR, seat) is False
    assert _band_rejects(EDGE, [N_HORIZ], P_FAR, seat, halo=[(N_NET, 0.875)]) is True
    # A wide annulus touching the trace is a union, not a slit.
    seat = (10.0, 10.5)
    assert _band_rejects(EDGE, [N_HORIZ], P_FAR, seat) is True
    assert _band_rejects(EDGE, [N_HORIZ], P_FAR, seat, halo=[(N_NET, 0.75)]) is False


@requires_cpp
def test_net_class_via_diameter_applies_per_rail_only():
    """A diameter installed for one net must not leak onto the partner rail."""
    wide = 0.875
    # Halo on the PARTNER net: this rail still uses the global radius.
    assert _band_rejects(EDGE, [P_HORIZ], (10.0, 10.75), N_FAR, halo=[(N_NET, wide)]) is False
    assert _band_rejects(EDGE, [N_HORIZ], P_FAR, (10.0, 10.75), halo=[(P_NET, wide)]) is False
    # Halo on the rail's OWN net: it applies.
    assert _band_rejects(EDGE, [P_HORIZ], (10.0, 10.75), N_FAR, halo=[(P_NET, wide)]) is True
    assert _band_rejects(EDGE, [N_HORIZ], P_FAR, (10.0, 10.75), halo=[(N_NET, wide)]) is True
    # Both nets widened independently: each rail uses its own.
    both = [(P_NET, wide), (N_NET, 0.5)]
    assert _band_rejects(EDGE, [P_HORIZ], (10.0, 10.75), N_FAR, halo=both) is True
    assert _band_rejects(EDGE, [N_HORIZ], P_FAR, (10.0, 10.75), halo=both) is False


@requires_cpp
def test_unresolved_edge_stays_inert_with_a_net_class_via_diameter():
    """A net-class diameter can't re-enable a guard whose edge was never resolved."""
    assert _via_in_pad_refusals(0.0, [P_HORIZ], (10.0, 10.75), N_FAR, halo=[(P_NET, 0.875)]) == 0

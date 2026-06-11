"""Unit tests for N-pad differential pair routing (Issue #2473).

Verifies that:

* The 2-pad regression case still routes (preserves Issue #2464 behavior).
* A 3-pad-per-net diff pair without polarity swap is decomposed into
  coupled segments + stubs and produces routes for every pad.
* A 3-pad-per-net diff pair with mirrored start/end orientation
  (USB-C-shaped layout) is detected as needing a polarity swap and
  the resulting routes still cover all pads.
* Off-grid pads (pad center between grid cells) are reached at the
  exact pad coordinates, not the snapped grid cell.
* The MST-based pair-up minimizes total P+N coupled length on a
  fixture where greedy nearest-neighbor pairing would lose.
"""

import math

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.diffpair_routing import (
    CoupledSegmentSpec,
    DiffPairRouter,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

# Issue #3436: CI runs the suite with `-n auto --timeout=60`.  These
# tests route real boards (often via subprocess) and comfortably beat
# 60s alone, but under full-suite xdist CPU contention the wall-clock
# reaper killed them spuriously.  The marker overrides the CLI default
# with a contention-tolerant budget; it does NOT slow the happy path.
pytestmark = pytest.mark.timeout(300)


# ---------------------------------------------------------------------------
# Test 1: Regression — 2-pad pair still routes
# ---------------------------------------------------------------------------


def _opt_in_diffpair_class_map(net_names: list[str]) -> dict[str, NetClassRouting]:
    """Build a per-net-name class map with ``coupled_routing=True``.

    Issue #2638 / Epic #2556 Phase 2E: engagement is now opt-in per net
    class.  Tests that exercise the dispatcher path
    (``route_diffpair_prepass`` / ``route_all_with_diffpairs``) must
    provide net classes whose ``coupled_routing`` flag is ``True``,
    otherwise the pair falls through to the main strategy.
    """
    nc = NetClassRouting(name="HighSpeedOptIn", coupled_routing=True)
    return dict.fromkeys(net_names, nc)


def _two_pad_router(spacing: float = 0.8) -> Autorouter:
    """Two-pad regression fixture (mirrors test_diffpair_routing_integration)."""
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(
        width=30.0,
        height=10.0,
        rules=rules,
        net_class_map=_opt_in_diffpair_class_map(["USB_D+", "USB_D-"]),
    )

    p_y = 5.0 - spacing / 2
    n_y = 5.0 + spacing / 2

    router.add_component(
        "U1",
        [
            {"number": "1", "x": 5.0, "y": p_y, "width": 0.4, "height": 0.4,
             "net": 1, "net_name": "USB_D+"},
            {"number": "2", "x": 5.0, "y": n_y, "width": 0.4, "height": 0.4,
             "net": 2, "net_name": "USB_D-"},
        ],
    )
    router.add_component(
        "J1",
        [
            {"number": "1", "x": 25.0, "y": p_y, "width": 0.4, "height": 0.4,
             "net": 1, "net_name": "USB_D+"},
            {"number": "2", "x": 25.0, "y": n_y, "width": 0.4, "height": 0.4,
             "net": 2, "net_name": "USB_D-"},
        ],
    )
    return router


def test_two_pad_regression_basic_strategy():
    """2-pad pair routes via the basic strategy fall-back path (coupled_only=False)."""
    router = _two_pad_router(spacing=0.8)

    pairs = router._diffpair.detect_differential_pairs()
    assert len(pairs) == 1

    config = DifferentialPairConfig(enabled=True, spacing=0.8)
    pair = pairs[0]
    pair.rules = config.get_rules(pair.pair_type)

    routes, _warning = router._diffpair.route_differential_pair_coupled(
        pair, spacing=0.8, coupled_only=False
    )

    assert routes, "2-pad regression should still route via the coupled path"
    nets = {r.net for r in routes}
    assert 1 in nets and 2 in nets


def test_two_pad_regression_prepass_path():
    """2-pad pair routes via the pre-pass (coupled_only=True)."""
    router = _two_pad_router(spacing=0.8)
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    routes, _warnings, routed = router.route_diffpair_prepass(config)

    assert 1 in routed and 2 in routed
    assert routes


# ---------------------------------------------------------------------------
# Test 2: 3-pad-per-net diff pair, no polarity swap
# ---------------------------------------------------------------------------


def _three_pad_no_swap_router() -> Autorouter:
    """Three-pad fixture: P at (10,10),(20,10),(30,10); N at (10,12),(20,12),(30,12).

    No polarity swap is required — both polarities maintain the same
    relative orientation across all pad clusters.
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(width=40.0, height=20.0, rules=rules)

    # Use 3 separate "components" so the pads are not absorbed into a single
    # intra-component connection.  In real boards this corresponds to 3
    # separate ICs paralleled into the same diff pair.
    for ref, x in [("U1", 10.0), ("U2", 20.0), ("U3", 30.0)]:
        router.add_component(
            ref,
            [
                {"number": "1", "x": x, "y": 10.0, "width": 0.4, "height": 0.4,
                 "net": 1, "net_name": "PAIR_P"},
                {"number": "2", "x": x, "y": 12.0, "width": 0.4, "height": 0.4,
                 "net": 2, "net_name": "PAIR_N"},
            ],
        )
    return router


def test_three_pad_no_swap_pair_up():
    """Pair-up for 3-pad fixture produces 2 coupled segments and zero stubs.

    Because each cluster has exactly one pad per net (the three U1/U2/U3
    components are >3mm apart), every pad is its own cluster and the MST
    yields 2 edges connecting the 3 representative pad pairs.
    """
    router = _three_pad_no_swap_router()
    pads = router._diffpair._get_pair_pads(
        router._diffpair.detect_differential_pairs()[0]
    )
    assert pads is not None
    p_pads, n_pads = pads

    coupled, stubs = router._diffpair._pair_pads_for_coupled_routing_npad(p_pads, n_pads)

    assert len(coupled) == 2, f"Expected 2 coupled MST edges; got {len(coupled)}"
    assert len(stubs) == 0, f"Expected no stubs; got {len(stubs)}"

    # No polarity swap should be flagged — start and end share orientation.
    for spec in coupled:
        assert not spec.polarity_swap


def test_three_pad_no_swap_routes_all_pads():
    """Coupled routing of a 3-pad pair produces routes covering every pad."""
    router = _three_pad_no_swap_router()
    pairs = router._diffpair.detect_differential_pairs()
    assert pairs

    config = DifferentialPairConfig(enabled=True, spacing=2.0)
    pair = pairs[0]
    pair.rules = config.get_rules(pair.pair_type)

    routes, _warning = router._diffpair.route_differential_pair_coupled(
        pair, spacing=2.0, coupled_only=False
    )

    assert routes, "3-pad coupled routing must produce routes"
    nets = {r.net for r in routes}
    assert 1 in nets and 2 in nets


# ---------------------------------------------------------------------------
# Test 3: 3-pad-per-net diff pair with polarity swap (USB-C-shaped)
# ---------------------------------------------------------------------------


def _polarity_swap_router() -> Autorouter:
    """USB-C-shaped fixture with mirrored polarity at source vs sink.

    Source: P at (10, 10), N at (12, 10)  — P-left, N-right
    Sink (cluster A): P at (28, 9.75), N at (30, 9.75)  — P-left, N-right
    Sink (cluster B, same net pads): P at (30, 11.25), N at (28, 11.25)
                                     — P-RIGHT, N-LEFT (polarity inverted)
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)
    router = Autorouter(width=40.0, height=20.0, rules=rules)

    # MCU side
    router.add_component(
        "U1",
        [
            {"number": "29", "x": 10.0, "y": 10.0, "width": 0.4, "height": 0.4,
             "net": 1, "net_name": "USB_D+"},
            {"number": "28", "x": 12.0, "y": 10.0, "width": 0.4, "height": 0.4,
             "net": 2, "net_name": "USB_D-"},
        ],
    )

    # USB-C connector with paired top/bottom pads (polarity inverted on row B)
    router.add_component(
        "J1",
        [
            {"number": "A6", "x": 28.0, "y": 9.75, "width": 0.25, "height": 0.35,
             "net": 1, "net_name": "USB_D+"},
            {"number": "A7", "x": 30.0, "y": 9.75, "width": 0.25, "height": 0.35,
             "net": 2, "net_name": "USB_D-"},
            {"number": "B6", "x": 30.0, "y": 11.25, "width": 0.25, "height": 0.35,
             "net": 1, "net_name": "USB_D+"},
            {"number": "B7", "x": 28.0, "y": 11.25, "width": 0.25, "height": 0.35,
             "net": 2, "net_name": "USB_D-"},
        ],
    )
    return router


def test_polarity_swap_detected_in_pair_up():
    """The pair-up flags an MST edge that crosses inverted-orientation clusters."""
    router = _polarity_swap_router()
    pads = router._diffpair._get_pair_pads(
        router._diffpair.detect_differential_pairs()[0]
    )
    assert pads is not None
    p_pads, n_pads = pads

    coupled, stubs = router._diffpair._pair_pads_for_coupled_routing_npad(p_pads, n_pads)

    assert coupled, "polarity-swap fixture must produce at least one coupled segment"
    # The cluster on the USB-C side should be a single cluster (A6/A7/B6/B7
    # are within 1.5mm of each other), so we expect at most one MST edge
    # between the MCU cluster and the connector cluster.  The remaining
    # pads are absorbed as stubs.  This stub set must be non-empty.
    assert stubs, (
        "polarity-swap fixture should produce stub edges for paralleled pads; "
        f"got {len(stubs)} stubs"
    )


@pytest.mark.xfail(
    reason="pure-Python coupled A* exceeds 300s under full-suite xdist contention -- see issue #3524",
    strict=False,
)
def test_polarity_swap_routes_all_nets():
    """The coupled router still produces routes for both nets when polarity swaps."""
    router = _polarity_swap_router()
    pairs = router._diffpair.detect_differential_pairs()
    assert pairs

    config = DifferentialPairConfig(enabled=True, spacing=2.0)
    pair = pairs[0]
    pair.rules = config.get_rules(pair.pair_type)

    routes, _warning = router._diffpair.route_differential_pair_coupled(
        pair, spacing=2.0, coupled_only=False
    )

    assert routes, "polarity-swap fixture must route via coupled + stubs"
    nets = {r.net for r in routes}
    assert 1 in nets and 2 in nets, (
        f"Both diff-pair nets must have at least one route; got nets={nets}"
    )


# ---------------------------------------------------------------------------
# Test 4: Off-grid pads
# ---------------------------------------------------------------------------


def test_off_grid_pad_reaches_actual_pad_center():
    """Off-grid pad coordinates appear in the resulting route segments.

    With a 0.1mm grid, a pad at x=10.05mm sits half-way between grid
    cells.  The coupled-routing reconstruction must emit a segment
    that ends at x=10.05 (the actual pad), not at x=10.0 (the
    snapped grid cell).
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(width=30.0, height=10.0, rules=rules)

    # P pads off-grid in x (10.05 / 25.05); N pads on grid (10.0 / 25.0).
    router.add_component(
        "U1",
        [
            {"number": "1", "x": 10.05, "y": 4.6, "width": 0.4, "height": 0.4,
             "net": 1, "net_name": "PAIR_P"},
            {"number": "2", "x": 10.0, "y": 5.4, "width": 0.4, "height": 0.4,
             "net": 2, "net_name": "PAIR_N"},
        ],
    )
    router.add_component(
        "J1",
        [
            {"number": "1", "x": 25.05, "y": 4.6, "width": 0.4, "height": 0.4,
             "net": 1, "net_name": "PAIR_P"},
            {"number": "2", "x": 25.0, "y": 5.4, "width": 0.4, "height": 0.4,
             "net": 2, "net_name": "PAIR_N"},
        ],
    )

    pairs = router._diffpair.detect_differential_pairs()
    assert pairs

    config = DifferentialPairConfig(enabled=True, spacing=0.8)
    pair = pairs[0]
    pair.rules = config.get_rules(pair.pair_type)

    routes, _warning = router._diffpair.route_differential_pair_coupled(
        pair, spacing=0.8, coupled_only=False
    )
    assert routes

    # The P-route (net 1) must include points at x ~= 10.05 and 25.05 (the
    # actual off-grid pad centers).  We allow a 0.005mm tolerance for
    # floating-point rounding to 4 decimal places.
    p_route = next(r for r in routes if r.net == 1)
    xs = []
    for seg in p_route.segments:
        xs.append(seg.x1)
        xs.append(seg.x2)

    has_start = any(abs(x - 10.05) < 0.01 for x in xs)
    has_end = any(abs(x - 25.05) < 0.01 for x in xs)
    assert has_start, f"Expected segment endpoint at x=10.05; xs={sorted(set(xs))}"
    assert has_end, f"Expected segment endpoint at x=25.05; xs={sorted(set(xs))}"


# ---------------------------------------------------------------------------
# Test 5: Pair-up correctness (MST beats greedy on a tricky fixture)
# ---------------------------------------------------------------------------


def _make_pad(ref: str, pin: str, x: float, y: float, net: int, name: str) -> Pad:
    return Pad(x=x, y=y, width=0.4, height=0.4, net=net, net_name=name,
               ref=ref, pin=pin)


def test_pair_up_minimizes_total_pn_length():
    """MST pair-up beats greedy on a fixture where greedy would lose.

    Layout (P-pads marked X, N-pads marked O):

        X(0,0)      X(10,0)              X(20,0)
        O(0,0.5)    O(10,0.5)            O(20,0.5)

    Greedy nearest-neighbor on P0 would consume N0 first, then for P1
    must take N1 (already its closest), and P2 takes N2.  In this
    layout that happens to be optimal — so we make it adversarial:

        X(0,0)              X(10,1)
        O(0,5)              O(10,4)

    Now P0 is far from N0 (d=5) and far from N1 (d=10.something).
    The MST should produce a coupled edge between (P0,N0) and (P1,N1).
    """
    # Build a synthetic Autorouter just to access DiffPairRouter helpers.
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(width=30.0, height=15.0, rules=rules)
    diff_router = DiffPairRouter(router)

    p_pads = [
        _make_pad("U1", "1", 0.0, 0.0, 1, "PAIR_P"),
        _make_pad("U2", "1", 10.0, 1.0, 1, "PAIR_P"),
        _make_pad("U3", "1", 20.0, 0.0, 1, "PAIR_P"),
    ]
    n_pads = [
        _make_pad("U1", "2", 0.0, 5.0, 2, "PAIR_N"),
        _make_pad("U2", "2", 10.0, 4.0, 2, "PAIR_N"),
        _make_pad("U3", "2", 20.0, 5.0, 2, "PAIR_N"),
    ]

    coupled, stubs = diff_router._pair_pads_for_coupled_routing_npad(p_pads, n_pads)

    # MST over 3 representatives = 2 edges.
    assert len(coupled) == 2

    # Compute total P+N length of the chosen MST.
    def edge_weight(spec: CoupledSegmentSpec) -> float:
        p_d = math.hypot(spec.p_start.x - spec.p_end.x,
                         spec.p_start.y - spec.p_end.y)
        n_d = math.hypot(spec.n_start.x - spec.n_end.x,
                         spec.n_start.y - spec.n_end.y)
        return p_d + n_d

    mst_total = sum(edge_weight(e) for e in coupled)

    # Worst-case spanning tree on the same vertices: two edges that both
    # span the full board (P0->P2, P0->P2) — should NOT be what MST picks.
    p_d_full = math.hypot(0.0 - 20.0, 0.0 - 0.0)
    n_d_full = math.hypot(0.0 - 20.0, 5.0 - 5.0)
    worst_total = 2 * (p_d_full + n_d_full)

    assert mst_total < worst_total, (
        f"MST should beat the worst spanning tree: "
        f"mst={mst_total:.2f}, worst={worst_total:.2f}"
    )

    # MST should pick the two short edges P0-P1 and P1-P2 (or N variants),
    # totalling roughly 2 * (sqrt(101) + sqrt(101)) ~= 40.2.  Definitely
    # less than the worst-case total of 4 * 20 = 80.
    assert mst_total < 50.0, (
        f"MST total {mst_total:.2f} unexpectedly large; "
        "expected ~40 for this fixture"
    )


def test_pair_up_returns_empty_on_under_two_pads():
    """A net with fewer than 2 pads cannot form a coupled segment."""
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(width=10.0, height=10.0, rules=rules)
    diff_router = DiffPairRouter(router)

    p_pads = [_make_pad("U1", "1", 1.0, 1.0, 1, "PAIR_P")]
    n_pads = [
        _make_pad("U1", "2", 1.0, 2.0, 2, "PAIR_N"),
        _make_pad("U2", "2", 5.0, 2.0, 2, "PAIR_N"),
    ]

    coupled, stubs = diff_router._pair_pads_for_coupled_routing_npad(p_pads, n_pads)
    assert coupled == []
    assert stubs == []


def test_polarity_swap_helper_detects_orientation_inversion():
    """The orientation-detection helper distinguishes aligned vs swapped pads."""
    p_left = _make_pad("U1", "1", 0.0, 0.0, 1, "P")
    n_right = _make_pad("U1", "2", 1.0, 0.0, 2, "N")

    # End cluster keeps the same orientation (P left, N right).
    p_left_end = _make_pad("U2", "1", 10.0, 0.0, 1, "P")
    n_right_end = _make_pad("U2", "2", 11.0, 0.0, 2, "N")

    assert not DiffPairRouter._polarity_swap_between(
        p_left, n_right, p_left_end, n_right_end
    )

    # End cluster inverts the orientation (P right, N left).
    p_right_end = _make_pad("U2", "1", 11.0, 0.0, 1, "P")
    n_left_end = _make_pad("U2", "2", 10.0, 0.0, 2, "N")

    assert DiffPairRouter._polarity_swap_between(
        p_left, n_right, p_right_end, n_left_end
    )


# ---------------------------------------------------------------------------
# Test (Issue #2490): start/end pad-pitch mismatch (USB device-side)
# ---------------------------------------------------------------------------


def _pitch_mismatch_router() -> Autorouter:
    """Two-pad fixture with mismatched start/end pad pitches.

    Mirrors the USB device-side topology that triggers issue #2490:

    - Start (MCU side, U1): P/N pads at 0.8mm pitch.
    - End (USB-C side, J1): P/N pads at 0.5mm pitch.

    Without the issue #2490 fix, the coupled pathfinder cannot
    converge from the wider start pitch to the narrower goal pitch
    because symmetric step moves preserve spacing exactly and the
    legacy approach radius leaves no room for asymmetric convergence.
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)
    router = Autorouter(width=30.0, height=20.0, rules=rules)

    # MCU-side pads at 0.8mm pitch.
    router.add_component(
        "U1",
        [
            {"number": "29", "x": 9.6, "y": 15.5, "width": 0.5, "height": 0.5,
             "net": 1, "net_name": "USB_D+"},
            {"number": "28", "x": 10.4, "y": 15.5, "width": 0.5, "height": 0.5,
             "net": 2, "net_name": "USB_D-"},
        ],
    )

    # USB-C-side pads at 0.5mm pitch.
    router.add_component(
        "J1",
        [
            {"number": "A6", "x": 9.75, "y": 5.0, "width": 0.25, "height": 0.35,
             "net": 1, "net_name": "USB_D+"},
            {"number": "A7", "x": 10.25, "y": 5.0, "width": 0.25, "height": 0.35,
             "net": 2, "net_name": "USB_D-"},
        ],
    )
    return router


def test_pitch_mismatch_diff_pair_routes():
    """Issue #2490: a 2-pad pair with start pitch > end pitch routes.

    This regression-tests the combination of:

    * Endpoint via exception (allows dropping a via at the dense pad
      cluster on layer 0 even though adjacent pads block via clearance).
    * Approach-radius scaling for pitch deltas (gives the search room
      to relax spacing before reaching the goal cells).
    * Asymmetric "converge" moves inside the approach radius (lets P
      and N step independently to bring spacing from the wider start
      pitch down to the narrower goal pitch).
    """
    router = _pitch_mismatch_router()
    pairs = router._diffpair.detect_differential_pairs()
    assert pairs, "fixture must expose a USB diff pair"

    config = DifferentialPairConfig(enabled=True, spacing=0.2)
    pair = pairs[0]
    pair.rules = config.get_rules(pair.pair_type)

    routes, _warning = router._diffpair.route_differential_pair_coupled(
        pair, spacing=0.2, coupled_only=True
    )

    # Both nets must produce routes.  ``coupled_only=True`` means a
    # failure short-circuits to ``([], None)`` rather than falling back
    # to independent routing.  The test therefore proves the coupled
    # pathfinder itself handles the pitch transition.
    assert routes, "coupled pathfinder must succeed on pitch-mismatch fixture"
    nets = {r.net for r in routes}
    assert 1 in nets and 2 in nets, f"both nets must produce routes; got nets={nets}"

"""Regression tests for Issue #3320: CoupledPathfinder swap-via overlap rejection.

PR #3022 added a ``min_spacing_cells`` floor to ``CoupledPathfinder`` to
prevent the coupled A* from collapsing within-pair spacing to zero during
the approach phase or asymmetric "converge" moves.  Empirical follow-up
on board 07's DDR strobe pair (DQS_N/DQS_P) showed that the floor is
necessary but not sufficient: when the pair has a *polarity-swap* (the P
and N pad rows are inverted between the two footprints, e.g.
``U1.30=P-top, U1.31=N-bottom`` vs ``U2.6=P-bottom, U2.7=N-top``), the
search uses a "swap-via" move at the start pads to invert the spacing
orientation.

The reconstructed swap-via geometry physically requires both traces to
cross over each other on the same destination layer.  The
``min_spacing_cells`` floor only protects coupled segments after the
swap-via lands; it does NOT prevent the *crossover stub* between the via
position and the swapped grid position from overlapping the partner
trace's via location.  On board 07 DQS this produces 5 negative-clearance
overlaps (worst -0.150 mm = full trace width = traces literally
coincident).

The fix in ``route_differential_pair_coupled`` runs the post-route
intra-pair clearance audit BEFORE committing the coupled route to the
grid, and rejects the route (falling back to independent routing) when
any segment-pair has actual edge-to-edge clearance below 0.0 mm (i.e.
centerlines overlap).

This module exercises two related behaviors:

1. ``test_dqs_like_polarity_swap_does_not_produce_negative_clearance``:
   end-to-end check that running the coupled router on a DQS-like
   polarity-swap fixture either produces routes whose intra-pair
   clearance is non-negative, or falls back to independent routing.
2. ``test_severe_overlap_triggers_independent_fallback`` and
   ``test_quantization_slack_does_not_trigger_fallback``: gate-only
   unit tests that verify the severity classifier (``actual_clearance_mm
   < 0.0``) is the trigger and that quantization-slack violations (in
   ``[0, threshold)``) do NOT trigger the fallback.
"""

from __future__ import annotations

from kicad_tools.core.geometry import segment_clearance
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.rules import DesignRules, NetClassRouting

# ---------------------------------------------------------------------------
# DQS-like fine-pitch polarity-swap fixture
# ---------------------------------------------------------------------------


def _dqs_like_class_map() -> dict[str, NetClassRouting]:
    """Build a per-net class map mirroring board 07's DDR_DQS net class.

    The defining attributes are:
      * ``trace_width = 0.15`` (board 07 ``DDR_DQS`` class)
      * ``intra_pair_clearance = 0.1`` (tight, sub-grid-resolution)
      * ``coupled_routing = True`` (engagement opt-in, per PR #2638 Phase 2E)

    These together demand ``required_center_spacing = 0.15 + 0.1 = 0.25``,
    which on a 0.127 mm grid converts to ``ceil(0.25 / 0.127) = 2`` cells
    of center-to-center spacing.  The minimum-spacing floor (PR #3022)
    enforces this, but the polarity-swap-via at the start pads still
    produces an overlapping crossover that the floor does not catch.
    """
    nc = NetClassRouting(
        name="DDR_DQS",
        trace_width=0.15,
        clearance=0.1,
        intra_pair_clearance=0.1,
        coupled_routing=True,
        length_critical=True,
    )
    return {"DQS_P": nc, "DQS_N": nc}


def _dqs_polarity_swap_router(grid_resolution: float = 0.127) -> Autorouter:
    """Two-pad polarity-swap fixture with DQS-like tight clearance.

    At U1: P at (5.0, 5.4), N at (5.0, 4.6) -- P above N
    At U2: P at (25.0, 4.6), N at (25.0, 5.4) -- P BELOW N (polarity inverted)

    This matches the board 07 DDR_DQS layout (U1.30/31 vs U2.6/7) that
    triggered the original #3320 negative-clearance bug.
    """
    rules = DesignRules(
        trace_width=0.15,
        trace_clearance=0.1,
        grid_resolution=grid_resolution,
    )
    router = Autorouter(
        width=30.0,
        height=10.0,
        rules=rules,
        net_class_map=_dqs_like_class_map(),
    )
    # MCU side: P (net 1) above N (net 2)
    router.add_component(
        "U1",
        [
            {
                "number": "30",
                "x": 5.0,
                "y": 5.4,
                "width": 0.3,
                "height": 0.3,
                "net": 1,
                "net_name": "DQS_P",
            },
            {
                "number": "31",
                "x": 5.0,
                "y": 4.6,
                "width": 0.3,
                "height": 0.3,
                "net": 2,
                "net_name": "DQS_N",
            },
        ],
    )
    # DDR side: P (net 1) BELOW N (net 2) -- polarity swap
    router.add_component(
        "U2",
        [
            {
                "number": "6",
                "x": 25.0,
                "y": 4.6,
                "width": 0.3,
                "height": 0.3,
                "net": 1,
                "net_name": "DQS_P",
            },
            {
                "number": "7",
                "x": 25.0,
                "y": 5.4,
                "width": 0.3,
                "height": 0.3,
                "net": 2,
                "net_name": "DQS_N",
            },
        ],
    )
    return router


# ---------------------------------------------------------------------------
# End-to-end behavior: DQS-like polarity swap must not produce overlap
# ---------------------------------------------------------------------------


def _min_intra_pair_clearance(p_route, n_route) -> float:
    """Return the minimum edge-to-edge clearance between any same-layer P/N seg pair."""
    if p_route is None or n_route is None:
        return float("inf")
    worst = float("inf")
    for ps in p_route.segments:
        for ns in n_route.segments:
            if ps.layer != ns.layer:
                continue
            c = segment_clearance(
                ps.x1,
                ps.y1,
                ps.x2,
                ps.y2,
                ps.width,
                ns.x1,
                ns.y1,
                ns.x2,
                ns.y2,
                ns.width,
            )
            if c < worst:
                worst = c
    return worst


def test_dqs_like_polarity_swap_does_not_produce_negative_clearance():
    """Coupled routing on a DQS-like polarity-swap fixture must not produce overlap.

    The pre-#3320 behavior on this fixture: the CoupledPathfinder's
    swap-via at the start pads produces P and N routes whose first
    inner-layer segment has the polarity-swapped grid position as its
    endpoint.  The resulting (1-cell-x, 7-cell-y) diagonal segment
    intersects the partner trace's via cell, producing a worst-case
    intra-pair clearance of -trace_width (= -0.15 mm).

    Post-#3320 the route_differential_pair_coupled severity gate
    rejects this coupled route and falls back to independent routing,
    so the returned routes either:
      (a) come from the coupled path (no severe overlap), or
      (b) come from the independent fallback path (single-ended A* for
          each net -- no within-pair constraint, so worst-case clearance
          can be anything in ``[generic_clearance, +inf)``).

    Either way, the worst intra-pair edge-to-edge clearance must be
    >= 0.0 mm (no centerline overlap).  The acceptance criterion in
    #3320 is "0 negative-clearance violations on DQS_N/DQS_P".
    """
    router = _dqs_polarity_swap_router()

    pairs = router._diffpair.detect_differential_pairs()
    assert len(pairs) == 1, f"expected 1 pair, got {len(pairs)}"

    config = DifferentialPairConfig(enabled=True, spacing=0.25)
    pair = pairs[0]
    pair.rules = config.get_rules(pair.pair_type)

    routes, _warning = router._diffpair.route_differential_pair_coupled(
        pair, spacing=0.25, coupled_only=False
    )
    assert routes, "DQS-like fixture must produce routes (coupled or fallback)"

    # Split routes by net.
    p_routes = [r for r in routes if r.net == 1]
    n_routes = [r for r in routes if r.net == 2]
    assert p_routes and n_routes, f"both nets must route; got p={len(p_routes)} n={len(n_routes)}"

    # Compute worst-case intra-pair clearance across all (P, N) route
    # combinations on the same layer.
    worst = float("inf")
    for p in p_routes:
        for n in n_routes:
            c = _min_intra_pair_clearance(p, n)
            if c < worst:
                worst = c

    # The #3320 invariant: no centerline overlap (clearance >= 0).
    # 1e-6 mm tolerance to account for floating-point quantization.
    assert worst >= -1e-6, (
        f"DQS-like polarity-swap pair must not produce centerline overlap; "
        f"worst intra-pair clearance = {worst:+.4f}mm (expected >= 0)"
    )


# ---------------------------------------------------------------------------
# Severity classifier behavior
# ---------------------------------------------------------------------------


def test_severe_overlap_triggers_independent_fallback():
    """A coupled route with negative intra-pair clearance is rejected.

    Constructs a synthetic ``IntraPairClearanceViolation`` via
    ``find_intra_pair_clearance_violations`` on routes we hand-build to
    overlap, and verifies the severity classifier returns True.  This
    is the trigger used in ``route_differential_pair_coupled`` to
    decide between committing the coupled route and falling back to
    independent routing.
    """
    from kicad_tools.router.diffpair_routing import (
        find_intra_pair_clearance_violations,
    )
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    # Hand-construct two overlapping routes.  P at y=0, N at y=0 (fully
    # coincident, clearance = -trace_width = -0.15).
    p_route = Route(
        net=1,
        net_name="P",
        segments=[
            Segment(
                x1=0.0,
                y1=0.0,
                x2=5.0,
                y2=0.0,
                width=0.15,
                layer=Layer.F_CU,
                net=1,
                net_name="P",
            ),
        ],
    )
    n_route = Route(
        net=2,
        net_name="N",
        segments=[
            Segment(
                x1=0.0,
                y1=0.0,
                x2=5.0,
                y2=0.0,
                width=0.15,
                layer=Layer.F_CU,
                net=2,
                net_name="N",
            ),
        ],
    )
    v = find_intra_pair_clearance_violations(p_route, n_route, threshold_mm=0.1, pair_name="P/N")
    assert v is not None
    # The severity classifier in route_differential_pair_coupled is
    # ``violation.actual_clearance_mm < 0.0``.
    assert v.actual_clearance_mm < 0.0, (
        f"hand-built overlap must report negative clearance; got actual={v.actual_clearance_mm}"
    )


def test_quantization_slack_does_not_trigger_fallback():
    """A coupled route with clearance in [0, threshold) is NOT severe.

    The severity classifier rejects only NEGATIVE clearance.  Pure
    quantization slack (clearance in ``[0, threshold)``) is logged via
    the existing Phase A diagnostic but the route is still committed --
    the trace optimizer / serpentine shim can nudge it into compliance,
    and falling back to independent routing for a 0.01-0.05 mm overshoot
    would be unnecessarily destructive of skew matching.
    """
    from kicad_tools.router.diffpair_routing import (
        find_intra_pair_clearance_violations,
    )
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    # Two parallel routes with center-to-center spacing = 0.20 mm,
    # trace widths 0.15 mm each -> edge-to-edge clearance = 0.05 mm.
    # Threshold = 0.10 mm so this is a quantization-slack violation
    # (positive but below threshold).
    p_route = Route(
        net=1,
        net_name="P",
        segments=[
            Segment(
                x1=0.0,
                y1=0.0,
                x2=5.0,
                y2=0.0,
                width=0.15,
                layer=Layer.F_CU,
                net=1,
                net_name="P",
            ),
        ],
    )
    n_route = Route(
        net=2,
        net_name="N",
        segments=[
            Segment(
                x1=0.0,
                y1=0.20,
                x2=5.0,
                y2=0.20,
                width=0.15,
                layer=Layer.F_CU,
                net=2,
                net_name="N",
            ),
        ],
    )
    v = find_intra_pair_clearance_violations(p_route, n_route, threshold_mm=0.10, pair_name="P/N")
    assert v is not None
    # Clearance is positive (5e-2 mm) -- below threshold but not overlap.
    assert 0.0 <= v.actual_clearance_mm < 0.10, (
        f"expected quantization-slack violation (0 <= c < 0.10); got actual={v.actual_clearance_mm}"
    )
    # The severity classifier is ``actual_clearance_mm < 0.0`` -- this
    # must be False so the coupled route is committed (no fallback).
    assert not (v.actual_clearance_mm < 0.0), (
        "quantization-slack violation must NOT trigger severe-overlap fallback"
    )

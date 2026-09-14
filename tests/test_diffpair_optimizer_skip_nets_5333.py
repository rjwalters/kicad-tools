"""Regression tests for issue #5333: the general-purpose geometric optimizer
silently undoing a qualified differential pair's length match.

``optimize_routes_grid_synced`` has carried a ``skip_nets`` parameter since
#3508/#3546 -- its bundled ``eliminate_zigzags`` / ``compress_staircase`` /
``convert_45_corners`` sub-passes are NOT length-preserving (only
``merge_collinear`` is) and know nothing about differential-pair skew, so
they can shorten one leg of a length-matched pair without knowing the leg
is part of a pair at all.  That parameter was NEVER threaded through from
any of ``route_cmd.py``'s four ``optimize_routes_grid_synced`` call sites,
so the protection #3508 documented never actually applied to a real
``kct route`` invocation.

Real re-measurement on Board07 (seed 42, native ABI 31): a coupled
MIPI_DAT0 pair qualified at construction time with 0.045 mm skew (well
inside the 0.05 mm authored tolerance).  The very next pipeline stage --
this exact, previously-unprotected optimize call -- shortened ONE leg by
0.437 mm with no diff-pair awareness at all, landing the pair at 0.482 mm
in the final saved PCB: a real, silent, authored-tolerance violation that
``kct check`` then reports as a ``diffpair_length_skew`` FAIL.  Direct
before/after instrumentation of ``optimize_routes_grid_synced`` in
isolation confirmed the shortening happens entirely inside this one call
(the immediately following DRC nudge and length-preserving consolidation
pass make no further change).

This file covers two layers:

1. ``_diffpair_optimizer_skip_nets`` in isolation (fast, no routing).
2. The underlying mechanism end-to-end at the ``Autorouter`` level: a
   hand-built pair with a real removable "staircase" detour on one leg
   (the same shape class the real corridor-guided MIPI_DAT0 route had --
   ``off_angle_segs=4`` in the production log) is shown to lose length
   under ``optimize_routes_grid_synced`` when unprotected, and to be
   preserved byte-for-byte when protected by the new helper's output.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.cli.route_cmd import _diffpair_optimizer_skip_nets
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import (
    OptimizationConfig,
    TraceOptimizer,
    make_collision_checker,
    optimize_routes_grid_synced,
)
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting


def _seg_len(seg: Segment) -> float:
    return math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)


def _route_len(route: Route) -> float:
    return sum(_seg_len(s) for s in route.segments)


# =============================================================================
# 1. ``_diffpair_optimizer_skip_nets`` in isolation
# =============================================================================


class _FakeNoPairsRouter:
    net_names: dict[int, str] = {1: "SOLO"}

    def get_diff_pair_map(self) -> dict[str, str]:
        return {}


class _FakeRaisingRouter:
    net_names: dict[int, str] = {1: "TEST_P", 2: "TEST_N"}

    def get_diff_pair_map(self) -> dict[str, str]:
        raise RuntimeError("detection blew up")


class _FakePairRouter:
    net_names: dict[int, str] = {1: "TEST_P", 2: "TEST_N", 3: "SOLO"}

    def get_diff_pair_map(self) -> dict[str, str]:
        return {"TEST_P": "TEST_N", "TEST_N": "TEST_P"}


def test_skip_nets_empty_when_no_net_names():
    class _Empty:
        net_names: dict[int, str] = {}

        def get_diff_pair_map(self) -> dict[str, str]:
            raise AssertionError("must short-circuit before calling this")

    assert _diffpair_optimizer_skip_nets(_Empty()) == set()


def test_skip_nets_empty_when_no_pairs_detected():
    assert _diffpair_optimizer_skip_nets(_FakeNoPairsRouter()) == set()


def test_skip_nets_empty_on_detection_failure():
    """A partner-map build failure forfeits protection, it must not crash
    the optimize stage (mirrors ``apply_match_group_tuning``'s own
    try/except around the same call)."""
    assert _diffpair_optimizer_skip_nets(_FakeRaisingRouter()) == set()


def test_skip_nets_covers_both_pair_halves_and_excludes_others():
    result = _diffpair_optimizer_skip_nets(_FakePairRouter())
    assert result == {1, 2}


# =============================================================================
# 2. End-to-end mechanism at the ``Autorouter`` level
# =============================================================================


def _staircase_optin_router() -> Autorouter:
    """20x10mm board, opted into ``coupled_routing`` (irrelevant to this
    test beyond making the pair a "real" declared one for detection)."""
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    nc = NetClassRouting(name="TestPair", coupled_routing=True)
    router = Autorouter(
        width=20.0,
        height=10.0,
        rules=rules,
        net_class_map=dict.fromkeys(["TEST_P", "TEST_N"], nc),
    )
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 2.0,
                "y": 4.6,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "TEST_P",
            },
            {
                "number": "2",
                "x": 2.0,
                "y": 5.4,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "TEST_N",
            },
        ],
    )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 18.0,
                "y": 4.6,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "TEST_P",
            },
            {
                "number": "2",
                "x": 18.0,
                "y": 5.4,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "TEST_N",
            },
        ],
    )
    return router


def _build_p_route(router: Autorouter) -> Route:
    """Straight leg, pad-to-pad: exactly 16.0mm."""
    seg = Segment(x1=2.0, y1=4.6, x2=18.0, y2=4.6, width=0.2, layer=Layer.F_CU, net=1)
    return Route(net=1, net_name="TEST_P", segments=[seg])


def _build_n_staircase_route(router: Autorouter) -> Route:
    """A removable "staircase" detour -- the same shape class the real
    MIPI_DAT0 route had (``off_angle_segs=4`` in the production log): a
    grid-quantized near-straight run that jogs up and back down every
    4mm.  Its physical length is a small, deliberate over-length versus
    the P leg's 16.0mm straight run (see ``STAIRCASE_OVERLENGTH_MM``
    below) -- standing in for a qualified pair's near-matched skew --
    that ``compress_staircase``/``eliminate_zigzags`` can collapse away
    by straightening the jogs, which would shorten it well past that
    match.
    """
    xs = [2.0, 6.0, 6.0, 10.0, 10.0, 14.0, 14.0, 18.0]
    ys = [5.4, 5.4, 5.42, 5.42, 5.4, 5.4, 5.42, 5.42]
    # Force the path back to the exact goal y at the end regardless of the
    # jog parity above (keeps pad connectivity exact).
    ys[-1] = 5.4
    segments = []
    for i in range(len(xs) - 1):
        segments.append(
            Segment(
                x1=xs[i],
                y1=ys[i],
                x2=xs[i + 1],
                y2=ys[i + 1],
                width=0.2,
                layer=Layer.F_CU,
                net=2,
            )
        )
    return Route(net=2, net_name="TEST_N", segments=segments)


def _optimizer_and_checker(router: Autorouter) -> tuple[TraceOptimizer, object]:
    opt_config = OptimizationConfig(
        merge_collinear=True,
        eliminate_zigzags=True,
        compress_staircase=True,
        convert_45_corners=True,
        corner_chamfer_size=0.5,
        minimize_vias=True,
    )
    collision_checker = make_collision_checker(router.grid)
    return TraceOptimizer(config=opt_config, collision_checker=collision_checker), collision_checker


def test_unprotected_optimize_shortens_the_staircase_leg():
    """Baseline (pre-#5333 behaviour): calling ``optimize_routes_grid_synced``
    with no ``skip_nets`` -- exactly what all four ``route_cmd.py`` call
    sites did before this fix -- lets the optimizer collapse the N leg's
    staircase and change its length, breaking the P/N length match that
    was qualified before this call ran."""
    router = _staircase_optin_router()
    p_route = _build_p_route(router)
    n_route = _build_n_staircase_route(router)
    router._mark_route(p_route)
    router._mark_route(n_route)
    router.routes.append(p_route)
    router.routes.append(n_route)

    p_before = _route_len(p_route)
    n_before = _route_len(n_route)
    delta_before = abs(n_before - p_before)
    # The fixture's staircase must be a small over-length versus the
    # straight P leg (standing in for a qualified pair's near-matched
    # skew) -- not a coincidental exact match.
    assert 0.0 < delta_before < 0.5, "fixture must start near-matched, not identical"

    optimizer, _ = _optimizer_and_checker(router)
    optimize_routes_grid_synced(router, optimizer)  # no skip_nets: pre-#5333 call shape

    n_after = next(r for r in router.routes if r.net == 2)
    p_after = next(r for r in router.routes if r.net == 1)
    delta_after = abs(_route_len(n_after) - _route_len(p_after))

    # The staircase must actually have been touched for this test to mean
    # anything -- if the optimizer left it alone the assertion below would
    # pass vacuously.
    assert _route_len(n_after) != pytest.approx(n_before, abs=1e-6), (
        "fixture did not exercise the optimizer's zigzag/staircase removal; "
        "adjust the staircase shape so this precondition holds"
    )
    assert abs(delta_after - delta_before) > 1e-6, (
        "expected the unprotected optimize call to silently change the "
        "pair's P/N length delta (in either direction -- #5333's own "
        "measured case made it WORSE, 0.045mm -> 0.482mm, but the bug is "
        "the optimizer changing a qualified pair's skew AT ALL without "
        "diff-pair awareness, not the direction) -- if this fails, "
        "TraceOptimizer's zigzag/staircase passes stopped being destructive "
        "and the underlying #5333 mechanism may need a different fixture"
    )


def test_diffpair_skip_nets_preserves_the_pair_length_match():
    """The #5333 fix: passing ``_diffpair_optimizer_skip_nets(router)`` as
    ``skip_nets`` -- what all four ``route_cmd.py`` call sites do now --
    leaves the qualified pair's geometry, and therefore its length match,
    untouched by this stage."""
    router = _staircase_optin_router()
    p_route = _build_p_route(router)
    n_route = _build_n_staircase_route(router)
    router._mark_route(p_route)
    router._mark_route(n_route)
    router.routes.append(p_route)
    router.routes.append(n_route)

    p_before = _route_len(p_route)
    n_before = _route_len(n_route)
    delta_before = abs(n_before - p_before)

    skip_nets = _diffpair_optimizer_skip_nets(router)
    assert skip_nets == {1, 2}, "both pair halves must be protected"

    optimizer, _ = _optimizer_and_checker(router)
    optimize_routes_grid_synced(router, optimizer, skip_nets=skip_nets)

    n_after = next(r for r in router.routes if r.net == 2)
    p_after = next(r for r in router.routes if r.net == 1)

    assert _route_len(n_after) == pytest.approx(n_before, abs=1e-9)
    assert _route_len(p_after) == pytest.approx(p_before, abs=1e-9)
    assert abs(_route_len(n_after) - _route_len(p_after) - delta_before) < 1e-9

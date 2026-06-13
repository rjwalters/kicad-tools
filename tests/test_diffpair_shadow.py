"""Tests for issue #3508: board 06 coupled-convergence machinery.

Covers the units this issue added or changed in
``kicad_tools.router.diffpair_routing``:

1. ``CoupledPathfinder._is_cell_blocked`` -- own-net passability.  Pad
   metal and clearance-halo cells carry ``is_obstacle = True`` on the
   grid (the #2915/#2940 negotiated-mode loophole guard); the coupled
   pathfinder must NOT treat that as a block for the pad's own net,
   matching the per-net pathfinder's ``cell.net != routing_net``
   convention.  Before the fix every pad was unreachable for its own
   coupled route and convergence was 0/9 at ANY budget.

2. ``CoupledPathfinder`` weighted A* (``heuristic_weight``) -- stored,
   clamped, and applied to ``f = g + w * h``.

3. ``create_serpentine`` partner-aware mode -- one-sided bulges (never
   toward the partner) and the triangular length arithmetic actually
   delivering the requested extra length.

4. ``DiffPairRouter`` geometry helpers -- ``_point_segment_distance``
   and ``_min_distance_to_partner``.

5. Mid-route asymmetric moves (issue #3508 relaxation of the #2490
   approach-phase-only restriction): asymmetric P-advance/N-advance
   moves are generated OUTSIDE the approach radius with a tight
   tolerance.

6. The trail PROXIMITY guard: an advancing trace may not land within
   ``min_spacing_cells`` (Euclidean, same layer) of the partner's
   accumulated trail -- the exact-cell guard alone admitted 1-cell
   passes (0.05 mm centerline distance = copper overlap).
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.diffpair_routing import (
    CoupledPathfinder,
    CoupledState,
    GridPos,
    create_serpentine,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def _make_pathfinder(**kwargs) -> CoupledPathfinder:
    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    defaults = {"grid": grid, "rules": rules, "target_spacing_cells": 4}
    defaults.update(kwargs)
    return CoupledPathfinder(**defaults)


# ---------------------------------------------------------------------------
# 1. own-net passability
# ---------------------------------------------------------------------------


def test_own_net_obstacle_cell_is_passable():
    """A blocked cell carrying the routing net is passable (pad metal/halo)."""
    pf = _make_pathfinder()
    cell = pf.grid.grid[0][10][10]
    cell.blocked = True
    cell.is_obstacle = True
    cell.net = 42
    assert pf._is_cell_blocked(10, 10, 0, 42) is False


def test_foreign_net_blocked_cell_is_blocked():
    pf = _make_pathfinder()
    cell = pf.grid.grid[0][10][10]
    cell.blocked = True
    cell.net = 7
    assert pf._is_cell_blocked(10, 10, 0, 42) is True


def test_net_zero_obstacle_blocked_for_signal_nets():
    """True obstacles (keepouts, board edge) carry net 0 and stay blocked."""
    pf = _make_pathfinder()
    cell = pf.grid.grid[0][10][10]
    cell.blocked = True
    cell.is_obstacle = True
    # cell.net stays 0
    assert pf._is_cell_blocked(10, 10, 0, 42) is True


# ---------------------------------------------------------------------------
# 2. weighted A*
# ---------------------------------------------------------------------------


def test_heuristic_weight_default_is_classic():
    pf = _make_pathfinder()
    assert pf.heuristic_weight == 1.0


def test_heuristic_weight_stored():
    pf = _make_pathfinder(heuristic_weight=1.5)
    assert pf.heuristic_weight == 1.5


def test_heuristic_weight_clamped_to_at_least_one():
    """Sub-1 weights would break A* termination guarantees -- clamped."""
    pf = _make_pathfinder(heuristic_weight=0.25)
    assert pf.heuristic_weight == 1.0


# ---------------------------------------------------------------------------
# 3. partner-aware serpentine
# ---------------------------------------------------------------------------


def _straight_route(net: int, name: str, y: float, length: float = 10.0) -> Route:
    r = Route(net=net, net_name=name)
    r.segments.append(
        Segment(
            x1=1.0,
            y1=y,
            x2=1.0 + length,
            y2=y,
            width=0.2,
            layer=Layer.F_CU,
            net=net,
            net_name=name,
        )
    )
    return r


def _route_length(route: Route) -> float:
    return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)


def test_partner_aware_serpentine_is_one_sided():
    """With a partner constraint, no bulge may cross toward the partner.

    Partner runs at y=10.35 above the target trace at y=10.0; every
    serpentine point must therefore stay at y <= 10.0 (bulge downward,
    away from the partner).
    """
    target = _straight_route(1, "P", 10.0)
    partner = _straight_route(2, "N", 10.35)
    ok = create_serpentine(
        target,
        length_to_add=2.0,
        partner_route=partner,
        intra_pair_clearance_mm=0.1,
    )
    assert ok, "partner-aware serpentine must succeed on an open straight run"
    for seg in target.segments:
        assert seg.y1 <= 10.0 + 1e-9 and seg.y2 <= 10.0 + 1e-9, (
            f"bulge crossed toward the partner: ({seg.x1},{seg.y1})->({seg.x2},{seg.y2})"
        )


def test_partner_aware_serpentine_adds_requested_length():
    """The triangular-bulge arithmetic must deliver ~length_to_add.

    The legacy square-bulge formula under-delivered by up to 10x
    (issue #3508); assert at least 70% of the request is realised.
    """
    target = _straight_route(1, "P", 10.0)
    partner = _straight_route(2, "N", 10.35)
    before = _route_length(target)
    requested = 2.5
    ok = create_serpentine(
        target,
        length_to_add=requested,
        partner_route=partner,
        intra_pair_clearance_mm=0.1,
    )
    assert ok
    added = _route_length(target) - before
    assert added >= 0.7 * requested, (
        f"serpentine added only {added:.3f}mm of the requested {requested:.3f}mm"
    )


def test_legacy_serpentine_still_alternates():
    """Without a partner constraint the legacy alternating wave persists."""
    target = _straight_route(1, "P", 10.0)
    ok = create_serpentine(target, length_to_add=2.0)
    assert ok
    above = any(s.y1 > 10.0 + 1e-9 or s.y2 > 10.0 + 1e-9 for s in target.segments)
    below = any(s.y1 < 10.0 - 1e-9 or s.y2 < 10.0 - 1e-9 for s in target.segments)
    assert above and below, "legacy serpentine should alternate sides"


# ---------------------------------------------------------------------------
# 4. geometry helpers
# ---------------------------------------------------------------------------


def _seg(x1, y1, x2, y2, layer=Layer.F_CU) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=1, net_name="P")


def test_point_segment_distance_perpendicular():
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    d = DiffPairRouter._point_segment_distance(5.0, 1.0, _seg(0, 0, 10, 0))
    assert d == pytest.approx(1.0)


def test_point_segment_distance_beyond_endpoint():
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    d = DiffPairRouter._point_segment_distance(12.0, 0.0, _seg(0, 0, 10, 0))
    assert d == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 5. mid-route asymmetric moves
# ---------------------------------------------------------------------------


def test_asymmetric_moves_generated_mid_route():
    """Asymmetric advance moves fire OUTSIDE the approach radius.

    Issue #3508: symmetric moves freeze the P->N offset vector, so a
    pair cannot turn a corner whose leg parallels the offset; the
    offset can only rotate via asymmetric moves, which #2490 had
    restricted to the approach phase.  Verify a state far from both
    start and goal produces at least one move where exactly one head
    advanced.
    """
    pf = _make_pathfinder(target_spacing_cells=4)
    state = CoupledState(GridPos(100, 100, 0), GridPos(100, 104, 0), (1, 0))
    neighbors = pf._get_coupled_neighbors(
        state,
        1,
        2,
        p_goal=GridPos(200, 100, 0),
        n_goal=GridPos(200, 104, 0),
        p_start=GridPos(10, 100, 0),
        n_start=GridPos(10, 104, 0),
    )
    asym = [
        (ns, c, v)
        for ns, c, v in neighbors
        if (ns.p_pos != state.p_pos) != (ns.n_pos != state.n_pos)
    ]
    assert asym, "expected asymmetric single-head moves mid-route"
    # Mid-route tolerance stays tight: every generated state keeps
    # spacing within +/-1 cell of the target.
    for ns, _c, is_via in neighbors:
        if is_via:
            continue
        spacing = math.hypot(ns.p_pos.x - ns.n_pos.x, ns.p_pos.y - ns.n_pos.y)
        assert abs(spacing - 4) <= 1 + 1e-9


# ---------------------------------------------------------------------------
# 6. trail proximity guard
# ---------------------------------------------------------------------------


def test_proximity_guard_rejects_near_partner_trail():
    """Landing 1 cell from the partner's trail is rejected.

    Exact-cell guards alone admitted 0.05 mm centerline passes (the
    measured MIPI_CLK -0.175 mm overlap).  With min_spacing_cells=4,
    a candidate 1 cell from a partner trail cell must be pruned.
    """
    pf = _make_pathfinder(target_spacing_cells=4, min_spacing_cells=4)
    state = CoupledState(GridPos(100, 100, 0), GridPos(100, 104, 0), (1, 0))
    # Partner (N) trail passes right next to where P wants to go.
    n_trail_cell = (101, 101, 0)
    buckets = {(n_trail_cell[0] // 4, n_trail_cell[1] // 4): [n_trail_cell]}
    neighbors = pf._get_coupled_neighbors(
        state,
        1,
        2,
        p_goal=GridPos(200, 100, 0),
        n_goal=GridPos(200, 104, 0),
        p_start=GridPos(10, 100, 0),
        n_start=GridPos(10, 104, 0),
        p_visited=frozenset({(100, 100, 0)}),
        n_visited=frozenset({n_trail_cell}),
        n_trail_buckets=buckets,
        p_trail_buckets={},
    )
    for ns, _c, is_via in neighbors:
        if is_via:
            continue
        if ns.p_pos != state.p_pos:  # P advanced
            d = math.hypot(ns.p_pos.x - 101, ns.p_pos.y - 101)
            assert d >= 4 - 1e-9, (
                f"P landed {d:.2f} cells from the partner trail (< min_spacing_cells=4): {ns.p_pos}"
            )


def test_proximity_guard_allows_exact_min_spacing():
    """Distance exactly == min_spacing_cells is NOT a violation."""
    pf = _make_pathfinder(target_spacing_cells=4, min_spacing_cells=4)
    state = CoupledState(GridPos(100, 100, 0), GridPos(100, 104, 0), (1, 0))
    # Partner trail directly above P's forward landing cell at exactly
    # 4 cells.
    n_trail_cell = (101, 96, 0)
    buckets = {(n_trail_cell[0] // 4, n_trail_cell[1] // 4): [n_trail_cell]}
    neighbors = pf._get_coupled_neighbors(
        state,
        1,
        2,
        p_goal=GridPos(200, 100, 0),
        n_goal=GridPos(200, 104, 0),
        p_start=GridPos(10, 100, 0),
        n_start=GridPos(10, 104, 0),
        p_visited=frozenset({(100, 100, 0)}),
        n_visited=frozenset({n_trail_cell}),
        n_trail_buckets=buckets,
        p_trail_buckets={},
    )
    forward = [
        ns for ns, _c, is_via in neighbors if not is_via and ns.p_pos == GridPos(101, 100, 0)
    ]
    assert forward, (
        "P's forward symmetric step at exactly min_spacing distance from "
        "the partner trail must be admitted"
    )


# ---------------------------------------------------------------------------
# 7. Issue #3508 decomposition: shadow-constructor opt-in gate
# ---------------------------------------------------------------------------


def test_shadow_construction_flag_defaults_off():
    """The geometric shadow constructor is opt-in (default False).

    The 2026-06-11 board 06 seed-42 integration run showed the
    constructor's committed geometry is not yet artifact-quality
    (stranded shadow tails, shadow-via/partner intersections, corridor
    competition stranding single-ended nets -> 16/21 reach).  Recipes
    must explicitly opt in once the follow-up issues land.
    """
    from kicad_tools.router.diffpair import DifferentialPairConfig

    assert DifferentialPairConfig().enable_shadow_construction is False
    assert DifferentialPairConfig(enabled=True).enable_shadow_construction is False


def test_shadow_construction_flag_plumbed_from_config():
    """``route_all_with_diffpairs`` copies the config flag onto the router."""
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.diffpair import DifferentialPairConfig

    rules = DesignRules()
    router = Autorouter(width=12.7, height=12.7, rules=rules)
    dpr = router._diffpair
    assert dpr.enable_shadow_construction is False

    # No pairs detected -> the call returns immediately, but the flag
    # must already have been copied from the config.
    router.route_all_with_diffpairs(
        diffpair_config=DifferentialPairConfig(enabled=True, enable_shadow_construction=True)
    )
    assert dpr.enable_shadow_construction is True

    router.route_all_with_diffpairs(diffpair_config=DifferentialPairConfig(enabled=True))
    assert dpr.enable_shadow_construction is False


# ---------------------------------------------------------------------------
# 8. Issue #3547: flag-off inertness of the #3508 coupled search upgrades
#
# PR #3546 shipped the #3508 coupled machinery as a gated opt-in
# (``enable_shadow_construction``, default False) with the contract that
# a flag-off run keeps recipes on their pre-#3508 budget-exit behaviour.
# Two pieces were found always-on (not gated by the flag):
#
#   1. the near-miss rescue (``_rescue_near_miss_coupled``), which commits
#      a coupled body + single-ended tails for a search that deferred, and
#   2. the CoupledPathfinder weighted-A* search upgrade
#      (``heuristic_weight=COUPLED_HEURISTIC_WEIGHT`` > 1), which changes
#      WHICH joint states the always-running coupled pre-phase explores --
#      so a search that DEFERRED on the pre-#3508 baseline can CONVERGE
#      (and commit) with the flag off, re-exposing the gated hazards
#      (#3542 corridor competition, #3544 pre-phase seg-seg violations).
#
# Both are now gated behind ``enable_shadow_construction``.  These tests
# drive ``route_differential_pair_coupled`` against a stubbed pathfinder
# so the flag-off/flag-on behaviour of each path is asserted directly:
#   - the search upgrade by capturing the ``heuristic_weight`` the
#     CoupledPathfinder is constructed with, and
#   - the rescue by spying on ``_rescue_near_miss_coupled``.
# ---------------------------------------------------------------------------


def _two_pad_coupled_router_and_pair():
    """A 2-pad diff pair + its router, ready for the coupled pre-phase.

    Returns ``(router, pair)`` where ``router._diffpair`` is the
    :class:`DiffPairRouter` under test and ``pair`` is a
    :class:`DifferentialPair` whose pads are registered on the router.
    """
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.diffpair import (
        DifferentialPair,
        DifferentialPairType,
        DifferentialSignal,
    )

    rules = DesignRules()
    router = Autorouter(width=30.0, height=10.0, rules=rules)
    p_y, n_y = 4.8, 5.2
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": p_y,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 5.0,
                "y": n_y,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
        ],
    )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 25.0,
                "y": p_y,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 25.0,
                "y": n_y,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
        ],
    )
    pair = DifferentialPair(
        name="USB_D",
        positive=DifferentialSignal(
            net_name="USB_D+",
            net_id=1,
            base_name="USB_D",
            polarity="P",
            notation="plus_minus",
        ),
        negative=DifferentialSignal(
            net_name="USB_D-",
            net_id=2,
            base_name="USB_D",
            polarity="N",
            notation="plus_minus",
        ),
        pair_type=DifferentialPairType.USB2,
    )
    return router, pair


class _StubPathfinder:
    """Stand-in for ``CoupledPathfinder`` with a scripted outcome.

    ``route_coupled`` returns ``_result`` (a committable (P, N) tuple to
    simulate a CONVERGED search, or ``None`` to simulate a DEFERRED one).
    The progress diagnostics are populated so the near-miss rescue branch
    is *eligible* to fire whenever the search deferred -- the only thing
    that should gate it is ``enable_shadow_construction``.
    """

    def __init__(self, result, rescue_eligible: bool = True):
        self._result = result
        self.last_timeout_exceeded = False
        self.last_iterations = 1
        self.last_best_progress = 0.0  # <= NEAR_MISS_RESCUE_CELLS
        self.last_best_state = object()
        # rescue eligibility requires a non-None last_best_node; tests that
        # are not exercising the rescue set this to None so the (real,
        # un-stubbed) rescue branch is never entered.
        self.last_best_node = object() if rescue_eligible else None
        self.last_rejections = {}

    def route_coupled(self, *_a, **_k):
        return self._result


def _patch_pathfinder_capture_weight(monkeypatch, result, rescue_eligible=True):
    """Patch the module ``CoupledPathfinder``; capture ``heuristic_weight``.

    Returns a ``captured`` dict whose ``"heuristic_weight"`` key records the
    value the pre-phase constructed the pathfinder with.
    """
    import kicad_tools.router.diffpair_routing as dpr_mod

    captured: dict[str, float] = {}

    def _factory(*_a, **kwargs):
        captured["heuristic_weight"] = kwargs.get("heuristic_weight")
        return _StubPathfinder(result, rescue_eligible=rescue_eligible)

    monkeypatch.setattr(dpr_mod, "CoupledPathfinder", _factory)
    return captured


def test_flag_off_uses_classic_astar_search(monkeypatch):
    """Flag OFF -> CoupledPathfinder built with classic A* (weight 1.0).

    The #3508 weighted-A* upgrade (``COUPLED_HEURISTIC_WEIGHT`` > 1) is
    what changes which joint states the search explores.  With
    ``enable_shadow_construction=False`` the pre-phase must construct the
    pathfinder with ``heuristic_weight == 1.0`` (the pre-#3508 search), so
    a search that deferred on the baseline still defers.
    """
    from kicad_tools.router.diffpair_routing import COUPLED_HEURISTIC_WEIGHT

    assert COUPLED_HEURISTIC_WEIGHT > 1.0, "fixture assumes the weighted-A* upgrade is > 1.0"
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = False
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    captured = _patch_pathfinder_capture_weight(monkeypatch, None, rescue_eligible=False)

    dpr.route_differential_pair_coupled(pair, coupled_only=True)

    assert captured.get("heuristic_weight") == 1.0, (
        "flag-off run must use classic optimal A* (heuristic_weight=1.0), "
        f"got {captured.get('heuristic_weight')}"
    )


def test_flag_on_uses_weighted_astar_search(monkeypatch):
    """Flag ON -> CoupledPathfinder built with the weighted-A* upgrade.

    Control for the search-upgrade gate: with
    ``enable_shadow_construction=True`` the pre-phase constructs the
    pathfinder with the #3508 ``COUPLED_HEURISTIC_WEIGHT``.
    """
    from kicad_tools.router.diffpair_routing import COUPLED_HEURISTIC_WEIGHT

    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = True
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    captured = _patch_pathfinder_capture_weight(monkeypatch, None, rescue_eligible=False)

    dpr.route_differential_pair_coupled(pair, coupled_only=True)

    assert captured.get("heuristic_weight") == COUPLED_HEURISTIC_WEIGHT, (
        "flag-on run must use the weighted-A* upgrade "
        f"({COUPLED_HEURISTIC_WEIGHT}), got {captured.get('heuristic_weight')}"
    )


def test_flag_off_does_not_invoke_near_miss_rescue(monkeypatch):
    """Flag OFF + search DEFERS near the goal -> rescue is NOT invoked.

    The stub pathfinder returns ``None`` with ``last_best_progress=0`` and
    a non-None ``last_best_node``, i.e. the exact precondition that makes
    the near-miss rescue eligible.  With the flag off the rescue must not
    even be called (spy asserts zero invocations).
    """
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = False
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    _patch_pathfinder_capture_weight(monkeypatch, None)

    calls = {"n": 0}

    def _spy(self, *a, **k):
        calls["n"] += 1
        return None

    monkeypatch.setattr(type(dpr), "_rescue_near_miss_coupled", _spy, raising=True)

    dpr.route_differential_pair_coupled(pair, coupled_only=True)

    assert calls["n"] == 0, (
        "near-miss rescue must NOT be invoked when enable_shadow_construction is False"
    )


def test_flag_on_invokes_near_miss_rescue(monkeypatch):
    """Flag ON + search DEFERS near the goal -> rescue IS invoked.

    Control for the rescue gate: same deferred-near-goal precondition, but
    with ``enable_shadow_construction=True`` the rescue is called.
    """
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = True
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    _patch_pathfinder_capture_weight(monkeypatch, None)

    calls = {"n": 0}

    def _spy(self, *a, **k):
        calls["n"] += 1
        return None  # rescue declines; we only assert it was consulted

    monkeypatch.setattr(type(dpr), "_rescue_near_miss_coupled", _spy, raising=True)

    dpr.route_differential_pair_coupled(pair, coupled_only=True)

    assert calls["n"] == 1, (
        "near-miss rescue must be invoked when enable_shadow_construction is True"
    )


# ---------------------------------------------------------------------------
# 9. Issue #3540: transactional pad-connectivity claim
#
# The shadow constructor (and its rescue-tail / stub-edge machinery) can
# commit copper that fails to actually REACH a goal pad while the per-spec
# commit has already marked that copper on the grid.  Left as-is the caller
# claims the pair's nets (#2464 reserve), the negotiated main strategy
# skips them, and the goal pads are STRANDED for the rest of the pipeline.
#
# ``route_differential_pair_coupled`` must make the claim TRANSACTIONAL:
# after committing the pair's copper (body + tails + stub edges), it
# verifies every pad of BOTH nets is reached.  On any gap it rips the
# pair's copper off the grid + route list and defers the whole pair
# (returns ``([], None)`` under ``coupled_only`` so the caller never
# claims, or falls through to the single-ended router otherwise).
# ---------------------------------------------------------------------------


def _coupled_routes_for_pair(pair, p_end_x: float, n_end_x: float) -> tuple[Route, Route]:
    """Build a committable (P, N) result for the 2-pad fixture.

    The U1 pads sit at x=5.0 and the J1 pads at x=25.0 (y=4.8 for P,
    y=5.2 for N).  Each returned route is a single horizontal segment
    from the U1 pad to ``*_end_x``; passing 25.0 reaches the J1 goal
    pad, while a shorter value (e.g. 15.0) STRANDS it -- the exact
    "claimed-but-unconnected goal pad" failure mode #3540 fixes.
    """
    p_route = Route(net=pair.positive.net_id, net_name=pair.positive.net_name)
    p_route.segments.append(
        Segment(
            x1=5.0,
            y1=4.8,
            x2=p_end_x,
            y2=4.8,
            width=0.2,
            layer=Layer.F_CU,
            net=pair.positive.net_id,
            net_name=pair.positive.net_name,
        )
    )
    n_route = Route(net=pair.negative.net_id, net_name=pair.negative.net_name)
    n_route.segments.append(
        Segment(
            x1=5.0,
            y1=5.2,
            x2=n_end_x,
            y2=5.2,
            width=0.2,
            layer=Layer.F_CU,
            net=pair.negative.net_id,
            net_name=pair.negative.net_name,
        )
    )
    return p_route, n_route


def test_shadow_claim_rolls_back_when_goal_pad_stranded(monkeypatch):
    """Flag ON + a committed route that strands a goal pad -> full rollback.

    The stub pathfinder converges with a P route that stops at x=15.0 --
    10 mm short of the J1.1 goal pad at x=25.0 -- so the P net has only
    1 of its 2 pads reachable from the committed copper.  The
    transactional claim must:

      * NOT return any routes (so the caller never claims the nets),
      * leave the autorouter's route list empty (the committed P and N
        copper is ripped), and
      * unmark every cell it had marked on the grid (no stranded copper).
    """
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = True
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    # P strands its J1 goal pad (stops at x=15.0); N reaches its goal.
    stranded = _coupled_routes_for_pair(pair, p_end_x=15.0, n_end_x=25.0)
    _patch_pathfinder_capture_weight(monkeypatch, stranded, rescue_eligible=False)

    grid = router.grid

    def _pair_cell_count() -> int:
        net_arr = grid._net
        return int(((net_arr == pair.positive.net_id) | (net_arr == pair.negative.net_id)).sum())

    occupied_before = _pair_cell_count()

    routes, warning = dpr.route_differential_pair_coupled(pair, coupled_only=True)

    assert routes == [], (
        "a shadow pair that strands a goal pad must NOT return routes "
        "(returning routes is what makes the caller claim the nets)"
    )
    assert warning is None
    # The committed copper was ripped from the autorouter's route list.
    assert not any(r.net in (pair.positive.net_id, pair.negative.net_id) for r in router.routes), (
        "stranded pair copper must be removed from autorouter.routes on rollback"
    )
    # And every cell it marked on the grid was unmarked (clean rollback).
    occupied_after = _pair_cell_count()
    assert occupied_after == occupied_before, (
        "rollback must unmark the stranded pair's copper from the grid "
        f"(cells before={occupied_before}, after={occupied_after})"
    )


def test_shadow_claim_commits_when_all_pads_reached(monkeypatch):
    """Control: a shadow pair that reaches every goal pad IS claimed.

    Same converged-search setup as the rollback test, but both routes run
    fully from the U1 pads to the J1 pads, so both nets are fully
    connected.  The transactional check must let the claim stand: routes
    are returned and the copper stays committed on the autorouter.
    """
    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = True
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    good = _coupled_routes_for_pair(pair, p_end_x=25.0, n_end_x=25.0)
    _patch_pathfinder_capture_weight(monkeypatch, good, rescue_eligible=False)

    routes, _warning = dpr.route_differential_pair_coupled(pair, coupled_only=True)

    p_nets = {r.net for r in routes}
    assert pair.positive.net_id in p_nets and pair.negative.net_id in p_nets, (
        "a fully-connected shadow pair must return both nets' routes so the caller claims them"
    )
    assert any(r.net == pair.positive.net_id for r in router.routes)
    assert any(r.net == pair.negative.net_id for r in router.routes)


# ---------------------------------------------------------------------------
# 10. Issue #3541: shadow via must not intersect the partner trace
#
# When the geometric shadow constructor places its own via before a guide
# layer change, the barrel sits at a perpendicular offset taken against the
# INCOMING guide leg's normal.  The guide BENDS at the via, so an offset
# that clears the incoming leg can still let the barrel intersect the
# OUTGOING leg when the guide turns back toward the shadow side.  At board
# 06's tightly-coupled gaps (0.075-0.15 mm) this produced a ~0.04 mm
# physical overlap between the shadow via and the partner copper -- a short
# that the recipe's 6b audit ripped (USB2_D / USB3_RX1 / USB3_RX2 / PCIE_TX
# de-coupled).
#
# The fix validates each candidate via site against the WHOLE guide
# polyline (barrel vs any-layer copper) with the same
# ``via_diameter/2 + trace_clearance + guide_width/2`` bound the crossing-
# tail synthesizer uses, and widens the perpendicular spread (the
# ``lat_mult`` lattice) until a site clears every guide segment.
# ---------------------------------------------------------------------------


def _via_clearance_bound(rules, guide) -> float:
    """The #3541 via-barrel-vs-partner bound the constructor enforces."""
    guide_width = max((g.width for g in guide.segments), default=rules.trace_width)
    return rules.via_diameter / 2 + rules.trace_clearance + guide_width / 2


def _layer_change_guide(p_start_pad, bend_end: tuple[float, float]) -> Route:
    """F_CU leg -> via at (12.0, 4.8) -> B_CU leg toward ``bend_end``.

    The pre-via leg approaches the layer change along +x; the post-via leg
    heads toward ``bend_end``.  Choosing a ``bend_end`` that sweeps the
    out-going leg back through the shadow-via neighbourhood is what made the
    pre-#3541 minimum-lateral via intersect the partner.
    """
    net, name = p_start_pad.net, p_start_pad.net_name
    guide = Route(net=net, net_name=name)
    guide.segments.append(
        Segment(
            x1=5.0,
            y1=4.8,
            x2=12.0,
            y2=4.8,
            width=0.2,
            layer=Layer.F_CU,
            net=net,
            net_name=name,
        )
    )
    guide.segments.append(
        Segment(
            x1=12.0,
            y1=4.8,
            x2=bend_end[0],
            y2=bend_end[1],
            width=0.2,
            layer=Layer.B_CU,
            net=net,
            net_name=name,
        )
    )
    guide.vias.append(
        Via(
            x=12.0,
            y=4.8,
            drill=0.35,
            diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU),
            net=net,
            net_name=name,
        )
    )
    return guide


def _shadow_setup(spacing_cells: int, bend_end: tuple[float, float]):
    """Build (dpr, pair, spec, pathfinder, guide) for the via-geometry test.

    Open 2-pad fixture (no obstacles), a tight coupled spacing, and a
    layer-changing guide bending toward ``bend_end``.
    """
    from kicad_tools.router.diffpair_routing import CoupledPathfinder, CoupledSegmentSpec

    router, pair = _two_pad_coupled_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = True
    spec = CoupledSegmentSpec(
        p_start=router.pads[("U1", "1")],
        p_end=router.pads[("J1", "1")],
        n_start=router.pads[("U1", "2")],
        n_end=router.pads[("J1", "2")],
    )
    pathfinder = CoupledPathfinder(
        grid=router.grid,
        rules=router.rules,
        target_spacing_cells=spacing_cells,
        net_class_map=getattr(router, "net_class_map", None),
    )
    guide = _layer_change_guide(router.pads[("U1", "1")], bend_end)
    return dpr, pair, spec, pathfinder, guide


def _stub_tail_route(self, _pf, head, goal, _layer, _label, _name, partner_segments=None):
    """Degenerate body-anchor tail (isolates the via geometry under test).

    Routing the real pad-reaching tail would have a straight fake tail
    cross the guide and trip the constructor's separate intra-pair / overlap
    self-checks -- artifacts unrelated to the #3541 via geometry.  A
    near-zero stub at the body anchor leaves the body's via intact.
    """
    r = Route(net=goal.net, net_name=goal.net_name)
    r.segments.append(
        Segment(
            x1=head.x,
            y1=head.y,
            x2=head.x + 0.001,
            y2=head.y,
            width=0.2,
            layer=head.layer,
            net=goal.net,
            net_name=goal.net_name,
        )
    )
    return r


# The #3541 load-bearing geometry: an out-going B_CU leg bending up-and-LEFT
# (``bend_end`` below x=12.0) that sweeps the guide back through the side=+1.0
# minimum-lateral (lat_mult=1.0) shadow-via neighbourhood.  At this geometry
# the un-spread via barrel lands 0.354 mm from the guide -- a SHORT against the
# ~0.65 mm ``via_clear`` bound -- and the guide's own seg-vs-seg self-check
# (``find_intra_pair_clearance_violations``) does NOT see it (it is a
# via-vs-trace overlap, not seg-vs-seg), so WITHOUT the guard the constructor
# COMMITS the shorting via.  WITH the guard the lattice widens to lat_mult>1.0
# and the via clears at 0.700 mm.  (Verified by deleting the guard: the
# committed via min-dist drops 0.700 -> 0.354 mm.)
_SHADOW_VIA_GUARD_BEND_END = (11.0, 7.7)


def test_shadow_via_clears_partner_at_tight_gap(monkeypatch):
    """End-to-end: the constructed shadow via clears the partner copper.

    Drives ``_shadow_route_pair`` with a layer-changing guide and a tightly-
    coupled spacing (4 cells = 0.4 mm, well below the ~0.65 mm via bound).
    Every via the shadow places must clear EVERY guide segment by at least
    ``via_diameter/2 + trace_clearance + guide_width/2`` -- the geometric
    guarantee the perpendicular spread provides (issue #3541 acceptance:
    "via_edge -> partner_copper >= trace_clearance, validated cell-by-cell").

    Uses the load-bearing geometry (:data:`_SHADOW_VIA_GUARD_BEND_END`) at
    which the un-spread minimum-lateral via shorts the guide, so the assertion
    is only satisfiable because the guard widened the lattice -- see
    ``test_shadow_via_guard_is_load_bearing`` for the matching negative control.
    """
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    spacing_cells = 4
    dpr, pair, spec, pathfinder, guide = _shadow_setup(
        spacing_cells, bend_end=_SHADOW_VIA_GUARD_BEND_END
    )

    monkeypatch.setattr(DiffPairRouter, "_tail_route", _stub_tail_route, raising=True)
    # Defeat the belt-and-braces overlap gate so we observe the via the
    # constructor PRODUCES (the fix must clear the guide by construction,
    # not merely fail the side over to a reject-everything None).
    monkeypatch.setattr(
        DiffPairRouter, "_pair_has_physical_overlap", lambda self, p, n: False, raising=True
    )

    result = dpr._shadow_route_pair(pair, spec, pathfinder, guide, spacing_cells)
    assert result is not None, "shadow constructor should place a clearing via, not give up"
    _p_route, n_route = result  # P is the guide; N is the geometric shadow.

    via_clear = _via_clearance_bound(dpr.autorouter.rules, guide)
    assert n_route.vias, "the shadow must carry its own layer-change via"
    for via in n_route.vias:
        for seg in guide.segments:
            dist = DiffPairRouter._point_segment_distance(via.x, via.y, seg)
            assert dist >= via_clear - 1e-9, (
                f"shadow via at ({via.x:.3f},{via.y:.3f}) intersects partner "
                f"copper: centerline distance {dist:.4f} mm < required "
                f"{via_clear:.4f} mm (barrel overlaps the guide trace)"
            )


def test_shadow_via_guard_is_load_bearing(monkeypatch):
    """Negative control: deleting the guard makes ``_shadow_route_pair`` short.

    This is the integration-level proof the #3541 guard is load-bearing -- it
    drives ``_shadow_route_pair`` END TO END (asserting on the route it
    RETURNS, not on the geometry helper) and contrasts two runs on the same
    load-bearing geometry (:data:`_SHADOW_VIA_GUARD_BEND_END`):

      * **Guard present** (production): the minimum-lateral (lat_mult=1.0) via
        site grazes the out-going guide leg, so the guard rejects it and the
        lattice widens; the committed via clears the guide by >= ``via_clear``.
      * **Guard absent** (the guard predicate stubbed out via
        ``_min_distance_to_partner`` -> +inf, the ONLY call site of that helper
        inside ``_shadow_route_pair``): the constructor COMMITS the grazing
        lat_mult=1.0 via, whose barrel sits < ``via_clear`` from the guide -- a
        short.  The seg-vs-seg self-check (``find_intra_pair_clearance_violations``)
        does NOT catch it because the overlap is via-vs-trace, not seg-vs-seg.

    The downstream ``_pair_has_physical_overlap`` belt-and-braces gate IS the
    backstop that would otherwise defer this short in production, so it is
    stubbed off in BOTH runs to isolate the guard's contribution -- exactly the
    PCIE_TX 0.0%-continuity short #3541 locks down (a future refactor that
    silently drops the guard re-exposes it, and this test then fails).
    """
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    spacing_cells = 4

    def _committed_shadow_via(disable_guard: bool):
        # A fresh patch context per run: the guard is patched out ONLY for the
        # unguarded run, then reverted, so the guarded run sees the real guard.
        with monkeypatch.context() as mp:
            dpr, pair, spec, pathfinder, guide = _shadow_setup(
                spacing_cells, bend_end=_SHADOW_VIA_GUARD_BEND_END
            )
            mp.setattr(DiffPairRouter, "_tail_route", _stub_tail_route, raising=True)
            # Stub OFF the belt-and-braces overlap gate in both runs: it is the
            # production backstop, but here we are isolating the GUARD's effect,
            # so what the constructor COMMITS (clean vs short) must be the
            # guard's doing, not the gate's.
            mp.setattr(
                DiffPairRouter, "_pair_has_physical_overlap", lambda self, p, n: False, raising=True
            )
            if disable_guard:
                # ``_min_distance_to_partner`` is called in exactly one place
                # inside ``_shadow_route_pair`` -- the #3541 via-vs-guide guard.
                # Forcing it to +inf makes every candidate pass the guard, i.e.
                # deletes the guard.
                mp.setattr(
                    DiffPairRouter,
                    "_min_distance_to_partner",
                    lambda self, *a, **k: float("inf"),
                    raising=True,
                )
            result = dpr._shadow_route_pair(pair, spec, pathfinder, guide, spacing_cells)
        assert result is not None, "shadow constructor should commit a route at this geometry"
        _p_route, n_route = result
        assert n_route.vias, "the shadow must carry its own layer-change via"
        via_clear = _via_clearance_bound(dpr.autorouter.rules, guide)
        min_dist = min(
            DiffPairRouter._point_segment_distance(via.x, via.y, seg)
            for via in n_route.vias
            for seg in guide.segments
        )
        return min_dist, via_clear

    # Guard ABSENT: the constructor commits the grazing lat_mult=1.0 via -- a
    # short.  This is the assertion that FAILS if the guard is restored, and
    # (equivalently) PASSES only because deleting the guard re-introduces the
    # #3541 short -- proving the guard is what prevents it.
    unguarded_dist, via_clear = _committed_shadow_via(disable_guard=True)
    assert unguarded_dist < via_clear, (
        "negative control failed: with the #3541 guard deleted, "
        "_shadow_route_pair must COMMIT a shorting via (barrel-to-guide "
        f"{unguarded_dist:.4f} mm < required {via_clear:.4f} mm).  If this "
        "passes, the geometry no longer exercises the guard and the positive "
        "case below is vacuous."
    )

    # Guard PRESENT (production): the same geometry now clears, because the
    # guard rejected the grazing site and the lattice widened.
    guarded_dist, _via_clear = _committed_shadow_via(disable_guard=False)
    assert guarded_dist >= via_clear - 1e-9, (
        "with the #3541 guard active the committed shadow via must clear the "
        f"guide (barrel-to-guide {guarded_dist:.4f} mm >= {via_clear:.4f} mm)"
    )
    # And the guard's effect is the difference between the two runs.
    assert guarded_dist > unguarded_dist, (
        "the guard must change the committed geometry: clearing via "
        f"({guarded_dist:.4f} mm) vs grazing via ({unguarded_dist:.4f} mm)"
    )

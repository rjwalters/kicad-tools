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
from kicad_tools.router.primitives import Route, Segment
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

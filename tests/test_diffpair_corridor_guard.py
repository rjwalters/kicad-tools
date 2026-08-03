"""Tests for the corridor-yield recovery (Issue #4463).

The coupled diff-pair pre-phase claims its corridors before the main
strategy runs, and its committed copper is **non-rippable** afterwards:
the negotiated loop's rip-up machinery only knows about the nets it
routes itself, so a single-ended net whose only corridor was sealed by a
committed coupled body can never be recovered.  Measured on board 06
shadow-ON (seed 42, main @ 0a8d724e): ``MIPI_D0+``, ``MIPI_D0-`` and
``USB_CC1`` all strand, every rip-up round rolls back with
``blocked only by non-rippable copper of <pair nets>``, and the loop
burns its entire 10-iteration ceiling (362.3 s) before giving up at
18/21 reach.

``DiffPairRouter._plan_corridor_yields`` closes that hole after the main
strategy, on ground truth: for every net the strategy actually failed to
connect it asks whether lifting the coupled copper makes the net routable,
and names the pairs standing on the resulting path.  ``route_all_with_
diffpairs`` then rips exactly those, re-runs the strategy once so the
negotiated loop can arrange the freed corridor globally, and keeps the
trade only when reach improved.

These tests cover the four units that make that safe:

1. ``_copper_conflicts`` -- which committed copper stands on the path a
   stranded net wants, and therefore has to yield.
2. ``_net_is_connected`` -- the reach oracle the trade is scored with.
3. ``_probe_net_routable`` -- the probe must leave NO trace on the grid
   or in ``autorouter.routes`` (it is a question, not a commitment).
4. ``_plan_corridor_yields`` + the orchestration around it -- a sealing
   pair is named, a distant pair is not, the plan commits nothing, an
   unrecoverable net costs no pair, a yield that gains no reach is
   reverted, and none of it runs with shadow construction off.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import (
    DifferentialPair,
    DifferentialPairConfig,
    DifferentialSignal,
)
from kicad_tools.router.diffpair_routing import DiffPairRouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def _seg(x1: float, y1: float, x2: float, y2: float, layer: Layer, net: int = 1) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=net)


# ---------------------------------------------------------------------------
# Fixture: a board whose single-ended net must cross the pair's corridor
# ---------------------------------------------------------------------------


def _channel_router() -> Autorouter:
    """``SIG`` (net 3) has one pad on each side of a horizontal channel.

    Copper across the whole channel on every layer therefore strands
    ``SIG`` exactly the way a committed coupled body strands ``MIPI_D0``
    on board 06.
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)
    router = Autorouter(width=20.0, height=8.0, rules=rules)
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 2.0,
                "y": 1.0,
                "width": 0.6,
                "height": 0.6,
                "net": 3,
                "net_name": "SIG",
            },
        ],
    )
    router.add_component(
        "U2",
        [
            {
                "number": "1",
                "x": 18.0,
                "y": 7.0,
                "width": 0.6,
                "height": 0.6,
                "net": 3,
                "net_name": "SIG",
            },
        ],
    )
    return router


def _wall_routes(router: Autorouter) -> list[Route]:
    """Copper across the FULL width of the board on every routable layer."""
    routes: list[Route] = []
    for idx in router.grid.get_routable_indices():
        layer = Layer(router.grid.index_to_layer(idx))
        routes.append(
            Route(
                net=1,
                net_name="PAIR+",
                segments=[_seg(0.0, 4.0, 20.0, 4.0, layer, net=1)],
            )
        )
    return routes


def _pair() -> DifferentialPair:
    return DifferentialPair(
        name="PAIR",
        positive=DifferentialSignal("PAIR+", 1, "PAIR", "P", "plus_minus"),
        negative=DifferentialSignal("PAIR-", 2, "PAIR", "N", "plus_minus"),
    )


def _other_pair() -> DifferentialPair:
    return DifferentialPair(
        name="OTHER",
        positive=DifferentialSignal("OTHER+", 4, "OTHER", "P", "plus_minus"),
        negative=DifferentialSignal("OTHER-", 5, "OTHER", "N", "plus_minus"),
    )


def _bystander() -> list[Route]:
    return [
        Route(
            net=4,
            net_name="OTHER+",
            segments=[_seg(0.5, 0.4, 3.0, 0.4, Layer.F_CU, net=4)],
        )
    ]


def _commit(router: Autorouter, routes: list[Route]) -> None:
    for route in routes:
        router._mark_route(route)
        router.routes.append(route)


# ---------------------------------------------------------------------------
# 1. _copper_conflicts -- what stands on the desired path
# ---------------------------------------------------------------------------


def test_copper_conflicts_same_layer_touching_copper():
    a = [Route(net=1, net_name="A", segments=[_seg(0.0, 0.0, 10.0, 0.0, Layer.F_CU)])]
    b = [Route(net=2, net_name="B", segments=[_seg(0.0, 0.25, 10.0, 0.25, Layer.F_CU, net=2)])]
    # Centreline gap 0.25 mm minus two 0.1 mm half-widths = 0.05 mm of air,
    # which is under the 0.2 mm clearance -> a genuine conflict.
    assert DiffPairRouter._copper_conflicts(a, b, clearance=0.2) is True


def test_copper_conflicts_same_layer_far_apart():
    a = [Route(net=1, net_name="A", segments=[_seg(0.0, 0.0, 10.0, 0.0, Layer.F_CU)])]
    b = [Route(net=2, net_name="B", segments=[_seg(0.0, 5.0, 10.0, 5.0, Layer.F_CU, net=2)])]
    assert DiffPairRouter._copper_conflicts(a, b, clearance=0.2) is False


def test_copper_conflicts_ignores_other_layers():
    a = [Route(net=1, net_name="A", segments=[_seg(0.0, 0.0, 10.0, 0.0, Layer.F_CU)])]
    b = [Route(net=2, net_name="B", segments=[_seg(0.0, 0.0, 10.0, 0.0, Layer.B_CU, net=2)])]
    assert DiffPairRouter._copper_conflicts(a, b, clearance=0.2) is False


def test_copper_conflicts_via_blocks_every_layer():
    """A through via is an obstacle on layers its owner never traced on."""
    via = Via(x=5.0, y=0.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
    a = [Route(net=1, net_name="A", segments=[], vias=[via])]
    b = [Route(net=2, net_name="B", segments=[_seg(0.0, 0.3, 10.0, 0.3, Layer.B_CU, net=2)])]
    assert DiffPairRouter._copper_conflicts(a, b, clearance=0.2) is True


# ---------------------------------------------------------------------------
# 2. _net_is_connected -- the reach oracle
# ---------------------------------------------------------------------------


def test_net_is_connected_is_false_without_copper():
    router = _channel_router()
    assert router._diffpair._net_is_connected(3) is False


def test_net_is_connected_is_true_for_a_single_pad_net():
    router = _channel_router()
    # Net 99 has no pads at all -- nothing to connect, nothing to strand.
    assert router._diffpair._net_is_connected(99) is True


# ---------------------------------------------------------------------------
# 3. _probe_net_routable -- a probe must leave no trace
# ---------------------------------------------------------------------------


def test_probe_leaves_no_copper_behind():
    router = _channel_router()
    dp = router._diffpair
    routes_before = len(router.routes)

    probe = dp._probe_net_routable(3, per_net_timeout=10.0)

    assert probe, "expected the unobstructed single-ended net to be routable"
    assert len(router.routes) == routes_before, (
        "the probe committed copper to autorouter.routes instead of rolling it back"
    )
    claimed = sum(
        1
        for idx in router.grid.get_routable_indices()
        for y in range(router.grid.rows)
        for x in range(router.grid.cols)
        if router.grid.grid[idx][y][x].net == 3 and not router.grid.grid[idx][y][x].is_obstacle
    )
    assert claimed == 0, f"{claimed} grid cell(s) still claimed by the probed net"


def test_probe_reports_a_sealed_net_as_unroutable():
    router = _channel_router()
    _commit(router, _wall_routes(router))
    assert router._diffpair._probe_net_routable(3, per_net_timeout=10.0) is None


# ---------------------------------------------------------------------------
# 4. _plan_corridor_yields
# ---------------------------------------------------------------------------


def test_plan_names_the_pair_that_seals_the_corridor():
    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    _commit(router, wall)
    assert dp._net_is_connected(3) is False

    to_yield, stranded = dp._plan_corridor_yields([3], [(_pair(), wall)])

    assert [p.name for p, _r in to_yield] == ["PAIR"]
    assert stranded == [3]
    # Planning commits nothing: the board is exactly as it was found.
    for route in wall:
        assert route in router.routes
    assert dp._net_is_connected(3) is False


def test_plan_skips_a_pair_that_is_not_the_blocker():
    """A pair that does not sit on the stranded net's path is not planned."""
    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    bystander = _bystander()
    _commit(router, [*wall, *bystander])

    to_yield, _stranded = dp._plan_corridor_yields(
        [3], [(_other_pair(), bystander), (_pair(), wall)]
    )

    assert [p.name for p, _r in to_yield] == ["PAIR"], (
        "only the sealing pair may be asked to yield its corridor"
    )
    for route in bystander:
        assert route in router.routes


def test_plan_is_empty_when_nothing_is_stranded():
    router = _channel_router()
    dp = router._diffpair
    bystander = _bystander()
    _commit(router, bystander)
    # Route SIG normally so it is connected before the pass runs.
    for route in router.route_net(3):
        assert route.net == 3
    assert dp._net_is_connected(3) is True

    assert dp._plan_corridor_yields([3], [(_other_pair(), bystander)]) == ([], [])
    for route in bystander:
        assert route in router.routes


def test_plan_leaves_an_unrecoverable_net_alone():
    """A net that stays unroutable with the coupled copper lifted is left be.

    ``SIG`` here is walled off by a foreign-net obstacle the pre-phase did
    not create, so yielding coupled corridors cannot help and no pair may
    be sacrificed for it.
    """
    router = _channel_router()
    dp = router._diffpair
    bystander = _bystander()
    _commit(router, bystander)
    # A wall owned by a net that is NOT a yield candidate.
    foreign = [
        Route(
            net=7,
            net_name="WALL",
            segments=[_seg(0.0, 4.0, 20.0, 4.0, Layer(router.grid.index_to_layer(idx)), net=7)],
        )
        for idx in router.grid.get_routable_indices()
    ]
    _commit(router, foreign)

    to_yield, stranded = dp._plan_corridor_yields([3], [(_other_pair(), bystander)])

    assert to_yield == []
    assert stranded == [3]
    for route in bystander:
        assert route in router.routes


def test_yield_is_reverted_when_it_does_not_gain_reach():
    """The trade is transactional on REACH.

    A yield that frees a corridor the strategy cannot use must put the
    coupled copper back -- the board 06 shadow-ON measurement that took
    reach from 18/21 to 15/21 was exactly this case scored dishonestly.
    """
    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    _commit(router, wall)

    to_yield, _stranded = dp._plan_corridor_yields([3], [(_pair(), wall)])
    assert [p.name for p, _r in to_yield] == ["PAIR"]

    # A strategy that routes nothing: the freed corridor buys no reach.
    kept, released, removed, added = dp._apply_corridor_yields(to_yield, [3], lambda: [])

    assert (kept, released, removed, added) == (False, set(), [], [])
    assert dp._net_is_connected(3) is False
    for route in wall:
        assert route in router.routes, "a reverted yield must restore the pair's copper"


def test_yield_is_kept_when_the_re_run_lands_the_stranded_net():
    """A yield that lets the strategy connect the stranded net is kept."""
    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    _commit(router, wall)

    to_yield, _stranded = dp._plan_corridor_yields([3], [(_pair(), wall)])

    def _strategy() -> list[Route]:
        return router.route_net(3)

    kept, released, removed, added = dp._apply_corridor_yields(to_yield, [3], _strategy)

    assert kept is True
    assert released == {1, 2}
    assert len(removed) == len(wall)
    assert added, "the re-run's copper must be returned to the caller"
    assert dp._net_is_connected(3) is True
    for route in wall:
        assert route not in router.routes


def test_recovery_does_not_run_with_shadow_construction_off():
    """Shadow-OFF runs (every board in CI today) never enter the recovery."""
    router = _channel_router()
    dp = router._diffpair
    calls: list[object] = []

    def _boom(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("the corridor-yield planner must not run with shadow OFF")

    dp._plan_corridor_yields = _boom  # type: ignore[method-assign]
    dp._apply_corridor_yields = _boom  # type: ignore[method-assign]
    config = DifferentialPairConfig(enabled=True, enable_shadow_construction=False)

    router.route_all_with_diffpairs(config, non_diffpair_strategy=lambda: [])

    assert calls == []
    assert getattr(router, "_coupled_prephase_stall_exit", False) is False

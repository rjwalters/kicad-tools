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

import pytest

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


# ---------------------------------------------------------------------------
# Issue #5895: pose-centerline trunks are first-class yield candidates
# ---------------------------------------------------------------------------


def test_yield_candidates_are_every_pair_with_shadow_on():
    router = _channel_router()
    dp = router._diffpair
    dp.enable_shadow_construction = True
    pair, other = _pair(), _other_pair()
    committed = [(pair, []), (other, [])]

    assert dp._corridor_yield_candidates(committed, set()) == committed


def test_yield_candidates_are_only_pose_trunks_with_shadow_off():
    """Shadow OFF: a pose trunk may yield; a joint-state pair may not.

    Board 06 (seed 42) with the pose search on: the pose-coupled MIPI_D0
    trunk seals MIPI_RST's corridor (reach 21/21 -> 20/21).  Joint-state
    pairs keep their pre-#5895 shadow-OFF behaviour (never yielded).
    """
    router = _channel_router()
    dp = router._diffpair
    dp.enable_shadow_construction = False
    pair, other = _pair(), _other_pair()
    committed = [(pair, []), (other, [])]

    assert dp._corridor_yield_candidates(committed, set()) == []
    assert dp._corridor_yield_candidates(committed, {id(other)}) == [(other, [])]


def test_yielded_pose_claim_is_released_and_restored_on_revert():
    """A yielded pose trunk's nets leave the two-phase claim set (#5895).

    The two-phase main pass skips claimed nets, so a yielded pose pair
    whose claim survived would never be re-routed.  A reverted yield puts
    the claim back with the copper.
    """
    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    _commit(router, wall)
    router._pose_coupled_nets = {1, 2}

    to_yield, _stranded = dp._plan_corridor_yields([3], [(_pair(), wall)])
    claims_during: list[set[int]] = []

    def _strategy() -> list[Route]:
        claims_during.append(set(router._pose_coupled_nets))
        return []

    kept, _released, _removed, _added = dp._apply_corridor_yields(to_yield, [3], _strategy)

    assert claims_during == [set()], "the re-run must see the pose claim released"
    assert kept is False
    assert router._pose_coupled_nets == {1, 2}, "a reverted yield restores the claim"


def test_recovery_does_not_run_without_a_pose_trunk_when_shadow_is_off():
    """Pose search ON but no trunk committed: still the pre-#5895 pipeline."""
    router = _channel_router()
    dp = router._diffpair
    dp.enable_pose_centerline = True
    calls: list[object] = []

    def _boom(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("no pose trunk committed -> no corridor-yield recovery")

    dp._plan_corridor_yields = _boom  # type: ignore[method-assign]
    dp._apply_corridor_yields = _boom  # type: ignore[method-assign]
    config = DifferentialPairConfig(enabled=True, enable_shadow_construction=False)

    router.route_all_with_diffpairs(config, non_diffpair_strategy=lambda: [])

    assert calls == []
    assert getattr(router, "_coupled_prephase_stall_exit", False) is False


def test_yield_rerun_starts_without_phantom_negotiated_usage():
    """The re-run must not inherit the lifted copper's usage counts (#5895).

    ``unmark_route`` clears occupancy but not negotiated usage, and
    ``route_all_negotiated`` never resets it.  Board 06 (seed 42, CI): the
    lifted legs' leftover usage showed up as overflow 4424 in the re-run's
    iteration 0 (first pass: 2), the loop ripped all 19 nets every
    iteration until the 300 s cap, and the contorted copper split the
    +3V3 pour.  A reverted yield leaves usage consistent with the restored
    copper.
    """
    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    _commit(router, wall)
    for route in wall:
        router.grid.mark_route_usage(route)
    assert int(router.grid._usage_count.sum()) > 0

    to_yield, _stranded = dp._plan_corridor_yields([3], [(_pair(), wall)])
    usage_during: list[int] = []

    def _strategy() -> list[Route]:
        usage_during.append(int(router.grid._usage_count.sum()))
        return []

    kept, _released, _removed, _added = dp._apply_corridor_yields(to_yield, [3], _strategy)

    assert usage_during == [0], "the re-run must start from zero negotiated usage"
    assert kept is False
    expected = 0
    for route in router.routes:
        expected += len(
            {c for s in route.segments for c in router.grid._get_segment_cells(s)}
            | {c for v in route.vias for c in router.grid._get_via_cells(v)}
        )
    assert int(router.grid._usage_count.sum()) == expected


# ---------------------------------------------------------------------------
# Issue #5923: the re-run cap is proportional to the first main pass
# ---------------------------------------------------------------------------


def _pin_rerun_budget(
    monkeypatch, *, ceiling=300.0, factor=1.0, floor=60.0, override=False, tail=0.2
) -> None:
    import kicad_tools.router.diffpair_routing as dpr

    monkeypatch.setattr(dpr, "_CORRIDOR_YIELD_RERUN_TAIL_FRACTION", tail)
    monkeypatch.setattr(dpr, "_CORRIDOR_YIELD_RERUN_S", ceiling)
    monkeypatch.setattr(dpr, "_CORRIDOR_YIELD_RERUN_FACTOR", factor)
    monkeypatch.setattr(dpr, "_CORRIDOR_YIELD_RERUN_FLOOR_S", floor)
    monkeypatch.setattr(dpr, "_CORRIDOR_YIELD_RERUN_OVERRIDE", override)


def test_rerun_cap_is_proportional_to_the_first_pass(monkeypatch):
    from kicad_tools.router.diffpair_routing import _corridor_yield_rerun_cap

    _pin_rerun_budget(monkeypatch, factor=1.0)
    assert _corridor_yield_rerun_cap(120.0) == 120.0
    _pin_rerun_budget(monkeypatch, factor=1.5)
    assert _corridor_yield_rerun_cap(100.0) == 150.0


def test_rerun_cap_has_a_floor(monkeypatch):
    from kicad_tools.router.diffpair_routing import _corridor_yield_rerun_cap

    _pin_rerun_budget(monkeypatch, floor=60.0)
    assert _corridor_yield_rerun_cap(5.0) == 60.0
    assert _corridor_yield_rerun_cap(0.0) == 60.0
    # A floor above the ceiling never lifts the cap past the ceiling.
    _pin_rerun_budget(monkeypatch, ceiling=30.0, floor=60.0)
    assert _corridor_yield_rerun_cap(5.0) == 30.0


def test_rerun_cap_is_bounded_by_the_ceiling(monkeypatch):
    from kicad_tools.router.diffpair_routing import _corridor_yield_rerun_cap

    _pin_rerun_budget(monkeypatch, ceiling=300.0)
    assert _corridor_yield_rerun_cap(1000.0) == 300.0
    # Unknown first-pass time: the pre-#5923 fixed cap.
    assert _corridor_yield_rerun_cap(None) == 300.0


def test_rerun_cap_env_setting_is_an_absolute_override(monkeypatch):
    from kicad_tools.router.diffpair_routing import _corridor_yield_rerun_cap

    _pin_rerun_budget(monkeypatch, ceiling=450.0, override=True)
    assert _corridor_yield_rerun_cap(10.0) == 450.0
    assert _corridor_yield_rerun_cap(1000.0) == 450.0


def test_rerun_cap_override_is_read_from_the_environment():
    """``KCT_CORRIDOR_YIELD_RERUN_S`` set at import time => override."""
    import os
    import subprocess
    import sys

    code = (
        "import kicad_tools.router.diffpair_routing as d;"
        "print(d._CORRIDOR_YIELD_RERUN_OVERRIDE, d._corridor_yield_rerun_cap(5.0))"
    )
    env = dict(os.environ)
    env.pop("KCT_CORRIDOR_YIELD_RERUN_FACTOR", None)
    env.pop("KCT_CORRIDOR_YIELD_RERUN_FLOOR_S", None)
    env["KCT_CORRIDOR_YIELD_RERUN_S"] = "42"
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["True", "42.0"]
    env.pop("KCT_CORRIDOR_YIELD_RERUN_S")
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["False", "60.0"]


def test_rerun_cap_is_applied_to_the_rerun_and_removed_after(monkeypatch):
    """The derived cap is live only during the re-run (#5923).

    The cap is the whole re-run's wall deadline; the negotiated loop's stage
    budget is its ``1 - TAIL_FRACTION`` share (0.8 x 120 = 96 s here).
    """
    from kicad_tools.router import wall_deadline

    _pin_rerun_budget(monkeypatch, factor=1.0, floor=60.0, ceiling=300.0)
    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    _commit(router, wall)
    assert router._negotiated_timeout_cap is None

    to_yield, _stranded = dp._plan_corridor_yields([3], [(_pair(), wall)])
    caps_during: list[float | None] = []
    left_during: list[float | None] = []

    def _strategy() -> list[Route]:
        caps_during.append(router._negotiated_timeout_cap)
        left_during.append(wall_deadline.remaining())
        return []

    dp._apply_corridor_yields(to_yield, [3], _strategy, first_pass_s=120.0)
    assert caps_during == [pytest.approx(96.0)]
    assert left_during[0] is not None and 119.0 < left_during[0] <= 120.0
    assert router._negotiated_timeout_cap is None
    assert not wall_deadline.active()
    assert dp._last_corridor_yield_rerun_cap_s == 120.0
    assert dp._last_corridor_yield_rerun_s is not None


def test_rerun_cap_is_removed_even_when_the_rerun_raises(monkeypatch):
    _pin_rerun_budget(monkeypatch)
    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    _commit(router, wall)
    to_yield, _stranded = dp._plan_corridor_yields([3], [(_pair(), wall)])

    def _strategy() -> list[Route]:
        raise RuntimeError("boom")

    from kicad_tools.router import wall_deadline

    with pytest.raises(RuntimeError):
        dp._apply_corridor_yields(to_yield, [3], _strategy, first_pass_s=90.0)
    assert router._negotiated_timeout_cap is None
    assert not wall_deadline.active(), "the re-run deadline must not leak"


def test_rerun_without_first_pass_time_keeps_the_fixed_cap(monkeypatch):
    _pin_rerun_budget(monkeypatch, ceiling=300.0)
    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    _commit(router, wall)
    to_yield, _stranded = dp._plan_corridor_yields([3], [(_pair(), wall)])
    caps_during: list[float | None] = []

    def _strategy() -> list[Route]:
        caps_during.append(router._negotiated_timeout_cap)
        return []

    dp._apply_corridor_yields(to_yield, [3], _strategy)
    assert caps_during == [pytest.approx(240.0)]  # 0.8 x the fixed 300 s


def test_route_all_with_diffpairs_passes_the_measured_first_pass_time():
    """The orchestrator times the first main pass and hands it to the yield."""
    import time as _time

    router = _channel_router()
    dp = router._diffpair
    seen: dict[str, object] = {}
    sentinel_pair = (_pair(), [])

    # Force the diff-pair path (the fixture has no pair pads); the pair's
    # nets have no pads, so its coupled attempt routes nothing.
    dp.detect_differential_pairs_with_source = lambda: [(_pair(), "test")]  # type: ignore[method-assign]
    dp._corridor_yield_candidates = lambda *_a, **_k: [sentinel_pair]  # type: ignore[method-assign]
    dp._plan_corridor_yields = lambda *_a, **_k: ([], [])  # type: ignore[method-assign]

    def _apply(*_args: object, **kwargs: object):
        seen.update(kwargs)
        return False, set(), [], []

    dp._apply_corridor_yields = _apply  # type: ignore[method-assign]
    calls: list[float | None] = []

    def _strategy() -> list[Route]:
        calls.append(router._negotiated_timeout_cap)
        _time.sleep(0.05)
        return []

    config = DifferentialPairConfig(enabled=True, enable_shadow_construction=True)
    router.route_all_with_diffpairs(config, non_diffpair_strategy=_strategy)

    assert calls == [None], "the first main pass must run uncapped"
    first_pass_s = seen.get("first_pass_s")
    assert isinstance(first_pass_s, float)
    assert 0.05 <= first_pass_s < 10.0


# ---------------------------------------------------------------------------
# Issue #5923 (Judge round): the cap is a wall deadline on the WHOLE re-run
# ---------------------------------------------------------------------------


def test_wall_deadline_is_identity_without_a_deadline():
    from kicad_tools.router import wall_deadline as wd

    assert not wd.active()
    assert wd.remaining() is None
    assert wd.expired() is False
    assert wd.clamp_search_timeout(None) is None
    assert wd.clamp_search_timeout(7.5) == 7.5
    assert wd.clamp_epoch_deadline(None) is None
    assert wd.clamp_epoch_deadline(123.0) == 123.0


def test_wall_deadline_clamps_searches_and_tail_deadlines():
    import time as _time

    from kicad_tools.router import wall_deadline as wd

    with wd.deadline(5.0):
        assert wd.active()
        assert wd.clamp_search_timeout(1.0) == 1.0
        # A longer or unbudgeted search is clamped to the time left.
        assert 4.0 < wd.clamp_search_timeout(60.0) <= 5.0
        assert 4.0 < wd.clamp_search_timeout(None) <= 5.0
        assert 4.0 < wd.clamp_search_timeout(0.0) <= 5.0
        # A tail stage's own (later) allowance is pulled in to the deadline.
        clamped = wd.clamp_epoch_deadline(_time.time() + 90.0)
        assert clamped is not None and clamped - _time.time() <= 5.0
        # A nested block never loosens the outer deadline ...
        with wd.deadline(100.0):
            assert wd.remaining() <= 5.0
        # ... and restores it on exit.
        assert 4.0 < wd.remaining() <= 5.0
    assert not wd.active()


def test_spent_wall_deadline_gives_searches_a_near_zero_but_bounded_budget():
    from kicad_tools.router import wall_deadline as wd

    with wd.deadline(0.0):
        assert wd.expired()
        # Never 0/None: both backends read those as "no deadline at all".
        assert wd.clamp_search_timeout(None) == wd.MIN_SEARCH_TIMEOUT_S
        assert wd.clamp_search_timeout(30.0) == wd.MIN_SEARCH_TIMEOUT_S


def test_wall_deadline_is_restored_when_the_block_raises():
    from kicad_tools.router import wall_deadline as wd

    with pytest.raises(RuntimeError), wd.deadline(5.0):
        raise RuntimeError("boom")
    assert not wd.active()


def test_pathfinder_route_entry_point_clamps_to_the_deadline(monkeypatch):
    """The per-search choke point: ``route()`` hands ``_route_impl`` the clamp."""
    from kicad_tools.router import wall_deadline as wd

    router = _channel_router()
    pathfinder = router.router
    seen: list[float | None] = []

    def _impl(*_a: object, per_net_timeout: float | None = None, **_k: object):
        seen.append(per_net_timeout)
        return None

    monkeypatch.setattr(pathfinder, "_route_impl", _impl)
    pads = router.nets[3]
    pad_a, pad_b = (router.pads[p] for p in pads[:2])

    pathfinder.route(pad_a, pad_b, per_net_timeout=30.0)
    with wd.deadline(2.0):
        pathfinder.route(pad_a, pad_b, per_net_timeout=30.0)
        pathfinder.route(pad_a, pad_b, per_net_timeout=None)
    with wd.deadline(0.0):
        pathfinder.route(pad_a, pad_b, per_net_timeout=30.0)

    assert seen[0] == 30.0, "no deadline installed: the caller's budget is untouched"
    assert 1.0 < seen[1] <= 2.0 and 1.0 < seen[2] <= 2.0
    assert seen[3] == wd.MIN_SEARCH_TIMEOUT_S


def test_python_router_route_entry_point_clamps_to_the_deadline(monkeypatch):
    from kicad_tools.router import wall_deadline as wd
    from kicad_tools.router.pathfinder import Router

    router = _channel_router()
    py_router = Router(router.grid, router.rules)
    seen: list[float | None] = []

    def _impl(*_a: object, per_net_timeout: float | None = None, **_k: object):
        seen.append(per_net_timeout)
        return None

    monkeypatch.setattr(py_router, "_route_impl", _impl)
    pad_a, pad_b = (router.pads[p] for p in router.nets[3][:2])
    with wd.deadline(2.0):
        py_router.route(pad_a, pad_b, per_net_timeout=None)
    assert seen and 1.0 < seen[0] <= 2.0


def _search_runs_to_its_budget(monkeypatch, router: Autorouter, calls: list[float]) -> None:
    """Every A* search fails only after spending its whole wall budget.

    The pathological case the #5923 judge measured: a search already in
    flight when the stage budget runs out.  Unbudgeted searches are modelled
    as 30 s so a missing clamp shows up as a 30 s overrun, not a hang.
    """
    import time as _time

    def _impl(*_a: object, per_net_timeout: float | None = None, **_k: object):
        budget = per_net_timeout if per_net_timeout else 30.0
        calls.append(budget)
        _time.sleep(budget)
        return None

    monkeypatch.setattr(router.router, "_route_impl", _impl)


def test_rerun_wall_time_is_bounded_by_the_deadline_including_in_flight_search(
    monkeypatch,
):
    """Observable contract: the re-run returns within cap + small tolerance.

    The strategy is the REAL ``route_all_negotiated`` with a 1000 s stage
    timeout and a 30 s per-net budget; every search runs to its budget.
    Before #5923's deadline, a single in-flight search alone would overrun a
    1.5 s cap by ~28 s and the clearance/sweep tail would add its own
    allowances.  Tolerance covers pure-Python bookkeeping on this tiny board.
    """
    import time as _time

    from kicad_tools.router import wall_deadline as wd

    _pin_rerun_budget(monkeypatch, factor=1.0, floor=1.5, ceiling=1.5)
    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    _commit(router, wall)
    to_yield, _stranded = dp._plan_corridor_yields([3], [(_pair(), wall)])
    calls: list[float] = []
    _search_runs_to_its_budget(monkeypatch, router, calls)

    def _strategy() -> list[Route]:
        return router.route_all_negotiated(max_iterations=5, timeout=1000.0, per_net_timeout=30.0)

    t0 = _time.monotonic()
    kept, released, removed, added = dp._apply_corridor_yields(
        to_yield, [3], _strategy, first_pass_s=1.0
    )
    wall_s = _time.monotonic() - t0

    assert calls, "the strategy must have searched"
    assert max(calls) <= 1.5, "every search is clamped to the re-run deadline"
    assert wall_s < 1.5 + 2.0, f"re-run took {wall_s:.2f}s against a 1.5s deadline"
    assert dp._last_corridor_yield_rerun_cap_s == 1.5
    # Nothing landed, so the spent re-run is reverted and the pair restored.
    assert (kept, released, removed, added) == (False, set(), [], [])
    for route in wall:
        assert route in router.routes
    assert not wd.active()
    assert router._negotiated_timeout_cap is None


def test_rerun_tail_stage_allowances_are_cut_to_the_deadline(monkeypatch):
    """A stage with its own fresh allowance (clearance pass, rescue sweep)
    is pulled in to the re-run deadline rather than added on top of it."""
    import time as _time

    from kicad_tools.router import wall_deadline as wd

    _pin_rerun_budget(monkeypatch, factor=1.0, floor=1.0, ceiling=1.0)
    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    _commit(router, wall)
    to_yield, _stranded = dp._plan_corridor_yields([3], [(_pair(), wall)])

    def _strategy() -> list[Route]:
        # Loop share first (stage timeout checked between "nets") ...
        stage_t0 = _time.monotonic()
        while _time.monotonic() - stage_t0 < router._negotiated_timeout_cap:
            _time.sleep(0.01)
        # ... then a 60 s tail allowance, as the #4159 sweep would take.
        tail_deadline = wd.clamp_epoch_deadline(_time.time() + 60.0)
        assert tail_deadline is not None
        while _time.time() < tail_deadline:
            _time.sleep(0.01)
        return []

    t0 = _time.monotonic()
    kept, *_rest = dp._apply_corridor_yields(to_yield, [3], _strategy, first_pass_s=1.0)
    assert _time.monotonic() - t0 < 1.0 + 0.5
    assert kept is False


def test_negotiated_loop_searches_are_clamped_by_an_installed_deadline(monkeypatch):
    """``route_all_negotiated`` itself honours a spent deadline: every search
    it (and its tail stages) issues gets the near-zero floor."""
    from kicad_tools.router import wall_deadline as wd

    router = _channel_router()
    calls: list[float] = []
    _search_runs_to_its_budget(monkeypatch, router, calls)
    with wd.deadline(0.0):
        router.route_all_negotiated(max_iterations=3, timeout=1000.0, per_net_timeout=30.0)
    assert calls
    assert max(calls) == wd.MIN_SEARCH_TIMEOUT_S


def test_abandon_is_a_no_op_without_an_installed_deadline():
    from kicad_tools.router import wall_deadline as wd

    assert wd.abandon("nothing installed") is False
    with wd.deadline(5.0) as excursion:
        assert excursion.abandoned is False
        assert wd.abandon("first") is True
        assert wd.abandon("second") is True
    assert excursion.abandoned is True
    assert excursion.abandon_reason == "first"
    assert wd.abandon("after the block") is False


def test_abandoned_rerun_is_reverted_even_when_it_gained_reach():
    """Abandoned copper skipped the DRC safety nets: it can never be kept."""
    from kicad_tools.router import wall_deadline as wd

    router = _channel_router()
    dp = router._diffpair
    wall = _wall_routes(router)
    _commit(router, wall)
    to_yield, _stranded = dp._plan_corridor_yields([3], [(_pair(), wall)])

    def _strategy() -> list[Route]:
        routes = router.route_net(3)
        assert wd.abandon("test: deadline spent before the safety nets")
        return routes

    kept, released, removed, added = dp._apply_corridor_yields(to_yield, [3], _strategy)
    assert (kept, released, removed, added) == (False, set(), [], [])
    assert dp._net_is_connected(3) is False, "the abandoned re-run's copper is removed"
    for route in wall:
        assert route in router.routes


def test_negotiated_tail_abandons_instead_of_running_safety_nets_past_the_deadline(
    monkeypatch,
):
    """A spent deadline at the post-loop safety nets: skip them and abandon.

    Outside an excursion the same call runs every safety net as before.
    """
    from kicad_tools.router import wall_deadline as wd

    ran: list[str] = []
    for name in (
        "_demote_seg_seg_overlap_nets",
        "_demote_pad_clearance_violation_nets",
        "_demote_via_segment_violation_nets",
    ):
        monkeypatch.setattr(Autorouter, name, lambda self, *_a, _n=name, **_k: ran.append(_n) or [])

    router = _channel_router()
    with wd.deadline(0.0) as excursion:
        router.route_all_negotiated(max_iterations=2, timeout=1000.0, per_net_timeout=5.0)
    assert ran == []
    assert excursion.abandoned is True

    # Control: same tail reached without an excursion.  The wall strands SIG
    # so the loop exits through the post-loop tail rather than the
    # all-routed early return.
    router = _channel_router()
    _commit(router, _wall_routes(router))
    router.route_all_negotiated(max_iterations=2, timeout=1000.0, per_net_timeout=5.0)
    assert len(ran) == 3, "no excursion installed: every safety net still runs"


def test_negotiated_tail_abandons_when_time_left_cannot_pay_for_the_safety_nets(
    monkeypatch,
):
    """Predictive abandon: the deadline is not spent yet, but less is left
    than the previous pass spent on the (uninterruptible) safety nets."""
    from kicad_tools.router import wall_deadline as wd

    ran: list[str] = []
    monkeypatch.setattr(
        Autorouter,
        "_demote_seg_seg_overlap_nets",
        lambda self, *_a, **_k: ran.append("seg") or [],
    )

    router = _channel_router()
    _commit(router, _wall_routes(router))
    router._last_safety_net_s = 1000.0  # the first pass's measured cost
    with wd.deadline(60.0) as excursion:
        router.route_all_negotiated(max_iterations=2, timeout=1000.0, per_net_timeout=5.0)
    assert excursion.abandoned is True
    assert ran == []
    assert router._last_safety_net_s == 1000.0, "an abandoned tail measures nothing"

    # Enough time left for the measured cost: the safety nets run, and their
    # cost is re-measured for the next excursion.
    router = _channel_router()
    _commit(router, _wall_routes(router))
    router._last_safety_net_s = 0.0
    with wd.deadline(60.0) as excursion:
        router.route_all_negotiated(max_iterations=2, timeout=1000.0, per_net_timeout=5.0)
    assert excursion.abandoned is False
    assert ran == ["seg"]
    assert router._last_safety_net_s >= 0.0

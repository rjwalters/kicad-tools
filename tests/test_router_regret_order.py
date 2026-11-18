"""Unit tests for regret-ordered net committing (Issue #9001).

Three layers, mirroring ``tests/test_router_crossing_order.py``:

1. Pure ranking in ``kicad_tools.router.regret_order`` -- descending regret
   within a band, bands never interleaved, all-ties degradation to the
   default order, both failure conventions.
2. The probe mechanics on a real ``Autorouter`` -- the anti-inert pin (the
   two probes return *different* paths whenever a second corridor exists),
   the single-corridor ``+inf`` convention, the byte-identical grid restore,
   and the clean accumulators (``commit_journal``, ``search_failures_by_net``).
3. The ``--order-method regret`` plumbing -- parser acceptance, escalation
   acceptance (it must ride with ``crossing``, not land in
   ``_SINGLE_ATTEMPT_ONLY_ORDER_METHODS``), and the forced-order handoff.
"""

from __future__ import annotations

import math
from dataclasses import replace as dc_replace
from types import SimpleNamespace

import numpy as np
import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.regret_order import (
    UNKNOWN_REGRET,
    regret_order,
    route_probe_cost,
)
from tests.route_cli import route_args

# ---------------------------------------------------------------------------
# regret_order -- the pure ranking
# ---------------------------------------------------------------------------


def _band(n: int) -> tuple:
    return (10, 0, 0.0, 2, 5.0, 0.0)


def test_regret_order_ranks_descending_within_a_band():
    order = regret_order(
        {1: 5.0, 2: 1.0, 3: 9.0},
        priority_key={1: _band(1), 2: _band(2), 3: _band(3)},
        class_band={1: 10, 2: 10, 3: 10},
    )
    assert order == [3, 1, 2]


def test_regret_order_never_interleaves_bands():
    """A huge regret cannot jump a net ahead of a higher-priority band."""
    order = regret_order(
        {1: 0.0, 2: math.inf},
        priority_key={1: _band(1), 2: _band(2)},
        class_band={1: 2, 2: 10},
    )
    assert order == [1, 2]


def test_regret_order_all_ties_degrade_to_default_order():
    """With every regret equal the result is the ``priority_key`` sort."""
    priority_key = {
        1: (10, 0, -3.0, 2, 30.0, -1.0),
        2: (10, 0, -1.0, 4, 10.0, -2.0),
        3: (10, 0, -2.0, 3, 20.0, -3.0),
    }
    class_band = {n: key[0] for n, key in priority_key.items()}
    order = regret_order({1: 1.5, 2: 1.5, 3: 1.5}, priority_key=priority_key, class_band=class_band)
    assert order == sorted(priority_key, key=lambda n: priority_key[n])


def test_regret_order_infinite_regret_sorts_first_in_its_band():
    order = regret_order(
        {1: 2.0, 2: math.inf, 3: 9.0},
        priority_key={n: _band(n) for n in (1, 2, 3)},
        class_band=dict.fromkeys((1, 2, 3), 10),
    )
    assert order[0] == 2


def test_regret_order_unknown_regret_keeps_default_position():
    """UNKNOWN_REGRET is not a promotion: the priority tiebreak decides."""
    priority_key = {1: (10, 0, 0.0, 2, 5.0, 0.0), 2: (10, 0, 0.0, 3, 5.0, 0.0)}
    class_band = {1: 10, 2: 10}
    order = regret_order(
        {1: UNKNOWN_REGRET, 2: 4.0}, priority_key=priority_key, class_band=class_band
    )
    assert order == [2, 1]


def test_regret_order_pour_band_pinned_last():
    order = regret_order(
        {1: 1.0, 2: math.inf, 3: 0.0},
        priority_key={1: _band(1), 2: _band(2), 3: (99, 0, 0.0, 0, 0.0, 0.0)},
        class_band={1: 10, 2: 10, 3: 99},
    )
    assert order == [2, 1, 3]


def test_regret_order_ties_break_by_net_id():
    order = regret_order(
        {7: 3.0, 2: 3.0},
        priority_key={2: _band(2), 7: _band(7)},
        class_band={2: 10, 7: 10},
    )
    assert order == [2, 7]


# ---------------------------------------------------------------------------
# route_probe_cost -- the pure geometry
# ---------------------------------------------------------------------------


def _probe_route_of_length(length: float, vias: int = 0) -> Route:
    return Route(
        net=1,
        net_name="T",
        segments=[Segment(x1=0.0, y1=0.0, x2=length, y2=0.0, width=0.25, layer=Layer.F_CU, net=1)],
        vias=[
            Via(
                x=0.0,
                y=0.0,
                drill=0.35,
                diameter=0.7,
                layers=(Layer.F_CU, Layer.B_CU),
                net=1,
            )
        ]
        * vias,
    )


def test_route_probe_cost_is_wirelength_plus_via_penalty():
    assert route_probe_cost(_probe_route_of_length(10.0, vias=2), via_cost=10.0) == pytest.approx(
        30.0
    )


def test_route_probe_cost_of_empty_geometry_is_zero():
    assert route_probe_cost(Route(net=1, net_name="T"), via_cost=10.0) == 0.0


# ---------------------------------------------------------------------------
# Probe mechanics on a real Autorouter
# ---------------------------------------------------------------------------

_WALL_X1, _WALL_X2 = 14.0, 16.0


def _two_layer_router():
    """A 30x30mm 2-layer router: ``add_component`` wires nets AND pads onto
    the grid, so the probes see the same prepared state the CLI hook sees."""
    from kicad_tools.router.core import Autorouter

    return Autorouter(width=30.0, height=30.0)


def _add_net(router, net: int, name: str, x1: float, y1: float, x2: float, y2: float) -> None:
    router.add_component(
        f"W{net}",
        [
            {
                "number": "1",
                "x": x1,
                "y": y1,
                "width": 0.5,
                "height": 0.5,
                "net": net,
                "net_name": name,
            },
            {
                "number": "2",
                "x": x2,
                "y": y2,
                "width": 0.5,
                "height": 0.5,
                "net": net,
                "net_name": name,
            },
        ],
    )


def _corridor_walls(router, gaps: list[tuple[float, float]], layers=None) -> None:
    """A full-height wall at x in [14, 16], with the given gaps (default: both layers)."""
    if layers is None:
        layers = [Layer.F_CU, Layer.B_CU]
    y = 0.0
    for g1, g2 in sorted(gaps):
        if g1 > y:
            router.grid.add_keepout(_WALL_X1, y, _WALL_X2, g1, layers=layers)
        y = g2
    if y < 30.0:
        router.grid.add_keepout(_WALL_X1, y, _WALL_X2, 30.0, layers=layers)


def _route_segments_set(route: Route) -> set[tuple[float, float, float, float]]:
    return {
        (round(s.x1, 3), round(s.y1, 3), round(s.x2, 3), round(s.y2, 3)) for s in route.segments
    }


def test_two_probes_return_different_paths_when_a_second_corridor_exists():
    """The anti-inert pin (Issue #9001 finding 4): same-net copper does not
    block, so a naive second search under the net's own id returns the first
    path and every regret is 0.  With the sentinel forbid, the forced
    second-best is a genuinely different path."""
    from kicad_tools.cli.route_cmd import _probe_net_regret, _regret_sentinel_net_id

    router = _two_layer_router()
    _add_net(router, 1, "NET_A", 5.0, 6.0, 25.0, 22.0)
    _corridor_walls(router, gaps=[(8.0, 10.0), (20.0, 22.0)])

    sentinel = _regret_sentinel_net_id(router)
    regret, first_ok = _probe_net_regret(router, 1, sentinel)

    assert first_ok is True
    assert regret > 0.0, "a forced detour must cost more than the best route"

    # Reproduce the two probes explicitly and pin the path difference, with
    # the same trimmed sentinel copper the implementation marks.
    from kicad_tools.router.crossing_order import minimum_spanning_skeleton
    from kicad_tools.router.regret_order import trimmed_probe_copper

    pads = [router.pads[k] for k in router.nets[1]]
    points = [(p.x, p.y) for p in pads]
    skeleton = minimum_spanning_skeleton(points)
    assert len(skeleton) == 1
    start = next(p for p in pads if (p.x, p.y) == skeleton[0][0])
    end = next(p for p in pads if (p.x, p.y) == skeleton[0][1])

    grid = router.grid
    net_class = router.net_class_map.get("NET_A")
    best = router.router.route(start, end, net_class=net_class)
    assert best is not None
    sentinel_route = trimmed_probe_copper(
        Route(
            net=sentinel,
            net_name="regret-probe-test",
            segments=[dc_replace(s, net=sentinel) for s in best.segments],
            vias=[dc_replace(v, net=sentinel) for v in best.vias],
        ),
        trim_mm=1.0,
    )
    assert sentinel_route.segments, "this fixture's route must have trimmable copper"
    saved = grid.commit_observer
    grid.commit_observer = None
    try:
        grid.mark_route(sentinel_route)
        grid._mark_route_on_cpp_cells(sentinel_route)
        try:
            second = router.router.route(start, end, net_class=net_class)
        finally:
            grid.unmark_route(sentinel_route)
    finally:
        grid.commit_observer = saved

    assert second is not None
    assert _route_segments_set(best) != _route_segments_set(second), (
        "the second probe must not return the first path -- that is the "
        "silently-inert failure mode (same-net copper does not block)"
    )


def test_second_probe_without_path_gives_infinite_regret():
    """Exactly one corridor: the second probe finds no path -> regret +inf.

    The single corridor must be single *across layers*: B.Cu is fully walled
    and the only F.Cu gap is what probe 1's trimmed sentinel copper blocks.
    """
    from kicad_tools.cli.route_cmd import _probe_net_regret, _regret_sentinel_net_id

    router = _two_layer_router()
    _add_net(router, 1, "NET_A", 5.0, 15.0, 25.0, 15.0)
    router.grid.add_keepout(_WALL_X1, 0.0, _WALL_X2, 30.0, layers=[Layer.B_CU])
    # A 1.2mm slot fits exactly one trace envelope (0.25mm width + 2x0.2mm
    # clearance): once probe 1's copper occupies it there is no second route.
    _corridor_walls(router, gaps=[(14.4, 15.6)], layers=[Layer.F_CU])

    regret, first_ok = _probe_net_regret(router, 1, _regret_sentinel_net_id(router))
    assert first_ok is True
    assert regret == math.inf


def test_first_probe_without_path_reports_unknown_regret():
    """A net that cannot route even alone is unmeasured: ``(0.0, False)`` --
    the bridge never promotes or demotes on a failed probe."""
    from kicad_tools.cli.route_cmd import _probe_net_regret, _regret_sentinel_net_id

    router = _two_layer_router()
    _add_net(router, 1, "NET_A", 5.0, 15.0, 25.0, 15.0)
    # Wall straight through the corridor, no gap at all.
    _corridor_walls(router, gaps=[])

    regret, first_ok = _probe_net_regret(router, 1, _regret_sentinel_net_id(router))
    assert first_ok is False
    assert regret == UNKNOWN_REGRET


def test_single_pad_net_has_no_connection_to_probe():
    """A one-pad net has no skeleton edge: unmeasured, ``(0.0, False)``."""
    from kicad_tools.cli.route_cmd import _probe_net_regret, _regret_sentinel_net_id

    router = _two_layer_router()
    router.add_component(
        "W1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 15.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET_A",
            },
        ],
    )

    regret, first_ok = _probe_net_regret(router, 1, _regret_sentinel_net_id(router))
    assert first_ok is False
    assert regret == UNKNOWN_REGRET


def test_probe_leaves_grid_byte_identical():
    """After the probe pair the grid's occupancy planes match their pristine
    prepared state (Issue #3545 snapshot + net-aware rip-up)."""
    from kicad_tools.cli.route_cmd import _probe_net_regret, _regret_sentinel_net_id

    router = _two_layer_router()
    _add_net(router, 1, "NET_A", 5.0, 6.0, 25.0, 22.0)
    _add_net(router, 2, "NET_B", 5.0, 15.0, 25.0, 15.0)
    _corridor_walls(router, gaps=[(8.0, 10.0), (14.0, 16.0), (20.0, 22.0)])

    grid = router.grid
    blocked_before = np.array(grid._blocked, copy=True)
    net_before = np.array(grid._net, copy=True)

    _probe_net_regret(router, 1, _regret_sentinel_net_id(router))
    _probe_net_regret(router, 2, _regret_sentinel_net_id(router))

    assert np.array_equal(grid._blocked, blocked_before)
    assert np.array_equal(grid._net, net_before)


def test_pour_net_pinned_last_and_never_probed():
    """Pour nets (``_get_net_priority`` band 99) are pinned last and consume
    no probe searches."""
    from kicad_tools.cli.route_cmd import _regret_net_order
    from kicad_tools.router.rules import NET_CLASS_POWER

    router = _two_layer_router()
    _add_net(router, 1, "NET_A", 5.0, 6.0, 25.0, 22.0)
    _add_net(router, 2, "GND", 5.0, 10.0, 25.0, 10.0)
    router.net_class_map["GND"] = NET_CLASS_POWER

    calls = {"n": 0}
    real_route = router.router.route

    def counting_route(*a, **k):
        calls["n"] += 1
        return real_route(*a, **k)

    router.router.route = counting_route  # type: ignore[method-assign]

    order = _regret_net_order(router, quiet=True)

    assert order[-1] == 2, "pour net must be pinned last by its class band"
    assert calls["n"] == 2, f"exactly two probes for one signal net, got {calls['n']}"


def test_regret_order_accumulators_stay_clean():
    """No probe traffic in ``commit_journal`` / ``search_failures_by_net``, and
    the sentinel never survives the probe phase."""
    from kicad_tools.cli.route_cmd import _regret_net_order, _regret_sentinel_net_id

    router = _two_layer_router()
    _add_net(router, 1, "NET_A", 5.0, 6.0, 25.0, 22.0)
    _add_net(router, 2, "NET_B", 5.0, 15.0, 25.0, 15.0)
    _corridor_walls(router, gaps=[(8.0, 10.0), (14.0, 16.0), (20.0, 22.0)])

    failure_store = getattr(router.router, "search_failures_by_net", {})
    sentinel = _regret_sentinel_net_id(router)

    order = _regret_net_order(router, quiet=True)

    assert sorted(order) == [1, 2]
    assert len(router.commit_journal.records) == 0, (
        "probe copper must never reach the commit journal"
    )
    assert failure_store == {}, "a probe failure must not poison the failure store"
    # No sentinel copper left on the grid.
    plane_nets = np.asarray(router.grid._net)
    assert not np.any(plane_nets == sentinel), "sentinel copper survived the probe"


# ---------------------------------------------------------------------------
# --order-method regret plumbing
# ---------------------------------------------------------------------------


def test_parser_accepts_regret():
    from tests.test_cli_order_method import _kct_route_subparser

    route_parser = _kct_route_subparser()
    args = route_parser.parse_args(["dummy.kicad_pcb", "--order-method", "regret"])
    assert args.order_method == "regret"


def test_regret_is_not_single_attempt_only():
    """The escalation gate must accept regret on every path (it is applied per
    attempt, like crossing) -- landing it in the single-attempt set would make
    it unbenchmarkable under the default --auto-layers recipe."""
    from kicad_tools.cli.route_cmd import (
        _PER_ATTEMPT_ORDER_METHODS,
        _SINGLE_ATTEMPT_ONLY_ORDER_METHODS,
        _validate_order_method_for_dispatch,
    )

    assert "regret" in _PER_ATTEMPT_ORDER_METHODS
    assert "regret" not in _SINGLE_ATTEMPT_ONLY_ORDER_METHODS

    args = SimpleNamespace(
        order_method="regret",
        auto_layers=True,
        adaptive_rules=True,
        auto_pcb_size=True,
        auto_mfr_tier=True,
    )
    assert _validate_order_method_for_dispatch(args) == 0


def test_apply_order_method_regret_installs_forced_order():
    from kicad_tools.cli.route_cmd import _apply_order_method

    router = _two_layer_router()
    _add_net(router, 1, "NET_A", 5.0, 6.0, 25.0, 22.0)
    _add_net(router, 2, "NET_B", 5.0, 15.0, 25.0, 15.0)
    _corridor_walls(router, gaps=[(8.0, 10.0), (14.0, 16.0), (20.0, 22.0)])

    args = SimpleNamespace(order_method="regret")
    _apply_order_method(router, args, quiet=True)

    order = router._forced_net_order
    assert order is not None
    assert sorted(order) == [1, 2]


def test_apply_order_method_regret_logs_net_order_line(capsys):
    """The harness's ``order_applied`` guard greps for 'Net order:
    --order-method' -- the regret arm must log it, with the A*-call accounting."""
    from kicad_tools.cli.route_cmd import _apply_order_method

    router = _two_layer_router()
    _add_net(router, 1, "NET_A", 5.0, 6.0, 25.0, 22.0)

    args = SimpleNamespace(order_method="regret")
    _apply_order_method(router, args, quiet=False)

    out = capsys.readouterr().out
    assert "Net order: --order-method regret" in out
    assert "probe A* calls" in out


def test_regret_prepass_is_deterministic():
    """Two identical board constructions produce the identical order (cf. #7580)."""
    from kicad_tools.cli.route_cmd import _regret_net_order

    orders = []
    for _ in range(2):
        router = _two_layer_router()
        _add_net(router, 1, "NET_A", 5.0, 6.0, 25.0, 22.0)
        _add_net(router, 2, "NET_B", 5.0, 15.0, 25.0, 15.0)
        _add_net(router, 3, "NET_C", 5.0, 26.0, 25.0, 26.0)
        _corridor_walls(router, gaps=[(8.0, 10.0), (14.0, 16.0), (20.0, 22.0), (24.0, 27.0)])
        orders.append(_regret_net_order(router, quiet=True))

    assert orders[0] == orders[1]


def test_route_args_helper_importable():
    """Keep the tests.route_cli import surface honest for future CLI-level
    regret dispatch tests."""
    assert callable(route_args)

"""Congested-vs-blocked classification of unrouted connections (Issue #5944).

Two fixtures, one per class, each routed for real on both pathfinder
backends:

* **blocked** -- pad ``R1.1`` is boxed in on both copper layers by four
  ``add_obstacle`` keepouts.  With every other trace removed there is still no
  path, so the connection is ``blocked`` and the four keepouts -- the frontier
  the solo search exhausted -- are its ``blockers``.
* **congested** -- nets ``A`` (west-east) and ``B`` (south-north) must cross on
  the only routable layer (``B.Cu`` is filled by a keepout, and side walls
  stop ``B`` from going around ``A``'s ends).  Whichever routes second fails;
  with the first one's copper lifted it routes, so it is ``congested`` and the
  first net is its ``contender``.

Also pinned: the grid (both the Python planes and the C++ mirror) and the
access-witness journal are exactly restored, the time budget is honoured, and
the result reaches the ``kct route --format json`` document both through the
CLI's pre-release stash and through a live library call.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.layers import Layer
from kicad_tools.router.output import get_routing_diagnostics_json
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.unrouted_cause import (
    CAUSE_BLOCKED,
    CAUSE_CONGESTED,
    CAUSE_UNCLASSIFIED,
    diagnose_unrouted,
)

BACKENDS = [
    pytest.param(True, id="python"),
    pytest.param(
        False,
        id="cpp",
        marks=pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not built"),
    ),
]

RULES = {"grid_resolution": 0.2, "trace_width": 0.2, "trace_clearance": 0.2}

# The four keepouts boxing R1.1 at (3, 5): (cx, cy, w, h).
BOX = [
    (3.0, 6.5, 3.0, 0.4),
    (3.0, 3.5, 3.0, 0.4),
    (1.5, 5.0, 0.4, 3.4),
    (4.5, 5.0, 0.4, 3.4),
]


def _boxed_pad_router(force_python: bool) -> Autorouter:
    router = Autorouter(12, 10, rules=DesignRules(**RULES), force_python=force_python)
    router.add_component("R1", [{"number": "1", "x": 3.0, "y": 5.0, "net": 1, "net_name": "SIG"}])
    router.add_component("R2", [{"number": "1", "x": 9.0, "y": 5.0, "net": 1, "net_name": "SIG"}])
    for layer in (Layer.F_CU, Layer.B_CU):
        for cx, cy, w, h in BOX:
            router.add_obstacle(cx, cy, w, h, layer)
    return router


def _crossing_router(force_python: bool) -> Autorouter:
    router = Autorouter(10, 10, rules=DesignRules(**RULES), force_python=force_python)
    router.add_component("J1", [{"number": "1", "x": 1.0, "y": 5.0, "net": 1, "net_name": "A"}])
    router.add_component("J2", [{"number": "1", "x": 9.0, "y": 5.0, "net": 1, "net_name": "A"}])
    router.add_component("J3", [{"number": "1", "x": 5.0, "y": 1.0, "net": 2, "net_name": "B"}])
    router.add_component("J4", [{"number": "1", "x": 5.0, "y": 9.0, "net": 2, "net_name": "B"}])
    router.add_obstacle(5.0, 5.0, 10.0, 10.0, Layer.B_CU)  # one routable layer
    router.add_obstacle(0.25, 5.0, 0.5, 10.0, Layer.F_CU)  # west wall
    router.add_obstacle(9.75, 5.0, 0.5, 10.0, Layer.F_CU)  # east wall
    return router


def _route(router: Autorouter) -> set[int]:
    router.route_all(per_net_timeout=30.0, suppress_no_timeout_warning=True)
    routed = {route.net for route in router.routes}
    return {n for n, pads in router.nets.items() if n > 0 and len(pads) >= 2} - routed


# ---------------------------------------------------------------------------
# The two classes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("force_python", BACKENDS)
def test_pad_boxed_in_by_keepout_is_blocked(force_python: bool) -> None:
    router = _boxed_pad_router(force_python)
    unrouted = _route(router)
    assert unrouted == {1}, "fixture sanity: the boxed pad must fail to route"

    diagnosis = diagnose_unrouted(router, unrouted, net_names={1: "SIG"})

    assert diagnosis is not None
    [conn] = diagnosis.connections
    assert conn.cause == CAUSE_BLOCKED
    assert {conn.source_pad, conn.target_pad} == {("R1", "1"), ("R2", "1")}
    assert conn.contenders == []
    kinds = {b["kind"] for b in conn.blockers}
    assert kinds == {"keepout"}, conn.blockers
    expected = {(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2) for cx, cy, w, h in BOX}
    assert {tuple(b["bbox"]) for b in conn.blockers} == {
        tuple(round(v, 3) for v in e) for e in expected
    }
    for blocker in conn.blockers:
        assert blocker["layers"] == ["F.Cu", "B.Cu"]
        assert blocker["cells"] > 0
    assert conn.frontier is not None
    assert conn.frontier["endpoint"] == {"ref": "R1", "pin": "1"}

    doc = conn.to_dict()
    assert doc["cause"] == "blocked" and "blockers" in doc and "contenders" not in doc


@pytest.mark.parametrize("force_python", BACKENDS)
def test_two_net_crossing_on_one_layer_is_congested(force_python: bool) -> None:
    router = _crossing_router(force_python)
    unrouted = _route(router)
    assert len(unrouted) == 1, "fixture sanity: exactly one of the crossing nets must fail"
    [loser] = unrouted
    winner = ({1, 2} - {loser}).pop()
    names = {1: "A", 2: "B"}

    diagnosis = diagnose_unrouted(router, unrouted, net_names=names)

    assert diagnosis is not None
    [conn] = diagnosis.connections
    assert conn.cause == CAUSE_CONGESTED
    assert conn.net_id == loser
    assert [c["net_id"] for c in conn.contenders] == [winner]
    assert conn.contenders[0]["net_name"] == names[winner]
    assert conn.contenders[0]["cells"] > 0
    assert conn.blockers == []
    assert conn.solo_path is not None and conn.solo_path["vias"] == 0

    doc = conn.to_dict()
    assert doc["cause"] == "congested" and "contenders" in doc and "blockers" not in doc


# ---------------------------------------------------------------------------
# The extra pass leaves no trace and stays in budget
# ---------------------------------------------------------------------------


def _dense_router(force_python: bool, seed: int = 2) -> Autorouter:
    """Many short nets on one routable layer, so committed routes' clearance
    halos overlap each other and the pads' halos.

    Lifting and re-adding routes through ``resync_route_occupancy`` re-derives
    the owner of those shared cells from the current route order, so this
    fixture is what exposes a restore that is not exact (#6002 review).
    """
    import random

    rnd = random.Random(seed)
    router = Autorouter(10, 10, rules=DesignRules(**RULES), force_python=force_python)
    for net in range(1, 13):
        for k in range(rnd.choice([2, 2, 3])):
            x = round(rnd.uniform(1, 9), 1)
            y = round(rnd.uniform(1, 9), 1)
            router.add_component(
                f"U{net}_{k}", [{"number": "1", "x": x, "y": y, "net": net, "net_name": f"N{net}"}]
            )
    router.add_obstacle(5.0, 5.0, 10.0, 10.0, Layer.B_CU)  # one routable layer
    return router


_CPP_CELL_FIELDS = (
    "blocked",
    "net",
    "usage_count",
    "is_obstacle",
    "is_zone",
    "pad_blocked",
    "original_net",
    "static_blocked",
    "avoidance_cost",
)


def _full_grid_snapshot(router: Autorouter) -> tuple:
    """Every ndarray on the Python grid, every per-cell C++ field, the routes."""
    grid = router.grid
    planes = {
        name: np.array(value, copy=True)
        for name, value in vars(grid).items()
        if isinstance(value, np.ndarray)
    }
    counted = getattr(grid, "_congestion_counted", None)
    planes["_congestion_counted"] = None if counted is None else np.array(counted, copy=True)
    cpp = getattr(grid, "_cpp_grid", None)
    cells = None
    if cpp is not None:
        impl = cpp._impl
        cells = [
            tuple(getattr(cell, f) for f in _CPP_CELL_FIELDS)
            for layer in range(grid.num_layers)
            for y in range(grid.rows)
            for x in range(grid.cols)
            for cell in [impl.at(x, y, layer)]
        ]
    return planes, cells, [id(r) for r in grid.routes]


def _assert_snapshots_equal(before: tuple, after: tuple) -> None:
    planes_b, cells_b, routes_b = before
    planes_a, cells_a, routes_a = after
    assert planes_b.keys() == planes_a.keys()
    changed = {}
    for name, value in planes_b.items():
        other = planes_a[name]
        if value is None or other is None:
            if value is not other:
                changed[name] = "None-ness differs"
        elif not np.array_equal(value, other):
            changed[name] = int((value != other).sum())
    assert not changed, f"Python grid planes not restored exactly: {changed}"
    if cells_b is not None:
        diff = sum(1 for p, q in zip(cells_b, cells_a, strict=True) if p != q)
        assert diff == 0, f"{diff} C++ grid cells not restored exactly"
    assert routes_b == routes_a, "grid.routes order/membership not restored"


@pytest.mark.parametrize("force_python", BACKENDS)
def test_grid_and_witness_journal_are_restored(force_python: bool) -> None:
    router = _dense_router(force_python)
    unrouted = _route(router)
    assert len(router.grid.routes) >= 2, "fixture sanity: several overlapping routes"
    assert unrouted, "fixture sanity: something is left to diagnose"
    journal: list[tuple[str, object]] = []

    def observer(event: str, route: object) -> None:
        journal.append((event, route))

    router.grid.commit_observer = observer
    generation = router.grid.occupancy_generation
    before = _full_grid_snapshot(router)

    diagnosis = diagnose_unrouted(router, unrouted)

    after = _full_grid_snapshot(router)
    assert diagnosis is not None and diagnosis.routes_lifted == len(router.grid.routes)
    _assert_snapshots_equal(before, after)
    assert router.grid.occupancy_generation > generation, "occupancy caches not invalidated"
    assert journal == [], "the diagnosis leaked lift/restore events into the witness journal"
    assert router.grid.commit_observer is observer


@pytest.mark.parametrize("force_python", BACKENDS)
def test_grid_is_restored_exactly_when_diagnosis_raises(
    force_python: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kicad_tools.router.unrouted_cause as uc

    router = _dense_router(force_python)
    unrouted = _route(router)
    assert len(router.grid.routes) >= 2, "fixture sanity: several overlapping routes"
    before = _full_grid_snapshot(router)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(uc, "_contenders", boom)
    monkeypatch.setattr(uc, "_blocked_report", boom)
    routed = {route.net for route in router.routes}
    with pytest.raises(RuntimeError, match="boom"):
        diagnose_unrouted(router, unrouted | routed)

    _assert_snapshots_equal(before, _full_grid_snapshot(router))


def test_exhausted_budget_reports_unclassified_not_a_guess() -> None:
    router = _crossing_router(force_python=True)
    unrouted = _route(router)

    diagnosis = diagnose_unrouted(router, unrouted, budget_s=0.0)

    assert diagnosis is not None
    assert diagnosis.budget_exhausted is True
    assert [c.cause for c in diagnosis.connections] == [CAUSE_UNCLASSIFIED]
    assert diagnosis.summary_dict()["unclassified"] == 1


def test_nothing_unrouted_returns_none() -> None:
    router = _crossing_router(force_python=True)
    assert diagnose_unrouted(router, set()) is None


def test_released_grid_returns_none() -> None:
    router = _boxed_pad_router(force_python=True)
    unrouted = _route(router)
    router.grid.release_arrays()
    assert diagnose_unrouted(router, unrouted) is None


# ---------------------------------------------------------------------------
# JSON report wiring
# ---------------------------------------------------------------------------


def test_json_report_carries_unrouted_cause_on_a_live_call() -> None:
    router = _crossing_router(force_python=True)
    _route(router)

    doc = get_routing_diagnostics_json(router, {"A": 1, "B": 2}, 2, diagnose_unrouted_budget=10.0)

    json.dumps(doc)  # serialisable
    [entry] = doc["unrouted"]
    assert entry["cause"] == "congested"
    assert entry["contenders"][0]["net_name"] in {"A", "B"}
    assert doc["unrouted_diagnosis"]["congested"] == 1
    assert doc["unrouted_diagnosis"]["budget_s"] == 10.0


def test_json_report_omits_the_keys_when_the_pass_is_off() -> None:
    router = _crossing_router(force_python=True)
    _route(router)

    doc = get_routing_diagnostics_json(router, {"A": 1, "B": 2}, 2)

    assert "unrouted" not in doc and "unrouted_diagnosis" not in doc


def _args(fmt: str = "json", budget: float = 20.0) -> argparse.Namespace:
    return argparse.Namespace(format=fmt, diagnose_unrouted_budget=budget)


def test_cli_stash_survives_grid_release(capsys: pytest.CaptureFixture[str]) -> None:
    """``kct route`` releases the grid before the report; the stash bridges it."""
    from kicad_tools.cli.route_cmd import (
        _release_routing_engine_state,
        _stash_unrouted_diagnosis,
    )

    router = _boxed_pad_router(force_python=True)
    _route(router)

    _stash_unrouted_diagnosis(router, _args())
    _release_routing_engine_state(router)
    doc = get_routing_diagnostics_json(router, {"SIG": 1}, 1)

    assert [e["cause"] for e in doc["unrouted"]] == ["blocked"]
    captured = capsys.readouterr()
    assert "Unrouted diagnosis: 0 congested, 1 blocked" in captured.err
    assert "Unrouted diagnosis" not in captured.out, "#5938: progress must not hit stdout"


@pytest.mark.parametrize("args", [_args(fmt="text"), _args(budget=0.0)], ids=["text", "budget0"])
def test_cli_stash_is_off_for_text_output_and_zero_budget(args: argparse.Namespace) -> None:
    from kicad_tools.cli.route_cmd import _stash_unrouted_diagnosis

    router = _boxed_pad_router(force_python=True)
    _route(router)

    _stash_unrouted_diagnosis(router, args)

    assert router.unrouted_diagnosis is None


def test_budget_flag_parses_and_is_forwarded() -> None:
    from kicad_tools.cli.commands import routing as routing_commands
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(
        ["route", "board.kicad_pcb", "--diagnose-unrouted-budget", "7.5"]
    )
    assert args.diagnose_unrouted_budget == 7.5

    captured: dict[str, list[str]] = {}

    def fake_main(argv: list[str]) -> int:
        captured["argv"] = argv
        return 0

    import kicad_tools.cli.route_cmd as route_cmd

    original = route_cmd.main
    route_cmd.main = fake_main  # type: ignore[assignment]
    try:
        routing_commands.run_route_command(args)
    finally:
        route_cmd.main = original  # type: ignore[assignment]
    argv = captured["argv"]
    assert argv[argv.index("--diagnose-unrouted-budget") + 1] == "7.5"


def test_cli_stash_diagnoses_the_missing_edge_of_a_partial_net() -> None:
    """A net with some copper but a stranded pad is diagnosed on its failed edge."""
    from kicad_tools.cli.route_cmd import _stash_unrouted_diagnosis

    router = _boxed_pad_router(force_python=True)
    router.add_component("R3", [{"number": "1", "x": 9.0, "y": 8.0, "net": 1, "net_name": "SIG"}])
    _route(router)
    assert 1 in {route.net for route in router.routes}, "fixture sanity: R2-R3 routes"

    _stash_unrouted_diagnosis(router, _args())

    diagnosis = router.unrouted_diagnosis
    assert diagnosis is not None
    assert [c.cause for c in diagnosis.connections] == [CAUSE_BLOCKED]
    [conn] = diagnosis.connections
    assert ("R1", "1") in {conn.source_pad, conn.target_pad}
    doc = get_routing_diagnostics_json(router, {"SIG": 1}, 1)
    assert [e["cause"] for e in doc["unrouted"]] == ["blocked"]
    assert doc["unrouted_diagnosis"]["connections"] == 1


def test_cli_stash_skips_nets_outside_the_placement_disposition() -> None:
    """With a disposition, only its eligible nets are diagnosed (as in the report)."""
    from kicad_tools.cli.route_cmd import _stash_unrouted_diagnosis
    from kicad_tools.placement.routing import RoutingPlacementDisposition

    router = _boxed_pad_router(force_python=True)
    _route(router)
    router.placement_disposition = RoutingPlacementDisposition(requested_nets=frozenset())

    _stash_unrouted_diagnosis(router, _args())

    assert router.unrouted_diagnosis is None


def test_cli_stash_treats_preserved_copper_as_connecting() -> None:
    """A net joined only by retained ``--preserve-existing`` copper is not unrouted."""
    from kicad_tools.cli.route_cmd import _stash_unrouted_diagnosis
    from kicad_tools.placement.routing import RoutingPlacementDisposition
    from kicad_tools.router.primitives import Route, Segment

    router = _boxed_pad_router(force_python=True)
    assert _route(router) == {1}, "fixture sanity: the boxed pad fails to route"
    router.placement_disposition = RoutingPlacementDisposition(requested_nets=frozenset({"SIG"}))
    router.existing_routes = [
        Route(
            net=1,
            net_name="SIG",
            segments=[Segment(3.0, 5.0, 9.0, 5.0, 0.2, Layer.F_CU, net=1, net_name="SIG")],
        )
    ]

    _stash_unrouted_diagnosis(router, _args())

    assert router.unrouted_diagnosis is None

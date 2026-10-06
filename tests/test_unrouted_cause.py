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


def _grid_state(router: Autorouter) -> tuple:
    grid = router.grid
    cpp = getattr(grid, "_cpp_grid", None)
    return (
        np.array(grid._blocked, copy=True),
        np.array(grid._net, copy=True),
        sorted(id(r) for r in grid.routes),
        cpp._impl.count_blocked() if cpp is not None else None,
    )


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


@pytest.mark.parametrize("force_python", BACKENDS)
def test_grid_and_witness_journal_are_restored(force_python: bool) -> None:
    router = _crossing_router(force_python)
    unrouted = _route(router)
    assert router.grid.routes, "fixture sanity: the winning net's copper is on the grid"
    journal: list[tuple[str, object]] = []

    def observer(event: str, route: object) -> None:
        journal.append((event, route))

    router.grid.commit_observer = observer
    before = _grid_state(router)

    diagnosis = diagnose_unrouted(router, unrouted)

    after = _grid_state(router)
    assert diagnosis is not None and diagnosis.routes_lifted == len(router.grid.routes)
    assert np.array_equal(before[0], after[0]), "blocked plane not restored"
    assert np.array_equal(before[1], after[1]), "net plane not restored"
    assert before[2] == after[2], "grid.routes membership not restored"
    assert before[3] == after[3], "C++ grid occupancy not restored"
    assert journal == [], "the diagnosis leaked lift/restore events into the witness journal"
    assert router.grid.commit_observer is observer


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

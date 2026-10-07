"""``kct route-auto`` classifies unrouted connections (Issue #6001).

Follow-up to #5944, which taught ``kct route`` (the Autorouter) to say why a
connection is unrouted.  ``kct route-auto`` (the RoutingOrchestrator) is a
separate code path; these tests drive it end to end on two boards in
``tests/fixtures/route_auto_6001/``:

* ``caged_pad.kicad_pcb`` -- pad ``R1.1`` of ``/SIG`` sits inside a cage of
  four keepout rule areas (``cage-0`` .. ``cage-3``) on both layers.  The
  connection is ``blocked`` and the cage areas are named as its blockers.
* ``crossing.kicad_pcb`` -- ``/A`` is already routed west-east on ``F.Cu``;
  ``/B`` must cross it south-north, but ``B.Cu`` is a keepout and two ``F.Cu``
  keepouts stop it from going round ``/A``'s ends.  ``/B`` is ``congested``
  with ``/A`` as its contender.

Before this issue no route-auto strategy saw ``/A``'s copper at all: every one
drew ``/B`` straight through it and reported success.  The short gate pinned
here is what turns that silent short into a failure the diagnosis can explain.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli import main as kct_main
from kicad_tools.mcp.tools.routing import route_net_auto
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.route_auto_diagnosis import (
    diagnose_route_auto_net,
    island_bridges,
    load_diagnosis_router,
)
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.strategies import RoutingResult, RoutingStrategy
from tests.test_unrouted_cause import _assert_snapshots_equal, _full_grid_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "route_auto_6001"
CAGED = FIXTURES / "caged_pad.kicad_pcb"
CROSSING = FIXTURES / "crossing.kicad_pcb"

BACKENDS = [
    pytest.param(True, id="python"),
    pytest.param(
        False,
        id="cpp",
        marks=pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not built"),
    ),
]


@pytest.fixture
def no_python_fallback(monkeypatch):
    """Skip the pure-Python A* retry after the C++ search gives up.

    Same rationale as ``test_route_auto_keepout_rule_areas_6059``: the caged
    pad is unroutable, and the hierarchical retry's negotiated loop would
    re-run each exhausted C++ search in pure Python.  No-op without C++.
    """
    from kicad_tools.router.cpp_backend import CppPathfinder

    monkeypatch.setattr(CppPathfinder, "_try_python_fallback", lambda *a, **k: None)


def _copy(tmp_path: Path, board: Path) -> Path:
    dst = tmp_path / board.name
    shutil.copy(board, dst)
    return dst


def _route_auto_json(capsys, argv: list[str]) -> tuple[int, dict]:
    rc = kct_main(["route-auto", *argv, "--format", "json"])
    out = capsys.readouterr().out
    return rc, json.loads(out)  # the whole of stdout is one document (#5938)


# ---------------------------------------------------------------------------
# Acceptance criteria, through ``kct route-auto --format json``
# ---------------------------------------------------------------------------


def test_pad_boxed_in_by_keepout_is_blocked(tmp_path, capsys, no_python_fallback) -> None:
    rc, doc = _route_auto_json(capsys, [str(_copy(tmp_path, CAGED)), "--net", "/SIG"])

    assert rc == 1
    [entry] = doc["nets"]
    assert entry["success"] is False
    [conn] = entry["unrouted"]
    assert conn["cause"] == "blocked", conn
    assert {conn["source_pad"]["ref"], conn["target_pad"]["ref"]} == {"R1", "R2"}
    assert "contenders" not in conn
    keepouts = [b for b in conn["blockers"] if b["kind"] == "keepout"]
    assert {b.get("name") for b in keepouts} == {"cage-0", "cage-1", "cage-2", "cage-3"}
    assert all(b["source"] == "rule_area" for b in keepouts)
    assert conn["frontier"]["endpoint"] == {"ref": "R1", "pin": "1"}
    summary = entry["unrouted_diagnosis"]
    assert summary["blocked"] == 1 and summary["connections"] == 1
    assert summary["budget_s"] == 20.0
    assert summary["elapsed_s"] < summary["budget_s"]


def test_net_crossing_a_routed_net_is_congested(tmp_path, capsys) -> None:
    src = _copy(tmp_path, CROSSING)
    out = tmp_path / "out.kicad_pcb"
    rc, doc = _route_auto_json(capsys, [str(src), "--net", "/B", "-o", str(out)])

    assert rc == 1
    [entry] = doc["nets"]
    assert entry["success"] is False
    assert "shorts net(s) '/A'" in entry["error"]
    [conn] = entry["unrouted"]
    assert conn["cause"] == "congested", conn
    assert [c["net_name"] for c in conn["contenders"]] == ["/A"]
    assert conn["contenders"][0]["cells"] > 0
    assert conn["solo_path"]["vias"] == 0
    assert "blockers" not in conn
    assert entry["unrouted_diagnosis"]["congested"] == 1
    assert entry["unrouted_diagnosis"]["routes_lifted"] == 1
    # Nothing -- least of all the short -- was written.
    assert not out.exists()


def test_budget_zero_skips_the_diagnosis(tmp_path, capsys, monkeypatch) -> None:
    import kicad_tools.router.route_auto_diagnosis as rad

    def _no_load(*_a, **_k):
        raise AssertionError("budget 0 must not load the diagnosis board")

    monkeypatch.setattr(rad, "load_diagnosis_router", _no_load)
    rc, doc = _route_auto_json(
        capsys,
        [str(_copy(tmp_path, CROSSING)), "--net", "/B", "--diagnose-unrouted-budget", "0"],
    )
    assert rc == 1
    [entry] = doc["nets"]
    assert entry["success"] is False
    assert "unrouted" not in entry and "unrouted_diagnosis" not in entry


def test_text_output_runs_no_diagnosis(tmp_path, capsys, monkeypatch) -> None:
    import kicad_tools.router.route_auto_diagnosis as rad

    monkeypatch.setattr(
        rad, "diagnose_route_auto_net", lambda **_k: pytest.fail("text mode diagnosed")
    )
    rc = kct_main(["route-auto", str(_copy(tmp_path, CROSSING)), "--net", "/B"])
    assert rc == 1
    assert "Unrouted diagnosis" not in capsys.readouterr().err


def test_corridor_only_strategy_is_unclassified_with_a_reason(tmp_path, capsys) -> None:
    """A forced corridor strategy searched no fine grid: nothing to re-run."""
    rc, doc = _route_auto_json(
        capsys,
        [str(_copy(tmp_path, CROSSING)), "--net", "/B", "--strategy", "global"],
    )
    assert rc == 1
    [conn] = doc["nets"][0]["unrouted"]
    assert conn["cause"] == "unclassified"
    assert "no fine routing grid" in conn["note"]
    assert "global" in conn["note"]
    assert doc["nets"][0]["unrouted_diagnosis"]["routes_lifted"] == 0


def test_diagnosis_failure_does_not_fail_the_route(tmp_path, capsys, monkeypatch) -> None:
    import kicad_tools.router.route_auto_diagnosis as rad

    def _boom(**_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rad, "diagnose_route_auto_net", _boom)
    rc, doc = _route_auto_json(capsys, [str(_copy(tmp_path, CROSSING)), "--net", "/B"])
    assert rc == 1
    assert "unrouted" not in doc["nets"][0]


def test_successful_net_carries_no_unrouted_block(tmp_path) -> None:
    result = route_net_auto(
        str(_copy(tmp_path, CROSSING)),
        "/A",
        strategy="hierarchical",
        diagnose_unrouted_budget=5.0,
    )
    # /A is already routed by its own copper: nothing is left to classify.
    assert "unrouted" not in result


# ---------------------------------------------------------------------------
# The diagnosis grid is restored exactly
# ---------------------------------------------------------------------------


def _failed_b_result() -> RoutingResult:
    return RoutingResult(
        success=False, net="/B", strategy_used=RoutingStrategy.HIERARCHICAL_DIFF_PAIR
    )


@pytest.mark.parametrize("force_python", BACKENDS)
def test_diagnosis_grid_is_restored_exactly(force_python: bool) -> None:
    rules = DesignRules()
    router = load_diagnosis_router(str(CROSSING), rules, force_python=force_python)
    assert len(router.grid.routes) == 1, "fixture sanity: /A's copper is a committed route"
    pads = [router.pads[("R3", "1")], router.pads[("R4", "1")]]
    before = _full_grid_snapshot(router)

    diagnosis = diagnose_route_auto_net(
        pcb_path=str(CROSSING),
        net_id=pads[0].net,
        net_name="/B",
        pads=pads,
        result=_failed_b_result(),
        strategies_attempted=[RoutingStrategy.HIERARCHICAL_DIFF_PAIR],
        rules=rules,
        budget_s=10.0,
        router=router,
    )

    _assert_snapshots_equal(before, _full_grid_snapshot(router))
    assert diagnosis is not None
    [conn] = diagnosis.connections
    assert conn.cause == "congested"
    assert [c["net_name"] for c in conn.contenders] == ["/A"]


# ---------------------------------------------------------------------------
# Which connections, and the short gate
# ---------------------------------------------------------------------------


class _P:
    def __init__(self, ref: str, x: float, y: float) -> None:
        self.ref, self.pin, self.x, self.y = ref, "1", x, y


def test_island_bridges_join_islands_once_each() -> None:
    a, b, c, d = _P("A", 0, 0), _P("B", 1, 0), _P("C", 10, 0), _P("D", 11, 0)
    # A-B and C-D already joined: one bridge, between the nearest pads.
    assert [(s.ref, t.ref) for s, t in island_bridges([a, b, c, d], [0, 0, 1, 1])] == [("B", "C")]
    # No copper at all: the plain MST.
    assert len(island_bridges([a, b, c, d], [0, 1, 2, 3])) == 3
    # Fully joined: nothing to diagnose.
    assert island_bridges([a, b, c, d], [0, 0, 0, 0]) == []


def test_existing_same_net_copper_joins_its_pads() -> None:
    from types import SimpleNamespace

    a, b, c = _P("A", 0, 0), _P("B", 5, 0), _P("C", 9, 0)
    seg = SimpleNamespace(start=(0.0, 0.0), end=(5.0, 0.0), layer="F.Cu")
    diagnosis = diagnose_route_auto_net(
        pcb_path="unused",
        net_id=1,
        net_name="N",
        pads=[a, b, c],
        result=RoutingResult(
            success=False, net="N", strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR
        ),
        strategies_attempted=[RoutingStrategy.GLOBAL_WITH_REPAIR],
        rules=DesignRules(),
        budget_s=5.0,
        existing_segments=[seg],
    )
    assert diagnosis is not None
    assert [(c.source_pad[0], c.target_pad[0]) for c in diagnosis.connections] == [("B", "C")]


def test_short_gate_spares_own_net_copper(tmp_path) -> None:
    """Re-routing /A over its own existing copper is not a short."""
    result = route_net_auto(str(_copy(tmp_path, CROSSING)), "/A", strategy="global")
    assert "shorts net" not in (result.get("error_message") or "")

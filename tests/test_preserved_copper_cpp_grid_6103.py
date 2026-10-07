"""Issue #6103: preserved copper reaches the C++ grid.

``load_pcb_for_routing(load_existing_routes=True)`` -- the loader behind
``kct route --nets``, ``--preserve-existing`` and ``--resume`` -- marked the
board's existing tracks and vias on the Python grid only.  The paired C++ grid
is built in ``Autorouter.__init__`` before that copper is loaded, so the C++
A* routed straight through it, post-route validation rejected every candidate
and the net fell back to the pure-Python search.

The fixture is ``route_auto_6001/crossing.kicad_pcb`` without its ``west`` /
``east`` keepouts: ``/A`` is routed west-east on F.Cu, B.Cu is a keepout, and
``/B`` must detour around ``/A``'s copper.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from kicad_tools.router.cpp_backend import CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.rules import DesignRules

DETOUR = Path(__file__).parent / "fixtures" / "route_6103" / "detour.kicad_pcb"
ARC = Path(__file__).parent / "fixtures" / "route_auto_6107" / "arc_on_path.kicad_pcb"

needs_cpp = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not built")


def _load(path: Path = DETOUR, **kwargs):
    return load_pcb_for_routing(
        str(path),
        rules=DesignRules(),
        use_pcb_rules=False,
        validate_drc=False,
        **kwargs,
    )


def _a_copper_cells(router) -> list[tuple[int, int]]:
    """Grid cells under /A's track, (102, 108) -> (128, 108) on F.Cu."""
    cells = []
    for x in (105.0, 110.0, 115.0, 120.0, 125.0):
        cells.append(router.grid.world_to_grid(x, 108.0))
    return cells


@needs_cpp
def test_loader_marks_preserved_copper_on_the_cpp_grid() -> None:
    router, _ = _load(load_existing_routes=True)
    cpp = router._cpp_grid
    assert cpp is not None
    for gx, gy in _a_copper_cells(router):
        assert router.grid._blocked[0, gy, gx]
        assert cpp._impl.at(gx, gy, 0).blocked, (gx, gy)


@needs_cpp
def test_without_existing_routes_the_cells_stay_free() -> None:
    """Control: the blocking above comes from the loaded copper, not the pads."""
    router, _ = _load(load_existing_routes=False)
    for gx, gy in _a_copper_cells(router):
        assert not router._cpp_grid._impl.at(gx, gy, 0).blocked


@needs_cpp
def test_detour_net_routes_on_cpp_without_python_fallback() -> None:
    router, _ = _load(load_existing_routes=True, skip_nets=["/A"])
    router.route_all(suppress_no_timeout_warning=True)
    stats = router.backend_info["fallback_stats"]
    assert stats["fallback_count"] == 0, stats
    routed = [r for r in router.routes if r.net_name == "/B"]
    assert routed and any(r.segments for r in routed)


@needs_cpp
def test_proof_memo_fingerprint_covers_preserved_copper() -> None:
    """#6098's fallback memo keys on the Python grid, which carries the copper."""
    with_copper, _ = _load(load_existing_routes=True)
    without, _ = _load(load_existing_routes=False)
    stamp = CppPathfinder._py_grid_state_stamp
    assert stamp(with_copper.grid)[1] != stamp(without.grid)[1]


@needs_cpp
def test_diagnosis_router_mirrors_each_route_exactly_once(monkeypatch) -> None:
    """The #6001 diagnosis loader no longer re-mirrors (double-marking)."""
    from kicad_tools.router.route_auto_diagnosis import load_diagnosis_router

    calls: Counter[int] = Counter()
    original = RoutingGrid._mark_route_on_cpp_cells

    def spy(self, route, max_trace_width=None):
        calls[id(route)] += 1
        return original(self, route, max_trace_width=max_trace_width)

    monkeypatch.setattr(RoutingGrid, "_mark_route_on_cpp_cells", spy)
    router = load_diagnosis_router(str(DETOUR), DesignRules())
    assert router.existing_routes
    assert {id(r) for r in router.existing_routes} == set(calls)
    assert set(calls.values()) == {1}


@needs_cpp
@pytest.mark.parametrize("board", [DETOUR, ARC], ids=["loader_tracks", "orchestrator_arcs"])
def test_hierarchical_board_router_mirrors_each_route_exactly_once(monkeypatch, board) -> None:
    """#6107's board-grid loader mirrors only its arcs; the loader did the rest.

    ``DETOUR`` carries tracks the loader mirrors (the double-marking case);
    ``ARC`` carries an arc only the orchestrator can mirror.
    """
    from kicad_tools.router.io import detect_layer_stack, parse_pcb_design_rules
    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.schema.pcb import PCB

    text = board.read_text()
    orchestrator = RoutingOrchestrator(
        pcb=PCB.load(str(board)),  # type: ignore[arg-type]
        rules=parse_pcb_design_rules(text).to_design_rules(),
        layer_stack=detect_layer_stack(text),
    )
    calls: Counter[int] = Counter()
    original = RoutingGrid._mark_route_on_cpp_cells

    def spy(self, route, max_trace_width=None):
        calls[id(route)] += 1
        return original(self, route, max_trace_width=max_trace_width)

    monkeypatch.setattr(RoutingGrid, "_mark_route_on_cpp_cells", spy)
    board_nets = {n.name for n in orchestrator.pcb.nets.values() if n.name}
    router, _ = orchestrator._load_board_router(board, "/B", board_nets)
    assert router.existing_routes
    assert {id(r) for r in router.existing_routes} == set(calls)
    assert set(calls.values()) == {1}

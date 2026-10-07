"""``kct route-auto`` honours board keepout rule areas (Issue #6059).

#6008 taught ``kct route`` (the Autorouter) to enforce ``(zone ... (keepout
(tracks not_allowed) (vias not_allowed)))`` rule areas.  ``kct route-auto``
(the RoutingOrchestrator) never reached that code: its global / escape /
subgrid / via-resolution / multi-resolution strategies plan coarse corridors
with no obstacle model, and its hierarchical strategy's AdaptiveAutorouter had
no source board, so it found no areas.  Every strategy ran straight through a
full-height wall.

These tests reuse the wall board from ``test_grid_keepout_rule_areas_6008`` and
check the written copper GEOMETRICALLY: headless ``kicad-cli pcb drc`` 10.0.1
does not flag keepout violations (#6039), so it cannot be the oracle.

Coordinates read back from the output board are board-relative: the board
outline starts at sheet (100, 100), which the schema ``PCB`` subtracts.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.cli import main as kct_main
from kicad_tools.mcp.tools.routing import route_net_auto
from kicad_tools.schema.pcb import PCB
from tests.test_grid_keepout_rule_areas_6008 import (
    BX0,
    BY0,
    WALL_X0,
    WALL_X1,
    _board,
    _keepout,
    _wall,
)

# The wall in the board-relative frame the orchestrator routes in.
REL_WALL_X0, REL_WALL_X1 = WALL_X0 - BX0, WALL_X1 - BX0

# A block in the middle of the board: a route can go above or below it.
BLOCK = [(113.0, 104.0), (117.0, 104.0), (117.0, 112.0), (113.0, 112.0)]
REL_BLOCK = (113.0 - BX0, 104.0 - BY0, 117.0 - BX0, 112.0 - BY0)

ALL_STRATEGIES = [
    "auto",
    "global",
    "escape",
    "hierarchical",
    "subgrid",
    "via_resolution",
    "multi_resolution",
]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text)
    return path


def _route(tmp_path: Path, text: str, strategy: str) -> tuple[dict, Path]:
    src = _write(tmp_path, text)
    out = tmp_path / f"out_{strategy}.kicad_pcb"
    result = route_net_auto(str(src), "/SIG", output_path=str(out), strategy=strategy)
    return result, out


def _written_copper(out: Path) -> tuple[list, list]:
    if not out.exists():
        return [], []
    pcb = PCB.load(str(out))
    return list(pcb.segments), list(pcb.vias)


def _copper_in_band(seg, x0: float, x1: float) -> bool:
    lo = min(seg.start[0], seg.end[0]) - seg.width / 2
    hi = max(seg.start[0], seg.end[0]) + seg.width / 2
    return hi > x0 and lo < x1


def _copper_in_rect(seg, rect: tuple[float, float, float, float]) -> bool:
    """Sampled test: does the trace's copper (half-width disc) enter ``rect``?"""
    x0, y0, x1, y1 = rect
    (ax, ay), (bx, by) = seg.start, seg.end
    half = seg.width / 2
    steps = max(1, int(math.hypot(bx - ax, by - ay) / 0.02))
    for i in range(steps + 1):
        t = i / steps
        px, py = ax + (bx - ax) * t, ay + (by - ay) * t
        dx = max(x0 - px, 0.0, px - x1)
        dy = max(y0 - py, 0.0, py - y1)
        if math.hypot(dx, dy) < half - 1e-6:
            return True
    return False


# ---------------------------------------------------------------------------
# The acceptance criterion: a full-height wall is never crossed, any strategy
# ---------------------------------------------------------------------------


@pytest.fixture
def no_python_fallback(monkeypatch):
    """Skip the pure-Python A* retry after the C++ search gives up.

    The wall makes the net unroutable, and the hierarchical strategy's
    negotiated loop re-tries it every iteration, rescue and rip-up probe.  Each
    C++ "open set exhausted" then falls back to the pure-Python A*, which
    exhausts the same grid 10-100x slower -- about 5 minutes per test.  The
    keepouts live on the shared grid both searches read, so the C++ search
    alone still exercises the enforcement under test.  Without the C++
    backend the Python router is the primary search and this is a no-op.
    """
    from kicad_tools.router.cpp_backend import CppPathfinder

    monkeypatch.setattr(CppPathfinder, "_try_python_fallback", lambda *a, **k: None)


@pytest.mark.parametrize("strategy", ALL_STRATEGIES)
def test_wall_is_never_crossed(tmp_path: Path, strategy: str, no_python_fallback) -> None:
    result, out = _route(tmp_path, _board(_wall()), strategy)

    segments, vias = _written_copper(out)
    crossing = [s for s in segments if _copper_in_band(s, REL_WALL_X0, REL_WALL_X1)]
    assert crossing == []
    assert [v for v in vias if REL_WALL_X0 < v.position[0] < REL_WALL_X1] == []
    # The only path crosses the wall, so the net cannot be routed.
    assert result["success"] is False
    assert result["error_message"]


@pytest.mark.parametrize("strategy", ["global", "escape", "subgrid", "via_resolution"])
def test_corridor_strategy_refusal_names_the_area(tmp_path: Path, strategy: str) -> None:
    result, out = _route(tmp_path, _board(_wall()), strategy)
    assert "keepout rule area" in result["error_message"]
    assert "'wall'" in result["error_message"]
    assert not out.exists()


def test_corridor_strategy_warns_once(tmp_path: Path, capsys) -> None:
    route_net_auto(str(_write(tmp_path, _board(_wall()))), "/SIG", strategy="global")
    err = capsys.readouterr().err
    assert err.count("keepout rule area") == 1
    assert "'global' strategy" in err
    assert "#6059" in err


def test_cli_route_auto_wall(tmp_path: Path, capsys, no_python_fallback) -> None:
    """End to end through ``kct route-auto``: non-zero exit, nothing written."""
    src = _write(tmp_path, _board(_wall()))
    out = tmp_path / "cli_out.kicad_pcb"
    rc = kct_main(["route-auto", str(src), "--net", "/SIG", "-o", str(out)])
    assert rc != 0
    segments, _vias = _written_copper(out)
    assert [s for s in segments if _copper_in_band(s, REL_WALL_X0, REL_WALL_X1)] == []
    assert "keepout rule area" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Enforcement, not just refusal: routes go around an area when they can
# ---------------------------------------------------------------------------


def test_hierarchical_routes_around_a_block(tmp_path: Path) -> None:
    result, out = _route(tmp_path, _board(_keepout("block", BLOCK)), "hierarchical")
    assert result["success"] is True, result["error_message"]
    segments, _vias = _written_copper(out)
    assert segments
    assert [s for s in segments if _copper_in_rect(s, REL_BLOCK)] == []


def test_auto_retries_hierarchical_around_a_block(tmp_path: Path) -> None:
    """The global corridor runs straight through the block and is refused;
    auto mode then retries the hierarchical strategy, which goes around."""
    result, out = _route(tmp_path, _board(_keepout("block", BLOCK)), "auto")
    assert result["success"] is True, result["error_message"]
    assert result["strategy_used"] == "HIERARCHICAL_DIFF_PAIR"
    segments, _vias = _written_copper(out)
    assert segments
    assert [s for s in segments if _copper_in_rect(s, REL_BLOCK)] == []


def test_hierarchical_detours_a_front_only_wall_on_the_back(tmp_path: Path) -> None:
    text = _board(_wall(layers='"F.Cu"', vias="allowed"))
    result, out = _route(tmp_path, text, "hierarchical")
    assert result["success"] is True, result["error_message"]
    segments, vias = _written_copper(out)
    front = [s for s in segments if s.layer == "F.Cu"]
    assert [s for s in front if _copper_in_band(s, REL_WALL_X0, REL_WALL_X1)] == []
    assert any(s.layer == "B.Cu" and _copper_in_band(s, REL_WALL_X0, REL_WALL_X1) for s in segments)
    assert vias


# ---------------------------------------------------------------------------
# No change where no track/via-blocking area exists (the fleet case)
# ---------------------------------------------------------------------------


def test_board_without_keepouts_routes_as_before(tmp_path: Path, capsys) -> None:
    result, out = _route(tmp_path, _board(), "global")
    assert result["success"] is True
    assert _written_copper(out)[0]
    assert "keepout" not in capsys.readouterr().err


def test_pour_only_rule_area_does_not_constrain(tmp_path: Path, capsys) -> None:
    pour_only = _wall(tracks="allowed", vias="allowed")
    result, out = _route(tmp_path, _board(pour_only), "global")
    assert result["success"] is True
    assert _written_copper(out)[0]
    assert "keepout" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# One shared parse for ``kct route`` and ``kct route-auto``
# ---------------------------------------------------------------------------


def test_route_and_route_auto_read_the_same_areas(tmp_path: Path) -> None:
    from kicad_tools.router.io import detect_layer_stack, load_pcb_for_routing
    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.router.rules import DesignRules

    text = _board(_wall(layers='"F.Cu"', vias="allowed"))
    src = _write(tmp_path, text)
    router, _ = load_pcb_for_routing(
        str(src), rules=DesignRules(), use_pcb_rules=False, validate_drc=False
    )
    route_areas = router._keepout_rule_area_polygons()

    pcb = PCB.load(str(src))
    orchestrator = RoutingOrchestrator(
        pcb=pcb,  # type: ignore[arg-type]
        rules=DesignRules(),
        layer_stack=detect_layer_stack(text),
    )
    auto_areas = orchestrator._keepout_mask().areas

    assert len(route_areas) == len(auto_areas) == 1
    (r,), (a,) = route_areas, auto_areas
    assert (r.layers, r.blocks_tracks, r.blocks_vias, r.name) == (
        a.layers,
        a.blocks_tracks,
        a.blocks_vias,
        a.name,
    )
    # Same polygon; ``kct route`` is sheet-absolute, route-auto board-relative.
    assert [(x - BX0, y - BY0) for x, y in r.polygon] == list(a.polygon)


def test_hierarchical_layer_escalation_capped_at_board_copper(tmp_path: Path) -> None:
    """A 2-layer board's hierarchical retry must not escape to In1/In2.Cu."""
    from kicad_tools.router.io import detect_layer_stack
    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.router.rules import DesignRules

    text = _board(_wall())
    pcb = PCB.load(str(_write(tmp_path, text)))
    orchestrator = RoutingOrchestrator(
        pcb=pcb,  # type: ignore[arg-type]
        rules=DesignRules(),
        layer_stack=detect_layer_stack(text),
    )
    from kicad_tools.mcp.tools.routing import _build_pads_for_net

    pads = _build_pads_for_net(pcb, 1, "/SIG")
    orchestrator._route_hierarchical("/SIG", None, pads)
    assert orchestrator._hierarchical is not None
    assert orchestrator._hierarchical.max_layers == 2
    assert orchestrator._hierarchical.rule_area_specs


def test_known_layer_stack_from_schema_copper_names() -> None:
    from types import SimpleNamespace

    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.router.rules import DesignRules

    copper = [SimpleNamespace(name=n) for n in ("F.Cu", "B.Cu", "In2.Cu", "In1.Cu")]
    pcb = SimpleNamespace(copper_layers=copper)
    stack = RoutingOrchestrator(pcb=pcb, rules=DesignRules())._known_layer_stack()  # type: ignore[arg-type]
    assert stack is not None
    assert [layer.name for layer in stack.layers] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


# ---------------------------------------------------------------------------
# Escape phases (#6061): the orchestrator installs the areas BEFORE escaping
#
# The #6061 escape and sub-grid gates only fire when the areas are already on
# the grid.  ``kct route-auto`` reaches EscapeRouter / SubGridRouter directly
# (not through the Autorouter entry points #6061 patched), so these tests
# drive the orchestrator's own escape steps on the #6061 BGA/QFN board.
# ---------------------------------------------------------------------------


def _escape_board(tmp_path: Path, force_python: bool = True):
    """The #6061 board loaded for routing, plus its schema model."""
    from tests import test_escape_keepout_rule_areas_6061 as esc

    text = esc._board(True)
    path = tmp_path / "escape.kicad_pcb"
    path.write_text(text)
    router = esc._load(tmp_path, with_areas=True, force_python=force_python)
    return esc, router, PCB.load(str(path))


def _package_pads(router, ref: str) -> list:
    return [pad for (r, _pin), pad in sorted(router.pads.items()) if r == ref]


def _grid_orchestrator(router, schema_pcb):
    """An orchestrator over ``router.grid`` that must install the areas ITSELF.

    The PCB-like object exposes the grid but not the Autorouter's own
    installer, so only the orchestrator's #6059 path can put the areas there.
    The Autorouter routes sheet-absolute, so the specs are shifted to match.
    """
    from types import SimpleNamespace

    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.router.rule_area_resolve import keepout_rule_area_specs

    pcb_like = SimpleNamespace(grid=router.grid, width=40.0, height=20.0)
    return RoutingOrchestrator(
        pcb=pcb_like,  # type: ignore[arg-type]
        rules=router.rules,
        layer_stack=router.layer_stack,
        rule_areas=keepout_rule_area_specs(schema_pcb, offset=schema_pcb.board_origin),
    )


def _no_corridor(orchestrator) -> None:
    """Isolate the escape phase: stub out the coarse corridor phase."""
    from kicad_tools.router.strategies import RoutingResult, RoutingStrategy

    def _empty(net, pads):
        return RoutingResult(
            success=True, net=net, strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR
        )

    orchestrator._route_global = _empty  # type: ignore[method-assign]


def test_escape_strategy_installs_areas_before_escaping(tmp_path: Path) -> None:
    esc, router, schema_pcb = _escape_board(tmp_path)
    orchestrator = _grid_orchestrator(router, schema_pcb)
    _no_corridor(orchestrator)
    assert router.grid._rule_area_keepouts is None

    found = []
    for ref in ("U1", "U2"):
        result = orchestrator._route_escape_then_global("pkg", _package_pads(router, ref))
        assert result.metrics.escape_segments > 0, ref
        found.append(result)

    assert len(router.grid._rule_area_keepouts) == 3
    routes = []
    for result in found:
        from kicad_tools.router.primitives import Route

        route = Route(net=0, net_name=f"escape-{result.net}")
        route.segments.extend(result.segments)
        route.vias.extend(result.vias)
        routes.append(route)
    assert esc._intrusions(routes) == []


def test_escape_strategy_without_install_would_intrude(tmp_path: Path) -> None:
    """Control: with the orchestrator's areas empty the same escape step
    lands copper in the areas, so the test above is not vacuous."""
    from types import SimpleNamespace

    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.router.primitives import Route

    esc, router, _schema_pcb = _escape_board(tmp_path)
    orchestrator = RoutingOrchestrator(
        pcb=SimpleNamespace(grid=router.grid, width=40.0, height=20.0),  # type: ignore[arg-type]
        rules=router.rules,
        layer_stack=router.layer_stack,
        rule_areas=[],
    )
    _no_corridor(orchestrator)
    route = Route(net=0, net_name="escape")
    for ref in ("U1", "U2"):
        result = orchestrator._route_escape_then_global("pkg", _package_pads(router, ref))
        route.segments.extend(result.segments)
        route.vias.extend(result.vias)
    assert esc._intrusions([route]) != []

    subgrid = Route(net=0, net_name="subgrid")
    pads = _package_pads(router, "U1") + _package_pads(router, "U2")
    result = orchestrator._route_subgrid_adaptive("pkg", pads)
    subgrid.segments.extend(result.segments)
    subgrid.vias.extend(result.vias)
    assert esc._intrusions([subgrid]) != []


def test_subgrid_strategy_installs_areas_before_escaping(tmp_path: Path) -> None:
    esc, router, schema_pcb = _escape_board(tmp_path)
    orchestrator = _grid_orchestrator(router, schema_pcb)
    _no_corridor(orchestrator)
    assert router.grid._rule_area_keepouts is None

    from kicad_tools.router.primitives import Route

    route = Route(net=0, net_name="subgrid")
    pads = _package_pads(router, "U1") + _package_pads(router, "U2")
    result = orchestrator._route_subgrid_adaptive("pkg", pads)
    route.segments.extend(result.segments)
    route.vias.extend(result.vias)
    assert len(router.grid._rule_area_keepouts) == 3
    assert esc._intrusions([route]) == []


def test_autorouter_pcb_delegates_install_to_its_own_resolution(tmp_path: Path) -> None:
    """An Autorouter handed in as ``pcb`` installs its own areas (which also
    carry any ``spatial_keepouts`` filter) before the escape step."""
    from kicad_tools.router.orchestrator import RoutingOrchestrator

    esc, router, _schema_pcb = _escape_board(tmp_path)
    orchestrator = RoutingOrchestrator(pcb=router, rules=router.rules)  # type: ignore[arg-type]
    _no_corridor(orchestrator)
    assert router.grid._rule_area_keepouts is None
    orchestrator._route_escape_then_global("pkg", _package_pads(router, "U1"))
    assert len(router.grid._rule_area_keepouts) == 3


def test_route_auto_bga_net_routes_around_keepout(tmp_path: Path) -> None:
    """End to end: a BGA ball wired to a test point beyond ``bga_east`` (all
    layers, tracks and vias).  The corridor runs straight through the area and
    is refused; auto mode retries hierarchical, which goes around."""
    from tests import test_escape_keepout_rule_areas_6061 as esc

    # Ball C6 of the 6x6 BGA sits at (112.0, 109.6) on net /B18.
    tp = _fp_tp("TP1", 121.0, 109.6, 18, "/B18")
    text = esc._board(True).rstrip().removesuffix(")") + tp + ")\n"
    src = tmp_path / "bga.kicad_pcb"
    src.write_text(text)
    out = tmp_path / "bga_out.kicad_pcb"
    result = route_net_auto(str(src), "/B18", output_path=str(out), strategy="auto")
    assert result["success"] is True, result["error_message"]
    assert result["strategy_used"] == "HIERARCHICAL_DIFF_PAIR"

    segments, vias = _written_copper(out)
    assert segments
    origin_x, origin_y = 100.0, 100.0
    for name, poly, layers, tracks, vias_rule, _uid in esc.AREAS:
        xs, ys = [p[0] - origin_x for p in poly], [p[1] - origin_y for p in poly]
        rect = (min(xs), min(ys), max(xs), max(ys))
        if tracks == "not_allowed":
            bad = [s for s in segments if f'"{s.layer}"' in layers and _copper_in_rect(s, rect)]
            assert bad == [], name
        if vias_rule == "not_allowed":
            assert [
                v
                for v in vias
                if math.hypot(
                    max(rect[0] - v.position[0], 0.0, v.position[0] - rect[2]),
                    max(rect[1] - v.position[1], 0.0, v.position[1] - rect[3]),
                )
                < v.size / 2
            ] == [], name


def _fp_tp(ref: str, x: float, y: float, net: int, net_name: str) -> str:
    return f"""  (footprint "TestPoint:TestPoint_Pad_D1.0mm" (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000099") (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (pad "1" smd circle (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net {net} "{net_name}"))
  )
"""

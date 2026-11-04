"""Issue #8370: per-net Python-fallback timing and the repeat-only skip.

On a large HV board the C++ A* drains its open set on the same wide power nets
every negotiated iteration and each hand-off re-ran the 10-100x slower Python
A*.  The first hand-off of a net still falls back (it rescues real nets,
#3456/#3956); once the Python A* has drained for that net, later NO_PATH /
PAIRWISE_BLOCKED hand-offs are skipped.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kicad_tools.router import cpp_backend
from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend unavailable")


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.35,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )


def _grid() -> RoutingGrid:
    grid = RoutingGrid(width=10.0, height=6.0, rules=_rules(), layer_stack=LayerStack.two_layer())
    col = grid.world_to_grid(5.0, 3.0)[0]
    grid._blocked[:, :, col - 2 : col + 3] = True
    grid._is_obstacle[:, :, col - 2 : col + 3] = True
    grid.bump_occupancy_generation()
    return grid


def _pads(net_name: str = "SIG") -> tuple[Pad, Pad]:
    common = {"width": 0.6, "height": 0.6, "net": 1, "net_name": net_name, "layer": Layer.F_CU}
    return Pad(x=2.0, y=3.0, **common), Pad(x=8.0, y=3.0, **common)


def _pathfinder() -> CppPathfinder:
    cpp_grid = CppGrid.from_routing_grid(_grid())
    pf = CppPathfinder(cpp_grid, _rules(), diagonal_routing=True)
    pf.set_routable_layers(cpp_grid.get_routable_indices())
    return pf


def _fallback(pf: CppPathfinder, code: int | None, net: str = "SIG", reason: str = "no path"):
    return pf._try_python_fallback(
        *_pads(net), reason=reason, cpp_failure_reason=code, negotiated_mode=True
    )


def _bump_grid(pf: CppPathfinder) -> None:
    """Change the grid CONTENT (never back to a prior state) so the #6098
    stamp memo cannot hit."""
    n = getattr(pf, "_test_bumps", 0) + 1
    pf._test_bumps = n  # type: ignore[attr-defined]
    pf._grid._py_grid._blocked[0, 0, n] = True
    pf._grid._py_grid.bump_occupancy_generation()


@pytest.mark.parametrize("name", ["FAILURE_NO_PATH", "FAILURE_PAIRWISE_BLOCKED"])
def test_first_falls_back_then_repeat_is_skipped_across_grid_changes(name: str) -> None:
    code = int(getattr(cpp_backend.router_cpp, name))
    pf = _pathfinder()
    with mock.patch.object(Router, "route", autospec=True, side_effect=Router.route) as spy:
        assert _fallback(pf, code) is None
        _bump_grid(pf)
        assert _fallback(pf, code) is None
        _bump_grid(pf)
        assert _fallback(pf, code) is None
    assert spy.call_count == 1
    stats = pf.fallback_stats
    assert stats["python_fallback_repeat_skips"] == 2
    assert stats["python_fallback_seconds_saved"] > 0.0
    assert stats["python_fallback_seconds_by_net"]["SIG"] > 0.0


def test_other_nets_still_fall_back() -> None:
    code = int(cpp_backend.router_cpp.FAILURE_NO_PATH)
    pf = _pathfinder()
    with mock.patch.object(Router, "route", autospec=True, side_effect=Router.route) as spy:
        _fallback(pf, code, net="A")
        _bump_grid(pf)
        _fallback(pf, code, net="B")
    assert spy.call_count == 2
    assert set(pf.fallback_stats["python_fallback_seconds_by_net"]) == {"A", "B"}


def test_env_opt_out_restores_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KICAD_ROUTER_SKIP_REPEAT_FALLBACK", "0")
    monkeypatch.setenv("KICAD_ROUTER_SKIP_PROVEN_FALLBACK", "0")
    code = int(cpp_backend.router_cpp.FAILURE_NO_PATH)
    pf = _pathfinder()
    with mock.patch.object(Router, "route", autospec=True, side_effect=Router.route) as spy:
        _fallback(pf, code)
        _bump_grid(pf)
        _fallback(pf, code)
    assert spy.call_count == 2
    assert pf.fallback_stats["python_fallback_repeat_skips"] == 0


def test_relief_mode_records_no_drain() -> None:
    code = int(cpp_backend.router_cpp.FAILURE_NO_PATH)
    pf = _pathfinder()
    pf.set_relief_mode(True)
    with mock.patch.object(Router, "route", autospec=True, side_effect=Router.route) as spy:
        _fallback(pf, code)
        _bump_grid(pf)
        _fallback(pf, code)
    assert spy.call_count == 2
    assert pf.fallback_stats["python_fallback_seconds_by_net"] == {}
    assert pf.fallback_stats["python_fallback_repeat_skips"] == 0


def test_non_drained_failure_does_not_record() -> None:
    """A Python search that did not drain (e.g. iteration cap) is no proof."""
    code = int(cpp_backend.router_cpp.FAILURE_NO_PATH)
    pf = _pathfinder()
    with mock.patch.object(Router, "route", autospec=True, return_value=None) as spy:
        # Router.last_route_exhausted defaults False when route is stubbed.
        _fallback(pf, code)
        _bump_grid(pf)
        _fallback(pf, code)
    assert spy.call_count == 2


def test_untried_pad_pair_on_drained_net_still_falls_back() -> None:
    """Issue #8377: the drained record is per pad-pair request, not per net."""
    code = int(cpp_backend.router_cpp.FAILURE_NO_PATH)
    pf = _pathfinder()
    a, b = _pads("SIG")
    c = Pad(x=2.0, y=5.0, width=0.6, height=0.6, net=1, net_name="SIG", layer=Layer.F_CU)

    def call(start: Pad, end: Pad):
        return pf._try_python_fallback(
            start, end, reason="no path", cpp_failure_reason=code, negotiated_mode=True
        )

    with mock.patch.object(Router, "route", autospec=True, side_effect=Router.route) as spy:
        assert call(a, b) is None  # drains pair (a, b)
        _bump_grid(pf)
        assert call(a, c) is not None  # never-tried pair: fallback runs and rescues it
        assert spy.call_count == 2
        _bump_grid(pf)
        assert call(a, b) is None  # identical repeat: skipped
        assert spy.call_count == 2
    assert pf.fallback_stats["python_fallback_repeat_skips"] == 1

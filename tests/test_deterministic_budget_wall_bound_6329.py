"""Issue #6329: name the wall clocks that still bind under --deterministic-budget.

Two same-seed ``kct route --deterministic-budget`` runs of board 05 can land
different copper.  A probed run found the cause inside the negotiated loop:
each rip-up iteration hands every per-net search a WALL-CLOCK budget (10% of
the remaining stage time, #3989), and the pure-Python A* fallback hits it long
before the 1,000,000-expansion cap (106,496 expansions in 57 s).  How far that
search gets depends on machine load.

These tests pin the attribution: the Python router records a wall-clock stop,
``CppPathfinder`` reports it (only when the tuned cap is active), the negotiated
loop's stage-deadline line matches the two-phase router's, and the help text
names the remaining wall-clock stages.
"""

from __future__ import annotations

import io
import logging
import subprocess
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import pytest

from kicad_tools.cli.parser import create_parser
from kicad_tools.cli.route_cmd import _normalize_deterministic_budget, _route_parser
from kicad_tools.placement.route_check import deadline_fired
from kicad_tools.router.algorithms.two_phase import TwoPhaseRouter
from kicad_tools.router.core import DETERMINISTIC_BUDGET_STAGE_DEADLINE_LINE
from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules
from tests.test_route_auto_mfr_tier_integration import _assert_deterministic

requires_cpp = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend unavailable")


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.35,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )


def _grid(wall: bool = True) -> RoutingGrid:
    """A 10x6 mm two-layer grid with a full-height wall at x = 5 mm."""
    grid = RoutingGrid(width=10.0, height=6.0, rules=_rules(), layer_stack=LayerStack.two_layer())
    if wall:
        col = grid.world_to_grid(5.0, 3.0)[0]
        grid._blocked[:, :, col - 2 : col + 3] = True
        grid._is_obstacle[:, :, col - 2 : col + 3] = True
        grid.bump_occupancy_generation()
    return grid


def _pads() -> tuple[Pad, Pad]:
    common = {"width": 0.6, "height": 0.6, "net": 1, "net_name": "SIG", "layer": Layer.F_CU}
    return Pad(x=2.0, y=3.0, **common), Pad(x=8.0, y=3.0, **common)


# ---------------------------------------------------------------------------
# Router.last_route_deadline_hit
# ---------------------------------------------------------------------------


def test_wall_clock_stop_is_recorded() -> None:
    router = Router(_grid(), _rules())
    assert router.route(*_pads(), per_net_timeout=1e-9) is None
    assert router.last_route_deadline_hit is True
    assert router.last_route_deadline_iterations > 0
    # A wall-clock stop is not a drain, so not an unreachability proof.
    assert router.last_route_exhausted is False


def test_drain_and_iteration_cap_are_not_wall_clock_stops() -> None:
    router = Router(_grid(), _rules())
    assert router.route(*_pads()) is None
    assert router.last_route_deadline_hit is False
    router._max_iterations_override = 5
    assert router.route(*_pads()) is None
    assert router.last_route_deadline_hit is False


def test_flag_is_reset_by_the_next_route() -> None:
    router = Router(_grid(), _rules())
    router.route(*_pads(), per_net_timeout=1e-9)
    assert router.last_route_deadline_hit is True
    router.route(*_pads())
    assert router.last_route_deadline_hit is False
    assert router.last_route_deadline_iterations == 0


# ---------------------------------------------------------------------------
# CppPathfinder reports a wall-clock bind only under the tuned cap
# ---------------------------------------------------------------------------


def _pathfinder(per_net_iterations: int) -> CppPathfinder:
    cpp_grid = CppGrid.from_routing_grid(_grid())
    pathfinder = CppPathfinder(
        cpp_grid, _rules(), diagonal_routing=True, per_net_iterations=per_net_iterations
    )
    pathfinder.set_routable_layers(cpp_grid.get_routable_indices())
    return pathfinder


def _wall_bound_python_route(self, *args, **kwargs):
    self.last_route_exhausted = False
    self.last_route_deadline_hit = True
    self.last_route_deadline_iterations = 1234
    return None


@requires_cpp
def test_wall_bound_fallback_is_reported_under_the_cap(caplog) -> None:
    pathfinder = _pathfinder(per_net_iterations=1_000_000)
    with (
        mock.patch.object(Router, "route", autospec=True, side_effect=_wall_bound_python_route),
        caplog.at_level(logging.WARNING, logger="kicad_tools.router.cpp_backend"),
    ):
        assert pathfinder.route(*_pads()) is None
        assert pathfinder.route(*_pads()) is None
    bound = pathfinder.fallback_stats["wall_bound_under_cap"]
    assert set(bound) == {"SIG"}
    assert "1,234 of 1,000,000 expansions" in bound["SIG"]
    lines = [r.getMessage() for r in caplog.records if "per-net wall clock bound" in r.getMessage()]
    assert len(lines) == 1  # once per net
    assert lines[0].startswith("[deterministic-budget] per-net wall clock bound net SIG")


@requires_cpp
def test_wall_clock_is_not_a_determinism_loss_without_the_cap() -> None:
    pathfinder = _pathfinder(per_net_iterations=0)
    with mock.patch.object(Router, "route", autospec=True, side_effect=_wall_bound_python_route):
        assert pathfinder.route(*_pads()) is None
    assert pathfinder.fallback_stats["wall_bound_under_cap"] == {}


# ---------------------------------------------------------------------------
# Stage-deadline line on the negotiated (Autorouter) path
# ---------------------------------------------------------------------------


def test_negotiated_stage_deadline_line_matches_two_phase() -> None:
    fake = SimpleNamespace(
        _determinism_loss_logged=False, router=SimpleNamespace(_max_search_iterations=12_000_000)
    )
    out = io.StringIO()
    with redirect_stdout(out):
        TwoPhaseRouter._note_stage_deadline_determinism_loss(fake)
    assert out.getvalue().strip() == DETERMINISTIC_BUDGET_STAGE_DEADLINE_LINE
    assert deadline_fired(DETERMINISTIC_BUDGET_STAGE_DEADLINE_LINE)


# ---------------------------------------------------------------------------
# Help text names the stages (both parser sites)
# ---------------------------------------------------------------------------


def _help(parser, flag: str) -> str:
    for action in parser._actions:
        if flag in action.option_strings:
            return action.help % {}
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            sub = action.choices.get("route")
            if sub is not None:
                return _help(sub, flag)
    raise AssertionError(f"{flag} not found")


@pytest.mark.parametrize("parser_factory", [create_parser, _route_parser])
def test_help_names_the_remaining_wall_clock_stages(parser_factory) -> None:
    text = _help(parser_factory(), "--deterministic-budget")
    assert "Issue #6329" in text
    assert "10% of the" in text
    assert "per-net wall clock bound" in text
    assert "post-negotiation sweep" in text
    assert "--deterministic-rescue" in text
    # Issue #6475: --deterministic-rescue removes the 10%-of-stage per-net
    # budget, so the help must not say it binds "even with both flags".
    assert "Issue #6475" in text
    assert "removes that budget" in text
    assert "stage deadline fired" in text


# ---------------------------------------------------------------------------
# Issue #6533: the slow mfr-tier fixture must not trip on this banner
# ---------------------------------------------------------------------------


def _banner(timeout: float | None) -> str:
    args = SimpleNamespace(
        deterministic_budget=True,
        deterministic_rescue=False,
        per_net_timeout=30.0,
        max_search_iterations=0,
        per_net_iterations=0,
        timeout=timeout,
    )
    out = io.StringIO()
    with redirect_stdout(out):
        _normalize_deterministic_budget(args, quiet=False)
    return out.getvalue()


def _proc(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["kct"], returncode=0, stdout=stdout, stderr="")


@pytest.mark.parametrize("timeout", [None, 600.0])
def test_mfr_tier_determinism_check_ignores_the_banner(timeout: float | None) -> None:
    """The banner quotes 'stage deadline fired' mid-line on every run (#6329).

    With ``--timeout`` set, the advisory even quotes the full
    ``[deterministic-budget] stage deadline fired`` marker -- still mid-line.
    """
    banner = _banner(timeout)
    assert "stage deadline fired" in banner  # the trap the bare substring fell into
    _assert_deterministic(_proc(banner + "Routed 7/7 nets\n"))


@pytest.mark.parametrize("timeout", [None, 600.0])
def test_mfr_tier_determinism_check_catches_a_fired_deadline(timeout: float | None) -> None:
    log = _banner(timeout) + DETERMINISTIC_BUDGET_STAGE_DEADLINE_LINE + "\nRouted 7/7 nets\n"
    with pytest.raises(AssertionError, match="stage deadline fired"):
        _assert_deterministic(_proc(log))

"""Regression tests for Issue #3456: silent C++ -> Python fallback.

Two defects, two test groups:

1. **Silent downgrade** -- when the C++ A* fails a net, the Python
   fallback grinds 3-7 MINUTES per net at default verbosity with no
   indication that anything is wrong ("router is slow").  The fix is a
   once-per-net-per-run ``logging.WARNING`` naming the net and the
   C++ failure reason, plus a ``fallback_reasons`` mapping in
   ``CppPathfinder.fallback_stats``.

2. **Standard-mode own-net-obstacle parity** -- the standard-mode
   (``allow_sharing=False``) branches of ``Pathfinder::is_trace_blocked``
   and ``Pathfinder::is_diagonal_blocked`` rejected cells holding the
   net's OWN ``is_obstacle`` pad copper
   (``cell.is_obstacle || cell.net != net``), while the Python siblings
   admit same-net cells regardless of the obstacle flag (Issue #864
   semantics).  The ``is_trace_blocked`` divergence was the operative
   bug behind the board-03 fallbacks: trace centerlines within
   trace-half-width of the net's own pad copper were rejected, sealing
   J1's USB-C pad pockets at the 0.05mm canonical grid (standard-mode
   C++ open set exhausted in ~800 iterations on the J1->U1 USB edges;
   JOY/BTN nets burned up to 6M iterations before FAILURE_NO_PATH).
   Build version 11 -> 12 aligns both predicates with Python; both are
   exposed on the binding surface so parity is pinned without a full
   board route.  Post-fix, board 03's canonical recipe routes with
   ZERO Python fallbacks
   (tests/test_board_03_regression.py::test_no_python_fallbacks).
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from kicad_tools.router.cpp_backend import (
    CppGrid,
    CppPathfinder,
    is_cpp_available,
    router_cpp,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)

CPP_BACKEND_LOGGER = "kicad_tools.router.cpp_backend"


def _make_pathfinder() -> tuple[CppPathfinder, RoutingGrid]:
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.35,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(
        width=10.0,
        height=10.0,
        rules=rules,
        layer_stack=LayerStack.two_layer(),
    )
    cpp_grid = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    pathfinder.set_routable_layers(cpp_grid.get_routable_indices())
    return pathfinder, grid


def _make_pads(net: int = 1, net_name: str = "NET1") -> tuple[Pad, Pad]:
    start = Pad(
        x=2.0,
        y=5.0,
        width=0.6,
        height=0.6,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
    )
    end = Pad(
        x=8.0,
        y=5.0,
        width=0.6,
        height=0.6,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
    )
    return start, end


def _fallback_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING
        and rec.name == CPP_BACKEND_LOGGER
        and "falling back" in rec.getMessage()
    ]


@requires_cpp
class TestFallbackWarning:
    """Issue #3456 requirement (a): the fallback must be LOUD."""

    def test_warning_fires_with_net_name_and_reason(self, caplog) -> None:
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="USB_D+")

        with caplog.at_level(logging.WARNING, logger=CPP_BACKEND_LOGGER):
            pathfinder._try_python_fallback(
                start, end, reason="no path (C++ A* open set exhausted)"
            )

        warnings = _fallback_warnings(caplog)
        assert len(warnings) == 1, (
            "Issue #3456: handing a net to the 10-100x-slower Python "
            "fallback must emit exactly one WARNING."
        )
        msg = warnings[0].getMessage()
        assert "USB_D+" in msg, "warning must name the falling-back net"
        assert "no path (C++ A* open set exhausted)" in msg, (
            "warning must include the C++ failure reason"
        )
        assert "fallback_stats" in msg, (
            "warning should point users at backend_info['fallback_stats']"
        )

    def test_warning_dedupes_per_net_per_run(self, caplog) -> None:
        """Negotiated-mode rip-up retries the same net many times --
        the warning must fire at most once per net per run."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="USB_D-")

        with caplog.at_level(logging.WARNING, logger=CPP_BACKEND_LOGGER):
            for _ in range(3):
                pathfinder._try_python_fallback(
                    start, end, reason="no path (C++ A* open set exhausted)"
                )

        assert len(_fallback_warnings(caplog)) == 1, (
            "Issue #3456: repeated fallbacks for the SAME net must not "
            "spam the log -- once per net per run."
        )

    def test_distinct_nets_each_warn(self, caplog) -> None:
        pathfinder, _ = _make_pathfinder()
        a_start, a_end = _make_pads(net=1, net_name="USB_D+")
        b_start, b_end = _make_pads(net=2, net_name="USB_CC1")

        with caplog.at_level(logging.WARNING, logger=CPP_BACKEND_LOGGER):
            pathfinder._try_python_fallback(a_start, a_end, reason="r1")
            pathfinder._try_python_fallback(b_start, b_end, reason="r2")

        messages = [rec.getMessage() for rec in _fallback_warnings(caplog)]
        assert len(messages) == 2
        assert any("USB_D+" in m for m in messages)
        assert any("USB_CC1" in m for m in messages)

    def test_fallback_reasons_recorded_in_stats(self) -> None:
        """``fallback_stats['fallback_reasons']`` must record the reason
        even when the Python fallback itself fails (attributability of
        slow FAILED grinds, not just slow successes)."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="USB_CC2")

        pathfinder._try_python_fallback(start, end, reason="per-net wall-clock deadline exceeded")

        stats = pathfinder.fallback_stats
        assert "fallback_reasons" in stats
        assert stats["fallback_reasons"].get("USB_CC2") == "per-net wall-clock deadline exceeded"

    def test_no_warning_in_relief_probe_mode(self, caplog) -> None:
        """Relief probes deliberately stress the search and are never
        committed -- a probe-time fallback is not a user-facing
        performance event."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="PROBE_NET")
        pathfinder.set_relief_mode(True)

        with caplog.at_level(logging.WARNING, logger=CPP_BACKEND_LOGGER):
            pathfinder._try_python_fallback(start, end, reason="probe")

        assert _fallback_warnings(caplog) == []

    def test_no_warning_when_deadline_already_exhausted(self, caplog) -> None:
        """When the shared per-net budget is spent the fallback is
        SKIPPED (issue #3474) -- no grind happens, so no warning."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="SPENT_NET")

        with caplog.at_level(logging.WARNING, logger=CPP_BACKEND_LOGGER):
            result = pathfinder._try_python_fallback(
                start, end, reason="r", deadline=time.monotonic() - 1.0
            )

        assert result is None
        assert _fallback_warnings(caplog) == []
        assert "SPENT_NET" not in pathfinder.fallback_stats["fallback_reasons"]

    def test_no_warning_when_cpp_handles_net(self, caplog) -> None:
        """A net the C++ A* routes directly must produce NO fallback
        warning and NO fallback_stats entries."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="CLEAN_NET")

        with caplog.at_level(logging.WARNING, logger=CPP_BACKEND_LOGGER):
            route = pathfinder.route(start, end)

        assert route is not None, "open 10x10mm grid: C++ A* must route this"
        assert _fallback_warnings(caplog) == []
        stats = pathfinder.fallback_stats
        assert stats["fallback_count"] == 0
        assert stats["fallback_nets"] == []
        assert stats["fallback_reasons"] == {}


@requires_cpp
class TestDescribeCppFailure:
    """Mapping of C++ FAILURE_* codes to human-readable reasons."""

    def test_known_codes_map_to_descriptions(self) -> None:
        pathfinder, _ = _make_pathfinder()
        cases = {
            router_cpp.FAILURE_NO_PATH: "no path",
            router_cpp.FAILURE_ITERATION_LIMIT: "iteration limit",
            router_cpp.FAILURE_TIMEOUT: "deadline",
            router_cpp.FAILURE_VIA_VIA_BLOCKED: "via",
        }
        for code, fragment in cases.items():
            desc = pathfinder._describe_cpp_failure(SimpleNamespace(failure_reason=int(code)))
            assert fragment in desc, f"code {code} -> {desc!r}"

    def test_unknown_code_degrades_gracefully(self) -> None:
        pathfinder, _ = _make_pathfinder()
        desc = pathfinder._describe_cpp_failure(SimpleNamespace(failure_reason=9999))
        assert "9999" in desc

    def test_via_blocked_includes_blocking_net(self) -> None:
        pathfinder, _ = _make_pathfinder()
        desc = pathfinder._describe_cpp_failure(
            SimpleNamespace(
                failure_reason=int(router_cpp.FAILURE_VIA_VIA_BLOCKED),
                blocking_via_net=42,
            )
        )
        assert "42" in desc


@requires_cpp
class TestDiagonalOwnNetObstacleStandardMode:
    """Issue #3456: standard-mode own-net-obstacle parity (diagonal).

    Standard-mode ``Pathfinder::is_diagonal_blocked`` must admit
    same-net cells regardless of ``is_obstacle`` -- parity with the
    Python ``Router._is_diagonal_corner_blocked`` standard-mode branch
    (Issue #864) and the negotiated-mode sibling fix from Issue #2989.
    (The operative board-03 seal was the matching ``is_trace_blocked``
    divergence -- see TestTraceBlockedOwnNetObstacleStandardMode -- but
    this predicate shares the same parity contract.)
    """

    @staticmethod
    def _paint_obstacle(cpp_grid: CppGrid, gx: int, gy: int, net: int) -> None:
        for layer_idx in range(cpp_grid._impl.layers):
            cpp_grid._impl.mark_blocked(gx, gy, layer_idx, net, True)

    def test_own_net_obstacle_corners_passable_standard_mode(self) -> None:
        """Both corner-adjacent cells are the net's OWN pad copper
        (blocked + is_obstacle).  The diagonal move must be admitted in
        standard mode, matching the Python sibling."""
        pathfinder, _ = _make_pathfinder()
        cpp_grid = pathfinder._grid

        net = 7
        gx, gy = cpp_grid._impl.world_to_grid(5.0, 5.0)
        # Corner-adjacent cells for a (+1, +1) diagonal from (gx, gy):
        # B = (gx, gy+1), C = (gx+1, gy).
        self._paint_obstacle(cpp_grid, gx, gy + 1, net)
        self._paint_obstacle(cpp_grid, gx + 1, gy, net)

        assert not pathfinder._impl.is_diagonal_blocked(gx, gy, 1, 1, 0, net, False), (
            "Issue #3456: same-net is_obstacle cells must be passable in "
            "standard mode (Python Issue #864 parity)."
        )

    def test_foreign_net_obstacle_corners_still_block(self) -> None:
        """The fix is a refinement, not a relaxation: FOREIGN-net
        obstacle cells must still block the diagonal."""
        pathfinder, _ = _make_pathfinder()
        cpp_grid = pathfinder._grid

        obstacle_net = 7
        probe_net = 99
        gx, gy = cpp_grid._impl.world_to_grid(5.0, 5.0)
        self._paint_obstacle(cpp_grid, gx, gy + 1, obstacle_net)
        self._paint_obstacle(cpp_grid, gx + 1, gy, obstacle_net)

        assert pathfinder._impl.is_diagonal_blocked(gx, gy, 1, 1, 0, probe_net, False), (
            "foreign-net obstacle cells must still block corner-cutting"
        )

    def test_python_parity_standard_mode(self) -> None:
        """Pin C++ <-> Python predicate parity on the exact cell state
        that regressed: blocked + is_obstacle + own net."""
        from kicad_tools.router.pathfinder import Router

        pathfinder, py_grid = _make_pathfinder()
        cpp_grid = pathfinder._grid
        rules = pathfinder._rules

        net = 7
        gx, gy = cpp_grid._impl.world_to_grid(5.0, 5.0)

        # Paint the same state on BOTH grids.
        for cx, cy in ((gx, gy + 1), (gx + 1, gy)):
            self._paint_obstacle(cpp_grid, cx, cy, net)
            for layer_idx in range(py_grid.layers):
                cell = py_grid.grid[layer_idx][cy][cx]
                cell.blocked = True
                cell.net = net
                cell.is_obstacle = True

        py_router = Router(py_grid, rules, diagonal_routing=True)

        for probe_net, label in ((net, "own-net"), (99, "foreign-net")):
            cpp_blocked = pathfinder._impl.is_diagonal_blocked(gx, gy, 1, 1, 0, probe_net, False)
            py_blocked = py_router._is_diagonal_corner_blocked(gx, gy, 1, 1, 0, probe_net, False)
            assert cpp_blocked == py_blocked, (
                f"Issue #3456 parity: {label} standard-mode diagonal "
                f"predicate diverges (cpp={cpp_blocked}, py={py_blocked})"
            )


@requires_cpp
class TestTraceBlockedOwnNetObstacleStandardMode:
    """Issue #3456 sibling: ``Pathfinder::is_trace_blocked`` standard
    mode must admit the net's OWN ``is_obstacle`` cells -- parity with
    the Python ``_is_trace_blocked`` standard branch (Issue #864:
    ``blocked & (net != net)``)."""

    @staticmethod
    def _paint_obstacle(cpp_grid: CppGrid, gx: int, gy: int, net: int) -> None:
        for layer_idx in range(cpp_grid._impl.layers):
            cpp_grid._impl.mark_blocked(gx, gy, layer_idx, net, True)

    def test_own_net_obstacle_passable_standard_mode(self) -> None:
        pathfinder, _ = _make_pathfinder()
        cpp_grid = pathfinder._grid

        net = 7
        gx, gy = cpp_grid._impl.world_to_grid(5.0, 5.0)
        self._paint_obstacle(cpp_grid, gx, gy, net)

        assert not pathfinder._impl.is_trace_blocked(gx, gy, 0, net, False), (
            "Issue #3456: a trace centerline over the net's OWN "
            "is_obstacle pad copper must be admitted in standard mode "
            "(Python Issue #864 parity)."
        )

    def test_foreign_net_obstacle_still_blocks(self) -> None:
        pathfinder, _ = _make_pathfinder()
        cpp_grid = pathfinder._grid

        gx, gy = cpp_grid._impl.world_to_grid(5.0, 5.0)
        self._paint_obstacle(cpp_grid, gx, gy, 7)

        assert pathfinder._impl.is_trace_blocked(gx, gy, 0, 99, False), (
            "foreign-net obstacle cells must still block trace placement"
        )

    def test_python_parity_standard_mode(self) -> None:
        from kicad_tools.router.pathfinder import Router

        pathfinder, py_grid = _make_pathfinder()
        cpp_grid = pathfinder._grid
        rules = pathfinder._rules

        net = 7
        gx, gy = cpp_grid._impl.world_to_grid(5.0, 5.0)
        self._paint_obstacle(cpp_grid, gx, gy, net)
        for layer_idx in range(py_grid.layers):
            cell = py_grid.grid[layer_idx][gy][gx]
            cell.blocked = True
            cell.net = net
            cell.is_obstacle = True

        py_router = Router(py_grid, rules, diagonal_routing=True)

        for probe_net, label in ((net, "own-net"), (99, "foreign-net")):
            cpp_blocked = pathfinder._impl.is_trace_blocked(gx, gy, 0, probe_net, False)
            py_blocked = py_router._is_trace_blocked(gx, gy, 0, probe_net, False)
            assert cpp_blocked == py_blocked, (
                f"Issue #3456 parity: {label} standard-mode trace "
                f"predicate diverges (cpp={cpp_blocked}, py={py_blocked})"
            )

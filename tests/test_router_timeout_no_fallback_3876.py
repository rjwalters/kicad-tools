"""Regression tests for Issue #3876: wall-clock FAILURE_TIMEOUT must NOT
trigger the slow Python fallback.

Mechanism (see ``CppPathfinder._try_python_fallback`` in
``src/kicad_tools/router/cpp_backend.py``):

The per-net budget is a WALL-CLOCK deadline checked inside the C++ A*
loop every ~1024 iterations.  Under machine load wall-clock advances
faster relative to node-expansion rate, so the deadline fires at a
*smaller, non-deterministic* iteration count and the C++ pathfinder
returns ``FAILURE_TIMEOUT`` where on an idle machine it would have found
the path.  ``FAILURE_TIMEOUT`` is therefore a LOAD ARTIFACT, not a
geometric dead-end.

The resume loop shares the SAME (already-expiring) deadline.  Before
#3876 a ``FAILURE_TIMEOUT`` was treated like a geometric failure and
handed to the pure-Python A* (10-100x slower), which then ALSO timed out
on the ~0 remaining budget -- wasting deadline that subsequent nets need.

#3876 short-circuits the fallback on ``FAILURE_TIMEOUT``: it returns
``None`` BEFORE constructing the Python ``Router`` or running the A*.
Genuine geometric failures (``FAILURE_NO_PATH``,
``FAILURE_VIA_VIA_BLOCKED``, ``FAILURE_ITERATION_LIMIT``) STILL fall back
-- the Python A*'s different neighbor expansion is the value-add there
(issue #3456).

These tests patch the lazily-imported ``pathfinder.Router`` and assert it
is (not) constructed depending on the C++ failure reason.  This is purely
Python-side wiring, so it does not require a routed board.
"""

from __future__ import annotations

import logging
from unittest import mock

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


@requires_cpp
class TestTimeoutShortCircuitsFallback:
    """A wall-clock ``FAILURE_TIMEOUT`` must NOT enter the Python fallback."""

    def test_timeout_does_not_construct_python_router(self, caplog) -> None:
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="TIMEOUT_NET")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            with caplog.at_level(logging.DEBUG, logger=CPP_BACKEND_LOGGER):
                result = pathfinder._try_python_fallback(
                    start,
                    end,
                    reason="per-net wall-clock deadline exceeded",
                    cpp_failure_reason=int(router_cpp.FAILURE_TIMEOUT),
                )

        assert result is None, (
            "issue #3876: a wall-clock FAILURE_TIMEOUT must fail the net "
            "fast (return None), not grind in the Python A*."
        )
        router_cls.assert_not_called()

    def test_timeout_short_circuit_emits_debug_not_warning(self, caplog) -> None:
        """The short-circuit happens BEFORE the loud #3456 fallback
        WARNING, so a load-induced timeout no longer logs the misleading
        'C++ gave up; falling back' line."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="TIMEOUT_NET2")

        with mock.patch("kicad_tools.router.pathfinder.Router"):
            with caplog.at_level(logging.DEBUG, logger=CPP_BACKEND_LOGGER):
                pathfinder._try_python_fallback(
                    start,
                    end,
                    reason="per-net wall-clock deadline exceeded",
                    cpp_failure_reason=int(router_cpp.FAILURE_TIMEOUT),
                )

        fallback_warnings = [
            rec
            for rec in caplog.records
            if rec.levelno == logging.WARNING and "falling back" in rec.getMessage()
        ]
        assert fallback_warnings == [], (
            "issue #3876: a FAILURE_TIMEOUT short-circuit must not emit "
            "the misleading 'falling back' WARNING."
        )
        debug_msgs = [
            rec.getMessage()
            for rec in caplog.records
            if rec.levelno == logging.DEBUG and "#3876" in rec.getMessage()
        ]
        assert any("TIMEOUT_NET2" in m for m in debug_msgs), (
            "the short-circuit should log a debug line naming the net"
        )

    def test_timeout_not_recorded_in_fallback_stats(self) -> None:
        """A short-circuited timeout never ran a fallback, so it must not
        pollute ``fallback_stats`` (no slow grind to attribute)."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="TIMEOUT_NET3")

        with mock.patch("kicad_tools.router.pathfinder.Router"):
            pathfinder._try_python_fallback(
                start,
                end,
                reason="per-net wall-clock deadline exceeded",
                cpp_failure_reason=int(router_cpp.FAILURE_TIMEOUT),
            )

        stats = pathfinder.fallback_stats
        assert "TIMEOUT_NET3" not in stats["fallback_reasons"]
        assert "TIMEOUT_NET3" not in stats["fallback_nets"]
        assert stats["fallback_count"] == 0


@requires_cpp
class TestGeometricFailuresStillFallBack:
    """Geometric failures must PRESERVE the existing fallback behavior."""

    @pytest.mark.parametrize(
        "failure_reason",
        [
            "FAILURE_NO_PATH",
            "FAILURE_VIA_VIA_BLOCKED",
            "FAILURE_ITERATION_LIMIT",
        ],
    )
    def test_geometric_failure_constructs_python_router(self, failure_reason) -> None:
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name=f"GEOM_{failure_reason}")
        code = int(getattr(router_cpp, failure_reason))

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            # Make the constructed router's route() return None so we only
            # assert on construction, not on a real Python A* run.
            router_cls.return_value.route.return_value = None
            pathfinder._try_python_fallback(
                start,
                end,
                reason="geometric failure",
                cpp_failure_reason=code,
            )

        router_cls.assert_called_once()

    def test_none_failure_reason_preserves_legacy_fallback(self) -> None:
        """``cpp_failure_reason=None`` (the default / pre-#3876 callers)
        must still fall back -- the guard is opt-in."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="LEGACY_NET")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            router_cls.return_value.route.return_value = None
            pathfinder._try_python_fallback(
                start,
                end,
                reason="legacy caller",
                cpp_failure_reason=None,
            )

        router_cls.assert_called_once()


@requires_cpp
class TestTimeoutGuardEdgeCases:
    """Edge cases: the guard must not crash and must not double-count."""

    def test_relief_mode_timeout_still_short_circuits(self) -> None:
        """Relief-probe mode is unaffected by the #3876 guard at the
        fallback layer: a relief-mode route returns before validation, but
        if the fallback IS reached with a timeout it must still skip."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="RELIEF_TIMEOUT")
        pathfinder.set_relief_mode(True)

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            result = pathfinder._try_python_fallback(
                start,
                end,
                reason="per-net wall-clock deadline exceeded",
                cpp_failure_reason=int(router_cpp.FAILURE_TIMEOUT),
            )

        assert result is None
        router_cls.assert_not_called()

    def test_timeout_guard_layers_on_3474_skip(self, caplog) -> None:
        """The #3474 ``remaining <= 0.05`` skip still fires independently.
        A FAILURE_NO_PATH with an exhausted deadline must STILL be skipped
        by #3474 (so #3876 does not change #3474's behavior)."""
        import time

        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="EXHAUSTED_GEOM")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            result = pathfinder._try_python_fallback(
                start,
                end,
                reason="no path",
                cpp_failure_reason=int(router_cpp.FAILURE_NO_PATH),
                deadline=time.monotonic() - 1.0,
            )

        assert result is None
        router_cls.assert_not_called()

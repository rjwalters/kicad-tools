"""Regression tests for Issue #3923: resume-exhaustion must NOT cascade
into the slow Python fallback.

Two independent but compounding defects were reported:

1. ``CppPathfinder._try_python_fallback`` had no fast-path for the
   resume-exhaustion failure class.  The C++ resumable pathfinder runs a
   post-route clearance-validation loop; two dead-end outcomes hand the net to
   this fallback: (a) "post-route clearance validation failed; exhausted 5
   resume attempts" -- 5 avoidance-boosted resumes each produced a
   geometrically valid path that still violated clearance (carries
   FAILURE_NONE; DOMINANT -- 22 of 30 board-07 fallbacks in the sweep); and
   (b) "resume after rejected goal cell failed: ..." -- a resumed search
   exhausted its open set (FAILURE_NO_PATH).  In both the pure-Python A* shares
   the SAME ``_py_grid`` and clearance model, so it reproduces the failure
   10-100x more slowly.  #3923 returns ``None`` BEFORE constructing the Python
   ``Router``.

2. ``route_with_layer_escalation`` had no pre-rung deduplication, so an
   identical ``(layer_count, layer_stack, via_in_pad_fallback, skip_nets)``
   config could run a full routing budget twice for +0 routed nets.  #3923
   fingerprints each attempted rung and skips a duplicate before any wall time
   is spent.  The fingerprint helper (``_rung_dedup_fingerprint``) is
   unit-tested directly.

The guard keys on the resume-loop reason markers ("resume attempts" /
"resume after rejected goal cell failed"), NOT on FAILURE_NO_PATH alone, so it
never fires for the initial-search failure -- "no path (C++ A* open set
exhausted)" contains "exhausted" but no resume marker, so single-corridor
geometries the Python 45-degree/waypoint expansion legitimately rescues still
fall back.  ``FAILURE_TIMEOUT`` (wall-clock artifact, handled by #3876) and
``FAILURE_VIA_VIA_BLOCKED`` (distinct via obstruction) are excluded and still
fall back.  It can be disabled with ``KICAD_ROUTER_SKIP_RESUME_FALLBACK=0``.
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

# The two reason strings emitted at the resume-loop dead-end call sites in
# ``CppPathfinder.route()``.  BOTH are short-circuited by the #3923 guard:
#
#   - CLEARANCE_EXHAUSTED (line ~1668): 5 boosted resumes each produced a
#     geometrically valid path that still violated clearance.  Carries
#     FAILURE_NONE (the last route SUCCEEDED as a search; clearance is the
#     obstruction) -- so the guard keys on the reason marker, not the code.
#     This is the DOMINANT case: 22 of 30 board-07 fallbacks in the sweep.
#   - RESUME_FAILED (line ~1707): a resumed search exhausted its open set
#     with FAILURE_NO_PATH.
CLEARANCE_EXHAUSTED_REASON = "post-route clearance validation failed; exhausted 5 resume attempts"
RESUME_FAILED_REASON = "resume after rejected goal cell failed: no path (C++ A* open set exhausted)"


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


# ---------------------------------------------------------------------------
# Defect 1: resume-exhaustion fast-path in _try_python_fallback
# ---------------------------------------------------------------------------


@requires_cpp
class TestResumeExhaustionShortCircuitsFallback:
    """A resume-loop dead-end must NOT enter the pure-Python fallback."""

    @pytest.mark.parametrize(
        ("reason", "cpp_code"),
        [
            # Dominant case: clearance-validation exhaustion carries FAILURE_NONE.
            (CLEARANCE_EXHAUSTED_REASON, "FAILURE_NONE"),
            # Resume search exhausted: carries FAILURE_NO_PATH.
            (RESUME_FAILED_REASON, "FAILURE_NO_PATH"),
        ],
    )
    def test_resume_exhaustion_does_not_construct_python_router(self, reason, cpp_code) -> None:
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="RESUME_EXHAUSTED")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            result = pathfinder._try_python_fallback(
                start,
                end,
                reason=reason,
                cpp_failure_reason=int(getattr(router_cpp, cpp_code)),
            )

        assert result is None, (
            "issue #3923: a resume-loop dead-end must fail the net fast "
            "(return None), not grind in the Python A*."
        )
        router_cls.assert_not_called()

    def test_resume_exhaustion_emits_debug_not_warning(self, caplog) -> None:
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="RESUME_DBG")

        with mock.patch("kicad_tools.router.pathfinder.Router"):
            with caplog.at_level(logging.DEBUG, logger=CPP_BACKEND_LOGGER):
                pathfinder._try_python_fallback(
                    start,
                    end,
                    reason=RESUME_FAILED_REASON,
                    cpp_failure_reason=int(router_cpp.FAILURE_NO_PATH),
                )

        fallback_warnings = [
            rec
            for rec in caplog.records
            if rec.levelno == logging.WARNING and "falling back" in rec.getMessage()
        ]
        assert fallback_warnings == [], (
            "issue #3923: a resume-exhaustion short-circuit must not emit "
            "the misleading 'falling back' WARNING."
        )
        debug_msgs = [
            rec.getMessage()
            for rec in caplog.records
            if rec.levelno == logging.DEBUG and "#3923" in rec.getMessage()
        ]
        assert any("RESUME_DBG" in m for m in debug_msgs), (
            "the short-circuit should log a debug line naming the net"
        )

    def test_resume_exhaustion_not_recorded_in_fallback_stats(self) -> None:
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="RESUME_STATS")

        with mock.patch("kicad_tools.router.pathfinder.Router"):
            pathfinder._try_python_fallback(
                start,
                end,
                reason=RESUME_FAILED_REASON,
                cpp_failure_reason=int(router_cpp.FAILURE_NO_PATH),
            )

        stats = pathfinder.fallback_stats
        assert "RESUME_STATS" not in stats["fallback_reasons"]
        assert "RESUME_STATS" not in stats["fallback_nets"]
        assert stats["fallback_count"] == 0

    def test_env_opt_out_restores_grind(self, monkeypatch) -> None:
        """KICAD_ROUTER_SKIP_RESUME_FALLBACK=0 restores pre-#3923 fallback."""
        monkeypatch.setenv("KICAD_ROUTER_SKIP_RESUME_FALLBACK", "0")
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="RESUME_OPTOUT")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            router_cls.return_value.route.return_value = None
            pathfinder._try_python_fallback(
                start,
                end,
                reason=RESUME_FAILED_REASON,
                cpp_failure_reason=int(router_cpp.FAILURE_NO_PATH),
            )

        router_cls.assert_called_once()


@requires_cpp
class TestResumeExhaustionGuardIsNarrow:
    """The guard must NOT fire for cases the Python fallback legitimately rescues."""

    def test_initial_no_path_still_falls_back(self) -> None:
        """An INITIAL-search FAILURE_NO_PATH (no resume keyword) must still
        fall back -- the Python 45-degree/waypoint expansion is the value-add
        for single-corridor geometries."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="INITIAL_NO_PATH")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            router_cls.return_value.route.return_value = None
            pathfinder._try_python_fallback(
                start,
                end,
                reason="no path (C++ A* open set exhausted)",
                cpp_failure_reason=int(router_cpp.FAILURE_NO_PATH),
            )

        router_cls.assert_called_once()

    def test_via_via_blocked_with_resume_keyword_still_falls_back(self) -> None:
        """Even with a resume reason, a non-NO_PATH failure (e.g.
        VIA_VIA_BLOCKED) must still fall back -- the open set was not empty."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="RESUME_VIA_BLOCKED")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            router_cls.return_value.route.return_value = None
            pathfinder._try_python_fallback(
                start,
                end,
                reason="resume after rejected goal cell failed: all via candidates blocked",
                cpp_failure_reason=int(router_cpp.FAILURE_VIA_VIA_BLOCKED),
            )

        router_cls.assert_called_once()

    def test_timeout_with_resume_keyword_short_circuits_via_3876(self) -> None:
        """A FAILURE_TIMEOUT is short-circuited by the earlier #3876 guard
        regardless of the reason string -- the #3923 guard is not what fires,
        but the net still fails fast (no Python grind)."""
        pathfinder, _ = _make_pathfinder()
        start, end = _make_pads(net_name="RESUME_TIMEOUT")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            result = pathfinder._try_python_fallback(
                start,
                end,
                reason=RESUME_FAILED_REASON,
                cpp_failure_reason=int(router_cpp.FAILURE_TIMEOUT),
            )

        assert result is None
        router_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Defect 2: pre-rung deduplication fingerprint
# ---------------------------------------------------------------------------


class TestRungDedupFingerprint:
    """``_rung_dedup_fingerprint`` collapses structurally-identical rungs."""

    def test_identical_configs_share_fingerprint(self) -> None:
        from kicad_tools.cli.route_cmd import _rung_dedup_fingerprint

        a = _rung_dedup_fingerprint(4, "4-Layer ALL-SIG", False, ["GND", "VCC"])
        b = _rung_dedup_fingerprint(4, "4-Layer ALL-SIG", False, ["GND", "VCC"])
        assert a == b, "identical (count, stack, fallback, skip_nets) must dedup"

    def test_skip_net_order_does_not_matter(self) -> None:
        from kicad_tools.cli.route_cmd import _rung_dedup_fingerprint

        a = _rung_dedup_fingerprint(4, "S", False, ["GND", "VCC"])
        b = _rung_dedup_fingerprint(4, "S", False, ["VCC", "GND"])
        assert a == b, "skip_nets is sorted -- set-order jitter must not defeat dedup"

    def test_via_in_pad_fallback_distinguishes(self) -> None:
        from kicad_tools.cli.route_cmd import _rung_dedup_fingerprint

        base = _rung_dedup_fingerprint(4, "S", False, [])
        fallback = _rung_dedup_fingerprint(4, "S", True, [])
        assert base != fallback, "the via-in-pad fallback rung is a genuinely different config"

    def test_layer_count_distinguishes(self) -> None:
        from kicad_tools.cli.route_cmd import _rung_dedup_fingerprint

        two = _rung_dedup_fingerprint(2, "S", False, [])
        four = _rung_dedup_fingerprint(4, "S", False, [])
        assert two != four

    def test_distinct_stacks_at_same_layer_count_not_deduped(self) -> None:
        """The default ladder has TWO distinct 4-layer stacks
        (SIG-GND-PWR-SIG vs ALL-SIG); they are genuinely different routing
        attempts and must BOTH run -- the stack identity is part of the
        fingerprint so they are never collapsed."""
        from kicad_tools.cli.route_cmd import _rung_dedup_fingerprint
        from kicad_tools.router import LayerStack

        plane = LayerStack.four_layer_sig_gnd_pwr_sig()
        allsig = LayerStack.four_layer_all_signal()
        a = _rung_dedup_fingerprint(4, plane.name, False, [])
        b = _rung_dedup_fingerprint(4, allsig.name, False, [])
        assert a != b, "distinct 4-layer stacks must NOT share a fingerprint (both must run)"

    def test_skip_nets_distinguish(self) -> None:
        from kicad_tools.cli.route_cmd import _rung_dedup_fingerprint

        empty = _rung_dedup_fingerprint(4, "S", False, [])
        with_skip = _rung_dedup_fingerprint(4, "S", False, ["GND"])
        assert empty != with_skip, "an auto-plane skip changes the board state -- must NOT dedup"

    def test_truthy_non_bool_fallback_coerced(self) -> None:
        """A truthy non-bool via_in_pad_fallback must not slip a duplicate
        through by hashing differently from ``True``."""
        from kicad_tools.cli.route_cmd import _rung_dedup_fingerprint

        assert _rung_dedup_fingerprint(4, "S", 1, []) == _rung_dedup_fingerprint(4, "S", True, [])

    def test_board05_4l_4l_pattern_dedups(self) -> None:
        """The board-05 ``[4L, 4L]`` re-run: two rungs with the same layer
        count, SAME stack, same (no) via-in-pad fallback, same skip_nets
        collapse to one fingerprint -- so the second rung is skipped before
        spending budget."""
        from kicad_tools.cli.route_cmd import _rung_dedup_fingerprint

        attempted: set = set()
        ladder = [
            (4, "4-Layer ALL-SIG", False, ["GND"]),  # 4L rung A
            (4, "4-Layer ALL-SIG", False, ["GND"]),  # 4L rung B -- identical
        ]
        executed = 0
        for layer_count, stack_id, fallback, skip in ladder:
            fp = _rung_dedup_fingerprint(layer_count, stack_id, fallback, skip)
            if fp in attempted:
                continue
            attempted.add(fp)
            executed += 1

        assert executed == 1, "issue #3923: the board-05 [4L, 4L] pair must execute exactly once"

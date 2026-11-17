"""Tests for the ``--deterministic-budget`` stage-deadline determinism-loss log (Issue #5765).

``--deterministic-budget`` (Issue #3538) pins the C++ A* iteration backstop
(``self.router._max_search_iterations``) so each per-net search terminates on
a fixed, machine-independent node-expansion count -- that is what makes the
routed output reproducible across machines.  The outer ``--timeout`` (and,
under ``--auto-layers`` escalation, the per-attempt fair slice computed by
``route_cmd._per_attempt_budgeted_timeout``) is documented as a SAFETY
backstop that should not normally bind.

Issue #5765 found this claim was not quite true: under multi-attempt
escalation loops (layer escalation, rule-relaxation tiers), the per-attempt
fair slice can be much smaller than ``--timeout`` itself -- board 03 with
``--timeout 600`` and 8 escalation attempts hands the first attempt only 75s,
and that slice becomes ``TwoPhaseRouter``'s stage ``timeout``
(``check_timeout()``), which can cut the rip-up/reroute loop between nets on
a slow-enough machine even though the iteration backstop never bound.

Rather than remove the fair slice (Issue #3881 / #4770 measured that it is
what recovers throughput on hard fixtures, and removing it costs board-07
real nets), this issue makes the resulting determinism loss ATTRIBUTABLE:
``TwoPhaseRouter._note_stage_deadline_determinism_loss`` logs a
``"[deterministic-budget] stage deadline fired -- run is not reproducible"``
line the first time a stage deadline fires while the iteration backstop is
pinned, so a non-reproducible run is visible in its own log instead of
looking identical to an ordinary, harmless deadline trim.

These tests exercise that log path directly, without needing a full board
route (mirrors the existing minimal-``TwoPhaseRouter`` construction pattern
used in ``tests/test_negotiated_timeout_propagation.py``).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from kicad_tools.router.algorithms.two_phase import TwoPhaseRouter


def _make_two_phase_router(max_search_iterations: int) -> TwoPhaseRouter:
    """Build a minimal ``TwoPhaseRouter`` with a stub ``router``.

    Only ``self.router._max_search_iterations`` is read by
    ``_note_stage_deadline_determinism_loss`` / ``_detailed_standard``'s
    ``check_timeout`` closure, so every other collaborator can be a bare
    stub -- mirrors the minimal-construction pattern in
    ``tests/test_negotiated_timeout_propagation.py``.
    """
    router_stub = SimpleNamespace(_max_search_iterations=max_search_iterations)
    return TwoPhaseRouter(
        grid=SimpleNamespace(),
        router=router_stub,
        rules=SimpleNamespace(cost_corridor_deviation=5.0),
        net_class_map=None,
        nets={},
        net_names={1: "NetA"},
        pads={},
        routes=[],
        routing_failures=[],
        get_net_priority=lambda n: n,
        route_net=lambda n: [],
        route_net_with_corridor=lambda *a, **k: [],
        mark_route=lambda r: None,
    )


class TestNoteStageDeadlineDeterminismLoss:
    """Unit tests for ``TwoPhaseRouter._note_stage_deadline_determinism_loss``."""

    def test_logs_when_iteration_backstop_pinned(self, capsys):
        """Logs the determinism-loss line when the iteration backstop is set."""
        two_phase = _make_two_phase_router(max_search_iterations=1_000_000)
        two_phase._note_stage_deadline_determinism_loss()
        out = capsys.readouterr().out
        assert "[deterministic-budget]" in out
        assert "stage deadline fired" in out
        assert "not reproducible" in out

    def test_silent_when_iteration_backstop_not_pinned(self, capsys):
        """No log line for an ordinary (non-deterministic-budget) deadline.

        When ``--deterministic-budget`` was never requested, a firing stage
        deadline is the normal, harmless mechanism (``--timeout`` doing its
        job) -- not a determinism loss, so nothing should be logged.
        """
        two_phase = _make_two_phase_router(max_search_iterations=0)
        two_phase._note_stage_deadline_determinism_loss()
        out = capsys.readouterr().out
        assert out == ""

    def test_logs_only_once_per_router_instance(self, capsys):
        """Repeated firings only log the determinism loss once.

        Multiple nets can each hit the same firing stage deadline (e.g. once
        the deadline has passed, EVERY subsequent net-order check also trips
        it) -- the log should not repeat once per net.
        """
        two_phase = _make_two_phase_router(max_search_iterations=1_000_000)
        two_phase._note_stage_deadline_determinism_loss()
        two_phase._note_stage_deadline_determinism_loss()
        two_phase._note_stage_deadline_determinism_loss()
        out = capsys.readouterr().out
        assert out.count("stage deadline fired") == 1

    def test_flag_starts_false_on_construction(self):
        """The dedup flag defaults to False so a router that never times out
        never logs anything (checked indirectly via the private attribute so
        this test fails loudly if the attribute is renamed/removed)."""
        two_phase = _make_two_phase_router(max_search_iterations=1_000_000)
        assert two_phase._determinism_loss_logged is False


class TestCheckTimeoutIntegration:
    """``check_timeout()`` closures call the determinism-loss hook when they fire."""

    def test_detailed_standard_logs_on_immediate_timeout_with_backstop_pinned(self, capsys):
        """``_detailed_standard``'s ``check_timeout()`` fires immediately
        (``start_time`` already in the past, ``timeout=0.0``) and, because the
        iteration backstop is pinned, logs the determinism-loss line."""
        two_phase = _make_two_phase_router(max_search_iterations=1_000_000)
        routes = two_phase._detailed_standard(
            net_order=[1],
            progress_callback=None,
            timeout=0.0,
            start_time=time.time() - 100.0,
            checkpoint_callback=None,
        )
        assert routes == []
        out = capsys.readouterr().out
        assert "[deterministic-budget] stage deadline fired" in out
        assert "not reproducible" in out

    def test_detailed_standard_silent_on_timeout_without_backstop(self, capsys):
        """Same immediate-timeout scenario, but WITHOUT the iteration backstop
        pinned -- an ordinary --timeout deadline, no determinism claim to
        lose, so no determinism-loss line is logged."""
        two_phase = _make_two_phase_router(max_search_iterations=0)
        routes = two_phase._detailed_standard(
            net_order=[1],
            progress_callback=None,
            timeout=0.0,
            start_time=time.time() - 100.0,
            checkpoint_callback=None,
        )
        assert routes == []
        out = capsys.readouterr().out
        assert "[deterministic-budget]" not in out

    def test_detailed_standard_no_timeout_configured_never_fires(self, capsys):
        """``timeout=None`` (no deadline configured) never fires
        ``check_timeout()``, so the loop actually attempts the net and no
        determinism-loss line is logged."""
        two_phase = _make_two_phase_router(max_search_iterations=1_000_000)
        two_phase._route_net = lambda n: []  # avoid touching the grid stub
        routes = two_phase._detailed_standard(
            net_order=[1],
            progress_callback=None,
            timeout=None,
            start_time=time.time(),
            checkpoint_callback=None,
        )
        assert routes == []
        out = capsys.readouterr().out
        assert "[deterministic-budget]" not in out

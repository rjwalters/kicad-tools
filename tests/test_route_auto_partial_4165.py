"""Orchestrator truth-in-exit-condition tests (Issue #4165).

The global-family strategies (``global`` / ``escape`` / ``subgrid``) route a
single two-terminal corridor and historically reported ``success=True`` even
when a multi-pad net had stranded pads.  These tests lock in the fix:

* :meth:`RoutingOrchestrator.route_net` now runs a REAL per-pad reachability
  check after a global-family strategy and demotes an incomplete route to
  ``success=False, partial=True`` with ``pads_connected``/``pads_total``.
* With retries enabled (the ``strategy="auto"`` default) a demoted partial
  falls back to ``hierarchical``, which completes the net.
* With retries disabled (a forced ``--strategy global``) the honest partial is
  surfaced instead of a silent fallback.
* Fully-routed single corridors, 2-pad nets, and ``hierarchical`` are
  unaffected (no spurious partial flag).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kicad_tools.router import (
    RoutingMetrics,
    RoutingOrchestrator,
    RoutingResult,
    RoutingStrategy,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules


def _pad(x, y, net=1, net_name="NET1", pin="1"):
    return Pad(x=x, y=y, width=1.0, height=1.0, net=net, net_name=net_name, ref="U1", pin=pin)


def _seg(x1, y1, x2, y2):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU, net=1, net_name="NET1")


@pytest.fixture
def design_rules():
    return DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)


@pytest.fixture
def bare_pcb():
    """A mock PCB that exposes NO copper accessors (forces empty existing copper)."""
    pcb = MagicMock(spec=["width", "height"])
    pcb.width = 65.0
    pcb.height = 56.0
    return pcb


# Three collinear-ish pads where a single corridor between the two most-distant
# ones (P0 <-> P1) strands the third (P2, off the line).
_THREE_PADS = [
    _pad(0.0, 0.0, pin="1"),
    _pad(10.0, 0.0, pin="2"),
    _pad(5.0, 20.0, pin="3"),
]


def test_global_partial_is_demoted(bare_pcb, design_rules):
    """A global corridor connecting 2/3 pads is demoted to partial, not success."""
    orch = RoutingOrchestrator(
        pcb=bare_pcb, rules=design_rules, backend="cpu", max_strategy_retries=0
    )

    def fake_execute(net, strategy, intent, pads_arg):
        # Emulate the global router: one corridor between the two distant pads.
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            segments=[_seg(0.0, 0.0, 10.0, 0.0)],
            metrics=RoutingMetrics(total_length_mm=10.0),
        )

    orch._execute_strategy = fake_execute
    result = orch.route_net("NET1", pads=_THREE_PADS)

    assert result.success is False
    assert result.partial is True
    assert result.pads_connected == 2
    assert result.pads_total == 3
    assert any("2/3" in w or "partial" in w.lower() for w in result.warnings)


def test_global_partial_falls_back_to_hierarchical_when_retries_enabled(bare_pcb, design_rules):
    """With retries on (auto default), a partial global route retries hierarchical."""
    orch = RoutingOrchestrator(
        pcb=bare_pcb, rules=design_rules, backend="cpu", max_strategy_retries=2
    )

    def fake_execute(net, strategy, intent, pads_arg):
        if strategy == RoutingStrategy.HIERARCHICAL_DIFF_PAIR:
            # The iterative router completes the net (all three pads joined).
            return RoutingResult(
                success=True,
                net=net,
                strategy_used=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
                segments=[
                    _seg(0.0, 0.0, 10.0, 0.0),
                    _seg(10.0, 0.0, 5.0, 20.0),
                ],
                metrics=RoutingMetrics(total_length_mm=32.0),
            )
        # Global family: partial corridor.
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            segments=[_seg(0.0, 0.0, 10.0, 0.0)],
        )

    orch._execute_strategy = fake_execute
    result = orch.route_net("NET1", pads=_THREE_PADS)

    assert result.success is True
    assert result.strategy_used == RoutingStrategy.HIERARCHICAL_DIFF_PAIR
    assert any("retry" in w.lower() for w in result.warnings)


def test_partial_kept_when_all_retries_stay_partial(bare_pcb, design_rules):
    """If no strategy completes the net, the best honest partial is surfaced."""
    orch = RoutingOrchestrator(
        pcb=bare_pcb, rules=design_rules, backend="cpu", max_strategy_retries=2
    )

    def fake_execute(net, strategy, intent, pads_arg):
        # Every strategy (including the hierarchical fallback) only manages the
        # same 2/3 corridor -- but it's still reported as global-family so it is
        # subject to the completion check.
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            segments=[_seg(0.0, 0.0, 10.0, 0.0)],
        )

    orch._execute_strategy = fake_execute
    result = orch.route_net("NET1", pads=_THREE_PADS)

    assert result.success is False
    assert result.partial is True
    assert result.pads_connected == 2
    assert result.pads_total == 3


def test_lucky_full_corridor_reports_clean_success(bare_pcb, design_rules):
    """A single corridor that happens to cover all pads reports success, no partial."""
    orch = RoutingOrchestrator(
        pcb=bare_pcb, rules=design_rules, backend="cpu", max_strategy_retries=0
    )
    # Three pads all on one line -> a single corridor covers all of them.
    pads = [_pad(0.0, 0.0, pin="1"), _pad(5.0, 0.0, pin="2"), _pad(10.0, 0.0, pin="3")]

    def fake_execute(net, strategy, intent, pads_arg):
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            segments=[_seg(0.0, 0.0, 10.0, 0.0)],
        )

    orch._execute_strategy = fake_execute
    result = orch.route_net("NET1", pads=pads)

    assert result.success is True
    assert result.partial is False
    assert result.pads_connected == 3
    assert result.pads_total == 3


def test_two_pad_net_not_flagged_partial(bare_pcb, design_rules):
    """A routed 2-pad net is never demoted (no multi-pad stranding possible)."""
    orch = RoutingOrchestrator(
        pcb=bare_pcb, rules=design_rules, backend="cpu", max_strategy_retries=0
    )
    pads = [_pad(0.0, 0.0, pin="1"), _pad(10.0, 0.0, pin="2")]

    def fake_execute(net, strategy, intent, pads_arg):
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            segments=[_seg(0.0, 0.0, 10.0, 0.0)],
        )

    orch._execute_strategy = fake_execute
    result = orch.route_net("NET1", pads=pads)

    assert result.success is True
    assert result.partial is False


def test_hierarchical_result_not_subject_to_completion_check(bare_pcb, design_rules):
    """Hierarchical completes by construction; it is trusted without demotion."""
    orch = RoutingOrchestrator(
        pcb=bare_pcb, rules=design_rules, backend="cpu", max_strategy_retries=0
    )

    def fake_execute(net, strategy, intent, pads_arg):
        # Even though the reported geometry only covers 2/3 pads, a hierarchical
        # result is trusted (its own iterative logic owns completeness).
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
            segments=[_seg(0.0, 0.0, 10.0, 0.0)],
        )

    orch._execute_strategy = fake_execute
    result = orch.route_net("NET1", pads=_THREE_PADS)

    assert result.success is True
    assert result.partial is False


def test_hard_failure_stays_failure_not_partial(bare_pcb, design_rules):
    """A global strategy that produces NO copper stays a hard failure."""
    orch = RoutingOrchestrator(
        pcb=bare_pcb, rules=design_rules, backend="cpu", max_strategy_retries=0
    )

    def fake_execute(net, strategy, intent, pads_arg):
        return RoutingResult(
            success=False,
            net=net,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            error_message="Global router failed to find corridor assignment",
        )

    orch._execute_strategy = fake_execute
    result = orch.route_net("NET1", pads=_THREE_PADS)

    assert result.success is False
    assert result.partial is False


def test_to_dict_exposes_partial_fields(bare_pcb, design_rules):
    """The partial signal is serialized for the CLI/MCP surface."""
    orch = RoutingOrchestrator(
        pcb=bare_pcb, rules=design_rules, backend="cpu", max_strategy_retries=0
    )

    def fake_execute(net, strategy, intent, pads_arg):
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            segments=[_seg(0.0, 0.0, 10.0, 0.0)],
        )

    orch._execute_strategy = fake_execute
    result = orch.route_net("NET1", pads=_THREE_PADS)
    d = result.to_dict()

    assert d["partial"] is True
    assert d["success"] is False
    assert d["pads_connected"] == 2
    assert d["pads_total"] == 3

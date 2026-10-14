"""Adaptive and combined flows save a ``_partial`` from the FIRST tier (#6625).

``route_with_rule_relaxation`` (``--adaptive-rules``) and
``route_with_combined_escalation`` only bound ``_interrupt_state["router"]``
after a tier completed, so a deadline / Ctrl+C in the first tier saved nothing.
Preserved-copper dedupe in the partial is covered by #6553 / #6626; here only
the tier's own copper is asserted.
"""

from __future__ import annotations

import json
import signal
from types import SimpleNamespace

import pytest

from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.router.core import Autorouter
from tests.test_route_layer_escalation_partial_6322 import (  # noqa: F401
    _argv,
    _clean_state,
    _copper,
    _partial,
    board,
    control,
)

_FLOWS = {
    "adaptive": ("--no-auto-layers", "--adaptive-rules"),
    "combined": ("--auto-layers", "--adaptive-rules"),
}


class _Hook:
    """Route for real, checkpoint, then interrupt on the ``interrupt_on``-th call.

    Earlier calls drop net 2 so that tier is incomplete and the ladder goes on.
    """

    def __init__(self, monkeypatch, *, interrupt, interrupt_on=1):
        self.calls = 0
        self.expected = None
        self.state_at_interrupt = None
        self.routers = []
        original = Autorouter.route_all_negotiated

        def route(router, *args, **kwargs):
            self.calls += 1
            self.routers.append(router)
            result = original(router, *args, **kwargs)
            if self.calls < interrupt_on:
                router.routes = [r for r in router.routes if r.net != 2]
            callback = kwargs.get("checkpoint_callback")
            if callback is not None:
                metrics = SimpleNamespace(iteration=0, routed_count=1, overflow=0)
                callback(list(router.routes), metrics)
            if self.calls < interrupt_on:
                return result
            self.state_at_interrupt = dict(route_cmd._interrupt_state)
            self.expected = _copper(router.to_sexp())
            interrupt()
            return result

        monkeypatch.setattr(Autorouter, "route_all_negotiated", route)


def _deadline():
    raise route_deadline.RouteDeadlineExpired()


def _ctrl_c():
    signal.raise_signal(signal.SIGINT)


@pytest.mark.parametrize("flow", list(_FLOWS))
class TestFirstTierInterrupt:
    def test_deadline_saves_partial(self, monkeypatch, board, control, flow):  # noqa: F811
        source, output = board
        hook = _Hook(monkeypatch, interrupt=_deadline)

        rc = route_cmd._in_process_main_impl(_argv(source, output, *_FLOWS[flow]))

        assert rc == route_deadline.TIMEOUT_EXIT
        assert hook.calls == 1
        partial = _partial(output)
        assert partial.is_file(), f"{flow} flow saved no _partial from tier 1"
        assert hook.expected[0] > 0
        assert _copper(partial.read_text()) == hook.expected
        record = json.loads(control.read_text())
        assert record["snapshot_saved"] is True
        assert record["snapshot"] == str(partial)
        # The in-flight tier is not a completed attempt.
        assert not hook.state_at_interrupt.get("best_completed_attempt")

    def test_ctrl_c_saves_partial(self, monkeypatch, board, flow):  # noqa: F811
        source, output = board
        hook = _Hook(monkeypatch, interrupt=_ctrl_c)

        with pytest.raises(SystemExit) as excinfo:
            route_cmd._in_process_main_impl(_argv(source, output, *_FLOWS[flow]))

        assert excinfo.value.code == 5
        assert _copper(_partial(output).read_text()) == hook.expected

    def test_completed_tier_beats_worse_in_flight_tier(
        self,
        monkeypatch,
        board,
        control,
        flow,  # noqa: F811
    ):
        source, output = board
        hook = _Hook(monkeypatch, interrupt=_deadline, interrupt_on=2)

        rc = route_cmd._in_process_main_impl(_argv(source, output, *_FLOWS[flow]))

        assert rc == route_deadline.TIMEOUT_EXIT
        assert hook.calls == 2
        first, second = hook.routers[0], hook.routers[1]
        assert first is not second
        # The completed first tier stays pinned; the in-flight one did not
        # displace it.
        assert hook.state_at_interrupt["router"] is first
        assert hook.state_at_interrupt.get("best_completed_attempt") is True
        assert _partial(output).is_file()

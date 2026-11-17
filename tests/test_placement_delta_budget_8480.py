"""Placement-delta feedback must not turn a finished route into a timeout (#8480).

Each trial's budget is re-read live (clamped to the stage time left), a trial is
skipped when too little remains, and a trial whose iteration 0 is clearly worse
than the accepted result is abandoned and reverted.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from kicad_tools.cli import route_cmd
from kicad_tools.router import PlacementDeltaFeedbackLoop
from tests.router.test_placement_delta_feedback import (
    FakePad,
    FakeRouter,
    MockFootprint,
    MockPCB,
    _mirror_delta,
    _rotate_delta,
    _ub_pad,
)


class _RecordingRouter(FakeRouter):
    """FakeRouter that records per-trial kwargs and honours progress_callback."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.timeouts: list[float | None] = []
        self.callback_results: list[bool] = []

    def route_all_negotiated(self, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        cb = kwargs.get("progress_callback")
        if cb is not None:
            self.routes = [
                object() for _ in range(len(self.nets) - 1 - len(self.get_failed_nets()))
            ]
            self.callback_results.append(cb(0.1, "Iteration 1/10: rip-up and reroute", True))
        return super().route_all_negotiated(**kwargs)


def _loop(failed_predicate, total_nets, proposer):
    pcb = MockPCB([MockFootprint("UB", 10.0, 10.0)])
    router = _RecordingRouter([FakePad(12.0, 10.0, "UB", "2")], total_nets, failed_predicate)
    loop = PlacementDeltaFeedbackLoop(
        router=router, pcb=pcb, verbose=False, delta_proposer=proposer
    )
    return loop, router, pcb


def _never_helps(_router):
    return [1]


def test_trial_budget_is_reread_and_exhaustion_stops_loop():
    loop, router, _ = _loop(_never_helps, 2, lambda _p: [_rotate_delta(), _mirror_delta()])
    budgets = iter([100.0, 50.0, 10.0, 10.0])
    result = loop.run_delta(
        max_adjustments=3,
        timeout=999.0,
        reuse_existing_routes=False,
        timeout_provider=lambda: next(budgets),
        min_trial_budget=30.0,
    )
    assert router.timeouts[-2:] == [100.0, 50.0]  # live value, not the fixed 999
    assert result.exit_reason == "pd_budget_exhausted"
    assert result.applied_deltas == []
    assert len(result.reverted_deltas) == 2


def test_no_trial_starts_below_minimum():
    loop, router, _ = _loop(_never_helps, 2, lambda _p: [_rotate_delta()])
    result = loop.run_delta(
        max_adjustments=3,
        reuse_existing_routes=False,
        timeout_provider=lambda: 5.0,
        min_trial_budget=30.0,
    )
    baseline_calls = router.route_calls
    assert baseline_calls == 1  # only the baseline pass
    assert result.exit_reason == "pd_budget_exhausted"


def test_clearly_worse_iteration_zero_is_abandoned_and_reverted():
    # Baseline: 1 of 10 nets failed.  Once UB's pad moves, 7 fail.
    def worse_after_move(router):
        return [1] if abs(_ub_pad(router).x - 12.0) < 1e-6 else list(range(1, 8))

    loop, router, _ = _loop(worse_after_move, 10, lambda _p: [_rotate_delta()])
    result = loop.run_delta(max_adjustments=1, reuse_existing_routes=False)
    assert router.callback_results == [False]
    assert result.applied_deltas == []
    assert result.reverted_deltas
    assert _ub_pad(router).x == 12.0  # placement restored


def test_slightly_worse_iteration_zero_is_not_abandoned():
    def slightly_worse(router):
        return [1] if abs(_ub_pad(router).x - 12.0) < 1e-6 else [1, 2]

    loop, router, _ = _loop(slightly_worse, 10, lambda _p: [_rotate_delta()])
    loop.run_delta(max_adjustments=1, reuse_existing_routes=False)
    assert router.callback_results == [True]


def test_delta_probe_timeout_tracks_remaining_stage_time():
    args = SimpleNamespace(
        placement_delta_feedback_timeout=5909.9,
        placement_delta_feedback_budget=3,
        _routing_deadline=time.monotonic() + 100.0,
        _wall_clock_deadline=time.monotonic() + 100.0,
    )
    first = route_cmd._delta_probe_timeout(args)
    args._routing_deadline = time.monotonic() + 20.0
    second = route_cmd._delta_probe_timeout(args)
    assert first <= 100.0 and second <= 20.0

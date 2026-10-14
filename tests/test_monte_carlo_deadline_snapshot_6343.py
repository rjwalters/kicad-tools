"""Issue #6343: a deadline during a Monte Carlo / evolutionary run keeps copper.

Both strategies held their best trial in local variables -- and, on the
default parallel path, ran every trial in a worker process -- until the whole
run finished.  A ``--timeout`` firing mid-run therefore unwound to
``_save_partial_results`` with ``router.routes == []`` and the CLI wrote a
board with no copper (``snapshot_saved: false``) after 20 minutes of trials.

These tests pin that the best *completed* trial is published on the router as
it is found, that the partial save restores it (``snapshot_state:
"best-trial"``), that a cooperative ``timeout`` returns it without waiting on
running workers, and that worker trials see the parent's layer stack and
search budgets.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.cli.route_deadline import RouteDeadlineExpired
from kicad_tools.router import core as core_mod
from kicad_tools.router.algorithms import evolutionary as evo_mod
from kicad_tools.router.algorithms import monte_carlo as mc_mod
from kicad_tools.router.core import Autorouter, worker_router_kwargs
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Route, Segment

BOARD = "(kicad_pcb (version 20240108))"


def _route(net: int, x: float) -> Route:
    seg = Segment(x, 5.0, x + 5.0, 5.0, 0.25, Layer.F_CU, net=net, net_name=f"N{net}")
    return Route(net=net, net_name=f"N{net}", segments=[seg])


def _router() -> Autorouter:
    router = Autorouter(width=50, height=40)
    router.net_names = {1: "N1", 2: "N2", 3: "N3"}
    router.nets = {1: [], 2: [], 3: []}
    return router


@pytest.fixture
def interrupt_state(tmp_path):
    """Bind ``_interrupt_state`` like the CLI does; restore it afterwards."""
    saved = dict(route_cmd._interrupt_state)
    source = tmp_path / "input.kicad_pcb"
    source.write_text(BOARD)
    output = tmp_path / "output.kicad_pcb"

    def bind(router):
        route_cmd._reset_partial_save_state()
        route_cmd._interrupt_state.update(
            router=router,
            output_path=output,
            pcb_path=source,
            quiet=True,
            best_completed_attempt=False,
        )
        return output.with_stem(output.stem + "_partial")

    yield bind
    route_cmd._interrupt_state.clear()
    route_cmd._interrupt_state.update(saved)


# ---------------------------------------------------------------------------
# Sequential path: deadline during trial N > 1 keeps trial N-1's copper
# ---------------------------------------------------------------------------


def test_monte_carlo_deadline_in_trial_two_keeps_trial_one_copper(interrupt_state, monkeypatch):
    router = _router()
    calls = {"n": 0}

    def fake_route_all(net_order=None, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # Trial 0 completes with one route ...
            router.routes = [_route(1, 10.0)]
            return list(router.routes)
        # ... trial 1 is mid-flight (partial copper of its own) when the
        # deadline fires.
        router.routes = [_route(2, 30.0)]
        raise RouteDeadlineExpired()

    monkeypatch.setattr(router, "route_all", fake_route_all)
    partial = interrupt_state(router)

    with pytest.raises(RouteDeadlineExpired):
        mc_mod.run_monte_carlo(router, num_trials=4, seed=1, verbose=False, num_workers=1)

    assert route_cmd._save_partial_results() is True
    text = partial.read_text()
    # Trial 0's copper (net 1 at x=10), not trial 1's in-flight route.
    assert "(start 10" in text
    assert "(start 30" not in text
    info = route_cmd._interrupt_state["partial_save_info"]
    assert info["snapshot_state"] == "best-trial"
    assert info["snapshot_strategy"] == "monte-carlo"
    assert info["snapshot_trial"] == 0


def test_evolutionary_deadline_mid_generation_keeps_best_chromosome(interrupt_state, monkeypatch):
    router = _router()
    calls = {"n": 0}

    def fake_route_all(net_order=None, **_kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            # Chromosome 1 routes two nets, so it is the best so far.
            router.routes = [_route(n, 5.0 * n) for n in range(1, calls["n"] + 1)]
            return list(router.routes)
        router.routes = []
        raise RouteDeadlineExpired()

    monkeypatch.setattr(router, "route_all", fake_route_all)
    partial = interrupt_state(router)

    # pop_size=4 so the deadline lands INSIDE generation 0: before #6343 the
    # best was only recorded once a whole generation finished.
    with pytest.raises(RouteDeadlineExpired):
        evo_mod.run_evolutionary(
            router, pop_size=4, generations=3, seed=1, verbose=False, num_workers=1
        )

    assert route_cmd._save_partial_results() is True
    text = partial.read_text()
    assert text.count("(segment") == 2
    info = route_cmd._interrupt_state["partial_save_info"]
    assert info["snapshot_state"] == "best-trial"
    assert info["snapshot_strategy"] == "evolutionary"
    assert info["snapshot_trial"] == 1


def test_in_progress_trial_is_saved_when_no_trial_completed(interrupt_state, monkeypatch):
    router = _router()

    def fake_route_all(net_order=None, **_kw):
        router.routes = [_route(3, 12.0)]
        raise RouteDeadlineExpired()

    monkeypatch.setattr(router, "route_all", fake_route_all)
    partial = interrupt_state(router)
    with pytest.raises(RouteDeadlineExpired):
        mc_mod.run_monte_carlo(router, num_trials=3, seed=1, verbose=False, num_workers=1)

    assert route_cmd._save_partial_results() is True
    assert "(start 12" in partial.read_text()
    assert route_cmd._interrupt_state["partial_save_info"]["snapshot_state"] == "live"


def test_finished_run_does_not_leave_a_stale_best_for_later_stages(monkeypatch):
    router = _router()
    monkeypatch.setattr(router, "route_all", lambda net_order=None, **_kw: [_route(1, 10.0)])
    mc_mod.run_monte_carlo(router, num_trials=2, seed=1, verbose=False, num_workers=1)
    assert router._best_trial_routes is None
    evo_mod.run_evolutionary(
        router, pop_size=2, generations=1, seed=1, verbose=False, num_workers=1
    )
    assert router._best_trial_routes is None


def test_ordinary_exception_clears_the_published_best(monkeypatch):
    router = _router()
    calls = {"n": 0}

    def fake_route_all(net_order=None, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_route(1, 10.0)]
        raise RuntimeError("boom")

    monkeypatch.setattr(router, "route_all", fake_route_all)
    with pytest.raises(RuntimeError):
        mc_mod.run_monte_carlo(router, num_trials=3, seed=1, verbose=False, num_workers=1)
    # The caller carries on after an ordinary failure; a later partial save
    # must not swap this run's stale best in over later copper.
    assert router._best_trial_routes is None


def test_sequential_timeout_stops_between_trials(monkeypatch):
    router = _router()
    calls = {"n": 0}

    def slow_route_all(net_order=None, **_kw):
        calls["n"] += 1
        time.sleep(0.3)
        return [_route(1, 10.0)]

    monkeypatch.setattr(router, "route_all", slow_route_all)
    routes = mc_mod.run_monte_carlo(
        router, num_trials=50, seed=1, verbose=False, num_workers=1, timeout=0.5
    )
    assert calls["n"] == 2
    assert len(routes) == 1


# ---------------------------------------------------------------------------
# Parallel path (the CLI default): trials run in worker processes
# ---------------------------------------------------------------------------


def _fake_parallel_trial(config):
    """Picklable stand-in for ``core._run_monte_carlo_trial``.

    Trial 0 finishes at once with copper; every other trial runs far longer
    than any test budget, as a real board-05 trial does.
    """
    if config["trial_num"] == 0:
        return [_route(1, 10.0)], 100.0, 0
    time.sleep(60)
    return [], 0.0, config["trial_num"]


def _fake_parallel_chromosome(config):
    if config["chrom_idx"] == 0:
        return [_route(1, 10.0)], 100.0, 0
    time.sleep(60)
    return [], 0.0, config["chrom_idx"]


def _assert_workers_gone(within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while multiprocessing.active_children() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert multiprocessing.active_children() == []


@pytest.mark.skipif(os.name != "posix", reason="uses SIGALRM")
def test_parallel_deadline_while_waiting_on_workers_keeps_best(interrupt_state, monkeypatch):
    """The ``interrupted_function == "wait"`` case from the report."""
    monkeypatch.setattr(core_mod, "_run_monte_carlo_trial", _fake_parallel_trial)
    router = _router()
    partial = interrupt_state(router)

    def _deadline(signum, frame):
        raise RouteDeadlineExpired()

    previous = signal.signal(signal.SIGALRM, _deadline)
    started = time.monotonic()
    try:
        signal.setitimer(signal.ITIMER_REAL, 3.0)
        with pytest.raises(RouteDeadlineExpired):
            mc_mod.run_monte_carlo(router, num_trials=3, seed=1, verbose=False, num_workers=2)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    # The unwind must not join workers that are a minute into a trial: that
    # join would eat the five-second save grace.
    assert time.monotonic() - started < 20

    assert route_cmd._save_partial_results() is True
    assert "(start 10" in partial.read_text()
    assert route_cmd._interrupt_state["partial_save_info"]["snapshot_state"] == "best-trial"


def test_parallel_monte_carlo_timeout_returns_best_without_waiting(monkeypatch):
    monkeypatch.setattr(core_mod, "_run_monte_carlo_trial", _fake_parallel_trial)
    router = _router()
    started = time.monotonic()
    routes = mc_mod.run_monte_carlo(
        router, num_trials=3, seed=1, verbose=False, num_workers=2, timeout=3.0
    )
    assert time.monotonic() - started < 20
    assert [r.net for r in routes] == [1]
    assert router._best_trial_routes is None
    # Abandoned trials are terminated, not left for the interpreter-exit hook
    # of ``concurrent.futures`` to join (which would stall exit for minutes).
    _assert_workers_gone()


def test_parallel_evolutionary_timeout_returns_best_without_waiting(monkeypatch):
    monkeypatch.setattr(evo_mod, "_run_evolutionary_trial", _fake_parallel_chromosome)
    router = _router()
    started = time.monotonic()
    routes = evo_mod.run_evolutionary(
        router, pop_size=3, generations=5, seed=1, verbose=False, num_workers=2, timeout=3.0
    )
    assert time.monotonic() - started < 20
    assert [r.net for r in routes] == [1]
    _assert_workers_gone()


# ---------------------------------------------------------------------------
# Issue #6364: a progress-callback cancel keeps the best and does not wait
# ---------------------------------------------------------------------------


def _fake_parallel_unscored_trial(config):
    """Trial 0 finishes at once but scores ``-inf``; the rest block."""
    if config["trial_num"] == 0:
        return [], float("-inf"), 0
    time.sleep(60)
    return [], 0.0, config["trial_num"]


def test_parallel_monte_carlo_cancel_returns_triggering_trial_without_waiting(monkeypatch):
    """A ``False`` callback return after the first completion while another
    trial blocks: the call returns promptly with that first trial as best."""
    monkeypatch.setattr(core_mod, "_run_monte_carlo_trial", _fake_parallel_trial)
    router = _router()
    calls: list[float] = []

    def cancel(progress, _message, _cancelable):
        calls.append(progress)
        return False

    started = time.monotonic()
    routes = mc_mod.run_monte_carlo(
        router, num_trials=3, seed=1, verbose=False, num_workers=2, progress_callback=cancel
    )
    # No ``shutdown(wait=True)`` join of the trial still sleeping for 60 s.
    assert time.monotonic() - started < 20
    # The trial whose completion triggered the cancel is not dropped.
    assert [r.net for r in routes] == [1]
    assert calls[0] == pytest.approx(1 / 3)
    _assert_workers_gone()


def test_parallel_monte_carlo_cancel_with_no_scored_trial_returns_empty(monkeypatch):
    """Edge case: cancelling before any trial produced a best still returns
    ``(None, -inf, -1)`` without hanging."""
    monkeypatch.setattr(core_mod, "_run_monte_carlo_trial", _fake_parallel_unscored_trial)
    router = _router()
    started = time.monotonic()
    result = mc_mod._run_parallel(
        autorouter=router,
        num_trials=3,
        use_negotiated=False,
        base_seed=1,
        base_order=[1, 2, 3],
        num_workers=2,
        verbose=False,
        progress_callback=lambda *_a: False,
    )
    assert time.monotonic() - started < 20
    assert result == (None, float("-inf"), -1)
    _assert_workers_gone()


# ---------------------------------------------------------------------------
# Issue #6364: a failed pre_save_hook stays "inconsistent" after a best-trial
# restore
# ---------------------------------------------------------------------------


def test_failed_hook_with_best_trial_stays_inconsistent(interrupt_state):
    router = _router()
    router.routes = [_route(2, 30.0)]
    mc_mod.publish_best_trial(
        router, [_route(1, 10.0)], strategy="monte-carlo", trial=4, score=100.0
    )
    partial = interrupt_state(router)

    def broken_hook():
        raise RuntimeError("restore failed")

    route_cmd._interrupt_state["pre_save_hook"] = broken_hook

    assert route_cmd._save_partial_results() is True
    # The best trial's copper is still what gets saved ...
    assert "(start 10" in partial.read_text()
    info = route_cmd._interrupt_state["partial_save_info"]
    # ... but the receipt keeps flagging the failed hook, and records both facts.
    assert info["snapshot_state"] == "inconsistent"
    assert info["snapshot_strategy"] == "monte-carlo"
    assert info["snapshot_trial"] == 4
    assert info["snapshot_restore_error"] == "RuntimeError: restore failed"


def test_failed_best_trial_hook_chain_stays_inconsistent(interrupt_state):
    """The hook shape PR #6549 (#6321) registers -- it reports ``best-trial``
    on success and chains an earlier hook -- must not mask a failure either."""
    router = _router()
    mc_mod.publish_best_trial(
        router, [_route(1, 10.0)], strategy="evolutionary", trial=2, score=50.0
    )
    interrupt_state(router)

    def previous_hook():
        raise OSError("cannot serialize moved placement")

    def chained_best_hook():
        hook_info, hook_text = previous_hook()
        return {**hook_info, "snapshot_state": "best-trial"}, hook_text

    route_cmd._interrupt_state["pre_save_hook"] = chained_best_hook

    assert route_cmd._save_partial_results() is True
    info = route_cmd._interrupt_state["partial_save_info"]
    assert info["snapshot_state"] == "inconsistent"
    assert info["snapshot_strategy"] == "evolutionary"
    assert info["snapshot_trial"] == 2
    assert "snapshot_restore_error" in info


def test_successful_hook_with_best_trial_reports_best_trial(interrupt_state):
    router = _router()
    mc_mod.publish_best_trial(
        router, [_route(1, 10.0)], strategy="monte-carlo", trial=1, score=100.0
    )
    interrupt_state(router)
    route_cmd._interrupt_state["pre_save_hook"] = lambda: ({}, None)

    assert route_cmd._save_partial_results() is True
    info = route_cmd._interrupt_state["partial_save_info"]
    assert info["snapshot_state"] == "best-trial"
    assert info["snapshot_trial"] == 1
    assert "snapshot_restore_error" not in info


# ---------------------------------------------------------------------------
# Worker payload: layer stack and search budgets reach worker trials
# ---------------------------------------------------------------------------


def test_worker_payload_carries_layer_stack_and_budgets():
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    parent = Autorouter(
        width=30,
        height=20,
        layer_stack=stack,
        per_net_iterations=1_000_000,
        max_search_iterations=5_000_000,
    )
    payload = parent._serialize_for_parallel()
    kwargs = worker_router_kwargs(payload)
    assert kwargs["per_net_iterations"] == 1_000_000
    assert kwargs["max_search_iterations"] == 5_000_000

    worker = Autorouter(width=30, height=20, **kwargs)
    assert worker.layer_stack is not None
    assert worker.layer_stack.num_layers == 4
    assert worker._per_net_iterations == 1_000_000
    assert worker._max_search_iterations == 5_000_000


@pytest.mark.parametrize("runner", ["monte-carlo", "evolutionary"])
def test_worker_trial_builds_router_with_parent_budgets(monkeypatch, runner):
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    parent = Autorouter(width=30, height=20, layer_stack=stack, per_net_iterations=1234)
    parent.net_names = {1: "N1"}
    parent.nets = {1: []}
    config = parent._serialize_for_parallel()
    seen = {}

    def capture(self, net_order=None, **_kw):
        seen["layers"] = self.layer_stack.num_layers if self.layer_stack else 2
        seen["per_net"] = self._per_net_iterations
        return []

    monkeypatch.setattr(Autorouter, "route_all", capture)
    if runner == "monte-carlo":
        config.update(trial_num=0, seed=1, base_order=[1], use_negotiated=False)
        core_mod._run_monte_carlo_trial(config)
    else:
        config.update(chrom_idx=0, seed=1, net_order=[1])
        evo_mod._run_evolutionary_trial(config)
    assert seen == {"layers": 4, "per_net": 1234}


def test_legacy_payload_keeps_default_router():
    assert worker_router_kwargs({}) == {}


# ---------------------------------------------------------------------------
# Supervised end to end: the .timeout.json receipt
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group supervision")
def test_supervised_monte_carlo_deadline_receipt_has_best_trial(tmp_path):
    source = tmp_path / "input.kicad_pcb"
    source.write_text(BOARD)
    output = tmp_path / "output.kicad_pcb"
    script = f"""
from pathlib import Path
import os, signal, threading, time
from types import SimpleNamespace
from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
signal.signal(signal.SIGTERM, route_deadline._deadline_signal)
router = Autorouter(width=50, height=40)
router.net_names = {{1: "N1", 2: "N2"}}
router.nets = {{1: [], 2: []}}
calls = []
def route_all(net_order=None, **kw):
    calls.append(1)
    if len(calls) == 1:
        seg = Segment(10.0, 5.0, 15.0, 5.0, 0.25, Layer.F_CU, net=1, net_name="N1")
        return [Route(net=1, net_name="N1", segments=[seg])]
    # Arm only once trial 0 has completed: the deadline lands in trial 1.
    timer = threading.Timer(1.0, os.kill, args=(os.getpid(), signal.SIGTERM))
    timer.daemon = True
    timer.start()
    while True:
        sum(range(10000))
router.route_all = route_all
def work(argv):
    route_deadline.configure_output(SimpleNamespace(pcb={str(source)!r}, output={str(output)!r}))
    route_cmd._interrupt_state.update(router=router, output_path=Path({str(output)!r}), pcb_path=Path({str(source)!r}))
    route_deadline.record_stage("routing")
    router.route_all_monte_carlo(num_trials=5, seed=1, verbose=False, num_workers=1)
    raise AssertionError("monte carlo must not complete")
route_cmd._main_impl = work
raise SystemExit(route_cmd._in_process_main([]))
"""
    started = time.monotonic()
    assert (
        route_deadline._supervise(
            [sys.executable, "-c", script], 60, tmp_path / "control.json", save_seconds=2
        )
        == 124
    )
    assert time.monotonic() - started < 62
    report = json.loads(output.with_suffix(".timeout.json").read_text())
    assert report["snapshot_saved"] is True
    assert report["snapshot_state"] == "best-trial"
    assert report["snapshot_strategy"] == "monte-carlo"
    assert report["snapshot_trial"] == 0
    snapshot = Path(report["snapshot"]).read_text()
    assert snapshot.count("(segment") == 1
    assert "(start 10" in snapshot

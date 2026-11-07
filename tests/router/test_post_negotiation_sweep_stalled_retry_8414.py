"""Issue #8414: a stalled pad-escape / sub-grid / via-conflict retry inside the
post-negotiation sweep must stop at the remaining whole-sweep and per-net budget.

The 2026-11-06 re-test logged ``Post-negotiation sweep: 133.3s (bound 60.0s)``
right after ``Q3B/Q4B: 0/1 pads escaped`` and ``Sub-grid retry for <wide power
net>``.  #8416 forwarded the remaining budget into the nested A*, but the
pure-Python steps around it (pad-escape generation, escape application, the
nested ``route_net`` and the via/trace resolver loops) still ran on a spent
budget.  These tests stall those steps and assert the sweep returns within its
announced bound plus a small slack.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from kicad_tools.router import wall_deadline
from kicad_tools.router.algorithms.mst import MSTRouter
from kicad_tools.router.core import Autorouter, DesignRules
from kicad_tools.router.failure_analysis import FailureCause
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.subgrid import SubGridAnalysis

# What the sweep may run past its bound: the step in flight when the bound
# expires.  The stalls below are 0.25 s per step, so 1.0 s is generous while
# still far below the unbounded cost (10 s).
SLACK_S = 1.0


def _fake_off_grid_pads(n: int) -> list:
    return [
        SimpleNamespace(
            pad=SimpleNamespace(ref="Q3B", pin=str(i), key=("Q3B", str(i)), x=0.0, y=0.0)
        )
        for i in range(n)
    ]


def _pin_access_router(monkeypatch) -> Autorouter:
    """A 2-pin net whose every edge fails PIN_ACCESS (so the retries fire)."""
    router = Autorouter(width=20.0, height=20.0, rules=DesignRules(grid_resolution=0.1))
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "PGND"},
            {"number": "2", "x": 10.0, "y": 5.0, "net": 1, "net_name": "PGND"},
        ],
    )
    router.use_waypoint_injection = False

    def _failing_edges(self, *args, **kwargs):
        router.routing_failures.append(
            SimpleNamespace(net=1, failure_cause=FailureCause.PIN_ACCESS, analysis=None)
        )
        return []

    for meth in ("route_net", "route_net_star"):
        monkeypatch.setattr(MSTRouter, meth, _failing_edges)
    return router


def _stall_pad_escapes(monkeypatch, router: Autorouter, n_pads: int, stall_s: float) -> list:
    """Every pad-escape attempt stalls ``stall_s`` and fails (wide power pad)."""
    attempts: list[int] = []
    subgrid = router._subgrid
    analysis = SubGridAnalysis(off_grid_pads=_fake_off_grid_pads(n_pads))
    monkeypatch.setattr(subgrid, "analyze_pads", lambda pads: analysis)

    def _stalled_escape(sgp):
        attempts.append(1)
        time.sleep(stall_s)
        return None, "no_grid_point"

    monkeypatch.setattr(subgrid, "_find_escape_for_pad", _stalled_escape)
    return attempts


def _sweep(router: Autorouter, *, budget: float, per_net: float, nets=(1,)):
    pads = {n: [p for p in router.pads.values() if p.net == n] for n in nets}
    t0 = time.monotonic()
    with wall_deadline.deadline(budget):
        rescued = router._post_negotiation_sweep(
            stranded_nets=list(nets),
            net_routes={n: [] for n in nets},
            pads_by_net=pads,
            per_net_timeout=per_net,
            deadline=time.time() + budget,
        )
    return rescued, time.monotonic() - t0


# --------------------------------------------------------------------------
# The sweep-level regression: stalled pad escapes on a wide power net.
# --------------------------------------------------------------------------


def test_sweep_with_stalled_pad_escapes_returns_within_whole_sweep_bound(monkeypatch, capsys):
    router = _pin_access_router(monkeypatch)
    # 40 pads x 0.25 s = 10 s unbounded, against a 1 s whole-sweep bound.
    attempts = _stall_pad_escapes(monkeypatch, router, n_pads=40, stall_s=0.25)

    rescued, elapsed = _sweep(router, budget=1.0, per_net=10.0)

    assert rescued == []
    assert attempts, "fixture must reach the pad-escape step"
    assert len(attempts) < 40, "escape generation must stop at the bound"
    assert elapsed < 1.0 + SLACK_S, f"sweep took {elapsed:.2f}s against a 1.0s bound"


def test_sweep_with_stalled_pad_escapes_stops_at_per_net_budget(monkeypatch, capsys):
    router = _pin_access_router(monkeypatch)
    attempts = _stall_pad_escapes(monkeypatch, router, n_pads=40, stall_s=0.25)

    # Whole-sweep bound is generous; the 0.5 s per-net budget must bind.
    rescued, elapsed = _sweep(router, budget=60.0, per_net=0.5)

    assert rescued == []
    assert attempts and len(attempts) < 40
    assert elapsed < 0.5 + SLACK_S, f"net took {elapsed:.2f}s against a 0.5s per-net bound"
    out = capsys.readouterr().out
    # An overrun past per-net + 1 s would be named; here it must not be.
    assert "per-net bound" not in out


def test_sweep_skips_pin_access_retries_once_budget_spent(monkeypatch):
    """A net whose edge search already spent the budget runs no retry at all."""
    router = _pin_access_router(monkeypatch)
    attempts = _stall_pad_escapes(monkeypatch, router, n_pads=40, stall_s=0.25)

    def _slow_failing_edges(self, *args, **kwargs):
        time.sleep(0.4)  # the edge search spends the whole per-net budget
        router.routing_failures.append(
            SimpleNamespace(net=1, failure_cause=FailureCause.PIN_ACCESS, analysis=None)
        )
        return []

    for meth in ("route_net", "route_net_star"):
        monkeypatch.setattr(MSTRouter, meth, _slow_failing_edges)

    rescued, elapsed = _sweep(router, budget=60.0, per_net=0.3)

    assert rescued == []
    assert attempts == [], "no pad-escape attempt may start on a spent budget"
    assert elapsed < 0.4 + SLACK_S


# --------------------------------------------------------------------------
# The individual steps.
# --------------------------------------------------------------------------


def test_generate_escape_segments_stops_between_pads():
    router = Autorouter(width=20.0, height=20.0, rules=DesignRules(grid_resolution=0.1))
    subgrid = router._subgrid
    analysis = SubGridAnalysis(off_grid_pads=_fake_off_grid_pads(5))
    calls: list[int] = []

    def _escape(sgp):
        calls.append(1)
        return None, "no_grid_point"

    subgrid._find_escape_for_pad = _escape
    result = subgrid.generate_escape_segments(analysis, should_stop=lambda: len(calls) >= 2)

    assert len(calls) == 2
    reasons = [result.failure_reasons[sgp.pad.key] for sgp in analysis.off_grid_pads]
    assert reasons == ["no_grid_point"] * 2 + ["budget_spent"] * 3


def test_generate_escape_segments_without_probe_searches_every_pad():
    router = Autorouter(width=20.0, height=20.0, rules=DesignRules(grid_resolution=0.1))
    subgrid = router._subgrid
    analysis = SubGridAnalysis(off_grid_pads=_fake_off_grid_pads(5))
    calls: list[int] = []

    def _escape(sgp):
        calls.append(1)
        return None, "no_grid_point"

    subgrid._find_escape_for_pad = _escape
    subgrid.generate_escape_segments(analysis)
    assert len(calls) == 5


def test_subgrid_retry_skips_nested_route_when_escapes_spend_the_budget():
    calls: list = []
    subgrid = MagicMock()
    subgrid.analyze_pads.return_value = SimpleNamespace(has_off_grid_pads=True)

    def _slow_escapes(analysis, should_stop=None):
        time.sleep(0.3)  # stalled pad-escape generation
        return SimpleNamespace(success_count=1, unblocked_count=1)

    subgrid.generate_escape_segments.side_effect = _slow_escapes
    fake = SimpleNamespace(
        nets={1: ["a", "b"]},
        pads={"a": object(), "b": object()},
        net_names={1: "N"},
        _subgrid=subgrid,
        routes=[],
        routing_failures=[],
        route_net=lambda net, **kw: calls.append(kw) or [],
    )

    assert Autorouter._retry_net_with_subgrid(fake, 1, per_net_timeout=0.2) == []
    assert calls == [], "nested route_net must not start on a spent budget"
    subgrid.apply_escape_segments.assert_not_called()


def test_via_conflict_resolver_stops_iterating_conflicts_on_spent_budget():
    calls: list = []
    blocker = SimpleNamespace(blocking_type="via")
    failure = SimpleNamespace(
        net=1,
        failure_cause=FailureCause.PIN_ACCESS,
        analysis=SimpleNamespace(pad_access_blockers=[blocker]),
    )
    conflicts = [SimpleNamespace(via=SimpleNamespace(x=float(i), y=0.0)) for i in range(5)]
    manager = MagicMock()
    manager.find_blocking_vias.return_value = conflicts
    manager.try_relocate.return_value = SimpleNamespace(success=False)

    def _stalled_rip(_conflict, route_net_fn):
        route_net_fn(1)
        time.sleep(0.25)
        return SimpleNamespace(success=False)

    manager.try_rip_reroute.side_effect = _stalled_rip
    fake = SimpleNamespace(
        nets={1: ["a"]},
        pads={"a": object()},
        net_names={1: "N"},
        routing_failures=[failure],
        via_manager=manager,
        route_net=lambda net, **kw: calls.append(kw["per_net_timeout"]) or [],
    )

    t0 = time.monotonic()
    Autorouter._resolve_via_conflicts_for_net(fake, 1, per_net_timeout=0.4)
    elapsed = time.monotonic() - t0

    assert manager.try_rip_reroute.call_count < 5
    assert elapsed < 0.4 + SLACK_S
    # Each nested route_net got what was LEFT, not the entry budget.
    assert calls == sorted(calls, reverse=True)
    assert calls[-1] < 0.4


def test_drc_precheck_reports_dirty_when_stopped():
    router = Autorouter(width=20.0, height=20.0, rules=DesignRules(grid_resolution=0.1))
    route = Route(net=1, net_name="N")
    route.segments.append(
        Segment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1)
    )
    # A stopped scan is reported dirty, so the sweep rolls the rescue back.
    assert router._rescued_net_adds_blocking_drc(
        1, [route], {1: [route]}, None, should_stop=lambda: True
    )


def test_net_budget_spent_is_false_without_any_wall_clock():
    """Unbudgeted / deterministic-budget runs keep their exact behaviour."""
    assert not Autorouter._net_budget_spent(None, time.monotonic() - 1e6)


def test_net_budget_spent_tracks_per_net_and_wall_deadline():
    assert Autorouter._net_budget_spent(1.0, time.monotonic() - 2.0)
    assert not Autorouter._net_budget_spent(10.0, time.monotonic())
    with wall_deadline.deadline(0.01):
        time.sleep(0.02)
        assert Autorouter._net_budget_spent(None, time.monotonic())

"""Issue #8414: post-negotiation sweep bound / retry budget forwarding."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from kicad_tools.router import wall_deadline
from kicad_tools.router.algorithms.mst import MSTRouter
from kicad_tools.router.core import Autorouter, DesignRules
from kicad_tools.router.failure_analysis import FailureCause


def test_remaining_net_budget_none_stays_none():
    assert Autorouter._remaining_net_budget(None, time.monotonic()) is None


def test_remaining_net_budget_is_remaining_not_fresh():
    started = time.monotonic() - 4.0
    left = Autorouter._remaining_net_budget(10.0, started)
    assert left is not None
    assert 5.0 < left <= 6.0


def test_remaining_net_budget_floored_when_spent():
    left = Autorouter._remaining_net_budget(1.0, time.monotonic() - 100.0)
    assert left == wall_deadline.MIN_SEARCH_TIMEOUT_S


def _recording_route_net(calls):
    def _route_net(net, **kwargs):
        calls.append((net, kwargs))
        return []

    return _route_net


def test_subgrid_retry_forwards_budget_to_route_net():
    """``_retry_net_with_subgrid`` must hand its budget to the nested ``route_net``."""
    calls: list = []
    subgrid = MagicMock()
    subgrid.analyze_pads.return_value = SimpleNamespace(has_off_grid_pads=True)
    subgrid.generate_escape_segments.return_value = SimpleNamespace(
        success_count=1, unblocked_count=1
    )
    subgrid.get_escape_routes.return_value = []
    fake = SimpleNamespace(
        nets={1: ["a", "b"]},
        pads={"a": object(), "b": object()},
        net_names={1: "N"},
        _subgrid=subgrid,
        routes=[],
        routing_failures=[],
        route_net=_recording_route_net(calls),
    )

    Autorouter._retry_net_with_subgrid(fake, 1, per_net_timeout=7.5)

    assert calls == [(1, {"_subgrid_retry": True, "per_net_timeout": 7.5})]


def _via_conflict_fake(calls, *, relocate_ok):
    blocker = SimpleNamespace(blocking_type="via")
    failure = SimpleNamespace(
        net=1,
        failure_cause=FailureCause.PIN_ACCESS,
        analysis=SimpleNamespace(pad_access_blockers=[blocker]),
    )
    conflict = SimpleNamespace(via=SimpleNamespace(x=1.0, y=2.0))
    manager = MagicMock()
    manager.find_blocking_vias.return_value = [conflict]
    manager.try_relocate.return_value = SimpleNamespace(success=relocate_ok)

    def _rip(_conflict, route_net_fn):
        route_net_fn(1)
        return SimpleNamespace(success=True)

    manager.try_rip_reroute.side_effect = _rip
    return SimpleNamespace(
        nets={1: ["a"]},
        pads={"a": object()},
        net_names={1: "N"},
        routing_failures=[failure],
        via_manager=manager,
        route_net=_recording_route_net(calls),
    )


def test_via_conflict_resolver_forwards_budget_on_final_retry():
    calls: list = []
    fake = _via_conflict_fake(calls, relocate_ok=True)

    Autorouter._resolve_via_conflicts_for_net(fake, 1, per_net_timeout=3.0)

    assert calls == [(1, {"_subgrid_retry": True, "per_net_timeout": 3.0})]


def test_via_conflict_resolver_forwards_budget_in_rip_reroute():
    calls: list = []
    fake = _via_conflict_fake(calls, relocate_ok=False)

    Autorouter._resolve_via_conflicts_for_net(fake, 1, per_net_timeout=3.0)

    # rip-reroute's route_net_fn, then the final retry: both budgeted.
    assert len(calls) == 2
    assert all(kw["per_net_timeout"] == 3.0 for _, kw in calls)


def test_route_net_pin_access_retries_get_remaining_budget(monkeypatch):
    """route_net hands the PIN_ACCESS retries what is LEFT of its budget."""
    router = Autorouter(width=20.0, height=20.0, rules=DesignRules(grid_resolution=0.1))
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "N1"},
            {"number": "2", "x": 10.0, "y": 5.0, "net": 1, "net_name": "N1"},
        ],
    )
    router.use_waypoint_injection = False

    def _failing_edge_router(self, *args, **kwargs):
        time.sleep(0.2)  # spend part of the budget
        router.routing_failures.append(
            SimpleNamespace(net=1, failure_cause=FailureCause.PIN_ACCESS, analysis=None)
        )
        return []

    for meth in ("route_net", "route_net_star"):
        monkeypatch.setattr(MSTRouter, meth, _failing_edge_router)

    seen: dict[str, float | None] = {}

    def _subgrid(net, per_net_timeout=None):
        seen["subgrid"] = per_net_timeout
        return []

    def _via(net, per_net_timeout=None):
        seen["via"] = per_net_timeout
        return []

    monkeypatch.setattr(router, "_retry_net_with_subgrid", _subgrid)
    monkeypatch.setattr(router, "_resolve_via_conflicts_for_net", _via)

    router.route_net(1, per_net_timeout=10.0)

    for key in ("subgrid", "via"):
        assert seen[key] is not None, key
        assert wall_deadline.MIN_SEARCH_TIMEOUT_S <= seen[key] < 10.0, key


def test_sweep_wall_deadline_clamps_nested_search():
    with wall_deadline.deadline(0.05):
        time.sleep(0.06)
        assert wall_deadline.clamp_search_timeout(None) == wall_deadline.MIN_SEARCH_TIMEOUT_S
    assert wall_deadline.clamp_search_timeout(None) is None

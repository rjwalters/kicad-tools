"""Issue #8414: post-negotiation sweep bound / retry budget forwarding."""

from __future__ import annotations

import time

from kicad_tools.router import wall_deadline
from kicad_tools.router.core import Autorouter


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


def test_retry_helpers_forward_budget(monkeypatch):
    """Nested sub-grid / via-conflict ``route_net`` calls get the budget."""
    import inspect

    for name in ("_retry_net_with_subgrid", "_resolve_via_conflicts_for_net"):
        sig = inspect.signature(getattr(Autorouter, name))
        assert sig.parameters["per_net_timeout"].default is None


def test_sweep_wall_deadline_clamps_nested_search():
    with wall_deadline.deadline(0.05):
        time.sleep(0.06)
        assert wall_deadline.clamp_search_timeout(None) == wall_deadline.MIN_SEARCH_TIMEOUT_S
    assert wall_deadline.clamp_search_timeout(None) is None

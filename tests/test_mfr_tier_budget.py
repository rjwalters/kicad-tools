"""Fast unit tests for ``kicad_tools.cli.mfr_tier_budget`` (Issue #3463).

These pin the per-tier wall-clock budget slicing that lets the
``--auto-mfr-tier`` escalation ladder reserve routing budget for later
tiers instead of letting the base tier consume the entire routing
deadline (the regression that aborted escalation before ``jlcpcb-tier1``).

No routing is performed here -- the slow end-to-end chain lives in
``test_route_auto_mfr_tier_integration.py``.  This module exercises the
budget arithmetic and the context-manager restore semantics directly.
"""

from __future__ import annotations

import time
import types

from kicad_tools.cli.mfr_tier_budget import (
    _PER_TIER_FLOOR_SEC,
    per_tier_routing_budget,
    per_tier_routing_deadline,
)


def _args(routing_deadline: float | None) -> types.SimpleNamespace:
    return types.SimpleNamespace(_routing_deadline=routing_deadline)


class TestPerTierRoutingDeadline:
    def test_none_when_no_deadline_configured(self) -> None:
        """Unbounded runs (no --timeout) return None -- legacy behaviour."""
        assert per_tier_routing_deadline(_args(None), tier_index=0, tier_count=2) is None

    def test_first_of_two_tiers_gets_half(self) -> None:
        """The base tier of a two-tier ladder must NOT get the whole budget.

        This is the core #3463 fix: with two tiers and 600s remaining the
        base tier is sliced to ~300s, leaving ~300s for jlcpcb-tier1.
        """
        now = 1000.0
        remaining = 600.0
        args = _args(now + remaining)
        deadline = per_tier_routing_deadline(args, tier_index=0, tier_count=2, now=now)
        assert deadline is not None
        slice_seconds = deadline - now
        assert slice_seconds == 300.0

    def test_last_tier_gets_whole_remaining_budget(self) -> None:
        """The final outstanding tier is handed the full remaining budget."""
        now = 1000.0
        original = now + 600.0
        args = _args(original)
        deadline = per_tier_routing_deadline(args, tier_index=1, tier_count=2, now=now)
        assert deadline == original

    def test_single_tier_ladder_is_passthrough(self) -> None:
        """A one-tier ladder gets the full deadline (no reservation)."""
        now = 1000.0
        original = now + 600.0
        args = _args(original)
        deadline = per_tier_routing_deadline(args, tier_index=0, tier_count=1, now=now)
        assert deadline == original

    def test_slice_rolls_forward_when_earlier_tier_finishes_fast(self) -> None:
        """A fast base tier enlarges the slice available to the next tier.

        The slice is recomputed from the *current* remaining budget over
        the *current* outstanding-tier count, so unused budget rolls
        forward automatically.
        """
        # Three-tier ladder, 900s total.  Base tier sliced to 300s but
        # finishes in 60s, so the second tier (with 840s remaining over 2
        # outstanding tiers) is sliced to 420s -- more than its naive
        # 1/3 share of the original budget.
        now0 = 0.0
        args = _args(now0 + 900.0)
        first = per_tier_routing_deadline(args, tier_index=0, tier_count=3, now=now0)
        assert first - now0 == 300.0
        # Simulate the base tier consuming only 60s of wall clock.
        now1 = 60.0
        second = per_tier_routing_deadline(args, tier_index=1, tier_count=3, now=now1)
        # Remaining = 840s over 2 outstanding tiers -> 420s slice.
        assert second - now1 == 420.0

    def test_floor_guard_prevents_zero_budget_tier(self) -> None:
        """Many tiers + tight budget: a tier is never sliced below the floor
        (unless the total remaining is itself below the floor)."""
        now = 0.0
        # 5 tiers, only 100s remaining -> naive slice would be 20s, below
        # the 30s floor; the floor wins.
        args = _args(now + 100.0)
        deadline = per_tier_routing_deadline(args, tier_index=0, tier_count=5, now=now)
        assert deadline is not None
        assert deadline - now == _PER_TIER_FLOOR_SEC

    def test_already_expired_returns_original(self) -> None:
        """When the routing deadline has already passed, return it unchanged
        so the caller's existing ``_deadline_expired`` check fires."""
        now = 1000.0
        original = now - 5.0  # already in the past
        args = _args(original)
        deadline = per_tier_routing_deadline(args, tier_index=0, tier_count=2, now=now)
        assert deadline == original


class TestPerTierRoutingBudgetContextManager:
    def test_narrows_then_restores(self) -> None:
        """Inside the window the deadline is narrowed; on exit it is
        restored to the original total."""
        now = time.monotonic()
        original = now + 600.0
        args = _args(original)
        with per_tier_routing_budget(args, tier_index=0, tier_count=2):
            # Narrowed to roughly half (allowing for monotonic drift).
            assert args._routing_deadline < original
            assert args._routing_deadline <= now + 320.0
        # Restored exactly.
        assert args._routing_deadline == original

    def test_restores_even_on_exception(self) -> None:
        """The original deadline is restored even if the body raises."""
        original = time.monotonic() + 600.0
        args = _args(original)
        try:
            with per_tier_routing_budget(args, tier_index=0, tier_count=2):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert args._routing_deadline == original

    def test_noop_when_unbounded(self) -> None:
        """No deadline configured -> context manager leaves args untouched."""
        args = _args(None)
        with per_tier_routing_budget(args, tier_index=0, tier_count=2):
            assert args._routing_deadline is None
        assert args._routing_deadline is None

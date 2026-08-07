"""Unit tests for the relief-rescue sub-search budget selector (Issue #4536).

``Autorouter._relief_rescue`` is a TRANSACTION: it rolls back unless every
displaced victim re-lands, so the budget those re-land searches get decides
routed REACH, not just runtime.  The historical flat 10 s wall clock straddles
the 8-12 s natural re-land time on CI runners, which made board-06's
``REQUIRED_SIGNAL_REACH`` gate a coin flip on machine speed.  These tests pin
the selection table for the deterministic replacement.

The method only reads two attributes off ``self``, so it is exercised against a
lightweight stub rather than a full ``Autorouter`` (which would need a board).
"""

from __future__ import annotations

from kicad_tools.router.core import (
    RELIEF_SUBSEARCH_BUDGET_S,
    RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S,
    Autorouter,
)


class _BudgetStub:
    """Minimal stand-in exposing only the two attributes the method reads."""

    def __init__(self, per_net_iterations: int = 0, max_search_iterations: int = 0) -> None:
        self._per_net_iterations = per_net_iterations
        self._max_search_iterations = max_search_iterations


def _budget(stub: _BudgetStub, per_net_timeout: float | None, deterministic: bool) -> float | None:
    return Autorouter._relief_subsearch_budget(stub, per_net_timeout, deterministic)


class TestReliefSubsearchBudget:
    def test_default_is_the_historical_wall_clock(self):
        """No opt-in, no standing cap -> unchanged 10 s behavior."""
        assert _budget(_BudgetStub(), None, False) == RELIEF_SUBSEARCH_BUDGET_S

    def test_tighter_per_net_timeout_still_binds(self):
        """A caller-supplied per-net cap below 10 s keeps binding (pre-#4536)."""
        assert _budget(_BudgetStub(), 4.0, False) == 4.0

    def test_looser_per_net_timeout_does_not_relax_the_default(self):
        assert _budget(_BudgetStub(), 30.0, False) == RELIEF_SUBSEARCH_BUDGET_S

    def test_opt_in_without_expansion_cap_keeps_the_wall_clock(self):
        """Degrade to today's behavior, never to an unbounded search."""
        assert _budget(_BudgetStub(), None, True) == RELIEF_SUBSEARCH_BUDGET_S
        assert _budget(_BudgetStub(), 4.0, True) == 4.0

    def test_opt_in_with_expansion_cap_hands_over_to_the_node_budget(self):
        """The node-expansion cap becomes the binding bound; the wall clock
        left behind is the far-above-natural-runtime backstop, so it cannot
        decide whether the rescue commits."""
        stub = _BudgetStub(per_net_iterations=1_000_000, max_search_iterations=12_000_000)
        assert _budget(stub, None, True) == RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S
        # A tight per-net wall clock does NOT re-bind under the opt-in: the
        # whole point is that seconds stop deciding reach.
        assert _budget(stub, 4.0, True) == RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S

    def test_memory_backstop_alone_also_counts_as_deterministic(self):
        """``--deterministic-budget`` sets only ``_max_search_iterations``."""
        stub = _BudgetStub(max_search_iterations=12_000_000)
        assert _budget(stub, None, True) == RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S

    def test_backstop_is_far_above_the_natural_subsearch_time(self):
        """Guard the 'non-load-bearing' claim: the backstop must stay an order
        of magnitude above the measured 8-12 s natural re-land time."""
        assert RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S >= 10 * RELIEF_SUBSEARCH_BUDGET_S

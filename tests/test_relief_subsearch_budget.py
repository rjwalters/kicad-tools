"""Unit tests for the relief-rescue sub-search budget selector (Issues #4536, #4730).

``Autorouter._relief_rescue`` is a TRANSACTION: it rolls back unless every
displaced victim re-lands, so the budget those re-land searches get decides
routed REACH, not just runtime.  The historical flat 10 s wall clock straddles
the 8-12 s natural re-land time on CI runners, which made board-06's
``REQUIRED_SIGNAL_REACH`` gate a coin flip on machine speed.  These tests pin
the selection table for the deterministic replacement.

Issue #4730 made the deterministic bound the fleet DEFAULT
(:data:`DETERMINISTIC_RESCUE_DEFAULT`).  That is only safe because the selector
degrades to the historical wall clock whenever no per-net node-expansion cap is
active, so a capless run is behavior-identical BY CONSTRUCTION -- the property
the "inherited default" cases below lock down, alongside the signature defaults
and the routing-log line that reports which bound is live.

The methods only read two attributes off ``self``, so they are exercised against
a lightweight stub rather than a full ``Autorouter`` (which would need a board).
"""

from __future__ import annotations

import inspect

from kicad_tools.router.core import (
    DETERMINISTIC_RESCUE_DEFAULT,
    RELIEF_SUBSEARCH_BUDGET_S,
    RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S,
    Autorouter,
)


class _BudgetStub:
    """Minimal stand-in exposing only the two attributes the methods read."""

    # Borrowed unbound so the stub gets the real selection logic without a board.
    _relief_subsearch_budget = Autorouter._relief_subsearch_budget

    def __init__(self, per_net_iterations: int = 0, max_search_iterations: int = 0) -> None:
        self._per_net_iterations = per_net_iterations
        self._max_search_iterations = max_search_iterations


def _budget(stub: _BudgetStub, per_net_timeout: float | None, deterministic: bool) -> float | None:
    return Autorouter._relief_subsearch_budget(stub, per_net_timeout, deterministic)


def _line(stub: _BudgetStub, per_net_timeout: float | None, deterministic: bool) -> str:
    return Autorouter._relief_subsearch_bound_line(stub, per_net_timeout, deterministic)


class TestReliefSubsearchBudget:
    def test_opt_out_is_the_historical_wall_clock(self):
        """Explicit opt-out, no standing cap -> unchanged 10 s behavior."""
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


class TestDeterministicRescueDefault:
    """Issue #4730: the deterministic bound is the fleet default."""

    def test_route_all_negotiated_defaults_to_the_deterministic_bound(self):
        default = (
            inspect.signature(Autorouter.route_all_negotiated)
            .parameters["deterministic_rescue"]
            .default
        )
        assert default is DETERMINISTIC_RESCUE_DEFAULT
        assert DETERMINISTIC_RESCUE_DEFAULT is True

    def test_relief_rescue_defaults_to_the_deterministic_bound(self):
        """The recursion and the two-phase hook both land on this default."""
        default = (
            inspect.signature(Autorouter._relief_rescue).parameters["deterministic_rescue"].default
        )
        assert default is DETERMINISTIC_RESCUE_DEFAULT

    def test_two_phase_hook_arity_leaves_the_default_in_force(self):
        """The #3471 stall-relief hook in ``algorithms/two_phase.py`` calls
        ``_relief_rescue`` POSITIONALLY with 8 arguments, so the flag must stay
        behind them or that path would silently receive a victim count as its
        rescue mode."""
        params = list(inspect.signature(Autorouter._relief_rescue).parameters)
        assert params[0] == "self"
        assert params[1:9] == [
            "failed_net",
            "neg_router",
            "net_routes",
            "pads_by_net",
            "present_factor",
            "per_net_timeout",
            "flush_print_fn",
            "elapsed_fn",
        ]
        assert params.index("deterministic_rescue") >= 9

    def test_inherited_default_without_a_cap_keeps_the_wall_clock(self):
        """The safety property of the flip: capless runs are byte-identical to
        the pre-#4730 behavior because there is no deterministic bound to hand
        over to."""
        stub = _BudgetStub()
        assert _budget(stub, None, DETERMINISTIC_RESCUE_DEFAULT) == RELIEF_SUBSEARCH_BUDGET_S
        assert _budget(stub, 4.0, DETERMINISTIC_RESCUE_DEFAULT) == 4.0
        assert _budget(stub, None, DETERMINISTIC_RESCUE_DEFAULT) == _budget(stub, None, False)

    def test_inherited_default_with_a_cap_hands_over_to_the_node_budget(self):
        stub = _BudgetStub(per_net_iterations=1_000_000, max_search_iterations=12_000_000)
        assert (
            _budget(stub, None, DETERMINISTIC_RESCUE_DEFAULT) == RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S
        )


class TestReliefSubsearchBoundLine:
    """The routing-log line is the greppable A/B evidence for the flip."""

    def test_cap_active_reports_the_iteration_bound(self):
        stub = _BudgetStub(per_net_iterations=1_000_000, max_search_iterations=12_000_000)
        line = _line(stub, None, DETERMINISTIC_RESCUE_DEFAULT)
        # Load-bearing evidence token -- board CI logs are grepped for it.
        assert "iteration-bounded" in line
        assert "machine-independent" in line
        assert "requested" not in line

    def test_inherited_default_without_a_cap_does_not_claim_a_request(self):
        """#4730 acceptance: with a True default, an ordinary capless run never
        'requested' anything, so the wall-clock arm must stay neutral."""
        line = _line(_BudgetStub(), None, DETERMINISTIC_RESCUE_DEFAULT)
        assert "requested" not in line
        assert "iteration-bounded" not in line
        assert f"{RELIEF_SUBSEARCH_BUDGET_S:.1f}s wall clock" in line
        assert "no per-net node-expansion cap is active" in line
        assert "disabled by caller" not in line

    def test_opt_out_names_the_caller_decision(self):
        """With a True default, ``deterministic_rescue=False`` is necessarily
        explicit -- the only arm that may attribute the choice to a caller."""
        stub = _BudgetStub(per_net_iterations=1_000_000)
        line = _line(stub, None, False)
        assert "disabled by caller" in line
        assert "iteration-bounded" not in line
        assert f"{RELIEF_SUBSEARCH_BUDGET_S:.1f}s wall clock" in line

    def test_every_arm_carries_the_same_greppable_prefix(self):
        capped = _BudgetStub(per_net_iterations=1_000_000)
        for line in (
            _line(capped, None, DETERMINISTIC_RESCUE_DEFAULT),
            _line(_BudgetStub(), None, DETERMINISTIC_RESCUE_DEFAULT),
            _line(capped, None, False),
        ):
            assert line.startswith("  Relief-rescue sub-search cap: ")

"""Unit tests for the relief-rescue sub-search budget selector (Issues #4536, #4730).

``Autorouter._relief_rescue`` is a TRANSACTION: it rolls back unless every
displaced victim re-lands, so the budget those re-land searches get decides
routed REACH, not just runtime.  The historical flat 10 s wall clock straddles
the 8-12 s natural re-land time on CI runners, which made board-06's
``REQUIRED_SIGNAL_REACH`` gate a coin flip on machine speed.  These tests pin
the selection table for the deterministic replacement.

Issue #4730 made the deterministic bound the default of the NEGOTIATED entry
point (:data:`DETERMINISTIC_RESCUE_DEFAULT`).  That is only safe because the
selector degrades to the historical wall clock whenever no per-net
node-expansion cap is active, so a capless run is behavior-identical BY
CONSTRUCTION -- the property the "inherited default" cases below lock down,
alongside the signature defaults and the routing-log line that reports which
bound is live.

The TWO-PHASE entry point is scoped OUT of that flip
(:data:`TWO_PHASE_DETERMINISTIC_RESCUE_DEFAULT` is ``False``): board 07 -- the
only board whose two-phase route fires rescues -- came back from the fleet A/B
with a changed copper-LVS open set and routed-DRC 8 -> 13 against an allowlist
of 8, and #4730's Acceptance forbids absorbing that.  ``TestTwoPhasePathScoping``
pins both halves of the resolution: the path default stays historical, AND the
bound is threaded through as a real per-call switch (the hook calls
``_relief_rescue`` positionally, so a ``functools.partial`` is what makes an
opt-in reachable at all).

The methods only read two attributes off ``self``, so they are exercised against
a lightweight stub rather than a full ``Autorouter`` (which would need a board).
"""

from __future__ import annotations

import functools
import inspect
from unittest.mock import MagicMock

from kicad_tools.router import core
from kicad_tools.router.core import (
    DETERMINISTIC_RESCUE_DEFAULT,
    RELIEF_SUBSEARCH_BUDGET_S,
    RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S,
    TWO_PHASE_DETERMINISTIC_RESCUE_DEFAULT,
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

    def test_two_phase_hook_arity_keeps_the_flag_keyword_only_in_practice(self):
        """The #3471 stall-relief hook in ``algorithms/two_phase.py`` calls
        ``_relief_rescue`` POSITIONALLY with 8 arguments, so the flag must stay
        behind them -- otherwise that path would silently receive a victim
        count as its rescue mode, and the ``functools.partial`` that binds the
        two-phase path's own default (#4730) would collide with a positional
        argument."""
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
        assert "off for this route" not in line

    def test_off_arm_does_not_attribute_the_choice_to_one_source(self):
        """``deterministic_rescue=False`` reaches this arm two ways -- an
        explicit caller opt-out on the negotiated path, or
        ``TWO_PHASE_DETERMINISTIC_RESCUE_DEFAULT`` on the two-phase path -- so
        the wording must name both rather than blaming a caller."""
        stub = _BudgetStub(per_net_iterations=1_000_000)
        line = _line(stub, None, False)
        assert "off for this route" in line
        assert "caller opt-out" in line
        assert "two-phase path default" in line
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


class TestTwoPhasePathScoping:
    """Issue #4730: the flip is scoped OUT of the two-phase path (board 07's
    measured regression), and that path gets a real per-call switch."""

    def test_two_phase_default_is_the_historical_wall_clock(self):
        """The documented negative result: board 07's copper-LVS open set
        changed and routed-DRC went 8 -> 13 over an allowlist of 8 when this
        path inherited the flip, so it must stay off."""
        assert TWO_PHASE_DETERMINISTIC_RESCUE_DEFAULT is False
        assert TWO_PHASE_DETERMINISTIC_RESCUE_DEFAULT is not DETERMINISTIC_RESCUE_DEFAULT

    def test_route_all_two_phase_takes_the_flag_and_defaults_it_to_the_path_value(self):
        param = inspect.signature(Autorouter.route_all_two_phase).parameters.get(
            "deterministic_rescue"
        )
        assert param is not None, "the two-phase path must expose the opt-in/opt-out"
        assert param.default is TWO_PHASE_DETERMINISTIC_RESCUE_DEFAULT

    def test_escape_entry_points_forward_the_flag(self):
        """``kct route`` reaches the two-phase path through these, so the
        switch has to exist there or boards 03/04/07 cannot be opted in."""
        for method in (
            Autorouter.route_with_escape,
            Autorouter.route_with_escape_and_diffpairs,
        ):
            param = inspect.signature(method).parameters.get("deterministic_rescue")
            assert param is not None, f"{method.__name__} must forward the flag"
            assert param.default is TWO_PHASE_DETERMINISTIC_RESCUE_DEFAULT

    def test_create_two_phase_router_binds_the_bound_onto_the_positional_hook(self, monkeypatch):
        """The #3471 hook calls ``_relief_rescue`` positionally, so a plain
        bound method carries no bound at all -- the partial is the fix."""
        captured: dict = {}

        class _FakeTwoPhaseRouter:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(core, "TwoPhaseRouter", _FakeTwoPhaseRouter)
        stub = MagicMock()

        Autorouter._create_two_phase_router(stub)
        hook = captured["relief_rescue"]
        assert isinstance(hook, functools.partial)
        assert hook.keywords["deterministic_rescue"] is TWO_PHASE_DETERMINISTIC_RESCUE_DEFAULT

        # The 8 positional arguments the hook passes must still land on the
        # underlying method, with the bound arriving as a keyword.
        hook(1, 2, 3, 4, 5, 6, 7, 8)
        args, kwargs = stub._relief_rescue.call_args
        assert args == (1, 2, 3, 4, 5, 6, 7, 8)
        assert kwargs == {"deterministic_rescue": TWO_PHASE_DETERMINISTIC_RESCUE_DEFAULT}

    def test_create_two_phase_router_honors_an_explicit_opt_in(self, monkeypatch):
        captured: dict = {}

        class _FakeTwoPhaseRouter:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(core, "TwoPhaseRouter", _FakeTwoPhaseRouter)
        Autorouter._create_two_phase_router(MagicMock(), deterministic_rescue=True)
        assert captured["relief_rescue"].keywords["deterministic_rescue"] is True

    def test_route_all_two_phase_forwards_the_flag_to_hook_and_banner(self, monkeypatch):
        """Both the wiring and the log line must report the value THIS call
        runs under -- the banner previously hardcoded the module constant."""
        printed: list[str] = []
        monkeypatch.setattr(core, "flush_print", lambda line: printed.append(line))

        for requested in (False, True):
            printed.clear()
            stub = MagicMock()
            stub._relief_subsearch_bound_line.return_value = f"banner:{requested}"

            Autorouter.route_all_two_phase(stub, deterministic_rescue=requested)

            assert stub._create_two_phase_router.call_args.kwargs == {
                "deterministic_rescue": requested
            }
            assert stub._relief_subsearch_bound_line.call_args.args[1] is requested
            assert f"banner:{requested}" in printed

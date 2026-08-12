"""Unit tests for the relief-rescue sub-search budget selector (Issues #4536, #4730).

``Autorouter._relief_rescue`` is a TRANSACTION: it rolls back unless every
displaced victim re-lands, so the budget those re-land searches get decides
routed REACH, not just runtime.  The historical flat 10 s wall clock straddles
the 8-12 s natural re-land time on CI runners, which made board-06's
``REQUIRED_SIGNAL_REACH`` gate a coin flip on machine speed.  These tests pin
the selection table for the deterministic replacement.

Issue #4730 proposed making the deterministic bound the fleet default and
WITHDREW the flip: board 07 -- routed through the NEGOTIATED entry point --
came back from the A/B with a changed copper-LVS open set and routed-DRC
8 -> 13 against an allowlist of 8, and #4730's Acceptance forbids absorbing
that.  So :data:`DETERMINISTIC_RESCUE_DEFAULT` is ``False`` on every entry
point (``TestDeterministicRescueDefault``) and board 06 keeps its explicit
opt-in.

Issue #4770 measured why and made the opt-in PERMANENT: the bound does not
buy board 07 a net, it loses two (26/31 -> 24/31 in the negotiated pass), and
the reported ``+1 net / +5 DRC`` comes from the placement-delta feedback
loop's relative accept-gate downstream.  ``TestPlacementDeltaFeedbackCaveat``
pins the two structural claims that finding rests on.

What the issue did land is the per-call switch, pinned by
``TestPerCallThreading``: every entry point that can reach a rescue takes
``deterministic_rescue``, and the two-phase stall-relief hook -- which calls
``_relief_rescue`` POSITIONALLY -- carries it on a ``functools.partial``, so a
future flip is a value change rather than a rewiring job.

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
    Autorouter,
)


class _BudgetStub:
    """Minimal stand-in exposing only the two attributes the methods read."""

    # Borrowed unbound so the stub gets the real selection logic without a board.
    _relief_subsearch_budget = Autorouter._relief_subsearch_budget
    # Issue #4724 extracted the "is a node-expansion cap active?" question into
    # a shared helper so the relief rescue and the post-negotiation sweep
    # cannot drift on what deterministic mode means; the selector calls it on
    # ``self``, so the stub borrows it too.
    _active_expansion_cap = Autorouter._active_expansion_cap

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
    """Issue #4730: the deterministic bound stays OPT-IN, on every path."""

    def test_the_flip_stayed_withdrawn(self):
        """The measured negative result: with the bound on by default, board
        07's copper-LVS open set changed and its routed-DRC went 8 -> 13 over
        an allowlist of 8 that main sits exactly at (CI runs 31281260812 and
        31283738762 vs main's 31277512785).  #4730's Acceptance forbids
        absorbing that, so the default must remain ``False`` until board 07's
        rescue outcome is understood."""
        assert DETERMINISTIC_RESCUE_DEFAULT is False

    def test_route_all_negotiated_uses_the_shared_default(self):
        default = (
            inspect.signature(Autorouter.route_all_negotiated)
            .parameters["deterministic_rescue"]
            .default
        )
        assert default is DETERMINISTIC_RESCUE_DEFAULT

    def test_relief_rescue_uses_the_shared_default(self):
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
        bound (#4730) would collide with a positional argument."""
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

    def test_default_selects_exactly_the_historical_bound(self):
        """The safety property of the withdrawal: an inherited default is
        indistinguishable from an explicit opt-out, capped or not."""
        for stub in (
            _BudgetStub(),
            _BudgetStub(per_net_iterations=1_000_000, max_search_iterations=12_000_000),
        ):
            assert _budget(stub, None, DETERMINISTIC_RESCUE_DEFAULT) == _budget(stub, None, False)
            assert _budget(stub, 4.0, DETERMINISTIC_RESCUE_DEFAULT) == _budget(stub, 4.0, False)
        assert _budget(_BudgetStub(), None, DETERMINISTIC_RESCUE_DEFAULT) == (
            RELIEF_SUBSEARCH_BUDGET_S
        )


class TestPlacementDeltaFeedbackCaveat:
    """Issue #4770: doc-drift guards for the two claims the A/B rests on.

    The ``DETERMINISTIC_RESCUE_DEFAULT`` docblock tells the next person two
    things that are only true as long as the code below stays as it is:

    1. reproduce the A/B by flipping the CONSTANT, because board 07's
       re-route passes go through :class:`PlacementDeltaFeedbackLoop`, which
       forwards no ``deterministic_rescue`` -- so a CLI-level wiring is a
       DIFFERENT experiment; and
    2. the #4159 post-negotiation sweep bound is identical in both arms by
       construction, which is what excludes it as a confound.

    Neither is a behaviour this repo wants to freeze -- they are statements
    the docblock makes.  If a future change falsifies one, this class goes
    red so the docblock is corrected in the same PR instead of rotting into
    a confidently wrong explanation.
    """

    def test_placement_delta_loop_forwards_no_deterministic_rescue(self):
        """Claim 1.  If this fails, ``PlacementDeltaFeedbackLoop`` gained the
        parameter -- update the docblock's "flip the constant, not the CLI"
        paragraph, because a CLI wiring would then reach board 07's re-routes
        and the two experiments would finally be equivalent."""
        from kicad_tools.router.placement_feedback import PlacementDeltaFeedbackLoop

        params = inspect.signature(PlacementDeltaFeedbackLoop.run_delta).parameters
        assert "deterministic_rescue" not in params

    def test_sweep_bound_cannot_see_the_rescue_flag(self):
        """Claim 2.  ``_post_negotiation_sweep_bounds`` takes only
        ``(timeout, per_net_timeout, elapsed, timed_out)``, so #4770's A/B
        arms print the same ``Post-negotiation sweep bound:`` line (confirmed
        empirically: ``60.0s whole-sweep / 10.0s per-net`` in both) and the
        #4159 sweep is excluded as a cause of the ``+1 net / +5 DRC``."""
        params = inspect.signature(Autorouter._post_negotiation_sweep_bounds).parameters
        assert list(params)[1:] == ["timeout", "per_net_timeout", "elapsed", "timed_out"]
        assert "deterministic_rescue" not in params


class TestReliefSubsearchBoundLine:
    """The routing-log line is the greppable A/B evidence for the bound."""

    def test_cap_active_reports_the_iteration_bound(self):
        stub = _BudgetStub(per_net_iterations=1_000_000, max_search_iterations=12_000_000)
        line = _line(stub, None, True)
        # Load-bearing evidence token -- board CI logs are grepped for it.
        assert "iteration-bounded" in line
        assert "machine-independent" in line
        assert "requested" not in line

    def test_opt_in_without_a_cap_states_the_fact_not_a_request(self):
        """This arm is reached by an opt-in run that simply has no expansion
        cap to hand the sub-searches to, so it reports the bound rather than
        attributing a decision."""
        line = _line(_BudgetStub(), None, True)
        assert "requested" not in line
        assert "iteration-bounded" not in line
        assert f"{RELIEF_SUBSEARCH_BUDGET_S:.1f}s wall clock" in line
        assert "no per-net node-expansion cap is active" in line
        assert "off for this route" not in line

    def test_off_arm_names_the_default_and_the_way_in(self):
        """``deterministic_rescue=False`` is what ordinary runs inherit, so the
        wording must not blame a caller -- it says this is the default and how
        to opt in."""
        stub = _BudgetStub(per_net_iterations=1_000_000)
        line = _line(stub, None, DETERMINISTIC_RESCUE_DEFAULT)
        assert "off for this route" in line
        assert "the default" in line
        assert "deterministic_rescue=True" in line
        assert "iteration-bounded" not in line
        assert f"{RELIEF_SUBSEARCH_BUDGET_S:.1f}s wall clock" in line

    def test_every_arm_carries_the_same_greppable_prefix(self):
        capped = _BudgetStub(per_net_iterations=1_000_000)
        for line in (
            _line(capped, None, True),
            _line(_BudgetStub(), None, True),
            _line(capped, None, DETERMINISTIC_RESCUE_DEFAULT),
        ):
            assert line.startswith("  Relief-rescue sub-search cap: ")


class TestPerCallThreading:
    """Issue #4730's durable half: every path that can reach a rescue carries
    the bound per call, so opting one in is a measured value change."""

    def test_route_all_two_phase_takes_the_flag(self):
        param = inspect.signature(Autorouter.route_all_two_phase).parameters.get(
            "deterministic_rescue"
        )
        assert param is not None, "the two-phase path must expose the opt-in/opt-out"
        assert param.default is DETERMINISTIC_RESCUE_DEFAULT

    def test_escape_entry_points_forward_the_flag(self):
        """``kct route`` reaches the two-phase path through these, so the
        switch has to exist there or boards 03/04/07 cannot be opted in."""
        for method in (
            Autorouter.route_with_escape,
            Autorouter.route_with_escape_and_diffpairs,
        ):
            param = inspect.signature(method).parameters.get("deterministic_rescue")
            assert param is not None, f"{method.__name__} must forward the flag"
            assert param.default is DETERMINISTIC_RESCUE_DEFAULT

    def test_hierarchical_delegation_forwards_an_explicit_opt_in(self):
        """``route_all_negotiated(hierarchical=True)`` hands the whole route to
        the two-phase path; dropping the flag there would silently discard a
        caller's opt-in."""
        stub = MagicMock()
        stub._negotiated_timeout_cap = None

        Autorouter.route_all_negotiated(stub, hierarchical=True, deterministic_rescue=True)

        assert stub.route_all_two_phase.call_args.kwargs["deterministic_rescue"] is True

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
        assert hook.keywords["deterministic_rescue"] is DETERMINISTIC_RESCUE_DEFAULT

        # The 8 positional arguments the hook passes must still land on the
        # underlying method, with the bound arriving as a keyword.
        hook(1, 2, 3, 4, 5, 6, 7, 8)
        args, kwargs = stub._relief_rescue.call_args
        assert args == (1, 2, 3, 4, 5, 6, 7, 8)
        assert kwargs == {"deterministic_rescue": DETERMINISTIC_RESCUE_DEFAULT}

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
        runs under -- the banner must not fall back to the module constant."""
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

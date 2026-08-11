"""Unit tests for the post-negotiation sweep bound selector (Issue #4724).

The #4159 rescue sweep gives every still-stranded net ONE solo attempt on the
live grid, gated by two wall clocks: ``POST_NEGOTIATION_SWEEP_BUDGET_S`` (60 s
for the whole pass) and ``POST_NEGOTIATION_SWEEP_PER_NET_S`` (10 s per net).
Both decide WHICH nets get rescued -- routed reach -- and both were applied
even to callers running unbudgeted (``timeout=None`` / ``per_net_timeout=None``)
in the deterministic-budget mode #4536 established precisely so that no wall
clock decides anything.  A per-net search that runs to its 1M-expansion cap
costs ~11 s on a CI runner, so the 10 s per-net bound straddles it the same way
the relief rescue's 10 s sub-search budget did (#4536).

These tests pin the selection table.  Budgeted callers must be unchanged --
that is the compatibility half of the fix, and the reason the new arm is keyed
on ``timeout is None`` AND an active node-expansion cap rather than on a new
flag.  The selector reads only two attributes off ``self``, so it is exercised
against a lightweight stub, mirroring ``tests/test_relief_subsearch_budget.py``.
"""

from __future__ import annotations

import inspect

from kicad_tools.router.core import (
    POST_NEGOTIATION_SWEEP_BUDGET_S,
    POST_NEGOTIATION_SWEEP_PER_NET_S,
    POST_NEGOTIATION_SWEEP_PER_NET_SAFETY_BACKSTOP_S,
    POST_NEGOTIATION_SWEEP_SAFETY_BACKSTOP_S,
    Autorouter,
)

# The cap values ``--deterministic-budget`` installs (route_cmd.py).
DETERMINISTIC_PER_NET_ITERATIONS = 1_000_000
DETERMINISTIC_MAX_SEARCH_ITERATIONS = 12_000_000


class _BoundsStub:
    """Minimal stand-in exposing only the two attributes the selector reads."""

    _active_expansion_cap = Autorouter._active_expansion_cap
    _post_negotiation_sweep_bounds = Autorouter._post_negotiation_sweep_bounds
    _post_negotiation_sweep_bound_line = Autorouter._post_negotiation_sweep_bound_line

    def __init__(self, per_net_iterations: int = 0, max_search_iterations: int = 0) -> None:
        self._per_net_iterations = per_net_iterations
        self._max_search_iterations = max_search_iterations


def _capped() -> _BoundsStub:
    return _BoundsStub(
        per_net_iterations=DETERMINISTIC_PER_NET_ITERATIONS,
        max_search_iterations=DETERMINISTIC_MAX_SEARCH_ITERATIONS,
    )


class TestActiveExpansionCap:
    def test_uncapped_router_reports_zero(self):
        assert _BoundsStub()._active_expansion_cap() == 0

    def test_the_smaller_of_the_two_caps_binds(self):
        assert _capped()._active_expansion_cap() == DETERMINISTIC_PER_NET_ITERATIONS

    def test_memory_backstop_alone_counts(self):
        """An explicit ``--max-search-iterations`` without a per-net cap."""
        stub = _BoundsStub(max_search_iterations=DETERMINISTIC_MAX_SEARCH_ITERATIONS)
        assert stub._active_expansion_cap() == DETERMINISTIC_MAX_SEARCH_ITERATIONS

    def test_relief_rescue_asks_the_same_question(self):
        """#4724 extracted this helper OUT of ``_relief_subsearch_budget``.

        A second copy of the ``min()`` is how two budget selectors drift into
        disagreeing about what "deterministic mode" means, so pin that the
        relief selector still routes through the shared helper.
        """
        source = inspect.getsource(Autorouter._relief_subsearch_budget)
        assert "_active_expansion_cap()" in source


class TestUnbudgetedCaller:
    """``timeout=None`` + ``per_net_timeout=None`` -- board 06's regen."""

    def test_with_expansion_cap_the_wall_clocks_stop_being_load_bearing(self):
        budget, per_net = _capped()._post_negotiation_sweep_bounds(None, None, 0.0, False)
        assert budget == POST_NEGOTIATION_SWEEP_SAFETY_BACKSTOP_S
        assert per_net == POST_NEGOTIATION_SWEEP_PER_NET_SAFETY_BACKSTOP_S

    def test_the_backstops_are_far_above_the_natural_times_they_replace(self):
        """~10x, matching ``RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S``'s sizing.

        A backstop that merely nudges the old value up would still straddle
        the ~11 s capped-search time and keep deciding reach.
        """
        assert POST_NEGOTIATION_SWEEP_SAFETY_BACKSTOP_S >= 10 * POST_NEGOTIATION_SWEEP_BUDGET_S
        assert (
            POST_NEGOTIATION_SWEEP_PER_NET_SAFETY_BACKSTOP_S
            >= 10 * POST_NEGOTIATION_SWEEP_PER_NET_S
        )

    def test_elapsed_time_cannot_move_the_deterministic_arm(self):
        """The whole point: no ``time.time()``-derived value reaches the bound."""
        first = _capped()._post_negotiation_sweep_bounds(None, None, 0.0, False)
        later = _capped()._post_negotiation_sweep_bounds(None, None, 9_999.0, False)
        timed_out = _capped()._post_negotiation_sweep_bounds(None, None, 9_999.0, True)
        assert first == later == timed_out

    def test_without_an_expansion_cap_the_historical_bounds_are_kept(self):
        """Degrade to today's behaviour, never to an unbounded sweep."""
        budget, per_net = _BoundsStub()._post_negotiation_sweep_bounds(None, None, 0.0, False)
        assert budget == POST_NEGOTIATION_SWEEP_BUDGET_S
        assert per_net == POST_NEGOTIATION_SWEEP_PER_NET_S

    def test_an_explicit_per_net_wall_clock_opts_back_out(self):
        """A caller that asked for seconds still gets seconds."""
        budget, per_net = _capped()._post_negotiation_sweep_bounds(None, 4.0, 0.0, False)
        assert budget == POST_NEGOTIATION_SWEEP_BUDGET_S
        assert per_net == 4.0


class TestBudgetedCallerIsUnchanged:
    """Every ``timeout``-carrying caller keeps the exact pre-#4724 numbers."""

    def test_remaining_stage_budget_clamps_the_sweep(self):
        budget, per_net = _capped()._post_negotiation_sweep_bounds(600.0, None, 570.0, False)
        assert budget == 30.0
        assert per_net == POST_NEGOTIATION_SWEEP_PER_NET_S

    def test_ample_remaining_budget_yields_the_flat_ceiling(self):
        budget, _ = _capped()._post_negotiation_sweep_bounds(600.0, None, 10.0, False)
        assert budget == POST_NEGOTIATION_SWEEP_BUDGET_S

    def test_a_timed_out_loop_gets_the_contained_allowance(self):
        budget, _ = _capped()._post_negotiation_sweep_bounds(600.0, None, 610.0, True)
        assert budget == POST_NEGOTIATION_SWEEP_BUDGET_S

    def test_overrun_without_the_timeout_flag_never_goes_negative(self):
        budget, _ = _capped()._post_negotiation_sweep_bounds(600.0, None, 610.0, False)
        assert budget == POST_NEGOTIATION_SWEEP_BUDGET_S

    def test_a_tight_per_net_timeout_still_binds(self):
        _, per_net = _capped()._post_negotiation_sweep_bounds(600.0, 4.0, 0.0, False)
        assert per_net == 4.0

    def test_a_loose_per_net_timeout_does_not_relax_the_cap(self):
        _, per_net = _capped()._post_negotiation_sweep_bounds(600.0, 30.0, 0.0, False)
        assert per_net == POST_NEGOTIATION_SWEEP_PER_NET_S


class TestBoundLine:
    """The routing log must say which bound was live (#4536 evidence rule)."""

    def test_deterministic_arm_is_greppable(self):
        line = _capped()._post_negotiation_sweep_bound_line(None, None, 0.0, False)
        assert "Post-negotiation sweep bound" in line
        assert "iteration-bounded" in line
        assert "#4724" in line

    def test_capless_arm_names_the_missing_cap(self):
        line = _BoundsStub()._post_negotiation_sweep_bound_line(None, None, 0.0, False)
        assert "iteration-bounded" not in line
        assert "no per-net node-expansion cap is active" in line

    def test_budgeted_arm_names_the_stage_timeout(self):
        line = _capped()._post_negotiation_sweep_bound_line(600.0, None, 570.0, False)
        assert "iteration-bounded" not in line
        assert "stage timeout" in line
        assert "30.0s whole-sweep" in line

    def test_explicit_per_net_wall_clock_arm_says_so(self):
        line = _capped()._post_negotiation_sweep_bound_line(None, 4.0, 0.0, False)
        assert "explicit per-net wall clock" in line


# ---------------------------------------------------------------------------
# End-to-end wiring: the bound the loop SELECTS is the bound it REPORTS
# ---------------------------------------------------------------------------


def _build_starved_router(**kwargs) -> Autorouter:
    """A long-haul net the batch loop cannot land, plus an easy net.

    Mirrors ``tests/router/test_post_negotiation_sweep.py``: patching the
    batch's internal per-net path strands net 1 while leaving the sweep's own
    ``route_net`` path real, so the sweep actually engages.
    """
    router = Autorouter(width=40.0, height=20.0, **kwargs)
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 2.0, "y": 10.0, "net": 1, "net_name": "LONGHAUL"},
            {"number": "2", "x": 38.0, "y": 10.0, "net": 1, "net_name": "LONGHAUL"},
        ],
    )
    router.add_component(
        "R2",
        [
            {"number": "1", "x": 10.0, "y": 2.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 10.0, "y": 18.0, "net": 2, "net_name": "NET2"},
        ],
    )
    orig = router._route_net_negotiated

    def patched(net, pf, per_net_timeout=None):
        if net == 1:
            return []
        return orig(net, pf, per_net_timeout=per_net_timeout)

    router._route_net_negotiated = patched
    return router


class TestLoopWiring:
    def test_unbudgeted_capped_run_reports_and_uses_the_iteration_bound(self, capsys):
        ar = _build_starved_router(
            max_search_iterations=DETERMINISTIC_MAX_SEARCH_ITERATIONS,
            per_net_iterations=DETERMINISTIC_PER_NET_ITERATIONS,
        )
        routes = ar.route_all_negotiated(
            max_iterations=2, timeout=None, adaptive=False, perturbation=False
        )
        out = capsys.readouterr().out
        assert "Post-negotiation sweep bound: iteration-bounded" in out
        # The sweep still does its job under the new bound.
        assert {r.net for r in routes} == {1, 2}

    def test_budgeted_run_still_reports_the_wall_clock(self, capsys):
        ar = _build_starved_router(
            max_search_iterations=DETERMINISTIC_MAX_SEARCH_ITERATIONS,
            per_net_iterations=DETERMINISTIC_PER_NET_ITERATIONS,
        )
        ar.route_all_negotiated(max_iterations=2, timeout=30.0, adaptive=False, perturbation=False)
        out = capsys.readouterr().out
        # Isolate the sweep's own line: the run also prints the #3881 per-net
        # A* cap line, which legitimately says "iteration-bounded" too.
        sweep_line = next(ln for ln in out.splitlines() if "Post-negotiation sweep bound" in ln)
        assert "iteration-bounded" not in sweep_line
        assert "stage timeout" in sweep_line

    def test_no_stranded_nets_prints_no_bound_line(self, capsys):
        """The sweep block is skipped entirely, so the log is unchanged."""
        ar = Autorouter(width=40.0, height=20.0)
        ar.add_component(
            "R2",
            [
                {"number": "1", "x": 10.0, "y": 2.0, "net": 2, "net_name": "NET2"},
                {"number": "2", "x": 10.0, "y": 18.0, "net": 2, "net_name": "NET2"},
            ],
        )
        ar.route_all_negotiated(max_iterations=2, timeout=None, adaptive=False, perturbation=False)
        assert "Post-negotiation sweep bound" not in capsys.readouterr().out

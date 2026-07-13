"""Issue #4095: surface budget-exited diff pairs to the CLI.

Background: ``DiffPairRouter.route_all_with_diffpairs`` routes differential
pairs through the ``CoupledPathfinder`` first, then defers any pair whose
coupled A* hits its per-pair or aggregate budget to the single-ended main
strategy (the #3089/#3439 budget-exit fallback).  The fallback is correct
-- no net goes unrouted -- but on bundle-dense boards the coupled attempts
can *regress* completion / DRC vs. a plain single-ended route (board 07:
34 vs 13 DRC errors, 22/31 vs 26/31 nets; epic #4049 closeout).

Before this issue the ``budget_exit_diff_nets`` set that drives the
fallback + the #3270 net-priority promotion was purely local to
``route_all_with_diffpairs`` -- never returned, never surfaced to the CLI.
Phase 1 (this issue) is warn-only + instrumentation:

* ``DiffPairRouter._last_budget_exit_pair_names`` records the base name of
  every pair that budget-exited during the most recent call.
* ``Autorouter.diffpair_budget_exit_pair_names()`` exposes that to the CLI.
* The CLI emits an unconditional (``not quiet``) "Differential Pair
  Budget-Exit Warning" naming the pairs and the regression risk.

These tests pin the boundary at the cheapest surface: the orchestrator
return-value / instance attribute and the Autorouter accessor.  Checkpoint-
and-compare (auto-fallback to the single-ended result) is an explicit
follow-up, NOT this issue.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.rules import DesignRules, NetClassRouting


def _opt_in_diffpair_class_map(net_names: list[str]) -> dict[str, NetClassRouting]:
    nc = NetClassRouting(name="HighSpeedOptIn", coupled_routing=True)
    return dict.fromkeys(net_names, nc)


def _two_pad_diffpair_router() -> Autorouter:
    """30x10mm board with one straight two-pad diff pair plus one plain net.

    Mirrors the fixture in ``tests/test_diffpair_aggregate_cap.py`` so the
    aggregate-budget budget-exit path is exercised the same way.
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(
        width=30.0,
        height=10.0,
        rules=rules,
        net_class_map=_opt_in_diffpair_class_map(["USB_D+", "USB_D-"]),
    )
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 4.6,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 5.0,
                "y": 5.4,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
            {
                "number": "3",
                "x": 5.0,
                "y": 8.0,
                "width": 0.4,
                "height": 0.4,
                "net": 3,
                "net_name": "GPIO1",
            },
        ],
    )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 25.0,
                "y": 4.6,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 25.0,
                "y": 5.4,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
            {
                "number": "3",
                "x": 25.0,
                "y": 8.0,
                "width": 0.4,
                "height": 0.4,
                "net": 3,
                "net_name": "GPIO1",
            },
        ],
    )
    return router


# ---------------------------------------------------------------------------
# Surface + accessor existence
# ---------------------------------------------------------------------------


def test_autorouter_exposes_budget_exit_accessor():
    """``Autorouter`` exposes the budget-exit pair names the CLI reads."""
    router = _two_pad_diffpair_router()
    # Before any diff-pair routing the accessor is empty (and does not
    # auto-initialise the lazy DiffPairRouter).
    assert router.diffpair_budget_exit_pair_names() == []


# ---------------------------------------------------------------------------
# Budget-exit surfaces the named pair
# ---------------------------------------------------------------------------


def test_aggregate_budget_exit_surfaces_pair_name():
    """A pair deferred by an exhausted aggregate budget appears by name in
    both the orchestrator instance attribute and the Autorouter accessor."""
    router = _two_pad_diffpair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    routes, _warnings = router._diffpair.route_all_with_diffpairs(
        config,
        aggregate_timeout=0.001,  # near-zero -> defer before coupled A*
    )

    # The single detected pair ("USB_D") budget-exited to single-ended.
    assert router._diffpair._last_budget_exit_pair_names == ["USB_D"], (
        "route_all_with_diffpairs must record the budget-exited pair by "
        f"name; saw {router._diffpair._last_budget_exit_pair_names}"
    )
    # And it is reachable through the Autorouter accessor the CLI uses.
    assert router.diffpair_budget_exit_pair_names() == ["USB_D"]

    # Fallback behaviour is unchanged: every net (incl. the diff-pair
    # members) is still routed by the main per-net strategy.
    routed_nets = {r.net for r in routes}
    assert {1, 2, 3} <= routed_nets


def test_budget_exit_instrumentation_counters():
    """Instrumentation counters are populated for a future checkpoint-and-
    compare follow-up to key on."""
    router = _two_pad_diffpair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    router._diffpair.route_all_with_diffpairs(config, aggregate_timeout=0.001)

    # One pair deferred before ever reaching the coupled A*.
    assert router._diffpair._last_budget_exit_count == 1
    assert router._diffpair._last_coupled_attempted_count == 0


def test_budget_exit_log_line_emitted(caplog):
    """A structured, greppable log line names the budget-exited pairs."""
    import logging

    router = _two_pad_diffpair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    with caplog.at_level(logging.WARNING):
        router._diffpair.route_all_with_diffpairs(config, aggregate_timeout=0.001)

    matching = [r for r in caplog.records if "DIFFPAIR_BUDGET_EXIT_FALLBACK" in r.getMessage()]
    assert matching, "expected a DIFFPAIR_BUDGET_EXIT_FALLBACK instrumentation log line"
    assert "USB_D" in matching[0].getMessage()


# ---------------------------------------------------------------------------
# Negative cases: coupled success and zero-pairs must NOT surface a warning
# ---------------------------------------------------------------------------


def test_coupled_success_surfaces_no_budget_exit():
    """When the pair routes coupled within budget (no aggregate cap), the
    budget-exit surface stays empty -- mirroring board 06 behaviour."""
    router = _two_pad_diffpair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    routes, _warnings = router._diffpair.route_all_with_diffpairs(config)

    assert router._diffpair._last_budget_exit_pair_names == []
    assert router.diffpair_budget_exit_pair_names() == []
    assert router._diffpair._last_budget_exit_count == 0
    # Sanity: the pair actually routed.
    assert {1, 2, 3} <= {r.net for r in routes}


def test_no_pairs_detected_surfaces_no_budget_exit():
    """A ``--differential-pairs`` run on a board with no detectable pairs
    hits the early-return path and surfaces no budget-exit warning."""
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(width=30.0, height=10.0, rules=rules)
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 5.0,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "GPIO1",
            },
        ],
    )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 25.0,
                "y": 5.0,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "GPIO1",
            },
        ],
    )
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    router._diffpair.route_all_with_diffpairs(config)

    assert router._diffpair._last_budget_exit_pair_names == []
    assert router.diffpair_budget_exit_pair_names() == []

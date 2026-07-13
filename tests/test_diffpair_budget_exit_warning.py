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


# ---------------------------------------------------------------------------
# Issue #4107: collapse skips the #3270 promotion; partial exit keeps it
#
# On budget-exit the coupled pass returns ``[], None`` BEFORE any grid
# commit, so on the all-pairs collapse the grid is pristine and the ONLY
# divergence from a plain single-ended route is the #3270 net-priority
# promotion (``_budget_exit_diff_nets`` -> ``complexity_tier = -1``).  The
# #4107 gate leaves ``_budget_exit_diff_nets`` EMPTY on the collapse
# signature so the single-ended pass runs with default ordering.  The
# promotion set is cleared again after the strategy callback returns
# (#3270 leak guard), so we observe it AT THE STRATEGY-CALL MOMENT via a
# ``non_diffpair_strategy`` callable that records it -- exactly the vantage
# point the real negotiated strategy sees.
# ---------------------------------------------------------------------------


def _hard_single_pair_router() -> Autorouter:
    """One diff pair whose long diagonal span never couples within a
    microscopic per-pair budget (forces the coupled A* to budget-exit),
    plus one plain net.  Drives the ALL-PAIRS collapse signature:
    ``coupled_attempted_count == 1``, ``coupled_routed_nets`` empty, the
    single considered pair in ``budget_exit_pair_names``.
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(
        width=40.0,
        height=30.0,
        rules=rules,
        net_class_map=_opt_in_diffpair_class_map(["DP+", "DP-"]),
    )
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 2.0,
                "y": 2.0,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "DP+",
            },
            {
                "number": "2",
                "x": 2.0,
                "y": 2.8,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "DP-",
            },
            {
                "number": "3",
                "x": 2.0,
                "y": 15.0,
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
                "x": 38.0,
                "y": 28.0,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "DP+",
            },
            {
                "number": "2",
                "x": 38.0,
                "y": 28.8,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "DP-",
            },
            {
                "number": "3",
                "x": 38.0,
                "y": 15.0,
                "width": 0.4,
                "height": 0.4,
                "net": 3,
                "net_name": "GPIO1",
            },
        ],
    )
    return router


def _one_easy_one_hard_pair_router() -> Autorouter:
    """Two diff pairs: an EASY pair with a short straight span that couples
    fast, and a HARD pair on a long diagonal that budget-exits.  With one
    pair coupling, ``coupled_routed_nets`` is non-empty -> the collapse
    signature does NOT hold -> the #3270 promotion still runs for the hard
    pair's nets.  This is design (4)'s "partial budget-exit does NOT trigger
    the skip".
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(
        width=40.0,
        height=30.0,
        rules=rules,
        net_class_map=_opt_in_diffpair_class_map(["E+", "E-", "H+", "H-"]),
    )
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
                "net_name": "E+",
            },
            {
                "number": "2",
                "x": 5.0,
                "y": 5.8,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "E-",
            },
            {
                "number": "3",
                "x": 2.0,
                "y": 2.0,
                "width": 0.4,
                "height": 0.4,
                "net": 3,
                "net_name": "H+",
            },
            {
                "number": "4",
                "x": 2.0,
                "y": 2.8,
                "width": 0.4,
                "height": 0.4,
                "net": 4,
                "net_name": "H-",
            },
        ],
    )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 12.0,
                "y": 5.0,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "E+",
            },
            {
                "number": "2",
                "x": 12.0,
                "y": 5.8,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "E-",
            },
            {
                "number": "3",
                "x": 38.0,
                "y": 28.0,
                "width": 0.4,
                "height": 0.4,
                "net": 3,
                "net_name": "H+",
            },
            {
                "number": "4",
                "x": 38.0,
                "y": 28.8,
                "width": 0.4,
                "height": 0.4,
                "net": 4,
                "net_name": "H-",
            },
        ],
    )
    return router


def test_collapse_skips_budget_exit_promotion():
    """When ALL considered coupled pairs budget-exit and none couple, the
    strategy runs with ``_budget_exit_diff_nets`` empty (no #3270 promotion)
    so the single-ended pass keeps its default ordering."""
    router = _hard_single_pair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    promotion_at_strategy: dict[str, set[int]] = {}

    def _record_promotion() -> list:
        # The #3270 promotion set is live only during the strategy call;
        # snapshot it here (the vantage point the negotiated strategy sees).
        promotion_at_strategy["nets"] = set(router._budget_exit_diff_nets)
        return []

    router._diffpair.route_all_with_diffpairs(
        config,
        coupled_only=True,
        per_pair_timeout=0.001,  # microscopic -> the pair's coupled A* budget-exits
        non_diffpair_strategy=_record_promotion,
    )

    # The single pair reached the coupled A* and budget-exited (collapse).
    assert router._diffpair._last_coupled_attempted_count == 1
    assert router._diffpair._last_budget_exit_pair_names == ["DP"]
    # THE GUARANTEE: promotion set empty at the strategy call -> pristine
    # single-ended-equivalent ordering.
    assert promotion_at_strategy["nets"] == set(), (
        "On the all-pairs collapse the #3270 promotion must be skipped; "
        f"strategy saw _budget_exit_diff_nets={promotion_at_strategy['nets']}"
    )


def test_collapse_still_fires_budget_exit_warning(caplog):
    """Instrumentation is NOT gated by the collapse skip: the operator is
    still told the pairs fell back (Phase-1 warning + accessor + the
    ``DIFFPAIR_BUDGET_EXIT_FALLBACK`` log)."""
    import logging

    router = _hard_single_pair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    with caplog.at_level(logging.WARNING):
        router._diffpair.route_all_with_diffpairs(
            config,
            coupled_only=True,
            per_pair_timeout=0.001,
            non_diffpair_strategy=lambda: [],
        )

    # Phase-1 instrumentation still populated on collapse.
    assert router._diffpair._last_budget_exit_pair_names == ["DP"]
    assert router.diffpair_budget_exit_pair_names() == ["DP"]
    assert router._diffpair._last_budget_exit_count == 1

    msgs = [r.getMessage() for r in caplog.records]
    # The Phase-1 fallback warning still fires (operator told).
    assert any("DIFFPAIR_BUDGET_EXIT_FALLBACK" in m and "DP" in m for m in msgs), (
        f"expected the Phase-1 fallback warning on collapse; saw {msgs}"
    )
    # And the new #4107 line records that the promotion was skipped.
    assert any("DIFFPAIR_COUPLED_COLLAPSE_SKIP_PROMOTION" in m for m in msgs), (
        f"expected the #4107 collapse-skip log line; saw {msgs}"
    )


def test_partial_exit_keeps_budget_exit_promotion():
    """When only SOME considered pairs budget-exit (one pair couples), the
    collapse signature does NOT hold, so the #3270 promotion still runs for
    the budget-exited pair's nets -- board-06-style behaviour is unchanged."""
    router = _one_easy_one_hard_pair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    promotion_at_strategy: dict[str, set[int]] = {}

    def _record_promotion() -> list:
        promotion_at_strategy["nets"] = set(router._budget_exit_diff_nets)
        return []

    router._diffpair.route_all_with_diffpairs(
        config,
        coupled_only=True,
        per_pair_timeout=0.5,  # easy pair couples; hard pair budget-exits
        non_diffpair_strategy=_record_promotion,
    )

    # Two pairs considered; only the HARD pair budget-exited.
    assert router._diffpair._last_coupled_attempted_count == 2
    assert router._diffpair._last_budget_exit_pair_names == ["H"]
    # Partial exit -> promotion KEPT: the hard pair's nets (3, 4) are
    # promoted for the strategy call.
    assert promotion_at_strategy["nets"] == {3, 4}, (
        "Partial budget-exit must keep the #3270 promotion; strategy saw "
        f"_budget_exit_diff_nets={promotion_at_strategy['nets']}"
    )


def test_aggregate_only_deferral_is_not_a_collapse():
    """An aggregate-timeout-only deferral where the coupled A* never ran
    (``coupled_attempted_count == 0``) is NOT the board-07 collapse: the
    collapse guard requires the coupled A* to have actually attempted at
    least one pair.  The promotion therefore still runs for the deferred
    nets (behaviour unchanged from before #4107)."""
    router = _two_pad_diffpair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    promotion_at_strategy: dict[str, set[int]] = {}

    def _record_promotion() -> list:
        promotion_at_strategy["nets"] = set(router._budget_exit_diff_nets)
        return []

    router._diffpair.route_all_with_diffpairs(
        config,
        aggregate_timeout=0.001,  # defer before the coupled A* runs
        non_diffpair_strategy=_record_promotion,
    )

    # No coupled A* attempted -> not a collapse.
    assert router._diffpair._last_coupled_attempted_count == 0
    assert router._diffpair._last_budget_exit_pair_names == ["USB_D"]
    # Promotion still applied to the deferred pair's nets (1, 2).
    assert promotion_at_strategy["nets"] == {1, 2}, (
        "Aggregate-only deferral (no coupled A* attempted) must NOT trigger "
        "the collapse skip; the #3270 promotion still runs. Strategy saw "
        f"_budget_exit_diff_nets={promotion_at_strategy['nets']}"
    )

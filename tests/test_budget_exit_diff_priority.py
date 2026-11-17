"""Tests for Issue #3270 budget-exit diff-pair priority promotion.

When the CoupledPathfinder pre-pass exhausts the per-pair iteration or
wall-clock budget on a diff pair, the pair's nets are deferred to the
main strategy.  This file pins the contract that those deferred nets
get a complexity-tier promotion (`tier=-1`) inside `_get_net_priority`,
so they route ahead of other nets in the same priority class when the
main strategy schedules its work.

Rationale: on board 06 the budget-exit USB3 pairs end up routing AFTER
heavy single-ended nets have colonised the inner-layer continuation
corridor reserved by `_reserve_pair_continuation_corridor` (Issue
#2677).  Routing them earlier inside the main strategy gives them first
pick of the still-clean corridor.  See PR comments on #3270 for the
full reach-by-iteration analysis.

The tests intentionally stub the routing internals and exercise only
the priority-tuple mechanic so they run fast and are robust against
unrelated routing changes.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.rules import NET_CLASS_HIGH_SPEED


def _build_minimal_autorouter() -> Autorouter:
    """Minimal Autorouter populated with three priority-2 nets.

    Each net is identical aside from net id so the priority tuple
    reduces to (priority, complexity_tier, -constraint, pad_count,
    distance, -congestion).  In this setup all three nets land at the
    same priority class (2), same pad count (2), same distance (small
    constant), so the tiebreaker is governed by ``complexity_tier``
    alone -- which is exactly what we want to probe.
    """
    ar = Autorouter(width=20.0, height=20.0)
    ar.nets[10] = [("J1", "1"), ("U1", "1")]
    ar.nets[11] = [("J1", "2"), ("U1", "2")]
    ar.nets[12] = [("J1", "3"), ("U1", "3")]
    ar.net_names = {10: "USB3_TX1+", 11: "USB3_TX1-", 12: "USB3_RX1+"}
    for name in ("USB3_TX1+", "USB3_TX1-", "USB3_RX1+"):
        ar.net_class_map[name] = NET_CLASS_HIGH_SPEED
    # Pads with sane geometry so the bounding-box diagonal is well-defined.
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad

    ar.pads[("J1", "1")] = Pad(
        x=0.0,
        y=0.0,
        width=0.3,
        height=0.3,
        net=10,
        net_name="USB3_TX1+",
        layer=Layer.F_CU,
        ref="J1",
    )
    ar.pads[("U1", "1")] = Pad(
        x=2.0,
        y=0.0,
        width=0.3,
        height=0.3,
        net=10,
        net_name="USB3_TX1+",
        layer=Layer.F_CU,
        ref="U1",
    )
    ar.pads[("J1", "2")] = Pad(
        x=0.0,
        y=1.0,
        width=0.3,
        height=0.3,
        net=11,
        net_name="USB3_TX1-",
        layer=Layer.F_CU,
        ref="J1",
    )
    ar.pads[("U1", "2")] = Pad(
        x=2.0,
        y=1.0,
        width=0.3,
        height=0.3,
        net=11,
        net_name="USB3_TX1-",
        layer=Layer.F_CU,
        ref="U1",
    )
    ar.pads[("J1", "3")] = Pad(
        x=0.0,
        y=2.0,
        width=0.3,
        height=0.3,
        net=12,
        net_name="USB3_RX1+",
        layer=Layer.F_CU,
        ref="J1",
    )
    ar.pads[("U1", "3")] = Pad(
        x=2.0,
        y=2.0,
        width=0.3,
        height=0.3,
        net=12,
        net_name="USB3_RX1+",
        layer=Layer.F_CU,
        ref="U1",
    )
    return ar


def test_budget_exit_nets_field_initialized_empty():
    """`_budget_exit_diff_nets` is initialized as an empty set."""
    ar = Autorouter(width=10.0, height=10.0)
    assert ar._budget_exit_diff_nets == set()


def test_budget_exit_net_gets_complexity_tier_minus_1():
    """A budget-exit diff net's priority tuple has complexity_tier == -1."""
    ar = _build_minimal_autorouter()

    # Baseline: all three nets have tier 0 (2-pin, short net).
    for nid in (10, 11, 12):
        prio = ar._get_net_priority(nid)
        # priority class, complexity_tier are tuple[0] and tuple[1]
        assert prio[1] == 0, f"Baseline priority for net {nid}: expected tier 0, got {prio}"

    # Promote net 10 (USB3_TX1+) to budget-exit.
    ar._budget_exit_diff_nets = {10}
    prio_promoted = ar._get_net_priority(10)
    prio_other = ar._get_net_priority(11)
    assert prio_promoted[1] == -1, (
        f"Budget-exit net 10 must get complexity_tier -1, got {prio_promoted}"
    )
    assert prio_other[1] == 0, (
        f"Non-promoted net 11 must keep its baseline tier 0, got {prio_other}"
    )
    # The promoted net sorts BEFORE the non-promoted one.
    assert prio_promoted < prio_other, (
        f"Budget-exit net must sort earlier than non-promoted siblings "
        f"in the same priority class.  promoted={prio_promoted} "
        f"other={prio_other}"
    )


def test_budget_exit_net_promotion_preserves_priority_class():
    """The priority CLASS (tuple[0]) is unchanged by the tier promotion.

    The promotion only affects the COMPLEXITY tier (tuple[1]); the
    priority class itself remains the net-class priority (here 2 for
    NET_CLASS_HIGH_SPEED).  This preserves the existing class-based
    ordering invariants (e.g. class-2 always before class-4).
    """
    ar = _build_minimal_autorouter()
    expected_class = NET_CLASS_HIGH_SPEED.priority

    ar._budget_exit_diff_nets = {10}
    prio = ar._get_net_priority(10)
    assert prio[0] == expected_class, (
        f"Priority class must remain {expected_class} after tier promotion, got {prio[0]}"
    )


def test_clearing_budget_exit_set_restores_baseline_priority():
    """Clearing `_budget_exit_diff_nets` reverts to the baseline priority.

    The diff-pair routing layer is contractually obliged to clear the
    set after the strategy callback returns (see `diffpair_routing.py`
    `route_all_with_diffpairs`); this test confirms the public API
    actually responds to that clear.
    """
    ar = _build_minimal_autorouter()
    ar._budget_exit_diff_nets = {10}
    promoted = ar._get_net_priority(10)
    ar._budget_exit_diff_nets = set()
    reverted = ar._get_net_priority(10)

    assert promoted[1] == -1
    assert reverted[1] == 0, (
        "After clearing _budget_exit_diff_nets, priority must revert "
        f"to baseline (tier 0), got {reverted}"
    )


def test_non_diff_net_not_affected_by_budget_exit_set():
    """A net id NOT in the set is never promoted.

    Defends against an off-by-one or set-membership bug that
    accidentally lifts a sibling's tier when only its partner is in
    the set.
    """
    ar = _build_minimal_autorouter()
    ar._budget_exit_diff_nets = {10}  # only TX1+
    prio_tx1_neg = ar._get_net_priority(11)  # TX1-
    prio_rx1_pos = ar._get_net_priority(12)  # RX1+
    assert prio_tx1_neg[1] == 0, prio_tx1_neg
    assert prio_rx1_pos[1] == 0, prio_rx1_pos

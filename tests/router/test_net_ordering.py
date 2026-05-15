"""Tests for net-ordering fairness across match groups (Issue #2914).

The :meth:`Autorouter._interleave_match_groups` helper front-loads a
single representative from each declared match group so no group can be
fully starved by the wall-clock budget.  Before this fix, board 07
(``boards/07-matchgroup-test``) routed A0..A7 zero times across four
layer-escalation attempts because the ADDR_BUS group (priority class 2)
sat strictly after the DDR / MIPI / HDMI groups (priority class 1) in
the sort order and the 600 s budget was exhausted by the higher-
priority groups before A0..A7 received any "Routing net..." log line.

This module verifies the helper's contract:

1. **Identity on unbatched input** -- a board with no declared match
   groups receives an order-preserving no-op.
2. **Front-loaded representatives** -- 10 short nets in group ``SHORT``
   and 2 long nets in group ``LONG`` produce an output where every
   group's first member appears in the leading ``G``-slot prefix
   (``G`` = number of detected groups).
3. **Length preservation** -- the output is a permutation of the input
   (same length, same membership).
4. **Tail order preservation** -- non-leader nets keep their input
   order in the tail.
5. **AC1 attempted-not-skipped guarantee** -- given a synthetic 12-net
   workload (10 short + 2 long, all in match groups) and a budget that
   visits only 6 nets, at least one member of EACH group appears in
   the visited prefix.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.rules import NetClassRouting

# =============================================================================
# Helpers
# =============================================================================


def _make_router_with_groups(
    short_count: int = 10,
    long_count: int = 2,
    short_group_name: str = "SHORT_BUS",
    long_group_name: str = "LONG_BUS",
) -> tuple[Autorouter, list[int], list[int]]:
    """Build a synthetic router with two match groups.

    Args:
        short_count: Number of "short" nets (declared into ``short_group_name``).
        long_count: Number of "long" nets (declared into ``long_group_name``).
        short_group_name: ``length_match_group`` value for the short group.
        long_group_name: ``length_match_group`` value for the long group.

    Returns:
        Tuple of (router, short_net_ids, long_net_ids).
    """
    short_class = NetClassRouting(
        name="SHORT_CLASS",
        priority=1,
        trace_width=0.20,
        clearance=0.15,
        length_critical=True,
        length_match_group=short_group_name,
    )
    long_class = NetClassRouting(
        name="LONG_CLASS",
        priority=1,
        trace_width=0.20,
        clearance=0.15,
        length_critical=True,
        length_match_group=long_group_name,
    )

    net_class_map: dict[str, NetClassRouting] = {}

    short_ids: list[int] = []
    long_ids: list[int] = []

    router = Autorouter(width=200.0, height=200.0, net_class_map=net_class_map)

    # Short nets at low net-ids, long nets at high net-ids -- so the
    # priority-sorted order (which falls back to net-id when all
    # priority-tuple components tie) places short nets first.  The
    # helper must still surface one long-bucket member at the front.
    for i in range(short_count):
        net_id = i + 1
        net_name = f"SBUS{i}"
        router.add_component(
            f"RS{i}_A",
            [{"number": "1", "x": float(i), "y": 0.0, "net": net_id, "net_name": net_name}],
        )
        router.add_component(
            f"RS{i}_B",
            [{"number": "1", "x": float(i) + 1.0, "y": 0.0, "net": net_id, "net_name": net_name}],
        )
        net_class_map[net_name] = short_class
        short_ids.append(net_id)

    for j in range(long_count):
        net_id = short_count + j + 1
        net_name = f"LBUS{j}"
        router.add_component(
            f"RL{j}_A",
            [
                {
                    "number": "1",
                    "x": 10.0,
                    "y": 100.0 + float(j),
                    "net": net_id,
                    "net_name": net_name,
                }
            ],
        )
        router.add_component(
            f"RL{j}_B",
            [
                {
                    "number": "1",
                    "x": 150.0,
                    "y": 100.0 + float(j),
                    "net": net_id,
                    "net_name": net_name,
                }
            ],
        )
        net_class_map[net_name] = long_class
        long_ids.append(net_id)

    # Ensure the router's net_class_map references the populated dict.
    router.net_class_map = net_class_map

    return router, short_ids, long_ids


# =============================================================================
# Tests
# =============================================================================


class TestIdentityOnNoGroups:
    """A board with no declared match groups receives a no-op."""

    def test_empty_input(self):
        router = Autorouter(width=50.0, height=50.0)
        assert router._interleave_match_groups([]) == []

    def test_no_match_groups_declared(self):
        """All nets ungrouped -> identity output."""
        router = Autorouter(width=50.0, height=50.0)
        # Add a couple of plain nets with no length_match_group declaration
        # and net names that don't match suffix-inference patterns.
        for i, name in enumerate(["NET_FOO", "NET_BAR", "NET_BAZ"]):
            router.add_component(
                f"R{i}_A",
                [{"number": "1", "x": float(i), "y": 0.0, "net": i + 1, "net_name": name}],
            )
            router.add_component(
                f"R{i}_B",
                [
                    {
                        "number": "1",
                        "x": float(i) + 1.0,
                        "y": 0.0,
                        "net": i + 1,
                        "net_name": name,
                    }
                ],
            )

        net_order = [1, 2, 3]
        out = router._interleave_match_groups(net_order)
        # No groups -> detect_match_groups returns [] -> short-circuit
        # to identity ordering.
        assert out == net_order


class TestFrontLoadedRepresentatives:
    """Each match group's first member is promoted to the front."""

    def test_two_groups_each_get_a_front_slot(self):
        """3 short + 3 long: front[0] in SHORT, front[1] in LONG."""
        router, short_ids, long_ids = _make_router_with_groups(
            short_count=3, long_count=3
        )
        # Priority-sorted: short nets first (lower bbox-diagonal), then long.
        net_order = short_ids + long_ids
        out = router._interleave_match_groups(net_order)

        # Length / membership preserved.
        assert sorted(out) == sorted(net_order)
        assert len(out) == 6

        # The first two output slots come from distinct groups -- one
        # leader per group, promoted to the front in priority-sorted
        # order across groups.
        assert out[0] in short_ids, (
            f"Front[0] should be SHORT-bucket leader; got {out[0]}"
        )
        assert out[1] in long_ids, (
            f"Front[1] should be LONG-bucket leader; got {out[1]}"
        )
        # Remaining slots: the non-leader members in their input order.
        assert sorted(out[2:]) == sorted(short_ids[1:] + long_ids[1:])

    def test_imbalanced_groups(self):
        """5 short + 2 long: front[0..1] are leaders, tail preserves order."""
        router, short_ids, long_ids = _make_router_with_groups(
            short_count=5, long_count=2
        )
        net_order = short_ids + long_ids
        out = router._interleave_match_groups(net_order)

        # Length preserved.
        assert sorted(out) == sorted(net_order)
        assert len(out) == 7

        # Front: SHORT leader then LONG leader (priority-sorted order).
        assert out[0] in short_ids
        assert out[1] in long_ids
        # Tail: remaining SHORT members in input order, then remaining LONG.
        # Specifically, the tail preserves the per-bucket input order:
        # SHORT[1..4] followed by LONG[1].
        assert out[2:6] == short_ids[1:]
        assert out[6] == long_ids[1]


class TestTailOrderPreserved:
    """Non-leader members retain their input order in the tail."""

    def test_short_bucket_tail_preserves_input_order(self):
        router, short_ids, long_ids = _make_router_with_groups(
            short_count=4, long_count=2
        )
        net_order = short_ids + long_ids  # [1, 2, 3, 4, 5, 6]
        out = router._interleave_match_groups(net_order)

        # Tail (out[2:]) = remaining SHORT (in input order) + remaining LONG.
        out_short_tail = [n for n in out[2:] if n in short_ids]
        assert out_short_tail == short_ids[1:], (
            "Tail must preserve input order within each bucket"
        )
        out_long_tail = [n for n in out[2:] if n in long_ids]
        assert out_long_tail == long_ids[1:]


class TestAttemptedNotSkippedGuarantee:
    """AC1: every group's first member appears in the first ``G`` slots.

    This is the load-bearing fairness contract: when the wall-clock
    runs out after ``K`` nets (``K >= G``, where ``G`` is the number of
    detected match groups), every match group has at least one member
    attempted.
    """

    def test_10_short_2_long_all_groups_in_first_2_slots(self):
        """Curator's AC4 scenario: 10 short + 2 long, both groups attempted."""
        router, short_ids, long_ids = _make_router_with_groups(
            short_count=10, long_count=2
        )
        net_order = short_ids + long_ids
        out = router._interleave_match_groups(net_order)

        # Length preservation.
        assert len(out) == 12
        assert sorted(out) == sorted(net_order)
        # Membership invariant.
        assert set(out) == set(net_order)

        # The LONG-bucket leader sits at out[1].  Pre-fix, ALL 10 short
        # nets came first and the long nets sat at positions 10/11 --
        # exactly the starvation pattern board 07 reproduced.
        assert out[1] in long_ids, (
            f"Long-bucket leader must be at out[1]; "
            f"got {out[1]} (LONG ids = {long_ids})"
        )
        # And the short bucket leader is at the very front.
        assert out[0] in short_ids

    def test_starvation_prefix_includes_both_groups(self):
        """With a budget of only 6 nets, BOTH groups are represented.

        This is the AC1 "attempted-not-skipped" guarantee in its
        starkest form: the synthetic budget exhausts after net 6, but
        at least one member of each group is among those 6 attempts.
        """
        router, short_ids, long_ids = _make_router_with_groups(
            short_count=10, long_count=2
        )
        net_order = short_ids + long_ids
        out = router._interleave_match_groups(net_order)

        # Simulate the wall-clock killing the loop after 6 attempts.
        attempted = out[:6]
        attempted_groups = {
            "SHORT" if nid in short_ids else "LONG" for nid in attempted
        }
        assert attempted_groups == {"SHORT", "LONG"}, (
            f"Both groups must be attempted in first 6 slots; "
            f"got {attempted_groups} from prefix {attempted}.  Pre-fix "
            f"board 07 ADDR_BUS starvation regression."
        )


class TestSingleGroupShortCircuit:
    """If every net falls in one group the helper still works.

    Note: a single match group still produces a front+tail split (the
    group's first member is promoted; remaining members fall into the
    tail).  The length-preservation invariant guards against drops.
    """

    def test_all_nets_in_one_group(self):
        """A single declared group: leader promoted, rest in tail."""
        single_class = NetClassRouting(
            name="ONE",
            priority=1,
            trace_width=0.20,
            clearance=0.15,
            length_critical=True,
            length_match_group="ONLY_BUS",
        )
        net_class_map = {f"NX{i}": single_class for i in range(4)}

        router = Autorouter(width=50.0, height=50.0, net_class_map=net_class_map)
        for i in range(4):
            net_name = f"NX{i}"
            router.add_component(
                f"RA{i}",
                [
                    {
                        "number": "1",
                        "x": float(i),
                        "y": 0.0,
                        "net": i + 1,
                        "net_name": net_name,
                    }
                ],
            )
            router.add_component(
                f"RB{i}",
                [
                    {
                        "number": "1",
                        "x": float(i) + 1.0,
                        "y": 0.0,
                        "net": i + 1,
                        "net_name": net_name,
                    }
                ],
            )
        router.net_class_map = net_class_map

        net_order = [1, 2, 3, 4]
        out = router._interleave_match_groups(net_order)
        # All 4 nets share the same group.  out[0] is the leader (input
        # order: net 1), and the tail preserves the remaining input order.
        assert len(out) == 4
        assert set(out) == set(net_order)
        # Membership and order match input (leader is already first).
        assert out == net_order

"""Unit tests for the discrete BundlePlan allocator (Issue #4256, A3).

These pin the search-free combinatorial corridor allocator on small
synthetic coupled-group fixtures — no full router or board geometry
required — per the curated acceptance criteria:

  * a **full reversal** yields either a conflict-free assignment (given a
    generous inner via-hop budget) or an explicit **"infeasible"** verdict
    (given a tight budget) — NEVER a silent partial;
  * a **planar (co-oriented)** bundle yields trivial in-order lanes with no
    via hops (the over-trigger guard);
  * infeasibility is a **first-class** output (an explicit ``BundlePlan``
    with ``feasible=False`` and a reason, empty lanes);
  * the diff-pair coupling constraint is enforced (both legs laned or the
    whole plan is infeasible — never one leg dropped).

Fixture convention mirrors ``test_bundle_river_inversions.py``: names sort
in the same order as primary projections; a reversed secondary places
name[i] at the mirrored projection.
"""

from __future__ import annotations

from kicad_tools.router.bundle_river import (
    LANE_LAYER_INNER,
    LANE_LAYER_OUTER,
    BundlePlan,
    CoupledGroup,
    CoupledMember,
    allocate_bundle_plan,
)


def _tmds_names() -> list[str]:
    """The board-07 TMDS bundle: 3 diff pairs = 6 nets."""
    return [
        "TMDS_D0_P",
        "TMDS_D0_N",
        "TMDS_D1_P",
        "TMDS_D1_N",
        "TMDS_D2_P",
        "TMDS_D2_N",
    ]


def _partner_map(names: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in names:
        if name.endswith("_P"):
            out[name] = name[:-2] + "_N"
        elif name.endswith("_N"):
            out[name] = name[:-2] + "_P"
    return out


def _coupled_group(
    names: list[str],
    *,
    reversed_secondary: bool,
    group_name: str = "TMDS",
) -> CoupledGroup:
    """Build a coupled group; secondary column optionally fully reversed."""
    n = len(names)
    id_by_name = {name: i + 1 for i, name in enumerate(names)}
    partners = _partner_map(names)
    members: list[CoupledMember] = []
    for i, name in enumerate(names):
        j = (n - 1 - i) if reversed_secondary else i
        partner_name = partners.get(name)
        members.append(
            CoupledMember(
                net_id=id_by_name[name],
                net_name=name,
                primary_projection=float(i),
                secondary_projection=float(j),
                pair_partner_id=id_by_name.get(partner_name) if partner_name else None,
            )
        )
    return CoupledGroup(group_name=group_name, members=tuple(members))


class TestPlanarTrivialLanes:
    """A co-oriented bundle => trivial in-order outer lanes, no via hops."""

    def test_planar_yields_in_order_outer_lanes(self) -> None:
        group = _coupled_group(_tmds_names(), reversed_secondary=False)
        plan = allocate_bundle_plan(group, inner_lane_budget=1)

        assert plan.feasible is True
        assert plan.infeasible is False
        # Every member laned exactly once.
        assert len(plan.lanes) == 6
        assert {lane.net_id for lane in plan.lanes} == {1, 2, 3, 4, 5, 6}
        # All on the outer strip, no via hops.
        assert all(lane.layer == LANE_LAYER_OUTER for lane in plan.lanes)
        assert all(lane.via_hop is False for lane in plan.lanes)
        assert plan.inner_lanes_required == 0
        # Lanes are in primary-projection order with unique slots.
        outer = sorted(plan.lanes, key=lambda ln: ln.order_index)
        assert [ln.net_id for ln in outer] == [1, 2, 3, 4, 5, 6]
        assert len({ln.order_index for ln in outer}) == 6

    def test_planar_lanes_do_not_share_a_cell_on_a_layer(self) -> None:
        """The feasibility invariant: unique (layer, order_index) per lane."""
        group = _coupled_group(_tmds_names(), reversed_secondary=False)
        plan = allocate_bundle_plan(group, inner_lane_budget=1)
        keys = {(lane.layer, lane.order_index) for lane in plan.lanes}
        assert len(keys) == len(plan.lanes)


class TestFullReversalBudgetDependent:
    """A full reversal: conflict-free with a generous budget, else infeasible."""

    def test_full_reversal_infeasible_with_tight_budget(self) -> None:
        # Full reversal of 6 nets: every pair crosses, so the losing nets
        # (5 of them) all mutually cross -> need 5 inner lanes. Budget 1 =>
        # explicit infeasible verdict, never a silent partial.
        group = _coupled_group(_tmds_names(), reversed_secondary=True)
        plan = allocate_bundle_plan(group, inner_lane_budget=1)

        assert plan.feasible is False
        assert plan.infeasible is True
        assert plan.lanes == ()  # no silent partial
        assert plan.reason  # non-empty explanation
        assert plan.inner_lanes_required > plan.inner_lane_budget
        assert "infeasible" in plan.reason.lower() or "budget" in plan.reason.lower()

    def test_full_reversal_feasible_with_generous_budget(self) -> None:
        # With enough inner lanes the same full reversal IS conflict-free:
        # every member laned, losers on distinct inner lanes.
        group = _coupled_group(_tmds_names(), reversed_secondary=True)
        plan = allocate_bundle_plan(group, inner_lane_budget=6)

        assert plan.feasible is True
        assert len(plan.lanes) == 6
        assert {lane.net_id for lane in plan.lanes} == {1, 2, 3, 4, 5, 6}
        # No two lanes share a cell on a layer.
        keys = {(lane.layer, lane.order_index) for lane in plan.lanes}
        assert len(keys) == len(plan.lanes)
        # The losing nets dip to the inner layer; each inner lane is unique.
        inner = [lane for lane in plan.lanes if lane.layer == LANE_LAYER_INNER]
        assert all(lane.via_hop for lane in inner)
        assert len({lane.order_index for lane in inner}) == len(inner)
        # Inner-lane crossings among losers are resolved (distinct colours).
        assert plan.inner_lanes_required <= plan.inner_lane_budget

    def test_full_reversal_is_never_silent_partial(self) -> None:
        """Whatever the budget, the plan lanes ALL members or NONE."""
        for budget in range(0, 8):
            group = _coupled_group(_tmds_names(), reversed_secondary=True)
            plan = allocate_bundle_plan(group, inner_lane_budget=budget)
            if plan.feasible:
                assert len(plan.lanes) == 6
            else:
                assert plan.lanes == ()


class TestSingleAdjacentSwapFeasible:
    """A single crossing needs exactly one inner lane => feasible at budget 1."""

    def test_one_crossing_feasible_budget_one(self) -> None:
        # A(0) B(1) C(2) D(3) primary; secondary swaps B<->C only.
        members = [
            CoupledMember(1, "A", 0.0, 0.0),
            CoupledMember(2, "B", 1.0, 2.0),
            CoupledMember(3, "C", 2.0, 1.0),
            CoupledMember(4, "D", 3.0, 3.0),
        ]
        group = CoupledGroup(group_name="SWAP", members=tuple(members))
        plan = allocate_bundle_plan(group, inner_lane_budget=1)

        assert plan.feasible is True
        assert len(plan.lanes) == 4
        # Exactly one net dips to the inner layer (the crossing loser).
        inner = [lane for lane in plan.lanes if lane.via_hop]
        assert len(inner) == 1
        assert plan.inner_lanes_required == 1


class TestCouplingConstraint:
    """Diff-pair partners must all be group members; else infeasible."""

    def test_missing_partner_is_infeasible(self) -> None:
        # net 1 claims a partner id (99) that is not in the group.
        members = [
            CoupledMember(1, "X_P", 0.0, 0.0, pair_partner_id=99),
            CoupledMember(2, "Y", 1.0, 1.0),
            CoupledMember(3, "Z", 2.0, 2.0),
            CoupledMember(4, "W", 3.0, 3.0),
            CoupledMember(5, "V", 4.0, 4.0),
        ]
        group = CoupledGroup(group_name="BROKEN", members=tuple(members))
        plan = allocate_bundle_plan(group)
        assert plan.infeasible is True
        assert "partner" in plan.reason.lower()
        assert plan.lanes == ()

    def test_tmds_partners_are_carried_on_lanes(self) -> None:
        group = _coupled_group(_tmds_names(), reversed_secondary=False)
        plan = allocate_bundle_plan(group, inner_lane_budget=1)
        # Every TMDS member is half a diff pair, so every lane carries a
        # partner id that is itself a group member.
        member_ids = {m.net_id for m in group.members}
        for lane in plan.lanes:
            assert lane.pair_partner_id is not None
            assert lane.pair_partner_id in member_ids


class TestNonMatchedBusRejected:
    """A non-clean matched bus is an explicit infeasible verdict."""

    def test_mismatched_rows_infeasible(self) -> None:
        # Secondary carries a net name not on the primary -> not a matched
        # bus. Build directly (the helper always matches by construction).
        members = [
            CoupledMember(1, "A", 0.0, 0.0),
            CoupledMember(1, "A", 1.0, 1.0),  # duplicate net id on the row
        ]
        group = CoupledGroup(group_name="DUP", members=tuple(members))
        plan = allocate_bundle_plan(group)
        assert plan.infeasible is True
        assert plan.lanes == ()


class TestInfeasibleFactory:
    """The explicit infeasibility constructor is well-formed."""

    def test_infeasible_plan_shape(self) -> None:
        plan = BundlePlan.infeasible_plan(
            "G", "because", inner_lanes_required=3, inner_lane_budget=1
        )
        assert plan.feasible is False
        assert plan.infeasible is True
        assert plan.lanes == ()
        assert plan.reason == "because"
        assert plan.inner_lanes_required == 3
        assert plan.inner_lane_budget == 1
        assert plan.lane_for(7) is None

"""Unit tests for the declared swap-group assignment (issue #5522, Phase 1
of Epic #5511).

Mirrors the synthetic-row style of ``test_bundle_river_inversions.py``
(``_reversed_rows`` / ``_planar_rows``): small hand-built two-row fixtures,
no PCB required, per the curated Test Plan's four synthetic cases:

  * a full reversal of the declared group -> the full reversing permutation,
    crossings 28 -> 0 for an 8-net bundle;
  * a genuinely planar (already co-oriented) declared group -> identity
    (empty) ``net_rebinding``, 0 -> 0 (the over-triggering / no-op guard);
  * a partial reversal -> only the flipped pair changes;
  * an undeclared member present in the row (e.g. ``DM0``) -> its binding
    is left untouched and its residual crossings are counted and attributed
    in the "whole group" figure, never hidden.
"""

from __future__ import annotations

from kicad_tools.router.bundle_river import RowMember, compute_facing_row_inversions
from kicad_tools.router.swap_groups import MIN_SWAP_GROUP_NETS, propose_swap_assignment


def _reversed_rows(names: list[str]) -> tuple[list[RowMember], list[RowMember]]:
    """A full-reversal primary/secondary row pair (mirrors the bundle_river helper)."""
    n = len(names)
    primary = [RowMember(net_id=i + 1, net_name=names[i], projection=float(i)) for i in range(n)]
    secondary = [
        RowMember(net_id=i + 1, net_name=names[i], projection=float(n - 1 - i)) for i in range(n)
    ]
    return primary, secondary


def _planar_rows(names: list[str]) -> tuple[list[RowMember], list[RowMember]]:
    """A co-oriented (planar) primary/secondary row pair."""
    n = len(names)
    primary = [RowMember(net_id=i + 1, net_name=names[i], projection=float(i)) for i in range(n)]
    secondary = [RowMember(net_id=i + 1, net_name=names[i], projection=float(i)) for i in range(n)]
    return primary, secondary


_DQ_NAMES = [f"DQ{i}" for i in range(8)]


class TestFullReversal:
    def test_reversed_dq0_dq7_yields_full_reversing_permutation(self) -> None:
        primary, secondary = _reversed_rows(_DQ_NAMES)
        group = set(_DQ_NAMES)
        assignment = propose_swap_assignment(primary, secondary, group)
        assert assignment is not None
        assert assignment.crossings_before == 28
        assert assignment.crossings_after == 0
        # Full reversal -> the sort-and-pair permutation reverses every slot.
        assert assignment.net_rebinding == {
            "DQ0": "DQ7",
            "DQ1": "DQ6",
            "DQ2": "DQ5",
            "DQ3": "DQ4",
            "DQ4": "DQ3",
            "DQ5": "DQ2",
            "DQ6": "DQ1",
            "DQ7": "DQ0",
        }
        # No wider group supplied -> the "whole group" figures coincide.
        assert assignment.group_crossings_before == 28
        assert assignment.group_crossings_after == 0
        assert assignment.fixed_members == ()

    def test_proposed_rebinding_actually_zeroes_crossings(self) -> None:
        """Applying the returned rebinding to the secondary row must measure 0."""
        primary, secondary = _reversed_rows(_DQ_NAMES)
        group = set(_DQ_NAMES)
        assignment = propose_swap_assignment(primary, secondary, group)
        assert assignment is not None

        id_by_name = {m.net_name: m.net_id for m in primary}
        rebound_secondary = [
            RowMember(
                net_id=id_by_name[assignment.net_rebinding[m.net_name]],
                net_name=assignment.net_rebinding[m.net_name],
                projection=m.projection,
            )
            for m in secondary
        ]
        assert compute_facing_row_inversions(primary, rebound_secondary) == []


class TestPlanarNoOpGuard:
    """The over-triggering guard: an already co-oriented group proposes nothing."""

    def test_planar_group_yields_identity_no_op(self) -> None:
        primary, secondary = _planar_rows(_DQ_NAMES)
        group = set(_DQ_NAMES)
        assignment = propose_swap_assignment(primary, secondary, group)
        assert assignment is not None
        assert assignment.crossings_before == 0
        assert assignment.crossings_after == 0
        # Identity permutation -> no non-identity entries to report.
        assert assignment.net_rebinding == {}


class TestPartialReversal:
    """Only the flipped pair changes; co-oriented members are untouched."""

    def test_single_adjacent_swap_changes_only_the_flipped_pair(self) -> None:
        # Primary: A(0) B(1) C(2) D(3); Secondary: A(0) C(1) B(2) D(3).
        primary = [
            RowMember(1, "A", 0.0),
            RowMember(2, "B", 1.0),
            RowMember(3, "C", 2.0),
            RowMember(4, "D", 3.0),
        ]
        secondary = [
            RowMember(1, "A", 0.0),
            RowMember(2, "B", 2.0),
            RowMember(3, "C", 1.0),
            RowMember(4, "D", 3.0),
        ]
        assignment = propose_swap_assignment(primary, secondary, {"A", "B", "C", "D"})
        assert assignment is not None
        assert assignment.crossings_before == 1
        assert assignment.crossings_after == 0
        assert assignment.net_rebinding == {"B": "C", "C": "B"}


class TestUndeclaredMember:
    """A member present in the row but NOT in the declared group stays fixed."""

    def test_undeclared_member_binding_is_unchanged_and_residual_is_attributed(self) -> None:
        # Full 4-net reversal; DM0 is a REAL member of the row but not
        # declared as swappable.
        names = ["DQ0", "DQ1", "DM0", "DQ2"]
        primary, secondary = _reversed_rows(names)
        group = {"DQ0", "DQ1", "DQ2"}

        assignment = propose_swap_assignment(
            primary, secondary, group, whole_group_rows=(primary, secondary)
        )
        assert assignment is not None
        # DM0 never appears as a rebinding key or value.
        assert "DM0" not in assignment.net_rebinding
        assert "DM0" not in assignment.net_rebinding.values()
        assert assignment.fixed_members == ("DM0",)
        # The declared subset (DQ0/DQ1/DQ2) is fully resolved...
        assert assignment.crossings_after == 0
        # ...but the whole-group figure still shows a residual crossing
        # against the fixed DM0 sibling -- never hidden.
        assert assignment.group_crossings_before == 6
        assert assignment.group_crossings_after == 1

    def test_undeclared_members_pad_binding_is_literally_unchanged(self) -> None:
        """Applying the rebinding must leave DM0's slot carrying DM0."""
        names = ["DQ0", "DQ1", "DM0", "DQ2"]
        primary, secondary = _reversed_rows(names)
        group = {"DQ0", "DQ1", "DQ2"}
        assignment = propose_swap_assignment(
            primary, secondary, group, whole_group_rows=(primary, secondary)
        )
        assert assignment is not None
        dm0_slot = next(m for m in secondary if m.net_name == "DM0")
        assert assignment.net_rebinding.get(dm0_slot.net_name, dm0_slot.net_name) == "DM0"


class TestMinimumGroupSize:
    """Fewer than MIN_SWAP_GROUP_NETS shared declared members -> ``None``."""

    def test_single_shared_member_returns_none(self) -> None:
        primary = [RowMember(1, "A", 0.0), RowMember(2, "B", 1.0)]
        secondary = [RowMember(1, "A", 1.0), RowMember(2, "B", 0.0)]
        assert propose_swap_assignment(primary, secondary, {"A"}) is None

    def test_min_swap_group_nets_constant_is_two(self) -> None:
        assert MIN_SWAP_GROUP_NETS == 2

    def test_no_shared_declared_members_returns_none(self) -> None:
        primary = [RowMember(1, "A", 0.0)]
        secondary = [RowMember(1, "A", 0.0)]
        assert propose_swap_assignment(primary, secondary, {"NOT_PRESENT"}) is None

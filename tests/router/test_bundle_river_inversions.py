"""Unit tests for the facing-row inversion analysis (Issue #4053).

These tests pin the search-free crossing-set computation on small
synthetic two-row fixtures — no full router or board geometry required —
per the curated Test Plan:

  * a known **full bus reversal** yields all C(n, 2) inverted pairs;
  * a genuinely **planar (co-oriented)** two-row fixture yields an EMPTY
    inversion set (the over-triggering regression guard — v1 must not
    reserve via-hop corridors where no crossing exists);
  * a **partial reversal** yields exactly the flipped pairs;
  * non-matched net sets between the rows are rejected (``None`` /
    empty), keeping v1 restricted to clean one-to-one matched buses.

The fixture geometry mirrors the board-07 DDR byte described in
``_apply_byte_lane_inner_priority``'s Issue #2962 docstring: U1's right
column carries DQ0 at the top ... DQ7 at the bottom; U2's facing left
column carries the same nets in the opposite row order.
"""

from __future__ import annotations

from kicad_tools.router.bundle_river import (
    RowMember,
    choose_via_hop_loser,
    compute_facing_row_inversions,
    compute_row_permutation,
    via_hop_loser_nets,
)


def _reversed_rows(
    names: list[str],
) -> tuple[list[RowMember], list[RowMember]]:
    """Build a primary/secondary row pair that is a FULL reversal.

    Primary column: names[i] at projection i (top-to-bottom in order).
    Secondary column: the SAME nets in reverse row order (mirror), i.e.
    names[i] at projection (n-1-i).  Net ids are 1..n by primary order.
    """
    n = len(names)
    primary = [RowMember(net_id=i + 1, net_name=names[i], projection=float(i)) for i in range(n)]
    secondary = [
        RowMember(net_id=i + 1, net_name=names[i], projection=float(n - 1 - i)) for i in range(n)
    ]
    return primary, secondary


def _planar_rows(names: list[str]) -> tuple[list[RowMember], list[RowMember]]:
    """Build a primary/secondary row pair that is co-oriented (planar)."""
    n = len(names)
    primary = [RowMember(net_id=i + 1, net_name=names[i], projection=float(i)) for i in range(n)]
    secondary = [RowMember(net_id=i + 1, net_name=names[i], projection=float(i)) for i in range(n)]
    return primary, secondary


class TestFullReversal:
    """A full bus reversal crosses every pair: C(n, 2) inversions."""

    def test_full_reversal_yields_all_pairs(self) -> None:
        names = ["DQ0", "DQ1", "DQ2", "DQ3", "DM0", "DQ4", "DQ5", "DQ6", "DQ7"]
        primary, secondary = _reversed_rows(names)
        inversions = compute_facing_row_inversions(primary, secondary)
        n = len(names)
        assert len(inversions) == n * (n - 1) // 2

    def test_ddr_eleven_net_reversal(self) -> None:
        """The board-07 DDR byte (11 nets) => C(11, 2) = 55 crossings."""
        names = [
            "DQ0",
            "DQ1",
            "DQ2",
            "DQ3",
            "DM0",
            "DQS_P",
            "DQS_N",
            "DQ4",
            "DQ5",
            "DQ6",
            "DQ7",
        ]
        primary, secondary = _reversed_rows(names)
        inversions = compute_facing_row_inversions(primary, secondary)
        assert len(inversions) == 55

    def test_reversal_pairs_are_ordered_and_named(self) -> None:
        names = ["A", "B", "C"]
        primary, secondary = _reversed_rows(names)
        inversions = compute_facing_row_inversions(primary, secondary)
        # All 3 pairs cross: (A,B), (A,C), (B,C).
        got = {(p.net_a_name, p.net_b_name) for p in inversions}
        assert got == {("A", "B"), ("A", "C"), ("B", "C")}


class TestPlanarGuard:
    """A co-oriented (non-reversed) bundle yields ZERO inversions.

    This is the over-triggering regression guard: the byte-lane synthetic
    fixtures in test_byte_lane_priority.py / _corridor_reservation.py place
    DQ_i at the SAME projection on both components, so the planner must
    reserve NO via-hop corridors for them.
    """

    def test_planar_two_row_yields_no_inversions(self) -> None:
        names = ["DQ0", "DQ1", "DQ2", "DQ3", "DQ4"]
        primary, secondary = _planar_rows(names)
        assert compute_facing_row_inversions(primary, secondary) == []

    def test_via_hop_loser_set_empty_for_planar(self) -> None:
        names = ["DQ0", "DQ1", "DQ2", "DQ3", "DQ4"]
        primary, secondary = _planar_rows(names)
        inversions = compute_facing_row_inversions(primary, secondary)
        assert via_hop_loser_nets(inversions) == []


class TestPartialReversal:
    """Only the flipped pairs cross; co-oriented pairs do not."""

    def test_single_adjacent_swap(self) -> None:
        # Primary order: A(0) B(1) C(2) D(3)
        # Secondary: A(0) C(1) B(2) D(3)  -> only (B, C) flips.
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
        inversions = compute_facing_row_inversions(primary, secondary)
        assert len(inversions) == 1
        assert {inversions[0].net_a_name, inversions[0].net_b_name} == {"B", "C"}


class TestNonMatchedRowsRejected:
    """v1 is restricted to clean one-to-one matched buses."""

    def test_different_net_sets_rejected(self) -> None:
        primary = [RowMember(1, "A", 0.0), RowMember(2, "B", 1.0)]
        secondary = [RowMember(1, "A", 0.0), RowMember(3, "X", 1.0)]
        assert compute_row_permutation(primary, secondary) is None
        assert compute_facing_row_inversions(primary, secondary) == []

    def test_duplicate_net_on_row_rejected(self) -> None:
        primary = [RowMember(1, "A", 0.0), RowMember(1, "A", 1.0)]
        secondary = [RowMember(1, "A", 0.0), RowMember(1, "A", 1.0)]
        assert compute_row_permutation(primary, secondary) is None

    def test_empty_rows_rejected(self) -> None:
        assert compute_row_permutation([], []) is None
        assert compute_facing_row_inversions([], []) == []


class TestViaHopLoserSelection:
    """The via-hop loser choice is deterministic and dedups per net."""

    def test_loser_is_later_name(self) -> None:
        assert choose_via_hop_loser(1, "DQ0", 2, "DQ7") == 2
        assert choose_via_hop_loser(2, "DQ7", 1, "DQ0") == 2

    def test_loser_dedup_preserves_order(self) -> None:
        # Full reversal of 4 nets: innermost nets lose repeatedly, but each
        # losing net should be hopped only once.
        names = ["A", "B", "C", "D"]
        primary, secondary = _reversed_rows(names)
        inversions = compute_facing_row_inversions(primary, secondary)
        losers = via_hop_loser_nets(inversions)
        # No duplicates.
        assert len(losers) == len(set(losers))

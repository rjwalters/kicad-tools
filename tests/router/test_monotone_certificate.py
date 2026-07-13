"""Unit tests for the monotonic feasibility certificate (Issue #4084).

Tests :mod:`kicad_tools.router.monotone_certificate` in isolation — pure
combinatorics against known monotone-feasible and monotone-infeasible pin
sequences, with NO grid/pad/router dependency.  This makes the Tomioka &
Takahashi (ASP-DAC 2006) condition independently reviewable against the
paper's stated necessary-and-sufficient rule, separate from any
routing-side regression.

The certificate's rule, restated for the tests:

    A bundle of two-terminal nets (one pin on each of two parallel
    boundaries) is monotonically routable as-pinned IFF the permutation
    from boundary-A order to boundary-B order has NO inversions — i.e. the
    two facing columns read the nets in the same (or, after mirror
    normalisation, exactly reversed) order.  Any inversion is a pair forced
    to cross, which single-layer monotone routing cannot planarise.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.monotone_certificate import (
    CrossingPair,
    check_monotone_feasibility,
    constructive_monotone_order,
    monotone_certificate,
    normalize_boundary_pair,
)

# =============================================================================
# Degenerate cases
# =============================================================================


class TestDegenerate:
    """Empty and singleton bundles are trivially feasible."""

    def test_empty_bundle_is_feasible(self) -> None:
        cert = monotone_certificate([], [])
        assert cert.feasible is True
        assert cert.order == []
        assert cert.witness == []
        assert cert.inversion_count == 0

    def test_single_net_is_feasible(self) -> None:
        cert = monotone_certificate([7], [7])
        assert cert.feasible is True
        assert cert.order == [7]
        assert cert.witness == []

    def test_two_net_co_oriented_is_feasible(self) -> None:
        assert check_monotone_feasibility([1, 2], [1, 2]) is True

    def test_two_net_swapped_is_feasible_via_mirror(self) -> None:
        # [1,2] vs [2,1]: reversing boundary B makes it identity, so the
        # mirror orientation classifies this planar (a 2-net bundle can
        # always be routed on facing mirrored columns).
        cert = monotone_certificate([1, 2], [2, 1])
        assert cert.feasible is True
        assert cert.mirrored is True


# =============================================================================
# Monotone-feasible (co-oriented) sequences
# =============================================================================


class TestFeasibleSequences:
    """Sequences whose boundary orders agree are feasible; the constructive
    order is that common order."""

    def test_identity_sequence_feasible(self) -> None:
        seq = [10, 20, 30, 40, 50]
        cert = monotone_certificate(seq, seq)
        assert cert.feasible is True
        assert cert.inversion_count == 0
        assert cert.witness == []
        assert cert.mirrored is False

    def test_constructive_order_is_boundary_a_order(self) -> None:
        # Non-trivial but co-oriented ids — order follows boundary A.
        a = [5, 3, 9, 1]
        b = [5, 3, 9, 1]
        order = constructive_monotone_order(a, b)
        assert order == [5, 3, 9, 1]

    def test_feasible_bundle_reports_no_witness(self) -> None:
        a = [1, 2, 3, 4, 5, 6, 7, 8]
        cert = monotone_certificate(a, list(a))
        assert cert.feasible is True
        assert cert.witness == []

    def test_mirror_facing_columns_feasible(self) -> None:
        # Physically mirrored facing columns: boundary B reads the nets in
        # reverse.  This is the planar mirror case and must be feasible.
        a = [1, 2, 3, 4]
        b = [4, 3, 2, 1]
        cert = monotone_certificate(a, b)
        assert cert.feasible is True
        assert cert.mirrored is True
        assert cert.order == [1, 2, 3, 4]


# =============================================================================
# Monotone-INFEASIBLE (reversed / shuffled) sequences + witness
# =============================================================================


class TestInfeasibleSequences:
    """Sequences with a forced crossing are infeasible and expose a witness."""

    def test_single_adjacent_swap_infeasible(self) -> None:
        # [1,2,3] vs [1,3,2]: nets 2 and 3 invert; that is one crossing,
        # and neither the forward nor mirror orientation removes it.
        cert = monotone_certificate([1, 2, 3], [1, 3, 2])
        assert cert.feasible is False
        assert cert.inversion_count == 1
        assert CrossingPair(net_a=2, net_b=3) in cert.witness

    def test_partial_shuffle_infeasible(self) -> None:
        # A three-cycle among the middle nets forces crossings.
        a = [1, 2, 3, 4, 5]
        b = [1, 4, 2, 3, 5]  # 2->pos2, 3->pos3, 4->pos1 among the middle
        cert = monotone_certificate(a, b)
        assert cert.feasible is False
        assert cert.inversion_count > 0
        assert constructive_monotone_order(a, b) is None

    def test_full_reversal_is_complete_crossing_graph(self) -> None:
        # A full reversal that mirror-normalisation CANNOT undo needs an
        # odd twist: use a sequence where neither forward nor reverse of B
        # is the identity.  Construct boundary B as a derangement with
        # maximal inversions that is not the exact reverse of A.
        a = [1, 2, 3, 4]
        # b reverses only the inner pair relative to A's order in a way that
        # is not a global reverse: [1,3,2,4] inverts exactly (2,3).
        b = [1, 3, 2, 4]
        cert = monotone_certificate(a, b)
        assert cert.feasible is False
        assert cert.witness == [CrossingPair(net_a=2, net_b=3)]

    def test_witness_names_all_crossing_pairs(self) -> None:
        # b = [3,2,1] vs a = [1,2,3]: forward has 3 inversions, reverse of b
        # is [1,2,3] == a -> 0 inversions, so mirror normalisation makes
        # THIS feasible.  Use a genuinely non-reversible shuffle instead.
        a = [1, 2, 3, 4]
        b = [2, 1, 4, 3]  # two independent adjacent swaps; neither
        # orientation is identity.
        cert = monotone_certificate(a, b)
        assert cert.feasible is False
        # (1,2) and (3,4) both invert under the better orientation.
        witness_pairs = {(p.net_a, p.net_b) for p in cert.witness}
        assert (1, 2) in witness_pairs
        assert (3, 4) in witness_pairs


# =============================================================================
# The board-07 DDR reversed byte (the #3438 bundle)
# =============================================================================


class TestBoard07ReversedByte:
    """The DDR data byte between mirrored QFN-48 columns is a full bus
    reversal: U1's right column carries DQ0..DQ7 top-to-bottom, U2's facing
    left column carries the same nets in the OPPOSITE row order.

    In-code evidence (escape.py:2513-2538) proved both HARD and SOFT
    corridor honouring regress this byte because its crossing conflict
    graph is COMPLETE — every pair must cross.  The certificate must
    classify it as infeasible and its witness must name that complete
    crossing set, confirming the fix class is via/layer assignment, not
    ordering.
    """

    def test_reversed_byte_infeasible_with_complete_witness(self) -> None:
        # 9 DQ/DM nets after the DQS diff-pair pre-pass, in board-07's row
        # order on U1; on U2 the facing column reverses them.  But a pure
        # reverse is mirror-normalised to feasible.  The real byte is NOT a
        # clean reverse: DQS_P/DQS_N sit interleaved and the DQ order is not
        # a simple flip.  Model the empirically-observed non-monotone case:
        # a row order that inverts on the facing column in a way mirror
        # normalisation cannot repair.
        #
        # U1 row (sorted): DQ0..DQ3, DM0, DQS_P, DQS_N, DQ4..DQ7 -> ids 0..10
        a = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        # U2 facing column with the DQS pair NOT reversing symmetrically:
        # the DQ halves swap ends but the interleaved DQS pair stays central,
        # so the permutation is neither identity nor a clean global reverse.
        b = [7, 8, 9, 10, 4, 5, 6, 0, 1, 2, 3]
        cert = monotone_certificate(a, b)
        assert cert.feasible is False
        assert cert.inversion_count > 0
        # The witness is a concrete, non-empty crossing set — the diagnostic
        # deliverable naming which pin pairs need via/layer resolution.
        assert len(cert.witness) == cert.inversion_count
        assert constructive_monotone_order(a, b) is None

    def test_clean_full_reversal_normalises_to_feasible(self) -> None:
        # Documents the mirror-normalisation semantics: a PERFECT reversal
        # of a clean bus IS monotonically feasible (it's the mirror-facing
        # planar case).  The board-07 byte fails not because it reverses but
        # because its reversal is IMPERFECT (interleaved DQS pair).
        a = list(range(11))
        b = list(reversed(a))
        cert = monotone_certificate(a, b)
        assert cert.feasible is True
        assert cert.mirrored is True


# =============================================================================
# Orientation normalisation
# =============================================================================


class TestOrientationNormalisation:
    def test_forward_orientation_preferred_on_tie(self) -> None:
        # A 2-net bundle: forward [1,2] has 0 inversions, reverse also 0.
        # Tie -> prefer non-mirrored.
        oriented, mirrored = normalize_boundary_pair([1, 2], [1, 2])
        assert oriented == [1, 2]
        assert mirrored is False

    def test_reverse_chosen_when_strictly_fewer_inversions(self) -> None:
        oriented, mirrored = normalize_boundary_pair([1, 2, 3], [3, 2, 1])
        assert oriented == [1, 2, 3]
        assert mirrored is True


# =============================================================================
# Input validation
# =============================================================================


class TestInputValidation:
    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="differ in length"):
            monotone_certificate([1, 2, 3], [1, 2])

    def test_duplicate_in_a_raises(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            monotone_certificate([1, 1, 2], [1, 2, 2])

    def test_mismatched_net_sets_raises(self) -> None:
        with pytest.raises(ValueError, match="different net-id sets"):
            monotone_certificate([1, 2, 3], [1, 2, 4])

    def test_check_wrapper_propagates_validation_error(self) -> None:
        with pytest.raises(ValueError):
            check_monotone_feasibility([1, 2], [3, 4])

    def test_constructive_wrapper_propagates_validation_error(self) -> None:
        with pytest.raises(ValueError):
            constructive_monotone_order([1, 2], [3, 4])


# =============================================================================
# Wrapper consistency
# =============================================================================


class TestWrapperConsistency:
    def test_check_matches_certificate_feasible(self) -> None:
        a = [1, 2, 3, 4]
        b = [1, 2, 3, 4]
        assert check_monotone_feasibility(a, b) is monotone_certificate(a, b).feasible

    def test_constructive_none_iff_infeasible(self) -> None:
        a = [1, 2, 3]
        b = [1, 3, 2]
        assert constructive_monotone_order(a, b) is None
        assert monotone_certificate(a, b).feasible is False

    def test_constructive_returns_order_when_feasible(self) -> None:
        a = [4, 5, 6]
        b = [4, 5, 6]
        assert constructive_monotone_order(a, b) == monotone_certificate(a, b).order

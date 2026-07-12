"""Facing-row inversion analysis for the scoped bundle river planner.

Issue #4053 (Phase 3 of epic #4049): a *scoped* river-routing planner for
straight facing-column bundles.  The board-07 DDR data byte is a **full
bus reversal** between two mirrored QFN-48 pin columns (U1's right column
carries ``DQ0`` at the top ``DQ7`` at the bottom; U2's facing left column
carries the same nets in the *opposite* row order), so every pair of nets
whose relative row order flips between the two columns must cross.  Planar
same-layer lane ordering cannot resolve a reversal (the "conflict graph"
of a full reversal is a complete graph): each crossing pair needs one net
to hop to an inner layer to pass under its partner.

This module owns the *pure, search-free* geometry step that the epic's
curation scoped for v1:

    resolve both facing rows -> diff their permutation -> emit the
    inversion set (the required crossing pairs) directly from the static
    placement.

Keeping it a free function (rather than a method on ``Autorouter``) makes
the crossing-set computation unit-testable on a small synthetic two-row
fixture without constructing a full router or board geometry, which the
curated Test Plan requires (assert the correct inversion pairs for a known
reversal AND an *empty* set for a genuinely planar, non-reversed two-row
fixture — the over-triggering regression guard).

The consumer (``Autorouter._apply_byte_lane_inner_priority``) reserves one
inner-layer via-hop corridor per inverted pair, gated behind the default-
OFF ``enable_bundle_river_planner`` flag (the #4051 precedent: even a
geometry-only auto-detect regressed production, so v1 ships opt-in).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RowMember:
    """One net's position on a facing row.

    Attributes:
        net_id: The net's integer id.
        net_name: The net's name (used to match members across the two
            facing rows — the bundle is a one-to-one name-matched bus).
        projection: The member's 1-D coordinate along the row's long axis
            (y for a vertical column, x for a horizontal one).  Only the
            *relative order* of projections is used, so the absolute
            coordinate system does not matter.
    """

    net_id: int
    net_name: str
    projection: float


@dataclass(frozen=True)
class InvertedPair:
    """A required crossing between two nets of a facing-column bundle.

    ``net_a`` / ``net_b`` are ordered so ``net_a`` is the *outer* net on
    the primary row (smaller sorted index): its relative row order flips
    versus ``net_b`` between the two facing columns, so on a single shared
    escape strip the two lanes must cross.  ``loser_net_id`` names the net
    v1 gives an inner-layer via-hop corridor to (the crossing net that
    dips under its partner); see ``choose_via_hop_loser`` for the rule.
    """

    net_a_id: int
    net_b_id: int
    net_a_name: str
    net_b_name: str
    loser_net_id: int


def _sorted_row(members: list[RowMember]) -> list[RowMember]:
    """Return members sorted by projection (ascending), name as tiebreak.

    The name tiebreak keeps the ordering deterministic when two pads share
    a projection (degenerate, but defends against nondeterministic output
    feeding the reservation pass).
    """
    return sorted(members, key=lambda m: (m.projection, m.net_name))


def compute_row_permutation(
    primary_row: list[RowMember],
    secondary_row: list[RowMember],
) -> list[tuple[str, int, int]] | None:
    """Diff the two facing rows into a per-net (primary_rank, secondary_rank).

    Both rows are sorted along their own long axis; each net's rank is its
    index in that sort.  The returned list is keyed by net name (the bus is
    matched one-to-one by name), one entry per net present in BOTH rows.

    Returns ``None`` when the rows do not form a clean one-to-one matched
    bus (different net-name sets between the rows), which is v1's explicit
    non-goal (partial reversals / non-matched net counts belong to #3673's
    shelved general planner).  A ``None`` return tells the caller to skip
    the planner for this group rather than guess.
    """
    if not primary_row or not secondary_row:
        return None

    primary_names = {m.net_name for m in primary_row}
    secondary_names = {m.net_name for m in secondary_row}
    # v1 restriction: exactly one-to-one name matching between the rows.
    if primary_names != secondary_names:
        return None
    if len(primary_names) != len(primary_row) or len(secondary_names) != len(secondary_row):
        # A net appears twice on a row -> not a clean single-column bus.
        return None

    primary_sorted = _sorted_row(primary_row)
    secondary_sorted = _sorted_row(secondary_row)

    primary_rank = {m.net_name: i for i, m in enumerate(primary_sorted)}
    secondary_rank = {m.net_name: i for i, m in enumerate(secondary_sorted)}
    net_id = {m.net_name: m.net_id for m in primary_sorted}

    perm: list[tuple[str, int, int]] = []
    for name in sorted(primary_rank, key=lambda n: primary_rank[n]):
        perm.append((name, net_id[name], secondary_rank[name]))
    return perm


def choose_via_hop_loser(
    net_a_id: int,
    net_a_name: str,
    net_b_id: int,
    net_b_name: str,
) -> int:
    """Pick which net of an inverted pair gets the inner-layer via hop.

    Deterministic rule: the net whose *name* sorts later takes the hop.
    The choice is arbitrary for correctness (either net can be the one to
    dip under) but must be stable so the reservation pass is reproducible
    across runs and the unit tests can pin it.  Name-sort (rather than
    net-id) keeps it independent of net-numbering churn between board
    regenerations.
    """
    if net_a_name <= net_b_name:
        return net_b_id
    return net_a_id


def compute_facing_row_inversions(
    primary_row: list[RowMember],
    secondary_row: list[RowMember],
) -> list[InvertedPair]:
    """Compute the crossing set (inverted pairs) between two facing rows.

    This is the search-free core of the v1 planner.  Two nets form an
    inverted pair when their relative order flips between the primary row
    and the secondary row — i.e. net A is above net B on the primary
    column but below it on the secondary column.  On a single shared
    F.Cu escape strip such a pair MUST cross, so one of the two needs an
    inner-layer via hop to pass under the other.

    For a *genuinely planar* (non-reversed) bundle — where both rows sort
    into the same net order — the inversion set is EMPTY, so the caller
    reserves no via-hop corridors and behaves exactly as before (the
    curated over-triggering regression guard).

    For a *full* bus reversal of ``n`` matched nets this returns all
    ``C(n, 2)`` pairs (every relative order flips).

    Args:
        primary_row: Members on the primary facing column.
        secondary_row: Members on the mirrored facing column.

    Returns:
        The list of inverted pairs (empty when the rows are co-oriented
        or when the rows are not a clean matched bus).  Ordering is
        deterministic (by the primary row's sorted rank of ``net_a``,
        then ``net_b``).
    """
    perm = compute_row_permutation(primary_row, secondary_row)
    if perm is None:
        return []

    inversions: list[InvertedPair] = []
    n = len(perm)
    for i in range(n):
        name_i, id_i, sec_i = perm[i]
        for j in range(i + 1, n):
            name_j, id_j, sec_j = perm[j]
            # perm is ordered by primary rank, so i < j means net_i is
            # above net_j on the primary column.  They cross iff net_i is
            # BELOW net_j on the secondary column (secondary rank flips).
            if sec_i > sec_j:
                loser = choose_via_hop_loser(id_i, name_i, id_j, name_j)
                inversions.append(
                    InvertedPair(
                        net_a_id=id_i,
                        net_b_id=id_j,
                        net_a_name=name_i,
                        net_b_name=name_j,
                        loser_net_id=loser,
                    )
                )
    return inversions


def via_hop_loser_nets(inversions: list[InvertedPair]) -> list[int]:
    """Return the distinct via-hop nets, one per inverted pair, deduped.

    A net can be the "loser" of several inverted pairs (in a full
    reversal the innermost nets lose many crossings).  For the corridor
    reservation pass we only need to hop each losing net ONCE — the hop
    lets it cross under every partner it inverts against — so this dedups
    the loser set preserving first-seen order (deterministic).
    """
    seen: set[int] = set()
    out: list[int] = []
    for pair in inversions:
        if pair.loser_net_id not in seen:
            seen.add(pair.loser_net_id)
            out.append(pair.loser_net_id)
    return out

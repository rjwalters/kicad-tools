"""Monotonic feasibility certificate for facing-boundary escape bundles.

Issue #4084 (Phase 1 of epic #4049, SER pre-stage).  Implements the
Tomioka & Takahashi (ASP-DAC 2006, IEEE 1594758) *necessary and
sufficient* condition for a bundle of two-terminal nets — each with one
pin on each of two **parallel boundaries** (facing pad columns) — to be
simultaneously connectable by **monotone** routes on a single layer, plus
the constructive net ordering that realises those routes when the
condition holds.

Why this matters (the SER problem class, FM1)
---------------------------------------------
The board-07 DDR data byte routes 11 nets between two mirrored QFN-48 pin
columns and fails 2/11 *even alone on an empty board* (#3438).  Greedy
per-net escape ordering seals corridors; three static permutations and one
reactive scheduler (#4051) all failed to beat the 10/11 identity baseline.
This is the textbook Simultaneous Escape Routing (SER) failure.  The
certificate is the untried lever: it decides *before any A\\* search*
whether the bundle can be planarised by ordering alone, and when it cannot,
it emits a **failure witness** naming the crossing pairs — direct
diagnostic input for the via/layer-assignment follow-ups (Phases 2/3),
which is a deliverable even when reach does not improve.

The condition, precisely
------------------------
Model each net ``i`` by the pair ``(a_i, b_i)`` where ``a_i`` is its rank
(position) along boundary A and ``b_i`` its rank along boundary B.  A
monotone route runs perpendicularly across the channel between the two
boundaries; along the boundary-parallel axis it advances monotonically from
``a_i`` to ``b_i``.  Two nets ``i`` and ``j`` are **forced to cross** iff
their relative order flips between the boundaries — i.e. ``a_i < a_j`` but
``b_i > b_j`` (an *inversion* of the boundary-A-to-boundary-B permutation).
A monotone single-layer realisation is **planar** (no crossings), so:

    The bundle is monotonically feasible as-pinned  <=>  the permutation
    from boundary-A order to boundary-B order has **no inversions** —
    i.e. it is the identity permutation.

This is the two-parallel-boundaries specialisation of Tomioka &
Takahashi's condition (the general result also admits the mirror case,
handled below by orientation normalisation): when the boundary-A ordering
and the boundary-B ordering agree, the nets nest without crossing and the
constructive order is simply that common order.  Any inversion is a pair
that *must* cross, which no single-layer monotone assignment can planarise
— exactly the board-07 reversed-byte situation, whose conflict graph is
the *complete* graph (every pair inverts).

Boundary orientation
--------------------
The two facing columns are physically mirrored: scanning both from the
same world-space direction, a *planar* (co-routable) bundle reads its nets
in **opposite** orders on the two columns (the classic "mirror" — like
reading a book through a mirror).  Callers pass each boundary's net
sequence in a consistent scan direction; :func:`normalize_boundary_pair`
resolves whether the identity or the reversal of boundary B is the
non-crossing target, so both co-oriented and mirror-facing placements are
classified correctly.  The certificate reports feasibility against the
*better* of the two orientations and records which one it used.

Scope
-----
Pure combinatorics: no grid, pad, or router dependency, so it is unit
testable in isolation against hand-constructed and paper-example pin
sequences.  The consumer
(:meth:`Autorouter._apply_byte_lane_inner_priority`) imports
:func:`check_monotone_feasibility` / :func:`constructive_monotone_order`
to gate escape ordering, and :class:`MonotoneCertificate` /
:func:`monotone_certificate` when it also wants the failure witness for
diagnostics.

Caveat (from the epic's literature survey): monotone/river theory assumes
single-layer, obstacle-free, two-point nets.  Real corridors with vias
fall outside the exact case, so this is used as a *certificate + prior*
(does ordering alone suffice? if not, which pairs must be resolved by
via/layer assignment?), never as a drop-in router.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CrossingPair:
    """One pair of nets forced to cross under a given boundary orientation.

    ``net_a`` precedes ``net_b`` along boundary A (``a_a < a_b``) but
    *follows* it along boundary B (``b_a > b_b``) — their relative order
    inverts between the two facing columns, so on a single shared escape
    layer the two monotone routes must intersect.  These pairs constitute
    the certificate's **failure witness**: the set that ordering alone
    cannot planarise and that a via/layer-assignment stage (Phase 2/3)
    must resolve.
    """

    net_a: int
    net_b: int


@dataclass(frozen=True)
class MonotoneCertificate:
    """Result of the Tomioka & Takahashi monotonic feasibility check.

    Attributes:
        feasible: ``True`` iff the bundle is monotonically routable
            as-pinned (the boundary-A-to-boundary-B permutation, under the
            chosen orientation, has no inversions).
        order: When ``feasible``, the constructive net order (nets in the
            common boundary order) that realises the non-crossing monotone
            routes.  Empty when infeasible.
        witness: When NOT ``feasible``, the crossing pairs that force the
            failure (every inverted pair under the chosen orientation).
            Empty when feasible.  This is the diagnostic deliverable: it
            names which pin pairs must be resolved by via/layer assignment.
        mirrored: Which boundary-B orientation was used to test feasibility
            — ``True`` if boundary B was reversed before comparison (the
            physical mirror-facing case), ``False`` for the co-oriented
            case.  Recorded so the consumer can report which orientation
            the classification assumed.
        inversion_count: Total inverted pairs under the chosen orientation
            (``0`` iff feasible; ``len(witness)``).  Exposed separately so
            a consumer can log "N of C(k,2) pairs cross" without walking
            the witness list.
    """

    feasible: bool
    order: list[int] = field(default_factory=list)
    witness: list[CrossingPair] = field(default_factory=list)
    mirrored: bool = False
    inversion_count: int = 0


def _validate_pin_sequences(
    pin_sequence_a: list[int],
    pin_sequence_b: list[int],
) -> None:
    """Raise ``ValueError`` if the two sequences are not a clean matched bus.

    The certificate is only meaningful when both boundaries carry the
    **same set** of net ids, each exactly once (a one-to-one matched
    two-terminal bundle).  Rather than silently returning a misleading
    classification, an ill-formed input raises so the caller can fall back
    to identity ordering deliberately.
    """
    if len(pin_sequence_a) != len(pin_sequence_b):
        raise ValueError(
            f"boundary sequences differ in length ({len(pin_sequence_a)} vs {len(pin_sequence_b)})"
        )
    set_a = set(pin_sequence_a)
    set_b = set(pin_sequence_b)
    if len(set_a) != len(pin_sequence_a):
        raise ValueError("boundary A sequence contains a duplicate net id")
    if len(set_b) != len(pin_sequence_b):
        raise ValueError("boundary B sequence contains a duplicate net id")
    if set_a != set_b:
        raise ValueError("boundary sequences carry different net-id sets")


def _count_inversions(perm: list[int]) -> tuple[int, list[tuple[int, int]]]:
    """Return (inversion count, inverted index pairs) of a permutation.

    ``perm[i]`` is the boundary-B rank of the net at boundary-A rank ``i``.
    An inversion is a pair ``i < j`` with ``perm[i] > perm[j]``: the two
    nets' relative order flips between the boundaries, so their monotone
    routes must cross.  For the small bundles this certificate targets
    (typically <= ~16 nets) the O(k^2) enumeration is trivial and, unlike
    a merge-sort inversion count, yields the actual crossing pairs needed
    for the witness.
    """
    inverted: list[tuple[int, int]] = []
    n = len(perm)
    for i in range(n):
        for j in range(i + 1, n):
            if perm[i] > perm[j]:
                inverted.append((i, j))
    return len(inverted), inverted


def normalize_boundary_pair(
    pin_sequence_a: list[int],
    pin_sequence_b: list[int],
) -> tuple[list[int], bool]:
    """Pick the boundary-B orientation with the fewest forced crossings.

    Two facing pad columns are physically mirrored, so a bundle that is
    perfectly co-routable can read its nets in *either* the same or the
    reversed order on boundary B depending on the scan convention the
    caller used.  Testing both orientations and keeping the one with fewer
    inversions makes the certificate robust to that convention: a genuine
    planar bundle scores zero inversions under exactly one orientation.

    Args:
        pin_sequence_a: Net ids in pin order along boundary A.
        pin_sequence_b: Net ids in pin order along boundary B (same set).

    Returns:
        ``(oriented_b, mirrored)`` where ``oriented_b`` is boundary B in
        the orientation to compare against boundary A, and ``mirrored`` is
        ``True`` iff boundary B was reversed to get there.  Ties (equal
        inversion counts) prefer the non-mirrored orientation for
        determinism.
    """
    rank_b_forward = {nid: i for i, nid in enumerate(pin_sequence_b)}
    perm_forward = [rank_b_forward[nid] for nid in pin_sequence_a]
    inv_forward, _ = _count_inversions(perm_forward)

    reversed_b = list(reversed(pin_sequence_b))
    rank_b_rev = {nid: i for i, nid in enumerate(reversed_b)}
    perm_rev = [rank_b_rev[nid] for nid in pin_sequence_a]
    inv_rev, _ = _count_inversions(perm_rev)

    if inv_rev < inv_forward:
        return reversed_b, True
    return list(pin_sequence_b), False


def monotone_certificate(
    pin_sequence_a: list[int],
    pin_sequence_b: list[int],
) -> MonotoneCertificate:
    """Compute the full Tomioka & Takahashi feasibility certificate.

    Args:
        pin_sequence_a: Net ids in pin order along boundary A.
        pin_sequence_b: Net ids in pin order along boundary B (same set of
            ids; each net has exactly one pin on each boundary).

    Returns:
        A :class:`MonotoneCertificate` with ``feasible``, the constructive
        ``order`` (when feasible), the crossing-pair ``witness`` (when
        not), the chosen ``mirrored`` orientation, and the
        ``inversion_count``.

    Raises:
        ValueError: if the two sequences are not a clean one-to-one matched
            bundle (different lengths, duplicates, or mismatched net sets).

    Degenerate cases: a 0- or 1-net bundle is trivially feasible (no pair
    can cross); its constructive order is the (possibly empty) boundary-A
    order.
    """
    _validate_pin_sequences(pin_sequence_a, pin_sequence_b)

    if len(pin_sequence_a) <= 1:
        return MonotoneCertificate(
            feasible=True,
            order=list(pin_sequence_a),
            witness=[],
            mirrored=False,
            inversion_count=0,
        )

    oriented_b, mirrored = normalize_boundary_pair(pin_sequence_a, pin_sequence_b)

    # perm[i] = boundary-B rank of the net at boundary-A rank i (under the
    # chosen orientation).  No inversions <=> identity permutation <=>
    # monotonically feasible; the constructive order is then the common
    # boundary order (boundary A's pin order).
    rank_b = {nid: i for i, nid in enumerate(oriented_b)}
    perm = [rank_b[nid] for nid in pin_sequence_a]
    inv_count, inverted_idx = _count_inversions(perm)

    if inv_count == 0:
        return MonotoneCertificate(
            feasible=True,
            order=list(pin_sequence_a),
            witness=[],
            mirrored=mirrored,
            inversion_count=0,
        )

    witness = [
        CrossingPair(net_a=pin_sequence_a[i], net_b=pin_sequence_a[j]) for i, j in inverted_idx
    ]
    return MonotoneCertificate(
        feasible=False,
        order=[],
        witness=witness,
        mirrored=mirrored,
        inversion_count=inv_count,
    )


def check_monotone_feasibility(
    pin_sequence_a: list[int],
    pin_sequence_b: list[int],
) -> bool:
    """Return ``True`` iff the bundle is monotonically routable as-pinned.

    Thin boolean wrapper over :func:`monotone_certificate` for callers that
    only need the yes/no decision (the two-parallel-boundaries condition of
    Tomioka & Takahashi, ASP-DAC 2006).  See that function for the full
    result including the constructive order and failure witness.

    Raises:
        ValueError: if the sequences are not a clean matched bundle.
    """
    return monotone_certificate(pin_sequence_a, pin_sequence_b).feasible


def constructive_monotone_order(
    pin_sequence_a: list[int],
    pin_sequence_b: list[int],
) -> list[int] | None:
    """Return the constructive non-crossing net order, or ``None``.

    When :func:`check_monotone_feasibility` holds, returns the net order
    (nets in the common boundary order) that realises the planar monotone
    routes — the order the escape scheduler should follow.  Returns
    ``None`` when the bundle is NOT monotonically feasible (there is no
    single-layer monotone order that avoids the forced crossings; the
    caller should fall back to identity and consult the witness from
    :func:`monotone_certificate`).

    Raises:
        ValueError: if the sequences are not a clean matched bundle.
    """
    cert = monotone_certificate(pin_sequence_a, pin_sequence_b)
    if not cert.feasible:
        return None
    return cert.order

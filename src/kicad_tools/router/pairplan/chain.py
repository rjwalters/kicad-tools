"""Diff-pair chain planning: which pad couples a pair's runs join (#6378).

Moved unchanged from :mod:`kicad_tools.router.lattice.coupled` (issue #6837,
slice 2c-i) so any routing engine can plan a pair's chain.  Pure geometry over
:class:`~kicad_tools.router.primitives.Pad`: no engine, no obstacle model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .geometry import Pt, dist

if TYPE_CHECKING:
    from ..primitives import Pad

_EPS = 1e-9


Couple = tuple[int, int]
"""A ``(p_index, n_index)`` P/N pad couple (indices into the input lists)."""


def _greedy_couples(pads_p: list[Pad], pads_n: list[Pad]) -> list[Couple]:
    """Couple each P pad with its nearest unclaimed N pad, nearest first."""
    cands = sorted(
        (dist((p.x, p.y), (n.x, n.y)), i, j)
        for i, p in enumerate(pads_p)
        for j, n in enumerate(pads_n)
    )
    couples: list[Couple] = []
    used_p: set[int] = set()
    used_n: set[int] = set()
    for _d, i, j in cands:
        if i in used_p or j in used_n:
            continue
        couples.append((i, j))
        used_p.add(i)
        used_n.add(j)
    return couples


def _couple_mid(pads_p: list[Pad], pads_n: list[Pad], c: Couple) -> Pt:
    p, n = pads_p[c[0]], pads_n[c[1]]
    return ((p.x + n.x) / 2.0, (p.y + n.y) / 2.0)


def _farthest_couples(
    pads_p: list[Pad], pads_n: list[Pad], couples: list[Couple]
) -> tuple[int, int]:
    """Indices into ``couples`` of the two couples whose midpoints are farthest apart."""
    best: tuple[float, int, int] | None = None
    for x in range(len(couples)):
        for y in range(x + 1, len(couples)):
            d = dist(
                _couple_mid(pads_p, pads_n, couples[x]), _couple_mid(pads_p, pads_n, couples[y])
            )
            if best is None or d > best[0]:
                best = (d, x, y)
    assert best is not None
    return best[1], best[2]


def choose_pair_endpoints(
    pads_p: list[Pad], pads_n: list[Pad]
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Pick the pair's main-run endpoints by proximity pairing.

    Greedily couples each P pad with its nearest unclaimed N pad, then
    returns the two couples whose midpoints are farthest apart as
    ``((ip_a, in_a), (ip_b, in_b))`` index tuples into the input lists.
    Returns ``None`` when fewer than two couples exist (degenerate pad
    topology -- the pair cannot form a coupled main run).

    These two couples are also the ENDS of :func:`choose_pair_chain`.
    """
    if not pads_p or not pads_n:
        return None
    couples = _greedy_couples(pads_p, pads_n)
    if len(couples) < 2:
        return None
    x, y = _farthest_couples(pads_p, pads_n, couples)
    return couples[x], couples[y]


@dataclass(frozen=True)
class PairChain:
    """A diff pair's signal path as an ordered chain of couples (#6378).

    Attributes:
        segments: Coupled runs ``(couple_from, couple_to)`` in chain order,
            end A first.  Exactly one segment (the farthest-apart trunk)
            when the pair passes through no interior part.
        hops: ``(entry_couple, exit_couple)`` inside each interior part
            that has distinct entry and exit couples -- routed as two
            ordinary single-ended connections (P hop, N hop), never as a
            coupled attempt.
    """

    segments: list[tuple[Couple, Couple]]
    hops: list[tuple[Couple, Couple]]


def choose_pair_chain(pads_p: list[Pad], pads_n: list[Pad]) -> PairChain | None:
    """Order a pair's couples along its signal path (issue #6378).

    Couples are formed and the two ENDS chosen exactly as
    :func:`choose_pair_endpoints` does (greedy nearest P/N, then the
    farthest-apart couples).  Couples are then grouped by footprint (the
    ref of both pads).  A couple whose P and N sit on different refs (one
    test point per net, or the two nets running through two different
    parts) never forms a part: it stays off the chain and is routed as an
    extra, as before.  An *interior part* is a same-ref group holding neither end couple
    whose couple-midpoint centroid projects strictly between the ends
    (``0 < t < 1`` along the end-to-end axis).  Interior parts are visited
    in projection order; each is entered at its couple nearest the previous
    chain node and left at its couple nearest the next one (the next
    part's centroid, or the far end).  A part with a single couple (a
    termination resistor tapping the pair, board 06 LVDS) or whose entry
    and exit coincide is not a pass-through and stays on the trunk.

    Board 03 USB_D: J1.B6/B7 -> U2.3/U2.1 (segment), U2.3/U2.1 ->
    U2.4/U2.6 (hop through the pass-through ESD part), U2.4/U2.6 ->
    R4.1/R3.1 (segment) -- instead of a J1 -> R trunk that has to cross
    U2's own pads.  Couples on an end part other than the end couple (the
    second row of a USB-C receptacle) and parts outside the end span stay
    off the chain: the caller routes them as extras, as before.

    With no interior part the result is the single farthest-apart trunk,
    identical to :func:`choose_pair_endpoints`.  Returns ``None`` when
    fewer than two couples exist.
    """
    return _pair_chain(pads_p, pads_n, split_at_taps=False)


def choose_pair_tap_chain(pads_p: list[Pad], pads_n: list[Pad]) -> PairChain | None:
    """The pair's chain split at its tap parts too (issue #6535).

    :func:`choose_pair_chain` keeps a tap -- a part holding a single couple,
    such as a termination resistor straddling P and N (board 06 LVDS R1..R4)
    -- on the single trunk.  When that trunk declines with ``pair-no-path``
    (the tap's own P/N pads straddle the straight corridor, and every pad is
    an obstacle to the fat agent), the caller may retry the pair as this
    chain: each tap becomes a chain node, so the pair runs IC -> tap -> IC
    and every segment ends *at* the tap's couple instead of having to get
    past it.  A tap contributes no hop (entry == exit); pass-through parts
    are chained exactly as :func:`choose_pair_chain` chains them.

    Returns ``None`` when the result would not differ from
    :func:`choose_pair_chain` (no tap part lies between the ends), so a
    caller never retries the same chain.
    """
    chain = _pair_chain(pads_p, pads_n, split_at_taps=True)
    if chain is None or chain == _pair_chain(pads_p, pads_n, split_at_taps=False):
        return None
    return chain


def _pair_chain(pads_p: list[Pad], pads_n: list[Pad], *, split_at_taps: bool) -> PairChain | None:
    """Shared body of :func:`choose_pair_chain` / :func:`choose_pair_tap_chain`."""
    if not pads_p or not pads_n:
        return None
    couples = _greedy_couples(pads_p, pads_n)
    if len(couples) < 2:
        return None
    ix, iy = _farthest_couples(pads_p, pads_n, couples)
    end_a, end_b = couples[ix], couples[iy]
    trunk = PairChain(segments=[(end_a, end_b)], hops=[])

    def group(c: Couple) -> object:
        ref_p, ref_n = pads_p[c[0]].ref, pads_n[c[1]].ref
        if ref_p and ref_p == ref_n:
            return ("ref", ref_p)
        return ("couple", c)

    g_a, g_b = group(end_a), group(end_b)
    if g_a == g_b:
        return trunk
    groups: dict[object, list[Couple]] = {}
    for c in couples:
        g = group(c)
        # Only a same-ref group is a part the pair passes through.  A couple
        # whose P and N pads sit on different refs (one test point per net,
        # or D+ and D- running through two different parts) is not a part:
        # it stays an extra, exactly as before #6378.
        if g in (g_a, g_b) or g[0] != "ref":  # type: ignore[index]
            continue
        groups.setdefault(g, []).append(c)
    # A tap (one couple on the part: a termination resistor straddling P and N)
    # has no distinct entry and exit -- the trunk passes it on a clear axis, so
    # it stays on the trunk.  Only a part with two or more couples is a
    # pass-through the signal enters on one side and leaves on the other.
    if not split_at_taps:
        groups = {g: m for g, m in groups.items() if len(m) >= 2}
    if not groups:
        return trunk

    m_a = _couple_mid(pads_p, pads_n, end_a)
    m_b = _couple_mid(pads_p, pads_n, end_b)
    ax, ay = m_b[0] - m_a[0], m_b[1] - m_a[1]
    len2 = ax * ax + ay * ay
    if len2 <= _EPS:
        return trunk
    interior: list[tuple[float, Pt, list[Couple]]] = []
    for members in groups.values():
        mids = [_couple_mid(pads_p, pads_n, c) for c in members]
        cx = sum(m[0] for m in mids) / len(mids)
        cy = sum(m[1] for m in mids) / len(mids)
        t = ((cx - m_a[0]) * ax + (cy - m_a[1]) * ay) / len2
        if 0.0 < t < 1.0:
            interior.append((t, (cx, cy), members))
    if not interior:
        return trunk
    interior.sort(key=lambda e: e[0])

    def nearest(members: list[Couple], to: Pt) -> Couple:
        return min(members, key=lambda c: (dist(_couple_mid(pads_p, pads_n, c), to), c))

    segments: list[tuple[Couple, Couple]] = []
    hops: list[tuple[Couple, Couple]] = []
    prev = end_a
    for k, (_t, _centroid, members) in enumerate(interior):
        nxt = interior[k + 1][1] if k + 1 < len(interior) else m_b
        entry = nearest(members, _couple_mid(pads_p, pads_n, prev))
        exit_ = nearest(members, nxt)
        if exit_ == entry:
            if not split_at_taps:
                continue  # no distinct exit: a tap, not a pass-through
            # Issue #6535: a tap is a chain node with no hop -- the segment
            # before it ends at its couple and the next one starts there.
            segments.append((prev, entry))
            prev = entry
            continue
        segments.append((prev, entry))
        hops.append((entry, exit_))
        prev = exit_
    if not segments:
        return trunk
    segments.append((prev, end_b))

    # Issue #6698: launch each end from the couple of its end PART that faces
    # the chain.  The farthest-apart pick keeps the far row of a dual-row
    # receptacle (board 03 J1.B6/B7), walled in behind the near row (A6/A7)
    # whose pads leave no room for a trace between them.  Re-pick, on the
    # same part only, the couple nearest the adjacent chain node; the other
    # row stays an extra (tied to it by the caller).  Hops are unchanged.
    def facing(end: Couple, g_end: object, to: Pt) -> Couple:
        if g_end[0] != "ref":  # type: ignore[index]
            return end  # P and N on different refs: not a part
        members = [c for c in couples if group(c) == g_end]
        if len(members) < 2:
            return end
        return min(
            members,
            key=lambda c: (dist(_couple_mid(pads_p, pads_n, c), to), c != end, c),
        )

    new_a = facing(end_a, g_a, _couple_mid(pads_p, pads_n, segments[0][1]))
    new_b = facing(end_b, g_b, _couple_mid(pads_p, pads_n, segments[-1][0]))
    segments[0] = (new_a, segments[0][1])
    segments[-1] = (segments[-1][0], new_b)
    return PairChain(segments=segments, hops=hops)

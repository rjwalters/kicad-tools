"""Per-pair skew attribution: where a finished pair's P-N length gap comes from (#6447).

A coupled pair is two offsets of one centerline.  At a mitred joint turning
``theta`` the outer leg gains, and the inner leg loses, ``pitch/2 *
tan(theta/2)`` on each of the two segments the joint joins, so every joint
adds ``2 * pitch * tan(theta / 2)`` to the P-N length difference (the
formula of :mod:`kicad_tools.router.pairplan.bend_balance`, here for any
angle).  Whatever a run's joints do not explain comes from how the legs
leave the run: a gateway fan or a via on one leg only (``via``), or a tie
or pad stub (``tie``).

:class:`SkewLedger` splits a finished pair into those sources.  The entries
plus :attr:`SkewLedger.residual_mm` always sum to :attr:`measured_skew_mm`,
so a caller can both name the source of a mismatch (a bump host sits next to
it) and see how much of the skew the ledger could not place.

Signs follow ``P - N``: a positive entry makes P longer.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Route

Pt = tuple[float, float]

#: Source kinds, in the order a report lists them.
KIND_JOINT = "joint"
KIND_VIA = "via"
KIND_TIE = "tie"
SOURCE_KINDS: tuple[str, ...] = (KIND_JOINT, KIND_VIA, KIND_TIE)

#: A via within this distance of a segment end splits the run there.
_VIA_SNAP_MM = 0.05
_EPS = 1e-9


@dataclass(frozen=True)
class SkewSource:
    """One place the pair's legs gain a length difference.

    Attributes:
        kind: One of :data:`SOURCE_KINDS`.
        x, y: Where it sits (a joint vertex, or the run end the leftover
            skew is attributed to).
        skew_mm: Its signed P-N contribution.
        run: Index of the via-delimited run it belongs to.
    """

    kind: str
    x: float
    y: float
    skew_mm: float
    run: int


@dataclass(frozen=True)
class SkewLedger:
    """The attributed skew of one pair.

    Attributes:
        measured_skew_mm: ``length(P) - length(N)`` of the two routes.
        sources: Attributed contributions.
        residual_mm: The part no source explains (``measured - sum``).
        pitch_mm: Centre-to-centre pitch the joint terms used.
        run_count: Number of via-delimited runs on the P leg (run indices of
            :attr:`sources` are in P's order).
        n_reversed: The N leg is routed from the far end, so its run ``k``
            is ledger run ``run_count - 1 - k``.
    """

    measured_skew_mm: float
    sources: tuple[SkewSource, ...]
    residual_mm: float
    pitch_mm: float
    run_count: int = 0
    n_reversed: bool = False

    @property
    def attributed_mm(self) -> float:
        """Sum of the sources' contributions."""
        return sum(s.skew_mm for s in self.sources)

    def total_by_kind(self) -> dict[str, float]:
        """Signed contribution of each kind in :data:`SOURCE_KINDS`."""
        out = dict.fromkeys(SOURCE_KINDS, 0.0)
        for s in self.sources:
            out[s.kind] = out.get(s.kind, 0.0) + s.skew_mm
        return out

    def anchors(self, run: int | None = None) -> list[Pt]:
        """Points of the sources worth compensating next to.

        Sources that cancel to nothing are skipped.  ``run`` limits the list
        to one via-delimited run (the bump gates' "same run" rule).
        """
        return [
            (s.x, s.y)
            for s in self.sources
            if abs(s.skew_mm) > _EPS and (run is None or s.run == run)
        ]

    def host_run(self, route: Route, seg_idx: int, *, host_is_p: bool) -> int | None:
        """Ledger run holding segment ``seg_idx`` of ``route``, or ``None``.

        ``None`` when the leg's runs do not line up with the ledger's (the
        route changed shape since the ledger was built).
        """
        k = _segment_run(route, seg_idx)
        if k is None:
            return None
        if host_is_p:
            return k if k < self.run_count else None
        if k >= self.run_count:
            return None
        return self.run_count - 1 - k if self.n_reversed else k

    def dominant(self, run: int | None = None) -> SkewSource | None:
        """The source with the largest contribution of the sign that needs fixing.

        "Needs fixing" is the sign of the measured skew: the entry that most
        explains it.  ``None`` when no source has that sign.
        """
        sign = 1.0 if self.measured_skew_mm >= 0.0 else -1.0
        pool = [
            s for s in self.sources if s.skew_mm * sign > _EPS and (run is None or s.run == run)
        ]
        return max(pool, key=lambda s: abs(s.skew_mm), default=None)


#: Two segment ends this close are the same polyline vertex (walk order).
_JOIN_MM = 1e-4


def _walk_order(route: Route) -> list[tuple[int, bool]]:
    """``(segment index, reversed)`` of ``route`` in copper order (#7864).

    A gateway pair route is emitted as ``[fan A, fan B, coupled body]``:
    its list order is not its copper order, so the runs read off the list
    pair the wrong pieces (board 06rf ``USB3_RX1``: "run 1" was the U2-end
    fan and "run 2" the body, so the bump gate measured a body bump to the
    U2 end, 37.8 mm away).  When the non-degenerate segments form one
    simple path, this is that path from the end the list starts nearest,
    each segment flipped as needed; otherwise (a branch, a gap) it is the
    list order, unchanged.
    """
    idx = [i for i, s in enumerate(route.segments) if math.hypot(s.x2 - s.x1, s.y2 - s.y1) > _EPS]
    listed = [(i, False) for i in idx]
    if len(idx) < 2:
        return listed

    def key(x: float, y: float) -> tuple[int, int]:
        return (round(x / _JOIN_MM), round(y / _JOIN_MM))

    ends: dict[tuple[int, int], list[int]] = {}
    for i in idx:
        s = route.segments[i]
        for k in {key(s.x1, s.y1), key(s.x2, s.y2)}:
            ends.setdefault(k, []).append(i)
    if any(len(v) > 2 for v in ends.values()):
        return listed
    tips = [k for k, v in ends.items() if len(v) == 1]
    if len(tips) != 2:
        return listed
    first = route.segments[idx[0]]
    start = min(
        tips, key=lambda k: math.hypot(k[0] * _JOIN_MM - first.x1, k[1] * _JOIN_MM - first.y1)
    )
    order: list[tuple[int, bool]] = []
    used: set[int] = set()
    at = start
    while True:
        nxt = [i for i in ends[at] if i not in used]
        if not nxt:
            break
        i = nxt[0]
        s = route.segments[i]
        rev = key(s.x1, s.y1) != at
        order.append((i, rev))
        used.add(i)
        at = key(s.x1, s.y1) if rev else key(s.x2, s.y2)
    return order if len(order) == len(idx) else listed


def _split_runs(route: Route) -> tuple[list[list[Pt]], dict[int, int]]:
    """Via-delimited, single-layer polylines of ``route`` in copper order
    (:func:`_walk_order`), plus the run each segment index belongs to
    (zero-length segments are not mapped)."""
    runs: list[list[Pt]] = []
    seg_run: dict[int, int] = {}
    cur: list[Pt] = []
    prev_end: Pt | None = None
    prev_layer = None
    for i, rev in _walk_order(route):
        seg = route.segments[i]
        a, b = (seg.x1, seg.y1), (seg.x2, seg.y2)
        if rev:
            a, b = b, a
        split = prev_end is not None and (
            prev_layer != seg.layer
            or math.dist(prev_end, a) > _VIA_SNAP_MM
            or any(
                math.hypot(v.x - prev_end[0], v.y - prev_end[1]) <= _VIA_SNAP_MM
                or math.hypot(v.x - a[0], v.y - a[1]) <= _VIA_SNAP_MM
                for v in route.vias
            )
        )
        if split and cur:
            runs.append(cur)
            cur = []
        if not cur:
            cur = [a]
        cur.append(b)
        seg_run[i] = len(runs)
        prev_end, prev_layer = b, seg.layer
    if cur:
        runs.append(cur)
    return runs, seg_run


def _runs(route: Route) -> list[list[Pt]]:
    """Via-delimited, single-layer polylines of ``route``, in order."""
    return _split_runs(route)[0]


def _segment_run(route: Route, seg_idx: int) -> int | None:
    """Index of the run holding segment ``seg_idx`` of ``route``."""
    return _split_runs(route)[1].get(seg_idx)


def _length(points: Sequence[Pt]) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))


def _orient(p_runs: list[list[Pt]], n_runs: list[list[Pt]]) -> list[list[Pt]]:
    """``n_runs`` reversed when the N leg is routed from the far end."""
    if not p_runs or not n_runs:
        return n_runs
    near = math.dist(p_runs[0][0], n_runs[0][0])
    far = math.dist(p_runs[0][0], n_runs[-1][-1])
    if far + _EPS < near:
        return [list(reversed(r)) for r in reversed(n_runs)]
    return n_runs


def _joint_terms(p: list[Pt], n: list[Pt], pitch: float) -> list[tuple[Pt, float]]:
    """``((x, y), signed P-N skew)`` for each interior joint of one run pair."""
    out: list[tuple[Pt, float]] = []
    centre = [((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0) for a, b in zip(p, n, strict=True)]
    for i in range(1, len(centre) - 1):
        d_in = (centre[i][0] - centre[i - 1][0], centre[i][1] - centre[i - 1][1])
        d_out = (centre[i + 1][0] - centre[i][0], centre[i + 1][1] - centre[i][1])
        if math.hypot(*d_in) <= _EPS or math.hypot(*d_out) <= _EPS:
            continue
        turn = math.atan2(
            d_in[0] * d_out[1] - d_in[1] * d_out[0], d_in[0] * d_out[0] + d_in[1] * d_out[1]
        )
        if abs(turn) <= 1e-9 or abs(turn) >= math.pi - 1e-9:
            continue
        # Which side P is on, from this vertex's own offset: > 0 is left of travel.
        side = d_in[0] * (p[i][1] - n[i][1]) - d_in[1] * (p[i][0] - n[i][0])
        if abs(side) <= _EPS:
            continue
        p_left = side > 0.0
        # A left turn shortens the left leg; the right leg is the outer one.
        p_inner = p_left == (turn > 0.0)
        mag = 2.0 * pitch * math.tan(abs(turn) / 2.0)
        out.append((centre[i], -mag if p_inner else mag))
    return out


#: A leg piece within this of the pitch from a parallel partner piece is
#: coupled (:func:`_leg_joint_terms`).
_COUPLED_TOL_MM = 0.02
#: Parallel within this sine (the impedance rule's tolerance).
_PARALLEL_SIN = 0.1


def _coupled_partner(a: Pt, b: Pt, other: list[Pt], pitch: float) -> Pt | None:
    """A point of a piece of ``other`` running beside ``a-b`` at ``pitch``.

    ``None`` when no piece of ``other`` is parallel to ``a-b``, overlaps it
    along its length and sits ``pitch`` away (centre to centre).
    """
    length = math.dist(a, b)
    if length <= _EPS:
        return None
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    for c, d in zip(other, other[1:], strict=False):
        olen = math.dist(c, d)
        if olen <= _EPS:
            continue
        if abs(ux * (d[1] - c[1]) - uy * (d[0] - c[0])) / olen > _PARALLEL_SIN:
            continue
        mx, my = (c[0] + d[0]) / 2.0, (c[1] + d[1]) / 2.0
        if abs(abs((mx - a[0]) * uy - (my - a[1]) * ux) - pitch) > _COUPLED_TOL_MM:
            continue
        t1 = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
        t2 = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
        if min(max(t1, t2), length) - max(min(t1, t2), 0.0) > _EPS:
            return (mx, my)
    return None


def _leg_joint_terms(p: list[Pt], n: list[Pt], pitch: float) -> list[tuple[Pt, float]]:
    """Joint terms of a run pair whose legs have differing vertex counts (#7864).

    The pairwise centreline of :func:`_joint_terms` needs one N vertex per P
    vertex; a gateway body's convergence jogs into its via pairs give the
    legs different counts (06rf ``USB3_RX1``: 17 P vertices, 18 N), which
    used to leave the whole body -- every joint of its winding -- as a single
    entry at one run end.  Here each interior P vertex whose two pieces both
    run coupled beside N at the pitch is a joint of P's own turn (the
    offset legs of a coupled run turn together); P is inner when N sits on
    the outside of the turn.  Joints in a jog are not coupled and stay with
    the run's leftover.
    """
    out: list[tuple[Pt, float]] = []
    for i in range(1, len(p) - 1):
        d_in = (p[i][0] - p[i - 1][0], p[i][1] - p[i - 1][1])
        d_out = (p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1])
        if math.hypot(*d_in) <= _EPS or math.hypot(*d_out) <= _EPS:
            continue
        turn = math.atan2(
            d_in[0] * d_out[1] - d_in[1] * d_out[0], d_in[0] * d_out[0] + d_in[1] * d_out[1]
        )
        if abs(turn) <= 1e-9 or abs(turn) >= math.pi - 1e-9:
            continue
        q_in = _coupled_partner(p[i - 1], p[i], n, pitch)
        q_out = _coupled_partner(p[i], p[i + 1], n, pitch)
        if q_in is None or q_out is None:
            continue
        # Which side N is on, from the incoming piece: > 0 is left of travel.
        side = d_in[0] * (q_in[1] - p[i - 1][1]) - d_in[1] * (q_in[0] - p[i - 1][0])
        if abs(side) <= _EPS:
            continue
        p_left = side < 0.0
        p_inner = p_left == (turn > 0.0)
        mag = 2.0 * pitch * math.tan(abs(turn) / 2.0)
        # Report at the centreline vertex, half a pitch toward N.
        nx, ny = -d_in[1], d_in[0]
        norm = math.hypot(nx, ny)
        sgn = 1.0 if side > 0.0 else -1.0
        at = (p[i][0] + sgn * nx / norm * pitch / 2.0, p[i][1] + sgn * ny / norm * pitch / 2.0)
        out.append((at, -mag if p_inner else mag))
    return out


def _coupled_pitch(p_runs: list[list[Pt]], n_runs: list[list[Pt]]) -> float | None:
    """The legs' coupled centre-to-centre spacing, or ``None`` (#7864).

    The spacing (to 1 um) over which the most P length runs parallel to,
    and overlapping, an N piece of the same run.  Run starts are no measure
    of it when a run begins at pads or fan vias wider than the pitch (06rf
    ``USB3_RX1``'s runs start 1.0 and 0.8 mm apart, its body runs 0.376 mm).
    """
    weight: dict[float, float] = {}
    for p, n in zip(p_runs, n_runs, strict=False):
        for a, b in zip(p, p[1:], strict=False):
            length = math.dist(a, b)
            if length <= _EPS:
                continue
            ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
            best: tuple[float, float] | None = None
            for c, d in zip(n, n[1:], strict=False):
                olen = math.dist(c, d)
                if olen <= _EPS:
                    continue
                if abs(ux * (d[1] - c[1]) - uy * (d[0] - c[0])) / olen > _PARALLEL_SIN:
                    continue
                t1 = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
                t2 = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
                overlap = min(max(t1, t2), length) - max(min(t1, t2), 0.0)
                if overlap <= _EPS:
                    continue
                mx, my = (c[0] + d[0]) / 2.0, (c[1] + d[1]) / 2.0
                gap = abs((mx - a[0]) * uy - (my - a[1]) * ux)
                if gap > _EPS and (best is None or gap < best[0]):
                    best = (gap, overlap)
            if best is not None:
                k = round(best[0], 3)
                weight[k] = weight.get(k, 0.0) + best[1]
    if not weight:
        return None
    return max(weight.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def build_skew_ledger(
    p_route: Route, n_route: Route, *, pitch_mm: float | None = None
) -> SkewLedger:
    """Attribute the P-N length difference of a finished pair to its sources.

    Args:
        p_route: The P leg.
        n_route: The N leg.
        pitch_mm: Centre-to-centre pitch.  Default: measured, as the mean
            separation of paired run starts.

    Runs are paired by order.  When the legs do not split into the same
    number of runs, or a run pair has differing vertex counts, joints are not
    placed for that run: its whole skew becomes one ``via`` or ``tie`` entry
    (a run that starts or ends at a via is ``via``).
    """
    p_runs = _runs(p_route)
    n_raw = _runs(n_route)
    n_runs = _orient(p_runs, n_raw)
    n_reversed = n_runs is not n_raw
    measured = sum(_length(r) for r in p_runs) - sum(_length(r) for r in n_runs)

    pairable = len(p_runs) == len(n_runs)
    pitch = pitch_mm
    if pitch is None:
        pitch = _coupled_pitch(p_runs, n_runs) if pairable else None
    if pitch is None:
        gaps = [math.dist(p[0], n[0]) for p, n in zip(p_runs, n_runs, strict=False)]
        pitch = sum(gaps) / len(gaps) if gaps else 0.0

    sources: list[SkewSource] = []
    if pairable:
        for k, (p, n) in enumerate(zip(p_runs, n_runs, strict=True)):
            run_skew = _length(p) - _length(n)
            joints = (
                _joint_terms(p, n, pitch) if len(p) == len(n) else _leg_joint_terms(p, n, pitch)
            )
            for (x, y), s in joints:
                sources.append(SkewSource(KIND_JOINT, x, y, s, k))
            left = run_skew - sum(s for _, s in joints)
            if abs(left) > _EPS:
                at_via = len(p_runs) > 1
                # Put it at the run end where the legs stray most from the pitch.
                ends = [(p[0], n[0]), (p[-1], n[-1])]
                a, b = max(ends, key=lambda e: abs(math.dist(*e) - pitch))
                sources.append(
                    SkewSource(
                        KIND_VIA if at_via else KIND_TIE,
                        (a[0] + b[0]) / 2.0,
                        (a[1] + b[1]) / 2.0,
                        left,
                        k,
                    )
                )
    attributed = sum(s.skew_mm for s in sources)
    return SkewLedger(
        measured_skew_mm=measured,
        sources=tuple(sources),
        residual_mm=measured - attributed,
        pitch_mm=pitch,
        run_count=len(p_runs),
        n_reversed=n_reversed,
    )

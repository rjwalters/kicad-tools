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
    """

    measured_skew_mm: float
    sources: tuple[SkewSource, ...]
    residual_mm: float
    pitch_mm: float

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


def _runs(route: Route) -> list[list[Pt]]:
    """Via-delimited, single-layer polylines of ``route``, in order."""
    runs: list[list[Pt]] = []
    cur: list[Pt] = []
    prev = None
    for seg in route.segments:
        a, b = (seg.x1, seg.y1), (seg.x2, seg.y2)
        if math.dist(a, b) <= _EPS:
            continue
        split = prev is not None and (
            prev.layer != seg.layer
            or any(
                math.hypot(v.x - prev.x2, v.y - prev.y2) <= _VIA_SNAP_MM
                or math.hypot(v.x - seg.x1, v.y - seg.y1) <= _VIA_SNAP_MM
                for v in route.vias
            )
        )
        if split and cur:
            runs.append(cur)
            cur = []
        if not cur:
            cur = [a]
        cur.append(b)
        prev = seg
    if cur:
        runs.append(cur)
    return runs


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
    n_runs = _orient(p_runs, _runs(n_route))
    measured = sum(_length(r) for r in p_runs) - sum(_length(r) for r in n_runs)

    pairable = len(p_runs) == len(n_runs)
    pitch = pitch_mm
    if pitch is None:
        gaps = [math.dist(p[0], n[0]) for p, n in zip(p_runs, n_runs, strict=False)]
        pitch = sum(gaps) / len(gaps) if gaps else 0.0

    sources: list[SkewSource] = []
    if pairable:
        for k, (p, n) in enumerate(zip(p_runs, n_runs, strict=True)):
            run_skew = _length(p) - _length(n)
            joints = _joint_terms(p, n, pitch) if len(p) == len(n) else []
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
    )

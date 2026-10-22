"""Geometry gates for diff-pair skew-compensation bumps (issue #7521, AC3).

Operator ruling Q16 (best-practice intra-pair skew compensation) bounds where
and how large a skew bump may be.  Sources are cited in
``docs/research/diffpair-skew-compensation.md``: [N1][T] for excursion and
jog, [T][R1] for proximity:

* **Excursion** at most :data:`MAX_EXCURSION_GAPS` x the nominal pair gap.
  The sources (AN10373's S1 < 2S, with S1 the leg spacing inside the bump)
  bound the push-out at less than one gap (operator ruling Q27).  Operator
  ruling Q35 relaxes that to 2x as an **interim**, until source-side skew
  avoidance exists; issue #7649 tracks moving back to 1x.
* **Jog** (a run of the bump parallel to its host line) at least
  :data:`MIN_JOG_WIDTHS` x the trace width, so no loop is a sub-width stub.
* **Proximity**: the bump sits within :data:`MAX_SOURCE_DISTANCE_MM` of a
  mismatch source, in the same via-delimited segment.  The sources are the
  ends of that via-delimited run (a pad or a via, where the legs part ways) and
  the partner's vias that land on it.  A bump never hops a via to compensate.

The gates apply to every candidate, square or chamfered
(:data:`~kicad_tools.router.diffpair_length_tuning.SKEW_BUMP_CHAMFER_WIDTHS`),
and measure the geometry the generator actually produced.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .tuning_pattern import _closest_point

if TYPE_CHECKING:
    from .optimizer.serpentine import SerpentineConfig
    from .primitives import Route, Segment

#: Excursion limit, in nominal pair gaps (edge to edge).  A bump may reach it.
#:
#: INTERIM (operator ruling Q35, issue #7649): 2x the gap.  The sources
#: (AN10373's S1 < 2S) and operator ruling Q27 put the limit below 1x, but at
#: < 1x each chamfered bump adds only ~0.83 h of length, too little to close
#: most skews (board 06 failed its skew gate on 5 pairs, the bench boards 15
#: of 20).  Issue #7649 tracks source-side skew avoidance (symmetric breakout,
#: balanced bends, polarity swap) and the return of this constant to 1x.
MAX_EXCURSION_GAPS: float = 2.0

#: The generator sizes bumps to this fraction of the limit, so a bump the
#: tuner builds sits under the cap with a margin for coordinate rounding.
EXCURSION_HEADROOM: float = 0.99

#: Smallest jog (flat run parallel to the host line), in trace widths.
MIN_JOG_WIDTHS: float = 3.0

#: Farthest a bump may sit from its mismatch source (mm).
MAX_SOURCE_DISTANCE_MM: float = 15.0

#: A partner via this close to the host's run is a mismatch source on it.
#: Judgement value (not from the source notes), in the style of the
#: *Judgement:* entries of ``docs/research/diffpair-skew-compensation.md``.
PARTNER_VIA_REACH_MM: float = 2.0

_EPS_MM = 1e-6
#: A via within this distance of two segments' shared end splits them.
_VIA_SNAP_MM = 0.05


def jog_spaced(config: SerpentineConfig, width: float) -> SerpentineConfig:
    """``config`` with loop spacing wide enough for :data:`MIN_JOG_WIDTHS` jogs.

    The generator's flat tops and connectors are ``min_spacing * gap_factor``
    long, so raising ``min_spacing`` is what lengthens them.
    """
    floor = MIN_JOG_WIDTHS * width / config.gap_factor
    if config.min_spacing >= floor:
        return config
    return dataclasses.replace(config, min_spacing=floor * (1.0 + 1e-9))


def max_excursion_mm(nominal_gap_mm: float) -> float:
    """The excursion limit for a pair whose edge-to-edge gap is ``nominal_gap_mm``.

    A bump may reach it but not exceed it (the 2x interim of issue #7649); see
    :func:`bump_amplitude_cap_mm` for the amplitude the generator is given.
    """
    return MAX_EXCURSION_GAPS * nominal_gap_mm


def bump_amplitude_cap_mm(nominal_gap_mm: float) -> float:
    """Largest loop amplitude the generator may use for ``nominal_gap_mm``."""
    return EXCURSION_HEADROOM * max_excursion_mm(nominal_gap_mm)


def bump_gate_violation(
    new_segments: Sequence[Segment],
    host_route: Route,
    host_idx: int,
    partner_route: Route,
    *,
    nominal_gap_mm: float,
) -> str | None:
    """Why a candidate bump breaks an AC3 gate, or ``None`` when it passes.

    Args:
        new_segments: The inserted serpentine (lead-in and lead-out included),
            replacing one host segment of ``host_route``.
        host_route: The tuned leg *before* the insert.
        host_idx: Index in ``host_route`` of the segment ``new_segments``
            replaced (:func:`replaced_index`).
        partner_route: The other leg of the pair.
        nominal_gap_mm: The pair's edge-to-edge gap.
    """
    if len(new_segments) < 2:
        return None
    ax, ay = new_segments[0].x1, new_segments[0].y1
    bx, by = new_segments[-1].x2, new_segments[-1].y2
    length = math.hypot(bx - ax, by - ay)
    if length <= _EPS_MM:
        return None
    ux, uy = (bx - ax) / length, (by - ay) / length

    def offset(x: float, y: float) -> float:
        return ux * (y - ay) - uy * (x - ax)

    # Excursion: the farthest any point of the bump strays from the host line.
    limit = max_excursion_mm(nominal_gap_mm)
    excursion = max(
        abs(offset(x, y)) for s in new_segments for x, y in ((s.x1, s.y1), (s.x2, s.y2))
    )
    # Issue #7649: the limit is the 2x interim of operator ruling Q35.
    if limit > 0.0 and excursion > limit + _EPS_MM:
        return (
            f"bump excursion {excursion:.3f} mm exceeds {MAX_EXCURSION_GAPS:g}x the "
            f"pair gap ({limit:.3f} mm)"
        )

    # Jog: every run parallel to the host line, bar the entry and exit leads.
    for s in new_segments[1:-1]:
        seg_len = math.hypot(s.x2 - s.x1, s.y2 - s.y1)
        if seg_len <= _EPS_MM or abs(offset(s.x1, s.y1) - offset(s.x2, s.y2)) > _EPS_MM:
            continue
        if seg_len + _EPS_MM < MIN_JOG_WIDTHS * s.width:
            return (
                f"bump jog {seg_len:.3f} mm is under {MIN_JOG_WIDTHS:g}x the trace width "
                f"({MIN_JOG_WIDTHS * s.width:.3f} mm)"
            )

    # Proximity to a mismatch source within the host's via-delimited run.
    anchors = _section_anchors(host_route, partner_route, host_idx)
    if anchors:
        # The bump's own copper only: the lead-in and lead-out span the whole
        # host segment, so their ends sit on the host line at its endpoints
        # and would make a bump mid-host look close to a source.
        points = [
            (x, y)
            for s in new_segments
            for x, y in ((s.x1, s.y1), (s.x2, s.y2))
            if abs(offset(x, y)) > _EPS_MM
        ]
        if not points:
            return None
        nearest = min(math.hypot(px - qx, py - qy) for px, py in points for qx, qy in anchors)
        if nearest > MAX_SOURCE_DISTANCE_MM + _EPS_MM:
            return (
                f"bump is {nearest:.1f} mm from the nearest mismatch source "
                f"(limit {MAX_SOURCE_DISTANCE_MM:g} mm)"
            )
    return None


def replaced_index(host_route: Route, candidate: Route) -> int:
    """Index of the host segment ``candidate`` replaced.

    A candidate shares its leading segments with ``host_route`` by identity
    and then continues with the inserted serpentine.
    """
    idx = 0
    for a, b in zip(host_route.segments, candidate.segments, strict=False):
        if a is not b:
            break
        idx += 1
    return idx


def _section_anchors(
    host_route: Route, partner_route: Route, host_idx: int
) -> list[tuple[float, float]]:
    """Mismatch sources of the via-delimited run holding segment ``host_idx``."""
    segs = host_route.segments
    if not segs or host_idx >= len(segs):
        return []

    def split_after(i: int) -> bool:
        a, b = segs[i], segs[i + 1]
        if a.layer != b.layer:
            return True
        return any(
            math.hypot(v.x - a.x2, v.y - a.y2) <= _VIA_SNAP_MM
            or math.hypot(v.x - b.x1, v.y - b.y1) <= _VIA_SNAP_MM
            for v in host_route.vias
        )

    first = host_idx
    while first > 0 and not split_after(first - 1):
        first -= 1
    last = host_idx
    while last < len(segs) - 1 and not split_after(last):
        last += 1
    section = segs[first : last + 1]
    anchors = [(section[0].x1, section[0].y1), (section[-1].x2, section[-1].y2)]
    for v in partner_route.vias:
        near = _closest_point(v.x, v.y, section)
        if near is not None and math.hypot(near[0] - v.x, near[1] - v.y) <= PARTNER_VIA_REACH_MM:
            anchors.append(near)
    return anchors

"""Tail fold: skew compensation where the pair's coupling is already broken (Issue #7891).

When the shorter leg of a pair ends (at its pad) while the longer leg runs on,
the longer leg's tail is uncoupled.  A bump anywhere else costs the partner
uncoupled length of its own, and under a binding uncoupled limit there may be
no headroom left for it: #7542's 0.2 mm pair (skew 2.0 mm, limit 2.1 mm) has
0.1 mm, while one loop costs at least 0.26 mm (Issue #7546).  The tail fold
adds the length *beside the partner's uncoupled tail* instead, so the copper
it adds couples to what was uncoupled.

Shape (host frame: ``u`` along the terminal segment towards the terminal
``T``, ``v`` its outer normal, away from the partner)::

        top1 (v = A)          top2         U-turn
    ____/------------------|--------------\
   /                                       |
  host ..... x0                 T <--------/   return run, back into T
                                (pad)

* The leg leaves the host line ``D`` before ``T`` (a 0.02 mm foot chamfer, a
  rise, a crown chamfer), runs at offset ``A`` *past* ``T`` by ``e``, turns
  back through four 45-degree corners (crown chamfer, a short vertical, foot
  chamfer), and returns along the host line into ``T`` from the far side.
* Every corner turns 45 degrees (operator ruling Q32); a single 135-degree
  return leg, which the #7546 builder measured first, is not used.
* The top is two collinear segments split just before ``T``'s pad (``top1``,
  ``top2``).  kicad-cli 10.0.6 anchors each parallel pairing at the partner's
  stored start; with the partner stored the other way the probe of the top's
  far end crosses the return run and the whole top reads uncoupled (9.4 mm on
  the #7542 pair).  Split, only ``top2`` is blocked: the same pair reads
  0.86 mm one way and 1.65 mm the other, in both kct and pinned kicad-cli.
  The tuner's uncoupled guard measures a fold in **both** partner directions.
* Copper added: ``2A + 2e`` plus the chamfer terms (:func:`fold_length_added`),
  so the smallest fold adds about ``2W``; small skews are left to bumps.

A fold is only an *option*: the tuner runs it through every gate a bump goes
through (excursion, Q16 jog, mismatch-source proximity, the own-copper rule of
:mod:`~kicad_tools.router.diffpair_own_copper`, the foreign-copper gate and
the uncoupled guard), and only when no bump closes the pair.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from .diffpair_bump_gates import MIN_JOG_WIDTHS
from .optimizer.serpentine import SerpentineResult

if TYPE_CHECKING:
    from .diffpair_own_copper import OwnCopperRule
    from .optimizer.serpentine import SerpentineConfig
    from .primitives import Route, Segment

_KEY_DIGITS = 4
_EPS = 1e-9


@dataclasses.dataclass
class TailFoldResult(SerpentineResult):
    """A generated tail fold (a :class:`SerpentineResult` the tuner can gate).

    ``amplitude`` is ``A`` and ``overrun`` is ``e`` (mm); ``terminal`` is the
    route end the fold returns into.
    """

    amplitude: float = 0.0
    overrun: float = 0.0
    terminal: tuple[float, float] = (0.0, 0.0)


def fold_chamfers(config: SerpentineConfig, width: float) -> tuple[float, float]:
    """``(crown, foot)`` chamfers (mm) of a fold on a ``width`` trace, unclamped.

    The crown is where a leg meets the top (it must be at least half a width
    for the top to couple, Issue #7546); the foot is where a leg meets the host
    line.  The same values as the tuner's asymmetric bumps.
    """
    crown = max(0.0, config.corner_chamfer_widths) * width
    foot = (
        crown if config.foot_chamfer_length_mm is None else max(0.0, config.foot_chamfer_length_mm)
    )
    return crown, foot


def fold_length_added(amplitude: float, overrun: float, crown: float, foot: float) -> float:
    """Copper a fold adds over the host it replaces (mm).

    Rise (foot chamfer, vertical, crown chamfer) and U-turn (crown chamfer,
    vertical, foot chamfer) each climb ``amplitude`` on two chamfers of
    ``(crown + foot) * sqrt(2)``.  The top runs ``overrun`` past the
    terminal; the U-turn's return point sits ``crown - foot`` further on, so
    the return run is ``overrun + crown - foot`` long.  Summed against the
    replaced host: ``2A + 2e + 2 sqrt(2) (c + f) - 2c - 4f``.
    """
    return (
        2.0 * amplitude
        + 2.0 * overrun
        + 2.0 * math.sqrt(2.0) * (crown + foot)
        - 2.0 * crown
        - 4.0 * foot
    )


def _key(x: float, y: float) -> tuple[float, float]:
    return (round(x, _KEY_DIGITS), round(y, _KEY_DIGITS))


def route_terminals(route: Route) -> list[tuple[int, bool]]:
    """``(segment index, at_end)`` of every route end that is not a via.

    A terminal is a segment end no other segment of the route meets and no
    via of the route covers; ``at_end`` says it is the segment's second point.
    """
    count: dict[tuple[float, float], int] = {}
    for seg in route.segments:
        for pt in ((seg.x1, seg.y1), (seg.x2, seg.y2)):
            count[_key(*pt)] = count.get(_key(*pt), 0) + 1
    out: list[tuple[int, bool]] = []
    for idx, seg in enumerate(route.segments):
        for at_end, (x, y) in ((False, (seg.x1, seg.y1)), (True, (seg.x2, seg.y2))):
            if count[_key(x, y)] != 1:
                continue
            if any(math.hypot(v.x - x, v.y - y) <= v.diameter / 2.0 for v in route.vias):
                continue
            out.append((idx, at_end))
    return out


def partner_tail_mm(
    partner: Route, tx: float, ty: float, ux: float, uy: float, nx: float, ny: float, layer: object
) -> float:
    """How far the partner runs on past the terminal ``T``, beside the host line (mm).

    The partner's same-layer segments parallel to the host direction ``u``,
    on the inner side (opposite the outer normal ``n``) and passing beside
    ``T``: the farthest point of their run along ``u`` past ``T``.  ``0`` when
    the partner stops at or before ``T``.  A fold stays within this tail, so
    all of its copper runs beside partner copper that was uncoupled.
    """
    best = 0.0
    nearest = math.inf
    runs: list[tuple[float, float, float]] = []
    for seg in partner.segments:
        if seg.layer != layer:
            continue
        dx, dy = seg.x2 - seg.x1, seg.y2 - seg.y1
        length = math.hypot(dx, dy)
        if length <= _EPS or abs(dx * uy - dy * ux) > 1e-6 * length:
            continue  # not parallel to the host
        t1 = (seg.x1 - tx) * ux + (seg.y1 - ty) * uy
        t2 = (seg.x2 - tx) * ux + (seg.y2 - ty) * uy
        offset = (seg.x1 - tx) * nx + (seg.y1 - ty) * ny
        if offset >= 0.0:
            continue  # outer side: not the partner beside the host
        runs.append((min(t1, t2), max(t1, t2), -offset))
        if min(t1, t2) <= _EPS:
            nearest = min(nearest, -offset)
    for lo, hi, off in runs:
        if lo <= _EPS and abs(off - nearest) <= 1e-6:
            best = max(best, hi)
    return best


def _amplitudes(
    cap: float, width: float, crown: float, foot: float, pad_floor: float, ladder: Sequence[float]
) -> list[float]:
    """Fold heights to try, lowest first (a lower fold reads less uncoupled).

    Never under one trace width (the chamfered-bump floor of Issue #7546) or
    under the two chamfers of either leg; ``pad_floor`` is the height that
    clears the terminal's own pad.
    """
    floor = max(width, crown + foot) * (1.0 + 1e-6)
    values = {max(floor, pad_floor)} | {cap * f for f in ladder}
    return sorted(a for a in values if floor <= a <= cap + _EPS)


def tail_fold_options(
    *,
    route: Route,
    partner: Route,
    length_needed: float,
    config: SerpentineConfig,
    ladder: Sequence[float],
    own_copper: OwnCopperRule | None = None,
) -> Iterator[tuple[Route, TailFoldResult]]:
    """Yield ``(candidate_route, fold)`` tail folds adding ``length_needed`` mm.

    One candidate per route terminal and fold height (:func:`_amplitudes`),
    each sized so the fold adds exactly the need.  ``config`` supplies the
    capped amplitude and the crown / foot chamfers; ``own_copper`` (when given)
    the terminal's pad, which sets where the top splits and how high it must
    run to clear the pad.  Candidates are not gated here.
    """
    from .diffpair_length_tuning import _outer_normal_hint
    from .diffpair_own_copper import pad_half_extent
    from .primitives import Route

    for idx, at_end in route_terminals(route):
        host = route.segments[idx]
        if at_end:
            hx, hy, tx, ty = host.x1, host.y1, host.x2, host.y2
        else:
            hx, hy, tx, ty = host.x2, host.y2, host.x1, host.y1
        length = math.hypot(tx - hx, ty - hy)
        if length <= _EPS:
            continue
        ux, uy = (tx - hx) / length, (ty - hy) / length
        hint = _outer_normal_hint(host, partner)
        nx, ny = -uy, ux
        if nx * hint[0] + ny * hint[1] < 0.0:
            nx, ny = -nx, -ny
        width = host.width
        crown, foot = fold_chamfers(config, width)
        if crown <= 0.0 or foot <= 0.0:
            continue  # operator ruling Q32: every corner chamfered
        pad = own_copper.pad_at(route.net, tx, ty, host.layer) if own_copper else None
        reach = pad_half_extent(pad, ux, uy) if pad is not None else 0.0
        side = pad_half_extent(pad, nx, ny) if pad is not None else 0.0
        clearance = own_copper.clearance_mm if own_copper is not None else 0.0
        # The top splits where its probe clears the pad (and the return
        # run's end cap) by half a width.
        split_back = max(reach, width / 2.0) + width / 2.0
        jog = MIN_JOG_WIDTHS * width
        # D: the rise, then a top1 of one Q16 jog before the split.
        back = split_back + crown + foot + jog
        if length < back + width:  # keep a lead-in on the host
            continue
        pad_floor = side + width / 2.0 + clearance if pad is not None else 0.0
        tail = partner_tail_mm(partner, tx, ty, ux, uy, nx, ny, host.layer)
        for amp in _amplitudes(config.amplitude, width, crown, foot, pad_floor, ladder):
            overrun = (length_needed - fold_length_added(amp, 0.0, crown, foot)) / 2.0
            # top2 is a Q16 jog too.
            if overrun + split_back < jog - _EPS or overrun <= 0.0:
                continue
            # The U-turn stays beside the partner's tail.
            if overrun + crown > tail + _EPS:
                continue

            def at(along: float, out: float) -> tuple[float, float]:
                return (tx + ux * along + nx * out, ty + uy * along + ny * out)

            points = [
                (hx, hy),
                at(-back, 0.0),
                at(-back + foot, foot),
                at(-back + foot, amp - crown),
                at(-back + foot + crown, amp),
                at(-split_back, amp),
                at(overrun, amp),
                at(overrun + crown, amp - crown),
                at(overrun + crown, foot),
                at(overrun + crown - foot, 0.0),
                (tx, ty),
            ]
            if not at_end:
                points.reverse()
            new_segments: list[Segment] = [
                dataclasses.replace(host, x1=a[0], y1=a[1], x2=b[0], y2=b[1], tuning_pattern=None)
                for a, b in zip(points, points[1:], strict=False)
            ]
            fold = TailFoldResult(
                success=True,
                new_segments=new_segments,
                length_added=fold_length_added(amp, overrun, crown, foot),
                num_loops=1,
                message=f"tail fold A={amp:.3f} e={overrun:.3f} mm",
                amplitude=amp,
                overrun=overrun,
                terminal=(tx, ty),
            )
            yield (
                Route(
                    net=route.net,
                    net_name=route.net_name,
                    segments=route.segments[:idx] + new_segments + route.segments[idx + 1 :],
                    vias=route.vias.copy(),
                    is_escape=route.is_escape,
                ),
                fold,
            )


def reversed_route(route: Route) -> Route:
    """``route`` with every segment stored the other way (and the order reversed)."""
    from .primitives import Route

    return Route(
        net=route.net,
        net_name=route.net_name,
        segments=[
            dataclasses.replace(s, x1=s.x2, y1=s.y2, x2=s.x1, y2=s.y1)
            for s in reversed(route.segments)
        ],
        vias=route.vias.copy(),
        is_escape=route.is_escape,
    )


__all__ = [
    "TailFoldResult",
    "fold_length_added",
    "reversed_route",
    "route_terminals",
    "tail_fold_options",
]

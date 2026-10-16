"""Fan-via terminal gateways for diff-pair segments (issue #6444, slice 2a).

The lattice pair agent (:meth:`LatticePathfinder._route_pair_impl`) is
planar: it routes a coupled run only on a layer every endpoint pad owns.
When that layer has no corridor (board 06rf USB3, 07rf TMDS: a BGA row or a
keepout walls the pad layer off), the segment used to fall back to two
single-ended legs.  A **gateway** moves the coupled run to another layer: at
each end, P and N each get a short fan leg on the pad layer to ONE through
via, and the unchanged pair agent then routes the coupled run between the
via pairs on the gateway layer.

This module is the engine-agnostic half -- pure geometry, no lattice, no
obstacle model -- so it can move to ``router/pairplan/`` unchanged (slice
2c).  It proposes candidate via sites, ranks the legal ones, caps them, and
orders the end-A x end-B combinations.  Legality (via clearance, exact fan
verification, keepouts, via-in-pad eligibility) is supplied by the engine as
a callback, so the search never trusts geometry it has not verified.

Candidate families, per end, per gateway layer:

* **parallel** -- each via sits at the same offset ``heading * setback``
  from its own pad (a BGA dogbone: B1/B2 fan diagonally into the ball
  gaps).  Needs the pads at least a via-to-via gap apart.
* **centred** -- the two vias straddle the point ``mid + heading *
  setback`` perpendicular to the heading, a via-to-via gap apart.
* **staggered** (two senses) -- as centred, but the vias sit only a pair
  pitch apart across the heading and are offset along it, so their centres
  are still a via-to-via gap apart (pair pitch < via diameter + clearance).

Each family scans 8 octilinear headings over a setback ladder of ``k *
step`` (``k = 1, 2, ...``).  Only the smallest legal setback of each
(heading, family) is kept: a longer setback in the same direction is the
same escape with more uncoupled copper, and keeping it would crowd the
diverse escapes out of the cap.

The heading x setback ladder is the idea behind KiCadRoutingTools'
``_setback_ladder`` / ``_find_open_positions`` (MIT; see
``kct ecosystem show kicadroutingtools`` and
``docs/research/kicad-routing-tools-comparison.md`` section 3).  No code is
reused from it; this is an independent implementation for fan vias.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from .geometry import Pt, dist

#: The 8 octilinear unit headings, index ``i`` at ``i * 45`` degrees.
HEADINGS: tuple[Pt, ...] = tuple(
    (round(math.cos(i * math.pi / 4.0), 12), round(math.sin(i * math.pi / 4.0), 12))
    for i in range(8)
)

#: Most legal gateway candidates kept per terminal (pair end) and layer.
#: With one kept setback per (heading, family) the enumeration yields at
#: most ``8 * 4 = 32`` anyway; the cap is the documented bound.
MAX_GATEWAY_CANDIDATES = 32

#: Most (end A, end B) gateway combinations routed per segment and layer.
#: Each combination is one fat A* over the gateway layer, so this bounds the
#: retry cost of a declined segment.
MAX_GATEWAY_COMBOS = 6

#: Setback ladder step (mm) and its farthest rung.  The step is finer than a
#: typical pair pitch so a via can land in a BGA ball gap; the far rung keeps
#: fan legs short (uncoupled copper) even when the protocol has no
#: per-terminal budget.
GATEWAY_SETBACK_STEP_MM = 0.1
MAX_GATEWAY_SETBACK_MM = 2.5

#: Ranking weight (mm) for a gateway that heads AWAY from the other end of
#: the segment: ``BEND_WEIGHT_MM * (1 - cos(angle))`` is added to the fan
#: length, so a reversed heading costs ``2 * BEND_WEIGHT_MM``.
BEND_WEIGHT_MM = 1.0

#: Candidate families, in tie-break order.
FAMILIES: tuple[str, ...] = ("parallel", "centred", "stagger+", "stagger-")


@dataclass(frozen=True)
class GatewaySite:
    """One proposed pair of via sites at a segment end (geometry only)."""

    heading: int
    family: str
    setback: float
    via_p: Pt
    via_n: Pt


@dataclass(frozen=True)
class Gateway:
    """A legal gateway: via sites plus the verified pad-layer fan legs.

    ``fan_p`` / ``fan_n`` run from the pad centre to the via centre on the
    pad layer.  ``cost`` is the ranking key (fan length plus bend penalty).
    """

    layer: int
    site: GatewaySite
    fan_p: tuple[Pt, ...]
    fan_n: tuple[Pt, ...]
    cost: float

    @property
    def fan_len_p(self) -> float:
        return _poly_len(self.fan_p)

    @property
    def fan_len_n(self) -> float:
        return _poly_len(self.fan_n)

    def describe(self) -> str:
        """``layer=..., heading=..deg, setback=..mm, family`` for the log."""
        return (
            f"heading={self.site.heading * 45}deg setback={self.site.setback:.2f}mm "
            f"{self.site.family} fan P/N={self.fan_len_p:.2f}/{self.fan_len_n:.2f}mm"
        )


def _poly_len(poly: tuple[Pt, ...] | list[Pt]) -> float:
    return sum(dist(a, b) for a, b in zip(poly, poly[1:], strict=False))


def _unit(v: Pt) -> Pt | None:
    n = math.hypot(v[0], v[1])
    if n <= 1e-12:
        return None
    return (v[0] / n, v[1] / n)


def propose_gateway_sites(
    pad_p: Pt,
    pad_n: Pt,
    *,
    pitch: float,
    via_gap: float,
    step: float = GATEWAY_SETBACK_STEP_MM,
    max_setback: float = MAX_GATEWAY_SETBACK_MM,
) -> list[list[GatewaySite]]:
    """Every proposed site, grouped per (heading, family) ladder.

    Returns one list per ``(heading, family)`` in a fixed order, each list
    ordered by increasing setback -- the caller keeps the first legal site
    of each.  ``via_gap`` is the least cross-net via centre-to-centre
    distance; ``pitch`` is the pair's P/N centreline pitch.  The P via is
    always on P's side of the pair axis, so the two fan legs never cross.
    """
    mid = ((pad_p[0] + pad_n[0]) / 2.0, (pad_p[1] + pad_n[1]) / 2.0)
    axis = _unit((pad_p[0] - pad_n[0], pad_p[1] - pad_n[1]))  # N -> P
    pad_sep = dist(pad_p, pad_n)
    if step <= 0.0 or max_setback <= 0.0:
        return []
    rungs = [k * step for k in range(1, int(math.floor(max_setback / step + 1e-9)) + 1)]
    # Staggered: across-heading offset is one pitch, the along-heading
    # offset makes the centre distance the via gap.
    stagger_along = (
        math.sqrt(max(via_gap * via_gap - pitch * pitch, 0.0)) / 2.0 if via_gap > pitch else 0.0
    )

    ladders: list[list[GatewaySite]] = []
    for hi, h in enumerate(HEADINGS):
        perp = (-h[1], h[0])
        # Orient the perpendicular so +perp points to P's side.
        if axis is not None and perp[0] * axis[0] + perp[1] * axis[1] < 0.0:
            perp = (-perp[0], -perp[1])
        for family in FAMILIES:
            if family == "parallel" and pad_sep < via_gap - 1e-9:
                continue
            if family.startswith("stagger") and stagger_along <= 1e-9:
                continue
            ladder: list[GatewaySite] = []
            for s in rungs:
                if family == "parallel":
                    vp = (pad_p[0] + h[0] * s, pad_p[1] + h[1] * s)
                    vn = (pad_n[0] + h[0] * s, pad_n[1] + h[1] * s)
                else:
                    g = (mid[0] + h[0] * s, mid[1] + h[1] * s)
                    if family == "centred":
                        e, t = via_gap / 2.0, 0.0
                    else:
                        e = pitch / 2.0
                        t = stagger_along if family == "stagger+" else -stagger_along
                    vp = (g[0] + perp[0] * e + h[0] * t, g[1] + perp[1] * e + h[1] * t)
                    vn = (g[0] - perp[0] * e - h[0] * t, g[1] - perp[1] * e - h[1] * t)
                ladder.append(GatewaySite(hi, family, round(s, 6), vp, vn))
            if ladder:
                ladders.append(ladder)
    return ladders


def enumerate_gateways(
    pad_p: Pt,
    pad_n: Pt,
    toward: Pt,
    layer: int,
    check: Callable[[GatewaySite], tuple[list[Pt], list[Pt]] | None],
    *,
    pitch: float,
    via_gap: float,
    max_fan_mm: float | None = None,
    cap: int = MAX_GATEWAY_CANDIDATES,
    step: float = GATEWAY_SETBACK_STEP_MM,
    max_setback: float = MAX_GATEWAY_SETBACK_MM,
) -> tuple[list[Gateway], int]:
    """Legal gateways at one segment end, ranked, capped at ``cap``.

    ``check(site)`` is the engine's legality oracle: it returns the verified
    ``(fan_p, fan_n)`` polylines (pad -> via, on the pad layer) or ``None``.
    ``toward`` is the other end's midpoint (the bend penalty's reference).
    A fan longer than ``max_fan_mm`` (the protocol's per-terminal uncoupled
    budget) is pruned.  Returns ``(gateways, legal_count)`` where
    ``legal_count`` is how many legal candidates were found before the cap.
    """
    mid = ((pad_p[0] + pad_n[0]) / 2.0, (pad_p[1] + pad_n[1]) / 2.0)
    to_dir = _unit((toward[0] - mid[0], toward[1] - mid[1]))
    found: list[Gateway] = []
    for ladder in propose_gateway_sites(
        pad_p, pad_n, pitch=pitch, via_gap=via_gap, step=step, max_setback=max_setback
    ):
        for site in ladder:
            # A rung farther than the budget can only grow the fan.
            if max_fan_mm is not None and site.setback > max_fan_mm + 1e-9:
                break
            fans = check(site)
            if fans is None:
                continue
            fan_p, fan_n = fans
            lp, ln = _poly_len(fan_p), _poly_len(fan_n)
            if max_fan_mm is not None and max(lp, ln) > max_fan_mm + 1e-9:
                continue
            h = HEADINGS[site.heading]
            bend = 0.0 if to_dir is None else 1.0 - (h[0] * to_dir[0] + h[1] * to_dir[1])
            cost = lp + ln + BEND_WEIGHT_MM * bend
            found.append(Gateway(layer, site, tuple(fan_p), tuple(fan_n), round(cost, 9)))
            break  # smallest legal setback of this (heading, family) only
    found.sort(key=lambda g: (g.cost, g.site.heading, FAMILIES.index(g.site.family)))
    return found[:cap], len(found)


def gateway_side(gateway: Gateway, *, arriving: bool) -> bool:
    """True when P's via is LEFT of the coupled body's travel at this end.

    The body leaves end A along the gateway heading and arrives at end B
    against it.  "Left" is the same convention as
    :func:`~kicad_tools.router.lattice.coupled.side_bit`: the side an offset
    leg keeps all the way along the body, so both ends must agree.
    """
    h = HEADINGS[gateway.site.heading]
    d = (-h[0], -h[1]) if arriving else h
    n = (-d[1], d[0])
    vp, vn = gateway.site.via_p, gateway.site.via_n
    return (vp[0] - vn[0]) * n[0] + (vp[1] - vn[1]) * n[1] > 0.0


def handedness_consistent(ga: Gateway | None, gb: Gateway | None) -> bool:
    """Whether a coupled body can join ``ga`` to ``gb`` without a twist.

    An end with no gateway (``None``, its pads already own the layer) keeps
    the pair agent's own parity search, so it never rules a combination out.
    """
    if ga is None or gb is None:
        return True
    return gateway_side(ga, arriving=False) == gateway_side(gb, arriving=True)


def rank_gateway_combos(
    ends_a: list[Gateway | None],
    ends_b: list[Gateway | None],
    *,
    top_n: int = MAX_GATEWAY_COMBOS,
) -> list[tuple[Gateway | None, Gateway | None]]:
    """The ``top_n`` cheapest handedness-consistent (end A, end B) combos.

    ``None`` in an end list means "no gateway needed at this end" (its pads
    already own the gateway layer) and costs nothing.  A combination whose
    ends disagree on which side P runs (:func:`handedness_consistent`) is
    skipped: the body would need a polarity twist, which the pair agent
    declines.  Order is deterministic (cost, then list indices).
    """
    combos: list[tuple[float, int, int, Gateway | None, Gateway | None]] = []
    for i, ga in enumerate(ends_a):
        for j, gb in enumerate(ends_b):
            if not handedness_consistent(ga, gb):
                continue
            cost = (ga.cost if ga is not None else 0.0) + (gb.cost if gb is not None else 0.0)
            combos.append((round(cost, 9), i, j, ga, gb))
    combos.sort(key=lambda c: (c[0], c[1], c[2]))
    return [(ga, gb) for _c, _i, _j, ga, gb in combos[:top_n]]

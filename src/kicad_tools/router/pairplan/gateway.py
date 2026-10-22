"""Fan-via terminal gateways for diff-pair segments (issue #6444, slice 2a).

The lattice pair agent (:meth:`LatticePathfinder._route_pair_impl`) is
planar: it routes a coupled run only on a layer every endpoint pad owns.
When that layer has no corridor (board 06rf USB3, 07rf TMDS: a BGA row or a
keepout walls the pad layer off), the segment used to fall back to two
single-ended legs.  A **gateway** moves the coupled run to another layer: at
each end, P and N each get a short fan leg on the pad layer to ONE through
via, and the unchanged pair agent then routes the coupled run between the
via pairs on the gateway layer.

This module is the engine-agnostic half -- pure geometry, no engine, no
obstacle model.  Issue #6837 (slice 2c-i) moved it here unchanged from
``router/lattice/gateway.py``, which now only re-exports it.  It proposes candidate via sites, ranks the legal ones, caps them, and
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


#: Issue #7503: the impedance rule's coupling predicate, mirrored (not
#: imported, so ``router`` keeps no module-level ``validate`` dependency;
#: ``tests/router/lattice/test_gateway_fan_coupling_7503.py`` pins both to
#: ``validate.rules.impedance``).  Two same-layer partner pieces within
#: this edge-to-edge window that are parallel to within this sine and
#: overlap along their length are judged as a coupled run at that gap.
FAN_COUPLING_WINDOW_MM = 0.5
FAN_PARALLEL_SIN_TOLERANCE = 0.1

#: A fan run whose centreline spacing is within this of the pair pitch IS
#: the coupled geometry (the body's own gap), not a wrong-gap run.
FAN_AT_PITCH_TOL_MM = 0.01


def fans_couple_off_pitch(
    fan_p: list[Pt] | tuple[Pt, ...],
    fan_n: list[Pt] | tuple[Pt, ...],
    *,
    half: float,
    pitch: float,
) -> bool:
    """Whether two fan legs run side by side at a gap other than the pair's.

    Issue #7503: on board 06rf the USB3 TX1/RX2 gateways at J1 fanned both
    nets diagonally from pads one pad pitch (1.0 mm) apart, so the two 45
    degree legs ran parallel 0.707 mm apart centre to centre -- a 0.432 mm
    edge gap for 0.5-1.2 mm, about 112 ohm against the 90 ohm target.  The
    impedance rule judges any parallel, overlapping partner run inside its
    coupling window, so such a fan is real copper at the wrong gap.

    A pair of fan legs is rejected when some segment of one is parallel to
    a segment of the other (:data:`FAN_PARALLEL_SIN_TOLERANCE`), the two
    overlap along their length, their edge gap is inside
    :data:`FAN_COUPLING_WINDOW_MM`, and their centreline spacing is not the
    pair ``pitch``.  Legs that split apart (a centred fan into a via pair
    wider than the window, a fan straight off a row whose pitch already
    exceeds the window) or that run AT the pitch are kept.  Any positive
    overlap counts, which is never looser than the rule (it judges any
    overlap, or one at least a trace width long once #7575 lands).
    """
    width = 2.0 * half
    for a, b in zip(fan_p, fan_p[1:], strict=False):
        length = dist(a, b)
        if length <= 1e-9:
            continue
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        for c, d in zip(fan_n, fan_n[1:], strict=False):
            olen = dist(c, d)
            if olen <= 1e-9:
                continue
            if abs(ux * (d[1] - c[1]) - uy * (d[0] - c[0])) / olen > FAN_PARALLEL_SIN_TOLERANCE:
                continue
            t1 = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
            t2 = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
            if min(max(t1, t2), length) - max(min(t1, t2), 0.0) <= 1e-6:
                continue
            mx, my = (c[0] + d[0]) / 2.0, (c[1] + d[1]) / 2.0
            centre = abs((mx - a[0]) * uy - (my - a[1]) * ux)
            gap = centre - width
            if gap <= 0.0 or gap > FAN_COUPLING_WINDOW_MM + 1e-9:
                continue
            if abs(centre - pitch) > FAN_AT_PITCH_TOL_MM:
                return True
    return False


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


def gateway_side(gateway: Gateway, *, arriving: bool, through: bool = False) -> bool:
    """True when P's via is LEFT of the coupled body's travel at this end.

    The body leaves end A along the gateway heading and arrives at end B
    against it (``through=False``, the orientation every combination tried
    since #6444 assumes).  "Left" is the same convention as
    :func:`~kicad_tools.router.lattice.coupled.side_bit`: the side an offset
    leg keeps all the way along the body, so both ends must agree.

    ``through=True`` (issue #6836) is the **pass-under** orientation: the
    body reaches the via pair from the PAD side, crossing the end's own pad
    footprint on the gateway layer (legal only because those pads do not own
    that layer -- the condition for a gateway at all).  Its travel at the
    vias is the reverse of the ``through=False`` travel, so its side bit is
    the opposite one.  Board 06rf's J1 is the case: the USB2 gateway vias
    sit behind the A row, away from U1, and the only twist-free body runs
    from U1 under the B and A rows on B.Cu.
    """
    h = HEADINGS[gateway.site.heading]
    d = (-h[0], -h[1]) if arriving != through else h
    n = (-d[1], d[0])
    vp, vn = gateway.site.via_p, gateway.site.via_n
    return (vp[0] - vn[0]) * n[0] + (vp[1] - vn[1]) * n[1] > 0.0


def handedness_consistent(
    ga: Gateway | None,
    gb: Gateway | None,
    *,
    through_a: bool = False,
    through_b: bool = False,
) -> bool:
    """Whether a coupled body can join ``ga`` to ``gb`` without a twist.

    An end with no gateway (``None``, its pads already own the layer) keeps
    the pair agent's own parity search, so it never rules a combination out.
    ``through_a`` / ``through_b`` select each end's body orientation (see
    :func:`gateway_side`); both default to the pre-#6836 ``against`` model.
    """
    if ga is None or gb is None:
        return True
    return gateway_side(ga, arriving=False, through=through_a) == gateway_side(
        gb, arriving=True, through=through_b
    )


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

    Every combination here uses the ``against`` orientation at both ends.
    The pass-under combinations (issue #6836) are a separate, strictly later
    group: :func:`rank_through_combos`.
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


#: Per-end body orientations of a pass-under combination, in try order:
#: ``(through_a, through_b)``.  ``(False, False)`` is
#: :func:`rank_gateway_combos`' group and never appears here.
THROUGH_ORIENTATIONS: tuple[tuple[bool, bool], ...] = ((False, True), (True, False), (True, True))


@dataclass(frozen=True)
class GatewayCombo:
    """One pass-under (end A, end B) combination (issue #6836).

    ``through_a`` / ``through_b`` say which ends the coupled body reaches
    from the pad side (see :func:`gateway_side`).  An end with no gateway
    (``None``) is never ``through``.
    """

    ga: Gateway | None
    gb: Gateway | None
    through_a: bool
    through_b: bool


def rank_through_combos(
    ends_a: list[Gateway | None],
    ends_b: list[Gateway | None],
    *,
    top_n: int = MAX_GATEWAY_COMBOS,
) -> list[GatewayCombo]:
    """The ``top_n`` cheapest handedness-consistent PASS-UNDER combos (#6836).

    The fallback group: a caller walks it only after every combination of
    :func:`rank_gateway_combos` has declined, so a pair that couples today
    keeps its combination (and copper) exactly.  At least one gatewayed end
    is ``through``; an end with no gateway (``None``) cannot be.  Order is
    deterministic: cost, then list indices, then
    :data:`THROUGH_ORIENTATIONS` order.  Capped at ``top_n`` like the
    ``against`` group, so the retry budget per segment at most doubles.
    """
    combos: list[tuple[float, int, int, int, GatewayCombo]] = []
    for i, ga in enumerate(ends_a):
        for j, gb in enumerate(ends_b):
            for k, (ta, tb) in enumerate(THROUGH_ORIENTATIONS):
                if (ta and ga is None) or (tb and gb is None):
                    continue
                if not handedness_consistent(ga, gb, through_a=ta, through_b=tb):
                    continue
                cost = (ga.cost if ga is not None else 0.0) + (gb.cost if gb is not None else 0.0)
                combos.append((round(cost, 9), i, j, k, GatewayCombo(ga, gb, ta, tb)))
    combos.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    return [c[4] for c in combos[:top_n]]


#: Issue #7362: most WORKABLE combinations of one group (``against`` or
#: pass-under) that are routed to completion and compared.  Until #7362 the
#: first workable combination won, ranked only by fan length; the coupled
#: body's length never entered.  Each extra evaluation is one more fat A*
#: over the gateway layer, already within the ``MAX_GATEWAY_COMBOS`` tries
#: per layer and group, so this caps how many of those can be spent after
#: the first success.
MAX_WORKABLE_GATEWAY_COMBOS = 4

#: Issue #7463: the cap while the best workable combination so far seals
#: another net's #6697 escape site (see ``WorkableCombo.sealed``).
MAX_WORKABLE_SEALED_GATEWAY_COMBOS = 12

#: Issue #7362: an end's P/N axis must make at least this sine with the
#: segment chord to have a side at all (30 degrees).
POLARITY_SIDE_MIN_SIN = 0.5


def polarity_crosses(end_a: tuple[Pt, Pt], end_b: tuple[Pt, Pt]) -> bool:
    """Whether P sits on opposite sides of the segment at its two ends.

    ``end_a`` / ``end_b`` are each end's ``(P, N)`` launch points: the via
    pair of a gatewayed end, else the pads.  "Side" is measured against the
    chord from end A's midpoint to end B's, so a pair whose P is right of
    the chord at one end and left of it at the other must turn the coupled
    body through a half loop to keep P and N apart -- issue #7362's board 03
    ``USB_D`` (D+ right at J1, left via at U2) ran to y~9 on In2.Cu and
    doubled back past its own via pair, about +3 mm per net.

    The router rejects such a combination only in the PASS-UNDER group
    (:func:`rank_through_combos`): there, flipping an end's side bit is
    exactly what lets a crossed segment through
    :func:`handedness_consistent`, and the body has to hairpin between the
    vias and the pads to honour it.  An ``against`` combination that
    crosses is kept: board 06rf PCIE_TX and 07rf DQS couple through one (a
    U on the gateway layer around one end), and without it they fall back
    to a hybrid (PCIE_TX: 26.8 -> 55.8 mm per net) or to uncoupled legs.  An end whose P/N axis lies within 30 degrees of the
    chord has no clear side and never counts as crossing.
    """
    (pa, na), (pb, nb) = end_a, end_b
    ma = ((pa[0] + na[0]) / 2.0, (pa[1] + na[1]) / 2.0)
    mb = ((pb[0] + nb[0]) / 2.0, (pb[1] + nb[1]) / 2.0)
    d = _unit((mb[0] - ma[0], mb[1] - ma[1]))
    if d is None:
        return False

    def side(p: Pt, n: Pt) -> int:
        axis = _unit((p[0] - n[0], p[1] - n[1]))
        if axis is None:
            return 0
        cross = d[0] * axis[1] - d[1] * axis[0]
        if abs(cross) < POLARITY_SIDE_MIN_SIN:
            return 0
        return 1 if cross > 0.0 else -1

    sa, sb = side(pa, na), side(pb, nb)
    return sa * sb < 0


@dataclass(frozen=True)
class WorkableCombo:
    """One combination that routed, scored for selection (issue #7362).

    ``order`` is its position in the ranked try order (the pre-#7362 pick
    is the lowest workable one); ``length_p`` / ``length_n`` are the whole
    pair-owned route per net (fans + coupled body).
    """

    order: int
    length_p: float
    length_n: float
    # Issue #7463: how many of the combination's vias / tracks sit inside an
    # escape site reserved for another net (#6697).  A combination that seals
    # a neighbour in loses to any that does not, however short.
    sealed: int = 0
    # Issue #7650: how much of the body runs coupled at the pair pitch
    # (:func:`coupled_length`).  It only breaks a matched-length tie: of two
    # equally long combinations, the one with less uncoupled copper wins.
    coupled: float = 0.0

    @property
    def score(self) -> float:
        """P+N route length (mm)."""
        return self.length_p + self.length_n

    @property
    def matched(self) -> float:
        """P+N plus intra-pair skew, i.e. twice the longer net (mm, #7463).

        The length a pair has to budget once its shorter net is tuned up to
        the longer one.  Board 03 arm C: a 15.005 mm combination with
        0.595 mm skew (D- 16.075 mm, over USB_D's 16 mm bound) used to beat
        a 15.032 mm one with none.  Never below :attr:`score`, so
        :func:`combo_length_floor` still bounds it from below.
        """
        return self.score + abs(self.length_p - self.length_n)

    def key(self) -> tuple[int, float, float, int]:
        """Deterministic sort key: sealed count, matched length (to 1 um),
        longest coupled run (to 1 um, issue #7650), try order."""
        return (self.sealed, round(self.matched, 3), -round(self.coupled, 3), self.order)


def best_workable(combos: list[WorkableCombo]) -> WorkableCombo | None:
    """The combination to keep: lowest :meth:`WorkableCombo.key`."""
    return min(combos, key=WorkableCombo.key) if combos else None


#: Issue #7650: two leg pieces this close to the pair pitch (centre to
#: centre) and parallel within :data:`FAN_PARALLEL_SIN_TOLERANCE` are
#: coupled.  Offset emission puts the legs exactly at the pitch, so the
#: tolerance only absorbs float noise.
COUPLED_PITCH_TOL_MM = 0.01


def coupled_length(
    leg_a: list[Pt] | tuple[Pt, ...],
    leg_b: list[Pt] | tuple[Pt, ...],
    *,
    pitch: float,
) -> float:
    """Length of ``leg_a`` that runs beside ``leg_b`` at the pair pitch (mm).

    Issue #7650: the pair's coupled body, measured the way a coupling check
    sees it -- each piece of ``leg_a`` counts where a parallel piece of
    ``leg_b`` overlaps it at a centreline spacing of ``pitch``.  Fan legs,
    convergence jogs and the turn-in to a via pair do not count.  Board 03's
    USB_D gateway has two combinations of equal matched length (15.032 mm)
    whose R4-end via pairs differ: the centred one keeps the body coupled
    0.15 mm further, which is the tie-break :meth:`WorkableCombo.key` uses.
    """
    total = 0.0
    for a, b in zip(leg_a, leg_a[1:], strict=False):
        length = dist(a, b)
        if length <= 1e-9:
            continue
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        spans: list[tuple[float, float]] = []
        for c, d in zip(leg_b, leg_b[1:], strict=False):
            olen = dist(c, d)
            if olen <= 1e-9:
                continue
            if abs(ux * (d[1] - c[1]) - uy * (d[0] - c[0])) / olen > FAN_PARALLEL_SIN_TOLERANCE:
                continue
            mx, my = (c[0] + d[0]) / 2.0, (c[1] + d[1]) / 2.0
            if abs(abs((mx - a[0]) * uy - (my - a[1]) * ux) - pitch) > COUPLED_PITCH_TOL_MM:
                continue
            t1 = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
            t2 = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
            lo, hi = max(min(t1, t2), 0.0), min(max(t1, t2), length)
            if hi - lo > 1e-9:
                spans.append((lo, hi))
        # Union of the overlaps, so a partner piece split at a vertex is not
        # counted twice.
        spans.sort()
        end = -math.inf
        for lo, hi in spans:
            if hi <= end:
                continue
            total += hi - max(lo, end)
            end = hi
    return total


def combo_length_floor(end_a: tuple[Pt, Pt], end_b: tuple[Pt, Pt], fans: float) -> float:
    """A lower bound on a combination's P+N route length (issue #7362).

    ``fans`` is the summed fan length of both nets at both ends; each net
    then needs at least the straight distance between its own launch
    points.  A combination whose floor already exceeds the best workable
    key length (:attr:`WorkableCombo.matched`, never below P+N) is skipped
    without routing it.
    """
    (pa, na), (pb, nb) = end_a, end_b
    return fans + dist(pa, pb) + dist(na, nb)


def gateway_anchor_point(
    site: GatewaySite, reach: float, step: float = 0.05, *, through: bool = False
) -> Pt | None:
    """Start of a gateway's coupled body (issue #6444).

    The nearest point along the gateway heading from the via pair's midpoint
    that is at least ``reach`` (pair half-envelope + clearance + via radius)
    from both vias, so the fat centerline can start there with the vias as
    obstacles.  ``None`` past 4 mm (never on a sane via pitch).

    ``through=True`` (issue #6836, pass-under) walks the REVERSE heading
    instead: the body reaches the vias from the pad side, so it starts
    between the via pair and the pads it passes under.

    Moved from ``lattice/pathfinder.py`` (``_gateway_anchor``) by issue #6837.
    """
    h = HEADINGS[site.heading]
    if through:
        h = (-h[0], -h[1])
    g = ((site.via_p[0] + site.via_n[0]) / 2.0, (site.via_p[1] + site.via_n[1]) / 2.0)
    k = 0
    while k * step <= 4.0:
        d = k * step
        pt = (round(g[0] + h[0] * d, 6), round(g[1] + h[1] * d, 6))
        if dist(pt, site.via_p) >= reach and dist(pt, site.via_n) >= reach:
            return pt
        k += 1
    return None

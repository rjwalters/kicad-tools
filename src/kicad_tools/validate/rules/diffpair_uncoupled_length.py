"""Differential-pair uncoupled-length and via-count DRC rule.

Issue #6443 (piece (b) of epic #6441 phase P1).  For an *engaged* pair the
rule reports

* the **uncoupled length** of each net -- copper that runs beside no
  partner copper -- and compares the larger of the two (the figure KiCad's
  ``diff_pair_uncoupled`` checks) against the pair's limit, and
* the **via count** of each net against ``max_vias_per_net``, plus any
  difference between the P and N via counts.

How KiCad measures (pinned by fixture, kicad-cli 10.0.6)
========================================================

The ``.kicad_dru`` constraint ``diff_pair_uncoupled (max X)`` is evaluated
per pair, and the figure compared with ``X`` is, measured on synthetic
two-net boards (``tests/test_diffpair_uncoupled_length_6443.py``):

* **Per net, not summed.**  ``uncoupled(net) = routed length of the net's
  tracks - the part of them that is coupled``; the pair's figure is
  ``max(uncoupled(P), uncoupled(N))``.  A 20 mm P net with a 12 mm
  coupled N net reports 8 mm; the harness's older ``P + N`` figure would
  say 8 mm too, but P 20 / N 16 with 12 mm coupled reports 8 mm (the
  larger side), where the sum is 12 mm.
* **Coupled means exactly parallel.**  A P segment is coupled over the
  length its projection overlaps a same-layer N segment whose direction
  differs by no more than ~0.01 degrees (anti-parallel counts).  A 0.02
  degree skew already makes both traces uncoupled.  There is **no distance
  limit** unless a ``diff_pair_gap (max ..)`` constraint is present, and kct
  emits only ``(min ..)``.  Two parallel traces 10 mm apart are coupled.
* **Via barrels are not counted.**  A via is not track length: a pair whose
  vias sit side by side loses nothing, and offset vias cost only the
  track copper that no longer has a partner beside it.

:func:`measure_uncoupled` reproduces exactly that, so kct and kicad-cli agree
on pass/fail per pair; the known divergence (arcs are not measured, and
KiCad's number is known to misreport on meanders) is listed in
``docs/guides/diff-pairs/08-uncoupled-length.md``.

The limit
=========

``max_uncoupled_total_mm`` of the net class when set (explicit, ``error``).
Otherwise the protocol's per-terminal allowance (6.35 mm for USB2, TI
SPRAAR7J §3.3) times the number of components with a pad on the net: every
component along the net is another terminal at which the pair may
legitimately leave its partner.  A limit that is an ``uncited`` table value
is a ``warning``; the severity is resolved with the limit by
:func:`~kicad_tools.validate.diffpair_engagement.resolve_pair_thresholds`.

Graceful degradation matches the sibling pair rules: no engaged pairs, no
findings.  A pair with an unrouted half (P0's "coupled outcome without
copper") is skipped: it has no uncoupled length, only no copper.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_engagement import PairThresholds

#: Direction difference (degrees) up to which KiCad still calls two segments
#: parallel.  Measured on kicad-cli 10.0.6: 0.005 degrees couples, 0.02 does not.
KICAD_PARALLEL_TOLERANCE_DEG = 0.01
_SIN_TOL = math.sin(math.radians(KICAD_PARALLEL_TOLERANCE_DEG))

#: Copper closer than this to a trace's edge counts as touching it (mm).
_TOUCH_EPS = 1e-6

#: Float-comparison slack (mm) on a limit, as the sibling skew rule has.
UNCOUPLED_EPSILON_MM = 1e-6

# Phrases that mark a via-count finding.  Both via sites build their message
# from these and :func:`is_via_count_finding` matches on them, so the
# ``kct check`` summary can label via counts ``vias`` (Issue #6576).
VIA_OVER_LIMIT_PHRASE = "vias, limit"
VIA_UNEQUAL_PHRASE = "unequal via counts"


def is_via_count_finding(v: DRCViolation) -> bool:
    """True for a via-count finding (its measured/limit values are via counts)."""
    return v.rule_id == "diffpair_uncoupled_length" and (
        VIA_OVER_LIMIT_PHRASE in v.message or VIA_UNEQUAL_PHRASE in v.message
    )


#: ``(x1, y1, x2, y2, layer)``, optionally followed by the track width (needed
#: only to decide whether other copper stands between two traces).
Segment = tuple[float, float, float, float, str] | tuple[float, float, float, float, str, float]
#: ``(x1, y1, x2, y2, width, layer or None)``: a track (layer set) or a via (``None``:
#: all layers, a zero-length segment) that may stand between a pair's traces.
Obstacle = tuple[float, float, float, float, float, str | None]


class PadRect(NamedTuple):
    """A pad as a rotated rectangle in board coordinates."""

    cx: float
    cy: float
    hx: float  # half extents along the pad's own axes
    hy: float
    cos: float
    sin: float
    layers: frozenset[str]
    net: str


@dataclass(frozen=True)
class Copper:
    """The board copper a pair's traces may be separated by (see :func:`measure_uncoupled`)."""

    tracks: Sequence[Obstacle] = ()
    pads: Sequence[PadRect] = ()


@dataclass(frozen=True)
class PairMeasure:
    """What :func:`measure_pair` found on one pair."""

    p_name: str
    n_name: str
    p_routed_mm: float
    n_routed_mm: float
    p_uncoupled_mm: float
    n_uncoupled_mm: float
    vias_p: int
    vias_n: int
    components_p: int
    components_n: int

    @property
    def uncoupled_mm(self) -> float:
        """The KiCad figure: the larger of the two nets' uncoupled lengths."""
        return max(self.p_uncoupled_mm, self.n_uncoupled_mm)

    @property
    def components(self) -> int:
        """Components on the pair's nets (the larger side, normally equal)."""
        return max(self.components_p, self.components_n)

    @property
    def routed(self) -> bool:
        """Both halves carry copper."""
        return self.p_routed_mm > 0.0 and self.n_routed_mm > 0.0


def _length(seg: Segment) -> float:
    return math.hypot(seg[2] - seg[0], seg[3] - seg[1])


def _coupled_interval(own: Segment, other: Segment) -> tuple[float, float] | None:
    """Span of ``own`` (distance from its start) beside ``other``, or ``None``."""
    if own[4] != other[4]:
        return None
    ox, oy = own[2] - own[0], own[3] - own[1]
    tx, ty = other[2] - other[0], other[3] - other[1]
    lo, lt = math.hypot(ox, oy), math.hypot(tx, ty)
    if lo <= 0.0 or lt <= 0.0:
        return None
    # sin(angle) between the directions; the absolute slack absorbs the
    # 1 nm coordinate quantum on very short segments.
    if abs(ox * ty - oy * tx) > _SIN_TOL * lo * lt + 1e-6:
        return None
    ux, uy = ox / lo, oy / lo
    a = (other[0] - own[0]) * ux + (other[1] - own[1]) * uy
    b = (other[2] - own[0]) * ux + (other[3] - own[1]) * uy
    start, end = max(0.0, min(a, b)), min(lo, max(a, b))
    return (start, end) if end > start else None


def _seg_dist(
    p1: tuple[float, float],
    p2: tuple[float, float],
    q1: tuple[float, float],
    q2: tuple[float, float],
) -> float:
    """Minimum distance between segments ``p1-p2`` and ``q1-q2`` (either may be a point)."""

    def point_seg(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        dx, dy = b[0] - a[0], b[1] - a[1]
        d2 = dx * dx + dy * dy
        t = 0.0 if d2 <= 0.0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / d2))
        return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))

    def orient(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2 = orient(p1, p2, q1), orient(p1, p2, q2)
    d3, d4 = orient(q1, q2, p1), orient(q1, q2, p2)
    if d1 * d2 < 0.0 and d3 * d4 < 0.0:
        return 0.0  # proper crossing
    return min(
        point_seg(p1, q1, q2),
        point_seg(p2, q1, q2),
        point_seg(q1, p1, p2),
        point_seg(q2, p1, p2),
    )


def _width(seg: Segment) -> float:
    return float(seg[5]) if len(seg) > 5 else 0.0


def _pad_local(pad: PadRect, x: float, y: float) -> tuple[float, float]:
    """``(x, y)`` in the pad's own axes (KiCad's negated-angle convention)."""
    dx, dy = x - pad.cx, y - pad.cy
    return dx * pad.cos - dy * pad.sin, dx * pad.sin + dy * pad.cos


def _pad_on_layer(pad: PadRect, layer: str) -> bool:
    return layer in pad.layers or "*.Cu" in pad.layers


def _pad_seg_dist(pad: PadRect, p: tuple[float, float], q: tuple[float, float]) -> float:
    """Distance from segment ``p-q`` to the pad rectangle (0 when they intersect)."""
    x1, y1 = _pad_local(pad, *p)
    x2, y2 = _pad_local(pad, *q)
    hx, hy = pad.hx, pad.hy
    # Liang-Barsky clip of the segment against the box.
    t0, t1 = 0.0, 1.0
    dx, dy = x2 - x1, y2 - y1
    for pk, qk in ((-dx, x1 + hx), (dx, hx - x1), (-dy, y1 + hy), (dy, hy - y1)):
        if pk == 0.0:
            if qk < 0.0:
                break
        else:
            r = qk / pk
            if pk < 0.0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
            if t0 > t1:
                break
    else:
        return 0.0

    def to_box(x: float, y: float) -> float:
        return math.hypot(max(abs(x) - hx, 0.0), max(abs(y) - hy, 0.0))

    corners = [(sx * hx, sy * hy) for sx in (-1, 1) for sy in (-1, 1)]
    return min(
        to_box(x1, y1),
        to_box(x2, y2),
        *(_seg_dist((x1, y1), (x2, y2), c, c) for c in corners),
    )


def _buried(seg: Segment, net: str, pads: Sequence[PadRect]) -> bool:
    """True when both ends of ``seg`` lie inside one pad of its own net.

    A stub that is nothing but pad copper is not a trace KiCad pairs.
    """
    for pad in pads:
        if pad.net != net or not _pad_on_layer(pad, seg[4]):
            continue
        if all(
            abs(lx) < pad.hx - _TOUCH_EPS and abs(ly) < pad.hy - _TOUCH_EPS
            for lx, ly in (_pad_local(pad, seg[0], seg[1]), _pad_local(pad, seg[2], seg[3]))
        ):
            return True
    return False


def _blocked(a: Segment, b: Segment, t0: float, copper: Copper) -> bool:
    """True when copper intervenes between ``a`` and ``b`` where their overlap starts.

    The probe runs from ``a``'s centreline, ``t0`` mm from its start, straight
    across to ``b``'s centreline.  Any other primitive on the layer (a track of
    either net or a third net, a via, a pad) whose copper reaches the probe
    blocks the pairing -- except copper that overlaps ``a`` or ``b`` themselves,
    which is part of the same conductor, not something standing between them.
    """
    lo = math.hypot(a[2] - a[0], a[3] - a[1])
    ux, uy = (a[2] - a[0]) / lo, (a[3] - a[1]) / lo
    pa = (a[0] + ux * t0, a[1] + uy * t0)
    bx, by = b[2] - b[0], b[3] - b[1]
    lb = math.hypot(bx, by)
    vx, vy = bx / lb, by / lb
    k = (pa[0] - b[0]) * vx + (pa[1] - b[1]) * vy
    pb = (b[0] + vx * k, b[1] + vy * k)
    if math.hypot(pb[0] - pa[0], pb[1] - pa[1]) <= _TOUCH_EPS:
        return False
    minx, maxx = min(pa[0], pb[0]), max(pa[0], pb[0])
    miny, maxy = min(pa[1], pb[1]), max(pa[1], pb[1])
    wa, wb = _width(a), _width(b)
    a1, a2, b1, b2 = (a[0], a[1]), (a[2], a[3]), (b[0], b[1]), (b[2], b[3])
    for ob in copper.tracks:
        if ob[5] is not None and ob[5] != a[4]:
            continue
        r = ob[4] / 2.0
        if (
            max(ob[0], ob[2]) + r < minx
            or min(ob[0], ob[2]) - r > maxx
            or max(ob[1], ob[3]) + r < miny
            or min(ob[1], ob[3]) - r > maxy
        ):
            continue
        c1, c2 = (ob[0], ob[1]), (ob[2], ob[3])
        if _seg_dist(c1, c2, a1, a2) < r + wa / 2.0 - _TOUCH_EPS:
            continue
        if _seg_dist(c1, c2, b1, b2) < r + wb / 2.0 - _TOUCH_EPS:
            continue
        if _seg_dist(c1, c2, pa, pb) < r - _TOUCH_EPS:
            return True
    for pad in copper.pads:
        if not _pad_on_layer(pad, a[4]):
            continue
        reach = math.hypot(pad.hx, pad.hy)
        if (
            pad.cx + reach < minx
            or pad.cx - reach > maxx
            or pad.cy + reach < miny
            or pad.cy - reach > maxy
        ):
            continue
        if _pad_seg_dist(pad, a1, a2) < wa / 2.0 - _TOUCH_EPS:
            continue
        if _pad_seg_dist(pad, b1, b2) < wb / 2.0 - _TOUCH_EPS:
            continue
        if _pad_seg_dist(pad, pa, pb) <= _TOUCH_EPS:
            return True
    return False


def _coupled_length(
    own: Sequence[Segment],
    partner: Sequence[Segment],
    copper: Copper | None = None,
    own_leads: bool = True,
    own_net: str = "",
    partner_net: str = "",
) -> float:
    """Sum, over unblocked parallel ``own``/``partner`` pairs, of their overlap.

    Pairs are summed, not unioned: two partners beside the same stretch both
    count, as in KiCad (which can report a negative uncoupled length).
    ``own_leads`` says whose stored direction fixes where a pair's overlap
    "starts" for the blocking probe (the P net's, in KiCad).
    """
    pads = copper.pads if copper else ()
    own_live = [s for s in own if not (pads and _buried(s, own_net, pads))]
    partner_live = [s for s in partner if not (pads and _buried(s, partner_net, pads))]
    total = 0.0
    for a in own_live:
        for b in partner_live:
            span = _coupled_interval(a, b)
            if span is None:
                continue
            if copper:
                if own_leads:
                    t0 = span[0]
                else:
                    # b leads: its start is a's low end when the two run the
                    # same way, a's high end when they run opposite ways.
                    same = (a[2] - a[0]) * (b[2] - b[0]) + (a[3] - a[1]) * (b[3] - b[1]) > 0.0
                    t0 = span[0] if same else span[1]
                if _blocked(a, b, t0, copper):
                    continue
            total += span[1] - span[0]
    return total


def measure_uncoupled(
    own: Sequence[Segment],
    partner: Sequence[Segment],
    copper: Copper | None = None,
    own_leads: bool = True,
    own_net: str = "",
    partner_net: str = "",
) -> float:
    """Uncoupled length (mm) of ``own`` against ``partner``: KiCad's per-net figure.

    ``copper`` is the rest of the board's copper (see :class:`Copper`); without
    it every parallel overlap couples.  ``own_net`` / ``partner_net`` name the
    nets of the two lists so a trace buried in its own pad can be recognised.
    """
    coupled = _coupled_length(own, partner, copper, own_leads, own_net, partner_net)
    return max(0.0, sum(_length(s) for s in own) - coupled)


def _board_copper(pcb: PCB) -> Copper:
    """Every track, via and pad of ``pcb`` as pairing obstacles."""
    from kicad_tools.core.geometry import rotate_pad_offset

    tracks: list[Obstacle] = [
        (s.start[0], s.start[1], s.end[0], s.end[1], s.width, s.layer) for s in pcb.segments
    ]
    tracks += [
        (v.position[0], v.position[1], v.position[0], v.position[1], v.size, None) for v in pcb.vias
    ]
    pads: list[PadRect] = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            ox, oy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            ang = math.radians(pad.rotation)
            pads.append(
                PadRect(
                    fp.position[0] + ox,
                    fp.position[1] + oy,
                    pad.size[0] / 2.0,
                    pad.size[1] / 2.0,
                    math.cos(ang),
                    math.sin(ang),
                    frozenset(pad.layers),
                    pad.net_name or "",
                )
            )
    return Copper(tracks, pads)


def net_component_counts(pcb: PCB) -> dict[str, int]:
    """``{net name: number of components with a pad on the net}``."""
    refs: dict[str, set[str]] = {}
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_name:
                refs.setdefault(pad.net_name, set()).add(fp.reference)
    return {name: len(r) for name, r in refs.items()}


def measure_pair(
    pcb: PCB,
    p_id: int,
    n_id: int,
    p_name: str,
    n_name: str,
    components: dict[str, int] | None = None,
) -> PairMeasure:
    """Measure one pair on ``pcb`` the way KiCad's ``diff_pair_uncoupled`` does."""
    p_segs: list[Segment] = []
    n_segs: list[Segment] = []
    for net_id, bucket in ((p_id, p_segs), (n_id, n_segs)):
        for s in pcb.segments_in_net(net_id):
            bucket.append((s.start[0], s.start[1], s.end[0], s.end[1], s.layer, s.width))
    copper = _board_copper(pcb)
    comps = components if components is not None else net_component_counts(pcb)
    return PairMeasure(
        p_name=p_name,
        n_name=n_name,
        p_routed_mm=sum(_length(s) for s in p_segs),
        n_routed_mm=sum(_length(s) for s in n_segs),
        p_uncoupled_mm=measure_uncoupled(p_segs, n_segs, copper, True, p_name, n_name),
        n_uncoupled_mm=measure_uncoupled(n_segs, p_segs, copper, False, n_name, p_name),
        vias_p=sum(1 for _ in pcb.vias_in_net(p_id)),
        vias_n=sum(1 for _ in pcb.vias_in_net(n_id)),
        components_p=comps.get(p_name, 0),
        components_n=comps.get(n_name, 0),
    )


def uncoupled_limit(limits: PairThresholds, components: int) -> tuple[float | None, str | None]:
    """``(limit mm, how it was derived)`` for a pair, or ``(None, None)``.

    The class's explicit total wins; else the per-terminal allowance times
    the component count (at least one terminal).
    """
    if limits.max_uncoupled_total_mm is not None:
        return limits.max_uncoupled_total_mm, limits.provenance.get("max_uncoupled_total_mm")
    per = limits.max_uncoupled_per_terminal_mm
    if per is None:
        return None, None
    n = max(components, 1)
    src = limits.provenance.get("max_uncoupled_per_terminal_mm", "")
    return per * n, f"{per:g} mm x {n} component{'s' if n != 1 else ''} [{src}]"


def _key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


class DiffPairUncoupledLengthRule(DRCRule):
    """Uncoupled length and via count of engaged differential pairs.

    Attributes:
        rule_id: ``"diffpair_uncoupled_length"``.
        limits: ``{(min_net_id, max_net_id): PairThresholds}`` -- the
            resolved limits and their provenance and severity.  A pair
            without a limit is still measured (reported as ``info`` when
            ``emit_info``).
        engaged_pairs: ``{(min_net_id, max_net_id)}``.  Empty or ``None``:
            the rule is a no-op.
    """

    rule_id = "diffpair_uncoupled_length"
    name = "Differential-Pair Uncoupled Length"
    description = (
        "Validates that engaged differential pairs stay beside each other: "
        "the larger of the two nets' uncoupled lengths, and each net's via "
        "count, within the protocol or class limit."
    )

    def __init__(
        self,
        engaged_pairs: set[tuple[int, int]] | None = None,
        limits: dict[tuple[int, int], PairThresholds] | None = None,
        emit_info: bool = False,
        kct_only: dict[tuple[int, int], str] | None = None,
    ) -> None:
        """Initialize the rule.

        Args:
            engaged_pairs: Pairs to validate.
            limits: Per-pair resolved limits.
            emit_info: Also emit an ``info`` finding for a passing pair
                carrying the measurement (``kct check --verbose``).
            kct_only: ``{pair key: reason}`` for pairs KiCad's native check
                cannot see (names it does not recognise); each gets one
                ``info`` finding saying the limit is kct-only.
        """
        self._engaged = {_key(a, b) for a, b in (engaged_pairs or set())}
        self._limits = {_key(a, b): t for (a, b), t in (limits or {}).items()}
        self._emit_info = emit_info
        self._kct_only = {_key(a, b): why for (a, b), why in (kct_only or {}).items()}

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,  # noqa: ARG002 (signature mirrors peer rules)
    ) -> DRCResults:
        results = DRCResults()
        if not self._engaged:
            return results
        names = {n.number: n.name for n in pcb.nets.values() if n.name}
        comps = net_component_counts(pcb)
        for a, b in sorted(self._engaged):
            na, nb = names.get(a), names.get(b)
            if not na or not nb:
                continue
            measure = measure_pair(pcb, a, b, na, nb, comps)
            if not measure.routed:
                continue  # unrouted pair: no copper, no uncoupled number
            limits = self._limits.get((a, b))
            results.rules_checked += 1
            results.rules_checked_by_rule[self.rule_id] = (
                results.rules_checked_by_rule.get(self.rule_id, 0) + 1
            )
            self._check_uncoupled(results, measure, limits, (a, b))
            self._check_vias(results, measure, limits)
        return results

    # -- uncoupled length --------------------------------------------------

    def _check_uncoupled(
        self,
        results: DRCResults,
        m: PairMeasure,
        limits: PairThresholds | None,
        key: tuple[int, int],
    ) -> None:
        first, second = sorted((m.p_name, m.n_name))
        why = self._kct_only.get(key)
        if why:
            results.add(
                self._violation(
                    "info",
                    f"Differential pair {first}/{second}: {why}; the uncoupled-length "
                    "limit is checked by kct only",
                    m,
                    m.uncoupled_mm,
                    None,
                )
            )
        limit, how = uncoupled_limit(limits, m.components) if limits is not None else (None, None)
        if limit is None:
            return
        detail = (
            f"uncoupled length {m.uncoupled_mm:.3f} mm "
            f"(P {m.p_uncoupled_mm:.3f}, N {m.n_uncoupled_mm:.3f})"
        )
        if m.uncoupled_mm > limit + UNCOUPLED_EPSILON_MM:
            assert limits is not None
            results.add(
                self._violation(
                    limits.uncoupled_severity,
                    f"Differential pair {first}/{second} {detail} exceeds limit "
                    f"{limit:.3f} mm = {how}",
                    m,
                    m.uncoupled_mm,
                    limit,
                )
            )
        elif self._emit_info:
            results.add(
                self._violation(
                    "info",
                    f"Differential pair {first}/{second} {detail} within limit "
                    f"{limit:.3f} mm = {how}",
                    m,
                    m.uncoupled_mm,
                    limit,
                )
            )

    # -- vias --------------------------------------------------------------

    def _check_vias(
        self, results: DRCResults, m: PairMeasure, limits: PairThresholds | None
    ) -> None:
        first, second = sorted((m.p_name, m.n_name))
        if limits is not None and limits.max_vias_per_net is not None:
            cap = limits.max_vias_per_net
            src = limits.provenance.get("max_vias_per_net", "")
            for name, count in ((m.p_name, m.vias_p), (m.n_name, m.vias_n)):
                if count > cap:
                    results.add(
                        self._violation(
                            limits.vias_severity,
                            f"Differential pair {first}/{second}: net {name} has "
                            f"{count} {VIA_OVER_LIMIT_PHRASE} {cap} [{src}]",
                            m,
                            float(count),
                            float(cap),
                            nets=(name,),
                        )
                    )
        if m.vias_p != m.vias_n:
            results.add(
                self._violation(
                    "warning",
                    f"Differential pair {first}/{second} has {VIA_UNEQUAL_PHRASE} "
                    f"({m.p_name}: {m.vias_p}, {m.n_name}: {m.vias_n})",
                    m,
                    float(abs(m.vias_p - m.vias_n)),
                    0.0,
                )
            )

    def _violation(
        self,
        severity: str,
        message: str,
        m: PairMeasure,
        actual: float,
        required: float | None,
        nets: tuple[str, ...] | None = None,
    ) -> DRCViolation:
        pair = tuple(sorted((m.p_name, m.n_name)))
        return DRCViolation(
            rule_id=self.rule_id,
            severity=severity,
            message=message,
            location=None,
            layer=None,
            actual_value=round(actual, 4),
            required_value=round(required, 4) if required is not None else None,
            items=pair if severity != "info" else (),
            nets=nets if nets is not None else (m.p_name, m.n_name),
        )

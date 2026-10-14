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
from dataclasses import dataclass
from typing import TYPE_CHECKING

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

#: Float-comparison slack (mm) on a limit, as the sibling skew rule has.
UNCOUPLED_EPSILON_MM = 1e-6

Segment = tuple[float, float, float, float, str]  # x1, y1, x2, y2, layer


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


def _coupled_length(own: list[Segment], partner: list[Segment]) -> float:
    """Length of ``own`` that runs beside ``partner`` copper (union per segment)."""
    total = 0.0
    for seg in own:
        spans = sorted(s for s in (_coupled_interval(seg, p) for p in partner) if s)
        covered = 0.0
        cursor = 0.0
        for start, end in spans:
            start = max(start, cursor)
            if end > start:
                covered += end - start
                cursor = end
        total += covered
    return total


def measure_uncoupled(own: list[Segment], partner: list[Segment]) -> float:
    """Uncoupled length (mm) of ``own`` against ``partner``: KiCad's per-net figure."""
    return max(0.0, sum(_length(s) for s in own) - _coupled_length(own, partner))


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
            bucket.append((s.start[0], s.start[1], s.end[0], s.end[1], s.layer))
    comps = components if components is not None else net_component_counts(pcb)
    return PairMeasure(
        p_name=p_name,
        n_name=n_name,
        p_routed_mm=sum(_length(s) for s in p_segs),
        n_routed_mm=sum(_length(s) for s in n_segs),
        p_uncoupled_mm=measure_uncoupled(p_segs, n_segs),
        n_uncoupled_mm=measure_uncoupled(n_segs, p_segs),
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
                            f"{count} vias, limit {cap} [{src}]",
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
                    f"Differential pair {first}/{second} has unequal via counts "
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

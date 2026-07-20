"""Current-sense / analog layout lint (Phase 1).

Advisory-only analyzer that flags **sense** nets that run parallel and close
to **high-current / switching** nets on the *same* copper layer. Long parallel
runs at small gaps inductively/capacitively couple switching noise into a
high-impedance sense line, corrupting the measured current/voltage. This is the
metric that would have caught the softstart rev-C finding where ``/OC_TRIP_N``
ran adjacent to ``/GATE_NEG_A``.

Phase 1 scope (see issue #4328):

* For every sense net, scan all segments of every high-current net on the same
  layer and report, per sense net, the **maximum parallel-run length** and the
  **minimum edge-to-edge gap** to a single reported high-current blocker,
  flagged PASS/FAIL against thresholds. The FAIL rule is evaluated against
  *every* same-layer high-current blocker (not just the nearest-by-gap one);
  the reported blocker is the nearest-by-gap net on PASS and the
  worst-coupling offender (largest parallel run, tiebreak smallest gap) on
  FAIL.
* Sense / high-current nets are identified by
  :func:`kicad_tools.router.net_class.classify_from_name`
  (``ANALOG`` -> sense; ``HIGH_CURRENT_SIGNAL`` / ``POWER`` -> high-current),
  **plus** explicit ``sense_nets`` / ``hicur_nets`` name overrides so nets whose
  prefixes are not yet auto-classified (``GATE_*`` / ``SRC_*`` / ``TRK_*``) can
  be tagged today. Auto-classification of those prefixes is deferred to Phase 3.
* Only same-layer pairs are considered. Inner-layer / broadside coupling is
  Phase 3 (#4331); copper sense-loop area is Phase 2 (#4330).

The pairwise parallel-run + gap geometry is computed by the shared
:func:`kicad_tools.analysis.signal_integrity.calculate_coupling_geometry`
primitive (not duplicated here).

**FAIL rule** (physically motivated, documented for the census): a sense net is
``FAIL`` when, against *any* same-layer high-current blocker, it *both* runs
parallel for at least ``max_parallel_mm`` **and** is separated by at most
``min_gap_mm``. The rule is checked per blocker, so a long-parallel blocker at a
slightly larger (but still sub-threshold) gap triggers a FAIL even when a
different, closer blocker only grazes the sense line -- evaluating the
nearest-by-gap blocker alone would miss this and falsely PASS. A long run that
stays far away, or a close approach that is only momentary (short parallel run),
is ``PASS``. Both conditions must hold to fail because coupling scales with
parallel length *and* inverse gap -- either one alone is not sufficient to
corrupt a sense line.

This module is advisory only: it never raises on malformed geometry and skips
un-inspectable data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools.analysis.signal_integrity import calculate_coupling_geometry
from kicad_tools.router.net_class import NetClass, classify_from_name

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB, Segment, Via

__all__ = [
    "CurrentSenseAnalyzer",
    "CurrentSenseResult",
    "DEFAULT_MAX_LOOP_AREA_MM2",
    "DEFAULT_MAX_PARALLEL_MM",
    "DEFAULT_MIN_GAP_MM",
]

# Default thresholds (mm). A sense net running parallel to a high-current net
# for >= DEFAULT_MAX_PARALLEL_MM at an edge gap <= DEFAULT_MIN_GAP_MM is FAIL.
DEFAULT_MAX_PARALLEL_MM = 10.0
DEFAULT_MIN_GAP_MM = 0.5

# Phase 2 (#4330): default enclosed copper sense-loop area threshold (mm^2). A
# Kelvin sense loop whose enclosed area exceeds this is FAIL. 10.0 mm^2 is a
# conservative starting value pending EE confirmation (see the issue's note).
DEFAULT_MAX_LOOP_AREA_MM2 = 10.0

# Endpoint-snapping tolerance (mm) used when merging a conductor's segments into
# ordered polylines. Numerically-coincident joints (and via transitions, whose
# two layers' segments meet at the shared via position) are merged within this
# tolerance so ``shapely.ops.linemerge`` can chain them into one polyline.
_SNAP_TOL_MM = 1e-3

# Suffix conventions for auto-pairing a Kelvin sense net with its return
# conductor: ``(positive_suffix, negative_suffix)``. ``/I_SENSE_P`` pairs with
# ``/I_SENSE_N``, ``VSNS+`` with ``VSNS-``, ``FB_H`` with ``FB_L``.
_AUTO_PAIR_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_P", "_N"),
    ("+", "-"),
    ("_H", "_L"),
)


@dataclass
class CurrentSenseResult:
    """Per-sense-net current-sense coupling census row.

    Attributes:
        sense_net: Name of the sense net.
        nearest_hicur_net: Name of the reported same-layer high-current
            blocker, or ``None`` if the sense net has no same-layer
            high-current neighbor. Selection depends on status: on ``PASS``
            this is the nearest-by-gap blocker (smallest gap); on ``FAIL`` it
            is the *worst offender* -- the failing blocker with the largest
            ``max_parallel_mm`` (strongest coupling), tiebreak smallest gap.
        layer: Copper layer on which the reported coupling occurs, or ``None``.
        max_parallel_mm: Maximum parallel-run length (mm) against the reported
            blocker. ``0.0`` when there is no neighbor.
        min_gap_mm: Minimum edge-to-edge gap (mm) to the reported blocker, or
            ``None`` when there is no neighbor.
        status: ``"FAIL"`` or ``"PASS"``.
        margin_mm: Gap-clearance margin (``min_gap_mm - min_gap_threshold``);
            positive means the gap is above the minimum. ``None`` when there is
            no neighbor. Note a FAIL always has ``margin_mm <= 0`` *and* also
            exceeded the parallel-run threshold.
        loop_area_mm2: Phase 2 (#4330). Enclosed copper area (mm^2) of the
            Kelvin sense loop this net belongs to (sense conductor + its return
            conductor closed at both terminals), or ``None`` when no loop could
            be identified/closed (advisory graceful-degrade).
        loop_status: ``"FAIL"`` when ``loop_area_mm2 > max_loop_area``,
            ``"PASS"`` when ``<=``, or ``None`` when no loop was measured.
        loop_reason: Human-readable reason the loop area is ``None`` (e.g. no
            return conductor, or the loop did not close). ``None`` when a loop
            area was measured.
    """

    sense_net: str
    nearest_hicur_net: str | None
    layer: str | None
    max_parallel_mm: float
    min_gap_mm: float | None
    status: str
    margin_mm: float | None
    # Phase 2 (#4330) -- additive; default None so Phase-1 construction and any
    # sense net not part of an identified loop carries null loop fields.
    loop_area_mm2: float | None = None
    loop_status: str | None = None
    loop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict (mirrors sibling analyzers).

        Phase-1 keys are emitted unchanged; Phase-2 adds ``loop_area_mm2`` and
        ``loop_status`` (always), plus ``loop_reason`` only when it is set.
        """
        out: dict[str, Any] = {
            "sense_net": self.sense_net,
            "nearest_hicur_net": self.nearest_hicur_net,
            "layer": self.layer,
            "max_parallel_mm": round(self.max_parallel_mm, 3),
            "min_gap_mm": (None if self.min_gap_mm is None else round(self.min_gap_mm, 3)),
            "status": self.status,
            "margin": (None if self.margin_mm is None else round(self.margin_mm, 3)),
            "loop_area_mm2": (None if self.loop_area_mm2 is None else round(self.loop_area_mm2, 3)),
            "loop_status": self.loop_status,
        }
        if self.loop_reason is not None:
            out["loop_reason"] = self.loop_reason
        return out


class CurrentSenseAnalyzer:
    """Analyze sense-net vs. high-current-net same-layer parallel coupling.

    Advisory only; never raises. Construct with thresholds and optional net
    name overrides, then call :meth:`analyze` with a loaded :class:`PCB`.
    """

    def __init__(
        self,
        max_parallel_mm: float = DEFAULT_MAX_PARALLEL_MM,
        min_gap_mm: float = DEFAULT_MIN_GAP_MM,
        sense_nets: list[str] | None = None,
        hicur_nets: list[str] | None = None,
        max_loop_area_mm2: float = DEFAULT_MAX_LOOP_AREA_MM2,
        sense_pairs: list[tuple[str, str]] | list[list[str]] | None = None,
        sense_return: str | None = None,
    ) -> None:
        """Initialize the analyzer.

        Args:
            max_parallel_mm: Parallel-run threshold (mm) for FAIL.
            min_gap_mm: Edge-to-edge gap threshold (mm) for FAIL.
            sense_nets: Explicit sense-net names (override / augment
                auto-classification).
            hicur_nets: Explicit high-current-net names (override / augment
                auto-classification).
            max_loop_area_mm2: Phase 2 (#4330) enclosed-loop-area threshold
                (mm^2) for the loop FAIL rule.
            sense_pairs: Explicit Kelvin pairings ``[(sense, return), ...]``.
                Either order matches; the partner conductor closes the loop.
            sense_return: A single shared return-conductor name (e.g. a Kelvin
                ground) used to close any sense net that has no explicit or
                auto-detected partner.
        """
        self.max_parallel_mm = max_parallel_mm
        self.min_gap_mm = min_gap_mm
        self.sense_overrides = {n for n in (sense_nets or []) if n}
        self.hicur_overrides = {n for n in (hicur_nets or []) if n}
        self.max_loop_area_mm2 = max_loop_area_mm2
        # Normalize explicit pairs to a list of 2-tuples, dropping malformed
        # entries defensively (advisory-only contract).
        self.sense_pairs: list[tuple[str, str]] = [
            (str(p[0]), str(p[1]))
            for p in (sense_pairs or [])
            if p is not None and len(p) == 2 and p[0] and p[1]
        ]
        self.sense_return = sense_return or None

    # ------------------------------------------------------------------
    # Net classification
    # ------------------------------------------------------------------
    def classify_nets(self, pcb: PCB) -> tuple[set[str], set[str]]:
        """Return ``(sense_net_names, hicur_net_names)`` for a board.

        Auto-classifies by name via ``classify_from_name`` and folds in the
        explicit overrides. A net named in *both* the sense and high-current
        sets is treated as sense-only (it is the victim of interest); it is
        removed from the high-current set to avoid self-comparison noise.
        """
        sense: set[str] = set(self.sense_overrides)
        hicur: set[str] = set(self.hicur_overrides)

        for net in pcb.nets.values():
            name = net.name
            if not name:
                continue
            net_class = classify_from_name(name)
            if net_class == NetClass.ANALOG:
                sense.add(name)
            elif net_class in (NetClass.HIGH_CURRENT_SIGNAL, NetClass.POWER):
                hicur.add(name)

        # A net cannot be both victim and aggressor.
        hicur -= sense
        return sense, hicur

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------
    def analyze(self, pcb: PCB) -> list[CurrentSenseResult]:
        """Produce one :class:`CurrentSenseResult` per sense net.

        Returns an empty list when the board has no sense nets. Never raises.
        """
        try:
            sense_names, hicur_names = self.classify_nets(pcb)
        except Exception:
            return []

        if not sense_names:
            return []

        # Resolve each segment's net name robustly: prefer the segment's own
        # net_name, else fall back to the board net table by number (handles
        # number-only ``(net N)`` and KiCad 10 name-only references).
        num_to_name = {num: net.name for num, net in pcb.nets.items()}
        try:
            all_segments = list(pcb.segments)
        except Exception:
            all_segments = []

        named: list[tuple[str, Segment]] = []
        segs_by_name: dict[str, list[Segment]] = {}
        for seg in all_segments:
            name = seg.net_name or num_to_name.get(seg.net_number, "")
            if name:
                named.append((name, seg))
                segs_by_name.setdefault(name, []).append(seg)

        # Name-indexed vias (Phase 2) so a via-transitioned conductor can be
        # bridged at the shared via position when merging its polyline.
        vias_by_name: dict[str, list[Via]] = {}
        try:
            all_vias = list(pcb.vias)
        except Exception:
            all_vias = []
        for via in all_vias:
            vname = via.net_name or num_to_name.get(via.net_number, "")
            if vname:
                vias_by_name.setdefault(vname, []).append(via)

        hicur_named: list[tuple[str, Segment]] = [
            (name, seg) for name, seg in named if name in hicur_names
        ]

        conductor_names = set(segs_by_name)

        results: list[CurrentSenseResult] = []
        for sense_name in sorted(sense_names):
            sense_segs = segs_by_name.get(sense_name, [])
            result = self._analyze_sense_net(sense_name, sense_segs, hicur_named)
            area, status, reason = self._analyze_loop(
                sense_name, conductor_names, segs_by_name, vias_by_name
            )
            result.loop_area_mm2 = area
            result.loop_status = status
            result.loop_reason = reason
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Phase 2: copper sense-loop area
    # ------------------------------------------------------------------
    def _resolve_partner(self, sense_name: str, conductor_names: set[str]) -> str | None:
        """Return the return-conductor net name closing ``sense_name``'s loop.

        Resolution order: explicit ``sense_pairs`` -> shared ``sense_return``
        -> suffix auto-pair convention. The partner must itself be a routed
        conductor (present in ``conductor_names``) and distinct from the sense
        net. Returns ``None`` when no partner is found.
        """
        for a, b in self.sense_pairs:
            if sense_name == a and b != sense_name and b in conductor_names:
                return b
            if sense_name == b and a != sense_name and a in conductor_names:
                return a

        if (
            self.sense_return
            and self.sense_return != sense_name
            and self.sense_return in conductor_names
        ):
            return self.sense_return

        for pos, neg in _AUTO_PAIR_SUFFIXES:
            if sense_name.endswith(pos):
                cand = sense_name[: -len(pos)] + neg
                if cand != sense_name and cand in conductor_names:
                    return cand
            if sense_name.endswith(neg):
                cand = sense_name[: -len(neg)] + pos
                if cand != sense_name and cand in conductor_names:
                    return cand
        return None

    def _analyze_loop(
        self,
        sense_name: str,
        conductor_names: set[str],
        segs_by_name: dict[str, list[Segment]],
        vias_by_name: dict[str, list[Via]],
    ) -> tuple[float | None, str | None, str | None]:
        """Return ``(loop_area_mm2, loop_status, loop_reason)`` for one net.

        Advisory-only: any failure degrades to ``(None, None, reason)`` and
        never raises.
        """
        partner = self._resolve_partner(sense_name, conductor_names)
        if partner is None:
            return None, None, "no return conductor (single-ended, not closable)"

        try:
            area, reason = self._compute_loop_area(
                segs_by_name.get(sense_name, []),
                segs_by_name.get(partner, []),
                vias_by_name.get(sense_name, []),
                vias_by_name.get(partner, []),
            )
        except Exception:
            return None, None, "loop area computation failed"

        if area is None:
            return None, None, reason

        status = "FAIL" if area > self.max_loop_area_mm2 else "PASS"
        return area, status, None

    def _compute_loop_area(
        self,
        sense_segs: list[Segment],
        partner_segs: list[Segment],
        sense_vias: list[Via],
        partner_vias: list[Via],
    ) -> tuple[float | None, str | None]:
        """Compute the enclosed area (mm^2) of the sense<->return copper loop.

        Each conductor's segments are merged into a single ordered polyline
        (``shapely.ops.linemerge``, endpoints snapped within ``_SNAP_TOL_MM``
        and to any shared via position). The two polylines are closed with the
        nearest-endpoint terminals and the enclosed polygon area is returned.
        Returns ``(None, reason)`` on any non-closable geometry.
        """
        try:
            from shapely.geometry import Polygon  # type: ignore[import-untyped]
        except Exception:
            return None, "shapely unavailable"

        if not sense_segs or not partner_segs:
            return None, "no return conductor (single-ended, not closable)"

        merged_s = self._merge_conductor(sense_segs, sense_vias)
        merged_p = self._merge_conductor(partner_segs, partner_vias)
        if merged_s is None or merged_p is None:
            return None, "loop did not close (empty conductor geometry)"
        if merged_s.geom_type != "LineString" or merged_p.geom_type != "LineString":
            return None, "loop did not close (conductor is not a single contiguous run)"

        s_coords = list(merged_s.coords)
        p_coords = list(merged_p.coords)
        if len(s_coords) < 2 or len(p_coords) < 2:
            return None, "loop did not close (degenerate conductor)"

        s0, s1 = s_coords[0], s_coords[-1]
        p0, p1 = p_coords[0], p_coords[-1]

        def _d(a: tuple[float, float], b: tuple[float, float]) -> float:
            return math.hypot(a[0] - b[0], a[1] - b[1])

        # Pick the terminal pairing (straight vs. crossed) that yields the
        # shorter total closing length -- i.e. the simple, non-self-crossing
        # polygon -- then walk sense forward and return backward.
        straight = _d(s0, p0) + _d(s1, p1)
        crossed = _d(s0, p1) + _d(s1, p0)
        if straight <= crossed:
            ring = s_coords + p_coords[::-1]
        else:
            ring = s_coords + p_coords

        poly = Polygon(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)
        area = float(poly.area)
        if area <= 0.0:
            return None, "loop did not close (zero enclosed area)"
        return area, None

    @staticmethod
    def _merge_conductor(segs: list[Segment], vias: list[Via]) -> Any:
        """Merge a net's segments into ordered polyline(s).

        Endpoints are rounded to ``_SNAP_TOL_MM`` and snapped to any nearby via
        position so numerically-coincident joints and via transitions chain
        into one polyline via ``shapely.ops.linemerge``. Returns a shapely
        ``LineString`` / ``MultiLineString``, or ``None`` if there is no usable
        geometry.
        """
        from shapely.geometry import LineString
        from shapely.ops import linemerge  # type: ignore[import-untyped]

        via_positions = [tuple(v.position) for v in vias]

        def snap(pt: tuple[float, float]) -> tuple[float, float]:
            for vp in via_positions:
                if abs(pt[0] - vp[0]) <= _SNAP_TOL_MM and abs(pt[1] - vp[1]) <= _SNAP_TOL_MM:
                    return (round(vp[0], 3), round(vp[1], 3))
            return (round(pt[0], 3), round(pt[1], 3))

        lines = []
        for seg in segs:
            a = snap(seg.start)
            b = snap(seg.end)
            if a != b:
                lines.append(LineString([a, b]))

        if not lines:
            return None
        if len(lines) == 1:
            return lines[0]
        return linemerge(lines)

    def _analyze_sense_net(
        self,
        sense_name: str,
        sense_segs: list[Segment],
        hicur_named: list[tuple[str, Segment]],
    ) -> CurrentSenseResult:
        """Compute the census row for a single sense net."""
        # Per high-current net aggregates over coupled (parallel) pairs:
        #   min gap, the max parallel-run, and the layer of the min-gap pair.
        per_net_min_gap: dict[str, float] = {}
        per_net_max_parallel: dict[str, float] = {}
        per_net_layer: dict[str, str] = {}

        for s_seg in sense_segs:
            for h_name, h_seg in hicur_named:
                # Same-layer only (Phase 1).
                if s_seg.layer != h_seg.layer:
                    continue
                # Never compare a net to itself (defensive; hicur excludes
                # sense names already).
                if h_name == sense_name:
                    continue

                try:
                    parallel, gap = calculate_coupling_geometry(s_seg, h_seg)
                except Exception:
                    continue

                # Not actually coupled (non-parallel / degenerate).
                if parallel <= 0.0:
                    continue

                prev_max = per_net_max_parallel.get(h_name, 0.0)
                if parallel > prev_max:
                    per_net_max_parallel[h_name] = parallel

                prev_gap = per_net_min_gap.get(h_name)
                if prev_gap is None or gap < prev_gap:
                    per_net_min_gap[h_name] = gap
                    per_net_layer[h_name] = h_seg.layer

        if not per_net_min_gap:
            # No same-layer high-current neighbor: clean PASS.
            return CurrentSenseResult(
                sense_net=sense_name,
                nearest_hicur_net=None,
                layer=None,
                max_parallel_mm=0.0,
                min_gap_mm=None,
                status="PASS",
                margin_mm=None,
            )

        # Evaluate the AND FAIL rule against EVERY same-layer high-current
        # blocker -- not just the nearest-by-gap one. A blocker with a longer
        # parallel run at a slightly larger (but still sub-threshold) gap is a
        # real coupling risk that a nearest-by-gap-only check would miss
        # (false PASS). The net FAILs if ANY blocker satisfies the rule.
        failing = [
            name
            for name in per_net_min_gap
            if per_net_max_parallel.get(name, 0.0) >= self.max_parallel_mm
            and per_net_min_gap[name] <= self.min_gap_mm
        ]

        if failing:
            # FAIL: report the WORST offender = failing blocker with the
            # largest parallel run (strongest coupling), tiebreak smallest gap.
            offender = min(
                failing,
                key=lambda n: (-per_net_max_parallel.get(n, 0.0), per_net_min_gap[n]),
            )
            status = "FAIL"
        else:
            # PASS: preserve phase-1 behavior exactly -- report the
            # nearest-by-gap blocker so clean rows stay byte-identical.
            offender = min(per_net_min_gap, key=lambda n: per_net_min_gap[n])
            status = "PASS"

        min_gap = per_net_min_gap[offender]
        max_parallel = per_net_max_parallel.get(offender, 0.0)
        layer = per_net_layer.get(offender)
        margin = min_gap - self.min_gap_mm

        return CurrentSenseResult(
            sense_net=sense_name,
            nearest_hicur_net=offender,
            layer=layer,
            max_parallel_mm=max_parallel,
            min_gap_mm=min_gap,
            status=status,
            margin_mm=margin,
        )

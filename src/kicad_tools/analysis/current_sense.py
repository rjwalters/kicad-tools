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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools.analysis.signal_integrity import calculate_coupling_geometry
from kicad_tools.router.net_class import NetClass, classify_from_name

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB, Segment

__all__ = [
    "CurrentSenseAnalyzer",
    "CurrentSenseResult",
    "DEFAULT_MAX_PARALLEL_MM",
    "DEFAULT_MIN_GAP_MM",
]

# Default thresholds (mm). A sense net running parallel to a high-current net
# for >= DEFAULT_MAX_PARALLEL_MM at an edge gap <= DEFAULT_MIN_GAP_MM is FAIL.
DEFAULT_MAX_PARALLEL_MM = 10.0
DEFAULT_MIN_GAP_MM = 0.5


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
    """

    sense_net: str
    nearest_hicur_net: str | None
    layer: str | None
    max_parallel_mm: float
    min_gap_mm: float | None
    status: str
    margin_mm: float | None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict (mirrors sibling analyzers)."""
        return {
            "sense_net": self.sense_net,
            "nearest_hicur_net": self.nearest_hicur_net,
            "layer": self.layer,
            "max_parallel_mm": round(self.max_parallel_mm, 3),
            "min_gap_mm": (None if self.min_gap_mm is None else round(self.min_gap_mm, 3)),
            "status": self.status,
            "margin": (None if self.margin_mm is None else round(self.margin_mm, 3)),
        }


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
    ) -> None:
        """Initialize the analyzer.

        Args:
            max_parallel_mm: Parallel-run threshold (mm) for FAIL.
            min_gap_mm: Edge-to-edge gap threshold (mm) for FAIL.
            sense_nets: Explicit sense-net names (override / augment
                auto-classification).
            hicur_nets: Explicit high-current-net names (override / augment
                auto-classification).
        """
        self.max_parallel_mm = max_parallel_mm
        self.min_gap_mm = min_gap_mm
        self.sense_overrides = {n for n in (sense_nets or []) if n}
        self.hicur_overrides = {n for n in (hicur_nets or []) if n}

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
        for seg in all_segments:
            name = seg.net_name or num_to_name.get(seg.net_number, "")
            if name:
                named.append((name, seg))

        hicur_named: list[tuple[str, Segment]] = [
            (name, seg) for name, seg in named if name in hicur_names
        ]

        results: list[CurrentSenseResult] = []
        for sense_name in sorted(sense_names):
            sense_segs = [seg for name, seg in named if name == sense_name]
            results.append(self._analyze_sense_net(sense_name, sense_segs, hicur_named))
        return results

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

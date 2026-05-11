"""Differential-pair routing-continuity DRC rule.

Validates that an *engaged* differential pair's routed traces stay
coupled for a configurable fraction of their length.  An engaged pair
(per Epic #2556 Phase 2E, #2638) is one whose net class has
``coupled_routing == True`` and which passed the engagement-layer
single-ended refusal check; un-engaged pairs are intentionally not
checked.

The rule is part of Epic #2556 Phase 2G (Issue #2640):

- Phase 1A-1D (#2557/#2558/#2559/#2560/#2587, merged): added per-class
  ``intra_pair_clearance`` and within-pair clearance DRC.
- Phase 2E (#2638, in flight): adds ``coupled_routing`` opt-in flag and
  the ``should_engage_coupled(pair, net_class_routing, net_to_class)``
  helper that produces the engagement decision.
- Phase 2F (#2639, in flight): diff-pair-aware escape routing.
- Phase 2G (this rule): validates the *result* of engaged coupled
  routing -- a pair engaged by the router but whose traces diverged
  (e.g. one side took a long detour around an obstacle) is a defect.

What "coupled" means here:

For each P-side segment, the rule computes the length of the segment's
centerline whose nearest point on any N-side segment is

  1. within ``coupling_window_mm`` (edge-to-edge plus the manufacturer
     min clearance, capped at a few mm), AND
  2. parallel to the N-side segment within +/-15 degrees.

The pair's "coupled fraction" is

    sum_of_coupled_lengths / total_P_routed_length

and the violation fires when ``coupled_fraction <
threshold_for(pair)``.  ``threshold_for`` consults the per-pair
threshold map (constructor argument); pairs not in the map use the
module-level :data:`DEFAULT_COUPLED_CONTINUITY_THRESHOLD` (0.7).

The 70% default is calibrated against board 03 (USB joystick), whose
USB_D+/D- pair couples for ~60-80% of its length under the current
Phase 1 path (curator note on #2640).  A higher default (e.g. 0.9)
would fire on a currently-acceptable layout; a lower default (e.g.
0.5) would miss the failure mode this rule is meant to catch (a
nominally-coupled pair that diverges for 95% of its length).

Graceful degradation:

When ``engaged_pairs`` is ``None`` or empty (e.g., running ``kct
check`` standalone with no router context), the rule is a conservative
no-op.  The intended call site (the autorouter consumer, gated by
#2638's :func:`should_engage_coupled`) provides the set explicitly.

Scope (out of scope -- explicitly):

- Length skew between P and N (Phase 3J, separate rule).
- Impedance verification (Phase 3, separate rule).
- Pad-to-pad coupling at the launch from a package (Phase 2F's domain).
- N-side segments on different layers from P (engaged routing keeps a
  pair on the same layer by design; cross-layer coupled fragments
  count as un-coupled by this rule).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule
from .clearance import CopperElement, _segment_segment_clearance

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


# Module-level default threshold.  Calibrated against board 03 (curator
# note on #2640).  Use :meth:`NetClassRouting.effective_coupled_continuity_threshold`
# to consume this with per-class override semantics.
DEFAULT_COUPLED_CONTINUITY_THRESHOLD = 0.7

# Default coupling window (mm).  When the autorouter consumer constructs
# the rule, it should pass a per-pair window derived from the pair's
# ``intra_pair_clearance`` plus a small margin -- when no window is
# supplied the rule uses this conservative default of 0.5 mm, which
# covers the typical 0.075-0.25 mm intra-pair geometry plus margin.
DEFAULT_COUPLING_WINDOW_MM = 0.5

# Parallel tolerance (degrees) inside which two segments are considered
# parallel for coupling purposes.  +/-15 degrees mirrors the curator's
# spec on #2640 and the empirical orientation tolerance of board 03's
# USB pair.
DEFAULT_PARALLEL_TOLERANCE_DEG = 15.0


def _segment_length(seg: CopperElement) -> float:
    """Return the Euclidean length of a segment in mm."""
    x1, y1, x2, y2, _w = seg.geometry
    return math.hypot(x2 - x1, y2 - y1)


def _segment_angle_deg(seg: CopperElement) -> float:
    """Return the segment's orientation angle in degrees, in ``[0, 180)``.

    Since coupling is direction-agnostic (a P segment running east and
    an N segment running west are still "parallel" for coupling
    purposes), we normalize to the unsigned half-circle.
    """
    x1, y1, x2, y2, _w = seg.geometry
    raw = math.degrees(math.atan2(y2 - y1, x2 - x1))
    # Map to [0, 180) so anti-parallel == parallel.
    if raw < 0:
        raw += 180.0
    if raw >= 180.0:
        raw -= 180.0
    return raw


def _angle_difference_deg(a: float, b: float) -> float:
    """Return the smaller of the two angular differences in ``[0, 90]``.

    Both inputs are in ``[0, 180)``.  Output is the closer of
    ``|a - b|`` and ``180 - |a - b|``, capped at 90.
    """
    raw = abs(a - b)
    if raw > 90.0:
        raw = 180.0 - raw
    return raw


def _segment_coupled_overlap(
    p_seg: CopperElement,
    n_seg: CopperElement,
    coupling_window_mm: float,
    parallel_tolerance_deg: float = DEFAULT_PARALLEL_TOLERANCE_DEG,
) -> float:
    """Return the length (mm) of ``p_seg`` that is coupled to ``n_seg``.

    "Coupled" means the centerline-to-centerline edge-to-edge clearance
    is within ``coupling_window_mm`` AND the two segments are parallel
    within ``parallel_tolerance_deg``.

    Returns 0.0 when:
      - the segments are not parallel within tolerance, OR
      - the edge-to-edge clearance exceeds ``coupling_window_mm``, OR
      - either segment is on a different layer (caller should pre-filter
        on layer).

    NOTE: this is an approximation.  The exact projected-overlap window
    on two coplanar parallel segments would require projecting each
    endpoint onto the partner's centerline and intersecting the two
    parametric intervals.  For the use cases here (validating that a
    coupled-routed pair stays parallel for most of its length), a
    simpler all-or-nothing heuristic suffices: when the two segments
    pass the parallel-and-close test, the coupled length is the length
    of ``p_seg`` itself; otherwise zero.  This conservatively
    over-credits coupling on short overlap windows where P is much
    longer than N, but those configurations are exactly the ones a
    legitimately coupled pair avoids by construction (the router emits
    P and N as parallel mirror segments).

    Args:
        p_seg: The P-side ``CopperElement`` (must be a segment).
        n_seg: The N-side ``CopperElement`` (must be a segment).
        coupling_window_mm: Maximum edge-to-edge clearance (mm) for the
            pair to count as coupled.
        parallel_tolerance_deg: Maximum angular difference (deg) for the
            pair to count as parallel.  Defaults to 15.

    Returns:
        ``length_of_p_seg`` when the pair qualifies as coupled, else
        ``0.0``.
    """
    if p_seg.layer != n_seg.layer:
        return 0.0
    p_angle = _segment_angle_deg(p_seg)
    n_angle = _segment_angle_deg(n_seg)
    if _angle_difference_deg(p_angle, n_angle) > parallel_tolerance_deg:
        return 0.0
    clearance, _x, _y = _segment_segment_clearance(p_seg, n_seg)
    # Edge-to-edge clearance can be negative when traces overlap; treat
    # negative as "very close" (coupled).
    if clearance > coupling_window_mm:
        return 0.0
    return _segment_length(p_seg)


class DiffPairRoutingContinuityRule(DRCRule):
    """Validate routing continuity (coupled fraction) for engaged diff pairs.

    The rule consumes a caller-supplied ``engaged_pairs`` set --
    typically produced by #2638's :func:`should_engage_coupled` -- and
    a per-pair threshold map.  Pairs not in ``engaged_pairs`` are
    deliberately not checked (the rule applies only to pairs the
    designer opted into coupled routing for).

    Attributes:
        rule_id: ``"diffpair_routing_continuity"`` -- MUST exactly match
            the alias-table key in :mod:`kicad_tools.drc.violation`.
        engaged_pairs: ``{(min_net_id, max_net_id)}`` set of pairs whose
            net class has ``coupled_routing == True`` AND that passed
            engagement-layer refusal.  Pairs not in this set are not
            checked.  ``None`` is equivalent to an empty set (rule is a
            no-op).
        threshold_map: ``{(min_net_id, max_net_id) -> threshold}`` map of
            per-pair coupled-fraction thresholds.  Pairs missing from
            the map use the module-level default
            (:data:`DEFAULT_COUPLED_CONTINUITY_THRESHOLD`).
        coupling_window_mm: Maximum edge-to-edge clearance (mm) for the
            pair-coupling test.  Defaults to
            :data:`DEFAULT_COUPLING_WINDOW_MM`.
        parallel_tolerance_deg: Maximum angular deviation (deg) for the
            pair-coupling test.  Defaults to
            :data:`DEFAULT_PARALLEL_TOLERANCE_DEG`.
    """

    rule_id = "diffpair_routing_continuity"
    name = "Differential-Pair Routing Continuity"
    description = (
        "Validates that engaged differential pairs stay coupled (parallel "
        "and within the coupling window) for the per-class continuity "
        "threshold fraction of their length."
    )

    def __init__(
        self,
        engaged_pairs: set[tuple[int, int]] | None = None,
        threshold_map: dict[tuple[int, int], float] | None = None,
        coupling_window_mm: float = DEFAULT_COUPLING_WINDOW_MM,
        parallel_tolerance_deg: float = DEFAULT_PARALLEL_TOLERANCE_DEG,
        default_threshold: float = DEFAULT_COUPLED_CONTINUITY_THRESHOLD,
    ) -> None:
        """Initialize the rule.

        Args:
            engaged_pairs: ``{(min_net_id, max_net_id)}`` set of pairs to
                validate (each tuple's order is normalized to ``(min,
                max)``).  ``None`` or empty -> the rule is a no-op
                (graceful degradation when no router context is
                available, e.g., ``kct check`` standalone).
            threshold_map: Optional per-pair threshold overrides.  Keys
                are normalized to ``(min, max)`` net-id tuples to match
                ``engaged_pairs``.  Missing pairs use
                ``default_threshold``.
            coupling_window_mm: Edge-to-edge clearance ceiling (mm) for
                the coupling test.  Pairs spaced wider than this on a
                given segment pair do not count as coupled.
            parallel_tolerance_deg: Angular tolerance (deg) inside which
                P-side and N-side segments count as parallel.
            default_threshold: Fallback when no per-pair threshold is
                set.  Defaults to
                :data:`DEFAULT_COUPLED_CONTINUITY_THRESHOLD` (0.7).
        """
        # Normalize key ordering so callers don't have to.
        self._engaged: set[tuple[int, int]] = set()
        if engaged_pairs:
            for a, b in engaged_pairs:
                self._engaged.add((a, b) if a <= b else (b, a))

        self._threshold_map: dict[tuple[int, int], float] = {}
        if threshold_map:
            for (a, b), thr in threshold_map.items():
                key = (a, b) if a <= b else (b, a)
                self._threshold_map[key] = thr

        self._coupling_window_mm = coupling_window_mm
        self._parallel_tolerance_deg = parallel_tolerance_deg
        self._default_threshold = default_threshold

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,  # noqa: ARG002 (unused; signature mirrors peer rules)
    ) -> DRCResults:
        """Check routing continuity for all engaged pairs.

        Args:
            pcb: The PCB to check.
            design_rules: Active manufacturer design rules.  Unused by
                this rule today -- the coupling window and threshold are
                per-pair-explicit, not derived from
                ``min_clearance_mm`` -- but kept in the signature for
                consistency with other ``DRCRule`` subclasses.

        Returns:
            DRCResults with one violation per engaged pair whose
            coupled fraction is below its threshold.  Empty result when
            ``engaged_pairs`` is empty (graceful no-op).
        """
        results = DRCResults()
        # One "rule check" per engaged pair (matches the per-pair scope
        # of the rule).  Empty engaged set means 0 checks; the rule has
        # nothing to validate.
        results.rules_checked = len(self._engaged)

        if not self._engaged:
            return results

        # Bucket P-side and N-side segments by net.  We don't know which
        # half of a pair is "P" vs "N" without naming conventions, so we
        # treat each pair symmetrically: compute the coupled fraction of
        # each half's routed length, take the AVERAGE.  This avoids a
        # bias toward one half being much longer than the other.
        segments_by_net: dict[int, list[CopperElement]] = {}
        for layer in pcb.copper_layers:
            for seg in pcb.segments_on_layer(layer.name):
                if seg.net_number == 0:
                    continue
                elem = CopperElement.from_segment(seg)
                segments_by_net.setdefault(seg.net_number, []).append(elem)

        # Resolve net names for violation messages.
        net_names = {n.number: n.name for n in pcb.nets.values()}

        for net_a, net_b in self._engaged:
            a_segs = segments_by_net.get(net_a, [])
            b_segs = segments_by_net.get(net_b, [])
            # If either half has no routed segments, the rule has
            # nothing to measure -- skip to avoid division-by-zero, no
            # violation.
            a_total = sum(_segment_length(s) for s in a_segs)
            b_total = sum(_segment_length(s) for s in b_segs)
            if a_total <= 0.0 or b_total <= 0.0:
                continue

            # Compute coupled fraction of each half.
            a_coupled = self._coupled_length(a_segs, b_segs)
            b_coupled = self._coupled_length(b_segs, a_segs)
            frac_a = min(a_coupled / a_total, 1.0)
            frac_b = min(b_coupled / b_total, 1.0)
            coupled_fraction = (frac_a + frac_b) / 2.0

            threshold = self._threshold_map.get(
                (net_a, net_b), self._default_threshold
            )
            if coupled_fraction + 1e-9 < threshold:
                results.add(
                    self._make_violation(
                        net_a=net_a,
                        net_b=net_b,
                        name_a=net_names.get(net_a, ""),
                        name_b=net_names.get(net_b, ""),
                        coupled_fraction=coupled_fraction,
                        threshold=threshold,
                    )
                )

        return results

    def _coupled_length(
        self,
        own_segs: list[CopperElement],
        partner_segs: list[CopperElement],
    ) -> float:
        """Sum of own-segment lengths counted as coupled to *any* partner.

        For each segment in ``own_segs``, we take the maximum coupled
        length across all partner segments -- once a segment is found
        to be coupled, we attribute its full length once (not
        double-counted across multiple partners).
        """
        total = 0.0
        for own in own_segs:
            best = 0.0
            for partner in partner_segs:
                overlap = _segment_coupled_overlap(
                    own,
                    partner,
                    self._coupling_window_mm,
                    self._parallel_tolerance_deg,
                )
                if overlap > best:
                    best = overlap
                    if best >= _segment_length(own):
                        # Already maximally coupled; no need to keep scanning.
                        break
            total += best
        return total

    def _make_violation(
        self,
        net_a: int,
        net_b: int,
        name_a: str,
        name_b: str,
        coupled_fraction: float,
        threshold: float,
    ) -> DRCViolation:
        """Build a DRCViolation for an under-coupled engaged pair."""
        # Stable lexicographic naming so reports don't flap.
        first, second = sorted([name_a or f"net-{net_a}", name_b or f"net-{net_b}"])
        return DRCViolation(
            rule_id=self.rule_id,
            severity="error",
            message=(
                f"Engaged differential pair {first}/{second} routing "
                f"continuity {coupled_fraction:.1%} below threshold "
                f"{threshold:.1%}"
            ),
            location=None,
            layer=None,
            actual_value=round(coupled_fraction, 4),
            required_value=round(threshold, 4),
            items=(first, second),
            nets=(name_a, name_b),
        )

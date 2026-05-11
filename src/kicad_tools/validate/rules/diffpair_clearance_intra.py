"""Differential-pair within-pair clearance DRC rule.

Validates that segments belonging to a detected differential pair maintain
at least the per-class :attr:`~kicad_tools.router.rules.NetClassRouting.intra_pair_clearance`
edge-to-edge spacing.

Distinct from the generic :class:`~kicad_tools.validate.rules.clearance.ClearanceRule`,
which validates inter-pair (different-net) clearance against the manufacturer's
``min_clearance_mm``.  Within-pair edges are *allowed* to be tighter than the
inter-pair clearance (that's the whole point of the diff-pair geometry --
controlled coupling needs the two traces close), but they must still respect
the per-class intra threshold.

This rule is part of Epic #2556 Phase 1D (Issue #2560):

- Phase 1A (#2557) added :attr:`NetClassRouting.intra_pair_clearance`.
- Phase 1B (#2558) added layered diff-pair detection.
- Phase 1C (#2559, in flight) threads the value into the router.
- Phase 1D (this rule) validates the produced layout against the threshold.

Scope:

- Segment-to-segment only.  Pads and vias are out of scope -- their spacing
  is enforced by the generic ``clearance`` rule using the inter-pair
  ``min_clearance_mm`` threshold (a tighter intra-pair threshold for V-V or
  P-P would be a reasonable extension but is not part of this issue).
- Same-pair edges only (the two nets must be detected as the P/N halves of
  the same differential pair).  Cross-net edges that are NOT part of the
  same diff pair are validated by the generic clearance rule.

Diff-pair detection:

The rule reuses :func:`kicad_tools.router.diffpair.detect_differential_pairs`
to identify P/N pairs from net names, mirroring the suffix-inference behavior
that the router uses.  Explicit declarations and KiCad-group sources from
#2558 are honored when the caller pre-builds a ``diff_pair_map`` and passes
it to the constructor; otherwise the rule falls back to suffix detection
(matching the legacy router behavior, sufficient for the common USB/HDMI/
Ethernet cases).

The ``intra_pair_clearance`` value used for comparison defaults to a
caller-supplied per-pair map (``intra_pair_clearance_map``).  When omitted
or when a particular pair is not in the map, the rule falls back to the
manufacturer's ``min_clearance_mm`` -- which makes the rule equivalent to
the generic clearance rule (no extra constraint).  The intended call site
(the autorouter consumer in #2559) computes the per-pair map from
:meth:`NetClassRouting.effective_intra_pair_clearance` and passes it in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE, DRCRule
from .clearance import CopperElement, _segment_segment_clearance

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


class DiffPairClearanceIntraRule(DRCRule):
    """Validate within-pair separation of differential-pair segments.

    Iterates segment pairs on each copper layer; emits a violation when
    two segments are detected as belonging to the same differential pair
    AND their edge-to-edge separation is less than the pair's
    ``intra_pair_clearance`` threshold (after :data:`DRC_TOLERANCE`).

    The rule does NOT fire when:
      - The two segments are on different (unrelated) nets.
      - The two segments are on the same net (continuity is allowed to
        touch).
      - The two segments belong to a pair, but their separation is within
        the threshold (the structural correctness case -- tighter coupling
        is allowed).
      - The two nets match a single-ended refusal pattern such as
        ``USB_CC1``/``USB_CC2`` (per #2558 -- these are not diff pairs).

    Attributes:
        rule_id: ``"diffpair_clearance_intra"`` (must be the EXACT
            string the alias table in ``drc/violation.py`` keys on).
        intra_pair_clearance_map: Optional ``{(net_a, net_b) -> threshold_mm}``
            map.  Keys are stored as ``(min, max)`` net-number tuples so the
            order of the pair members doesn't matter.  When a detected pair
            is not in the map, the rule falls back to ``design_rules.min_clearance_mm``,
            which makes the rule equivalent to the generic clearance rule
            for that pair.
        diff_pair_overrides: Optional list of ``(net_a_name, net_b_name)`` pairs
            that should be treated as diff pairs even if suffix inference
            doesn't recognize them (mirrors the explicit-declaration source
            from #2558).  Net names are looked up against ``pcb.nets`` to
            resolve to net numbers at check time.
    """

    rule_id = "diffpair_clearance_intra"
    name = "Differential-Pair Within-Pair Clearance"
    description = (
        "Validates that within-pair separation of detected differential pairs "
        "respects the per-class intra_pair_clearance threshold."
    )

    def __init__(
        self,
        intra_pair_clearance_map: dict[tuple[int, int], float] | None = None,
        diff_pair_overrides: list[tuple[str, str]] | None = None,
    ) -> None:
        """Initialize the rule.

        Args:
            intra_pair_clearance_map: Optional ``{(net_a, net_b) -> threshold_mm}``
                map of explicit per-pair thresholds.  Keys are normalized to
                ``(min, max)`` net-number ordering.  Missing pairs fall back
                to ``design_rules.min_clearance_mm`` (equivalent to no extra
                constraint -- the generic clearance rule handles them).
            diff_pair_overrides: Optional ``[(p_name, n_name), ...]`` list to
                augment suffix inference (see #2558).  Useful when a board
                explicitly declares pairs via ``diffpair_partner`` or KiCad's
                ``diff_pair_template``.
        """
        # Normalize keys to (min, max) ordering so callers don't have to.
        self._intra_map: dict[tuple[int, int], float] = {}
        if intra_pair_clearance_map:
            for (a, b), thr in intra_pair_clearance_map.items():
                key = (a, b) if a <= b else (b, a)
                self._intra_map[key] = thr

        self._overrides: list[tuple[str, str]] = list(diff_pair_overrides or [])

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check intra-pair clearance on all copper layers.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules (used as fallback threshold when a
                pair is not in ``intra_pair_clearance_map``).

        Returns:
            DRCResults containing intra-pair clearance violations.
        """
        results = DRCResults()

        # Build the diff-pair set: { (min_net_id, max_net_id) }
        diff_pair_set = self._build_diff_pair_set(pcb)

        # Process each copper layer
        for layer in pcb.copper_layers:
            layer_name = layer.name
            violations = self._check_layer(
                pcb,
                layer_name,
                diff_pair_set,
                design_rules.min_clearance_mm,
            )
            for v in violations:
                results.add(v)

        # One rule check per layer (matches ClearanceRule convention).
        # Phase 4N (#2660): the *per-rule* counter records how many pairs
        # were actually inspected -- this is the CI signal "rule actually
        # ran on this board".  Bumping this only when ``diff_pair_set``
        # is non-empty matches the "rule was exercised" semantic: a board
        # with no detected pairs does NOT exercise the rule, even if the
        # layer-walk loop ran.
        results.rules_checked = len(pcb.copper_layers)
        if diff_pair_set:
            results.rules_checked_by_rule["diffpair_clearance_intra"] = len(diff_pair_set)

        return results

    def _build_diff_pair_set(self, pcb: PCB) -> set[tuple[int, int]]:
        """Build the set of ``(min_net_id, max_net_id)`` diff-pair tuples.

        Combines:
          - ``diff_pair_overrides`` from the constructor (explicit pairs,
            net names resolved via the PCB net table).
          - Suffix inference via ``router/diffpair.detect_differential_pairs``.

        Net IDs of zero (unconnected) are filtered out.
        """
        from kicad_tools.router.diffpair import detect_differential_pairs

        pairs: set[tuple[int, int]] = set()

        # Explicit overrides: resolve names -> ids via pcb.nets.
        if self._overrides:
            name_to_id = {net.name: net.number for net in pcb.nets.values()}
            for p_name, n_name in self._overrides:
                p_id = name_to_id.get(p_name)
                n_id = name_to_id.get(n_name)
                if p_id is None or n_id is None:
                    continue
                if p_id == 0 or n_id == 0:
                    continue
                key = (p_id, n_id) if p_id <= n_id else (n_id, p_id)
                pairs.add(key)

        # Suffix inference from net names.
        net_names = {net.number: net.name for net in pcb.nets.values()}
        for diff_pair in detect_differential_pairs(net_names):
            p_id = diff_pair.positive.net_id
            n_id = diff_pair.negative.net_id
            if p_id == 0 or n_id == 0:
                continue
            key = (p_id, n_id) if p_id <= n_id else (n_id, p_id)
            pairs.add(key)

        return pairs

    def _threshold_for(
        self,
        net_a: int,
        net_b: int,
        fallback: float,
    ) -> float:
        """Look up the intra_pair_clearance threshold for a pair, with fallback."""
        key = (net_a, net_b) if net_a <= net_b else (net_b, net_a)
        return self._intra_map.get(key, fallback)

    def _check_layer(
        self,
        pcb: PCB,
        layer_name: str,
        diff_pair_set: set[tuple[int, int]],
        fallback_clearance: float,
    ) -> list[DRCViolation]:
        """Check intra-pair clearance on a single copper layer.

        Iterates segment-to-segment pairs (pads/vias intentionally out of
        scope -- inter-pair clearance for circular elements is enforced by
        the generic clearance rule).  A violation fires when both segments
        are on nets that form a detected diff pair AND the edge-to-edge
        separation is below the pair's intra threshold.
        """
        violations: list[DRCViolation] = []

        # Segments only (segment-to-segment scope per Issue #2560).
        segments: list[CopperElement] = [
            CopperElement.from_segment(seg) for seg in pcb.segments_on_layer(layer_name)
        ]

        for i, elem1 in enumerate(segments):
            for elem2 in segments[i + 1 :]:
                # Skip same-net (continuity) and unconnected.
                if elem1.net_number == elem2.net_number:
                    continue
                if elem1.net_number == 0 or elem2.net_number == 0:
                    continue

                # Only fire on segments belonging to the same detected pair.
                key = (
                    (elem1.net_number, elem2.net_number)
                    if elem1.net_number <= elem2.net_number
                    else (elem2.net_number, elem1.net_number)
                )
                if key not in diff_pair_set:
                    continue

                threshold = self._threshold_for(
                    elem1.net_number, elem2.net_number, fallback_clearance
                )

                # Compute edge-to-edge clearance (reuses the same math the
                # generic ClearanceRule uses; do NOT duplicate).
                clearance, loc_x, loc_y = _segment_segment_clearance(elem1, elem2)

                if clearance + DRC_TOLERANCE < threshold:
                    violations.append(
                        self._create_violation(
                            elem1, elem2, clearance, threshold, layer_name, loc_x, loc_y
                        )
                    )

        return violations

    def _create_violation(
        self,
        elem1: CopperElement,
        elem2: CopperElement,
        actual: float,
        required: float,
        layer: str,
        loc_x: float,
        loc_y: float,
    ) -> DRCViolation:
        """Create a DRC violation for an intra-pair clearance issue."""
        # Stable net-name ordering in the message so test assertions and
        # diff'd reports don't flap when the iteration order changes.
        net_a, net_b = sorted([elem1.net_name, elem2.net_name])
        return DRCViolation(
            rule_id=self.rule_id,
            severity="error",
            message=(
                f"Within-pair clearance for diff pair {net_a}/{net_b}: "
                f"{actual:.3f}mm < intra_pair_clearance {required:.3f}mm"
            ),
            location=(round(loc_x, 3), round(loc_y, 3)),
            layer=layer,
            actual_value=round(actual, 4),
            required_value=required,
            items=(elem1.reference, elem2.reference),
            nets=(elem1.net_name, elem2.net_name),
        )

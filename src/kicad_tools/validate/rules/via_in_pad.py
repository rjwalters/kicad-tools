"""Via-in-pad DRC rule.

Detects vias whose drill circle overlaps an SMD pad on the same
net, which is only legal when the chosen manufacturer profile supports
via-in-pad processing (epoxy-filled and plated-over vias) AND the board
declares a specific, eligible :class:`~kicad_tools.manufacturers.fabrication_process.FabricationProcess`
whose geometric envelope (drill range, layer count, annular ring,
component-hole distance) the actual via satisfies (issue #5009).

The router consults the base capability flag via the existing
``MfrLimits.via_in_pad_supported`` field (see
``src/kicad_tools/router/mfr_limits.py``).  When the user asks for a
profile that does NOT support via-in-pad (default for ``jlcpcb``,
``oshpark``, ``seeed``, ``flashpcb``), the escape router refuses to
place an in-pad via -- but DRC must independently verify the same
constraint, because a hand-edited or third-party-routed board could
introduce in-pad vias that DRC would otherwise silently accept.

Three distinct outcomes for a via drilled inside an SMD pad on its net:

1. ``design_rules.via_in_pad_supported`` is ``False`` -- the manufacturer
   does not offer via-in-pad at all.  Reported as rule_id ``via_in_pad``
   (the original #2635 behavior).
2. ``via_in_pad_supported`` is ``True`` but ``via_in_pad_process_id``
   names no known :class:`FabricationProcess` (unset or unrecognized) --
   the manufacturer's *general* tier claims via-in-pad support, but no
   real, orderable process is declared for this specific layer/copper
   configuration.  A bare capability flag with no attached process must
   not suppress the finding.  Reported as rule_id
   ``via_in_pad_process_missing``, distinct from (1) so tooling can tell
   "capability entirely absent" apart from "capability present but
   undeclared process".
3. A process IS declared, but the via's actual geometry (drill diameter,
   annular ring), the board's copper-layer count, or its distance to the
   nearest other component's drilled hole fails to meet that process's
   published requirements.  Reported as rule_id
   ``via_in_pad_process_ineligible`` with the specific reason(s).

Only when a declared process's requirements are fully met does the rule
stay silent for that via.

Geometry of "via inside pad":

* SMD pads use the shared clearance geometry, honoring rounded pad outlines
  and absolute copper angles. Legacy callers without pad geometry use a
  rectangular bounding box.
* Any drill overlap with SMT copper requires the process, including partial
  overlap where the drill is not fully contained by the land.

Out of scope (explicitly): blind/buried vias, microvias, and
controlled-impedance differential pairs.  These require additional
DesignRules fields (see issue #2635 acceptance criteria and the
follow-up items the curator listed).

NOTE: KiCad's ``.kicad_dru`` format has no native via-in-pad rule, so
``dru_generator.py`` is intentionally not extended for this rule -- the
check lives entirely in pure-Python ``DRCChecker.check_via_in_pad``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kicad_tools.manufacturers.fabrication_process import (
    FabricationProcess,
    get_fabrication_process,
)
from kicad_tools.router.via_in_pad_eligibility import (
    ComponentHoleContext,
    component_holes_from_document,
    resolve_component_hole_context,
)

from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE, DRCRule

# The pad-bbox / overlap geometry now lives in a shared module so the
# ``fix-vias --relocate-in-pad`` command reuses exactly the same detector as
# this DRC rule (single source of truth -- issue #4359).  The private aliases
# are retained for backward compatibility with existing references to
# ``via_in_pad._pad_absolute_bbox`` / ``_via_inside_pad`` (e.g. the router's
# ``drc_nudge`` and ``stitch_cmd`` docstrings).
from .via_pad_geometry import (
    is_smd_pad as _is_smd_pad,
)
from .via_pad_geometry import (
    pad_absolute_bbox as _pad_absolute_bbox,
)
from .via_pad_geometry import (
    via_inside_pad as _via_inside_pad,
)

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad, Via


class ViaInPadRule(DRCRule):
    """Check that vias in SMD pads have a real, eligible fabrication process.

    Fires whenever a via is drilled inside an SMD pad on its own net AND
    one of the following holds (issue #5009):

    * ``design_rules.via_in_pad_supported`` is ``False`` -- rule_id
      ``via_in_pad``.
    * ``via_in_pad_supported`` is ``True`` but
      ``design_rules.via_in_pad_process_id`` names no known
      :class:`~kicad_tools.manufacturers.fabrication_process.FabricationProcess`
      -- rule_id ``via_in_pad_process_missing``.
    * A process IS declared, but the via's drill/annular-ring geometry,
      the board's copper-layer count, or its distance to the nearest
      other component's drilled hole fails that process's published
      requirements -- rule_id ``via_in_pad_process_ineligible``.

    For every via on the board, the rule scans SMD pads on the same net
    and flags the via if the drill circle overlaps a pad's copper
    outline beyond the geometry tolerance.

    The same-net constraint prevents false positives where a via is
    placed near (but not connected to) a pad on a different net -- those
    are caught by the regular clearance rule instead.
    """

    rule_id = "via_in_pad"
    name = "Via in Pad"
    description = (
        "Detects vias drilled inside SMD pads that lack an eligible "
        "filled-and-capped via-in-pad fabrication process"
    )

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check all vias against SMD pads on the same net.

        Args:
            pcb: The PCB to check.
            design_rules: The active manufacturer's design rules.

        Returns:
            DRCResults containing one violation per (via, pad) pair that
            violates the rule -- see the class docstring for the three
            distinct rule_ids this can emit.
        """
        results = DRCResults()
        results.rules_checked = 1

        supported = bool(getattr(design_rules, "via_in_pad_supported", False))
        process_id = getattr(design_rules, "via_in_pad_process_id", None)
        # Only resolve a process when the tier claims support at all --
        # an unsupported profile always uses the base "via_in_pad"
        # violation, even if a stray process_id were somehow present.
        process = get_fabrication_process(process_id) if supported else None

        # Collect SMD pads grouped by net number for O(N+M) scanning
        # rather than O(N*M) full cross product.  Net 0 is unconnected
        # and is intentionally excluded -- vias near unconnected pads
        # are caught by the clearance rule.
        pads_by_net: dict[int, list[tuple[Footprint, Pad, tuple[float, float, float, float]]]] = {}
        # Share the router's complete physical census: anonymous NPTH holes
        # count too, and malformed drilled geometry must fail closed.
        component_holes = component_holes_from_document(pcb)
        for fp in pcb.footprints:
            for pad in fp.pads:
                if not _is_smd_pad(pad):
                    continue
                if pad.net_number == 0:
                    continue
                bbox = _pad_absolute_bbox(pad, fp)
                pads_by_net.setdefault(pad.net_number, []).append((fp, pad, bbox))

        layer_count = len(pcb.copper_layers) if hasattr(pcb, "copper_layers") else None

        # For each via, check pads on the same net.
        for via in pcb.vias:
            if via.net_number == 0:
                continue
            candidates = pads_by_net.get(via.net_number)
            if not candidates:
                continue
            for fp, pad, bbox in candidates:
                if not _via_inside_pad(via, bbox, pad, fp):
                    continue
                if not supported:
                    results.add(self._make_violation(via, fp, pad))
                    continue
                if process is None:
                    results.add(self._make_process_missing_violation(via, fp, pad, process_id))
                    continue
                reasons = self._check_process_eligibility(
                    via,
                    process,
                    layer_count=layer_count,
                    hole_context=resolve_component_hole_context(
                        *via.position, via.drill, all_pads=component_holes
                    ),
                )
                if reasons:
                    results.add(
                        self._make_process_ineligible_violation(via, fp, pad, process, reasons)
                    )

        return results

    def _check_process_eligibility(
        self,
        via: Via,
        process: FabricationProcess,
        *,
        layer_count: int | None,
        hole_context: ComponentHoleContext,
    ) -> list[str]:
        """Return a list of human-readable reasons ``via`` fails ``process``.

        An empty list means the via satisfies every checkable requirement
        of the declared process. Delegates to
        :meth:`~kicad_tools.manufacturers.fabrication_process.FabricationProcess.eligibility_reasons`
        -- the single source of truth for this predicate, also consumed by
        the router's escape/repair decisions
        (:mod:`kicad_tools.router.via_in_pad_eligibility`, Issue #5201) so
        the two never disagree about which vias are legal.
        """
        drill = getattr(via, "drill", 0.0)
        size = getattr(via, "size", None)
        annular_ring_mm = (size - drill) / 2.0 if size is not None else None

        if not hole_context.known:
            return ["component-hole census has unknown or invalid drilled geometry"]

        return process.eligibility_reasons(
            layer_count=layer_count,
            drill_mm=drill,
            annular_ring_mm=annular_ring_mm,
            nearest_other_hole_distance_mm=hole_context.nearest_distance_mm,
            tolerance_mm=DRC_TOLERANCE,
        )

    def _make_violation(
        self,
        via: Via,
        fp: Footprint,
        pad: Pad,
    ) -> DRCViolation:
        """Build a DRCViolation for a single (via, pad) pair."""
        ref_label = f"{fp.reference}-{pad.number}"
        via_ref = f"Via-{via.uuid[:8]}" if via.uuid else "Via"
        net_name = via.net_name or pad.net_name or ""
        return DRCViolation(
            rule_id="via_in_pad",
            severity="error",
            message=(
                f"Via at ({via.position[0]:.3f}, {via.position[1]:.3f}) "
                f"drilled inside pad {ref_label} (net '{net_name}'); "
                f"current manufacturer profile does not support via-in-pad. "
                f"Switch to jlcpcb-tier1 or pcbway, or move the via off the pad."
            ),
            location=(round(via.position[0], 3), round(via.position[1], 3)),
            actual_value=round(via.drill, 4),
            required_value=None,
            items=(via_ref, ref_label),
            nets=(net_name,),
        )

    def _make_process_missing_violation(
        self,
        via: Via,
        fp: Footprint,
        pad: Pad,
        process_id: str | None,
    ) -> DRCViolation:
        """Build a violation for "supported but no eligible process declared"."""
        ref_label = f"{fp.reference}-{pad.number}"
        via_ref = f"Via-{via.uuid[:8]}" if via.uuid else "Via"
        net_name = via.net_name or pad.net_name or ""
        detail = (
            f"unrecognized via_in_pad_process_id {process_id!r}"
            if process_id
            else "via_in_pad_process_id is unset"
        )
        return DRCViolation(
            rule_id="via_in_pad_process_missing",
            severity="error",
            message=(
                f"Via at ({via.position[0]:.3f}, {via.position[1]:.3f}) drilled "
                f"inside pad {ref_label} (net '{net_name}') requires a "
                f"filled-and-capped via-in-pad process, but {detail}. A bare "
                f"via_in_pad_supported=True capability flag is not sufficient -- "
                f"the manufacturer's general tier supports via-in-pad somewhere "
                f"in its catalog, but no eligible FabricationProcess is declared "
                f"for this layer/copper configuration (see "
                f"kicad_tools.manufacturers.fabrication_process)."
            ),
            location=(round(via.position[0], 3), round(via.position[1], 3)),
            actual_value=round(via.drill, 4),
            required_value=None,
            items=(via_ref, ref_label),
            nets=(net_name,),
        )

    def _make_process_ineligible_violation(
        self,
        via: Via,
        fp: Footprint,
        pad: Pad,
        process: FabricationProcess,
        reasons: list[str],
    ) -> DRCViolation:
        """Build a violation for "process declared but not satisfied"."""
        ref_label = f"{fp.reference}-{pad.number}"
        via_ref = f"Via-{via.uuid[:8]}" if via.uuid else "Via"
        net_name = via.net_name or pad.net_name or ""
        reason_text = "; ".join(reasons)
        return DRCViolation(
            rule_id="via_in_pad_process_ineligible",
            severity="error",
            message=(
                f"Via at ({via.position[0]:.3f}, {via.position[1]:.3f}) drilled "
                f"inside pad {ref_label} (net '{net_name}') does not meet the "
                f"declared process {process.process_id!r} ({process.name}) "
                f"eligibility requirements: {reason_text}. Source: {process.source}"
            ),
            location=(round(via.position[0], 3), round(via.position[1], 3)),
            actual_value=round(via.drill, 4),
            required_value=process.min_via_drill_mm,
            items=(via_ref, ref_label),
            nets=(net_name,),
        )

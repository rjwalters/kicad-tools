"""Via-in-pad DRC rule.

Detects vias whose drill circle is fully covered by an SMD pad on the same
net, which is only legal when the chosen manufacturer profile supports
via-in-pad processing (epoxy-filled and plated-over vias).

The router consults the same flag via the existing
``MfrLimits.via_in_pad_supported`` field (see
``src/kicad_tools/router/mfr_limits.py``).  When the user asks for a
profile that does NOT support via-in-pad (default for ``jlcpcb``,
``oshpark``, ``seeed``, ``flashpcb``), the escape router refuses to
place an in-pad via -- but DRC must independently verify the same
constraint, because a hand-edited or third-party-routed board could
introduce in-pad vias that DRC would otherwise silently accept.

Geometry of "via inside pad":

* SMD pads are modelled as axis-aligned rectangles (post footprint
  rotation transformation) -- mirrors the representation used by the
  clearance rule.  For rotated footprints we use the axis-aligned
  bounding box; pads with non-cardinal rotations are conservatively
  reported when the BB contains the via even if the actual pad polygon
  does not.
* A via is "in the pad" when its drill circle is fully contained inside
  the pad rectangle, i.e., every point on the drill circle lies on or
  inside the pad edge.

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

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

# The pad-bbox / containment geometry now lives in a shared module so the
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
    """Check that vias are not placed inside SMD pads on unsupported profiles.

    Fires only when ``design_rules.via_in_pad_supported == False`` (the
    default for ``jlcpcb``, ``oshpark``, ``seeed``, ``flashpcb``).

    For every via on the board, the rule scans SMD pads on the same net
    and flags the via as an error if any pad's bounding box fully
    contains the drill circle.

    The same-net constraint prevents false positives where a via is
    placed near (but not connected to) a pad on a different net -- those
    are caught by the regular clearance rule instead.
    """

    rule_id = "via_in_pad"
    name = "Via in Pad"
    description = (
        "Detects vias drilled inside SMD pads on manufacturer profiles that "
        "do not support filled and plated-over via-in-pad processing"
    )

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check all vias against SMD pads on the same net.

        Args:
            pcb: The PCB to check.
            design_rules: The active manufacturer's design rules.  This
                rule short-circuits to no-op when
                ``design_rules.via_in_pad_supported`` is ``True``.

        Returns:
            DRCResults containing one ``via_in_pad`` violation per
            (via, pad) pair that violates the rule.
        """
        results = DRCResults()
        results.rules_checked = 1

        # Capability gate: when the manufacturer supports via-in-pad,
        # the rule is suppressed.  The acceptance criterion in #2635
        # requires this to be a no-op on jlcpcb-tier1 and pcbway.
        if getattr(design_rules, "via_in_pad_supported", False):
            return results

        # Collect SMD pads grouped by net number for O(N+M) scanning
        # rather than O(N*M) full cross product.  Net 0 is unconnected
        # and is intentionally excluded -- vias near unconnected pads
        # are caught by the clearance rule.
        pads_by_net: dict[int, list[tuple[Footprint, Pad, tuple[float, float, float, float]]]] = {}
        for fp in pcb.footprints:
            for pad in fp.pads:
                if not _is_smd_pad(pad):
                    continue
                if pad.net_number == 0:
                    continue
                bbox = _pad_absolute_bbox(pad, fp)
                pads_by_net.setdefault(pad.net_number, []).append((fp, pad, bbox))

        # For each via, check pads on the same net.
        for via in pcb.vias:
            if via.net_number == 0:
                continue
            candidates = pads_by_net.get(via.net_number)
            if not candidates:
                continue
            for fp, pad, bbox in candidates:
                if not _via_inside_pad(via, bbox):
                    continue
                results.add(self._make_violation(via, fp, pad))

        return results

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

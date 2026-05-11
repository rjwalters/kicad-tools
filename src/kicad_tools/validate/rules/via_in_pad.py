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

import math
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE, DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad, Via


def _pad_absolute_bbox(
    pad: Pad,
    footprint: Footprint,
) -> tuple[float, float, float, float]:
    """Return the axis-aligned bounding box for ``pad`` in board coords.

    Mirrors the transformation used by the clearance rule: rotates the
    pad center about the footprint origin and, for cardinal rotations,
    swaps width/height (otherwise uses the rotated rectangle's AABB).

    Returns:
        (min_x, min_y, max_x, max_y) tuple in mm.
    """
    angle_rad = math.radians(footprint.rotation)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    local_x, local_y = pad.position
    rotated_x = local_x * cos_a - local_y * sin_a
    rotated_y = local_x * sin_a + local_y * cos_a
    abs_x = footprint.position[0] + rotated_x
    abs_y = footprint.position[1] + rotated_y

    width, height = pad.size
    total_rotation = footprint.rotation % 360

    # For cardinal rotations, swap dimensions.
    if abs(total_rotation - 90) < 0.001 or abs(total_rotation - 270) < 0.001:
        bbox_w, bbox_h = height, width
    elif abs(total_rotation) < 0.001 or abs(total_rotation - 180) < 0.001:
        bbox_w, bbox_h = width, height
    else:
        # Axis-aligned bounding box of the rotated rectangle.
        abs_cos = abs(cos_a)
        abs_sin = abs(sin_a)
        bbox_w = width * abs_cos + height * abs_sin
        bbox_h = width * abs_sin + height * abs_cos

    half_w = bbox_w / 2
    half_h = bbox_h / 2
    return (abs_x - half_w, abs_y - half_h, abs_x + half_w, abs_y + half_h)


def _is_smd_pad(pad: Pad) -> bool:
    """Return True if ``pad`` is a surface-mount pad (no plated hole)."""
    # KiCad pad types: "smd", "thru_hole", "np_thru_hole", "connect"
    return pad.type == "smd"


def _via_inside_pad(via: Via, pad_bbox: tuple[float, float, float, float]) -> bool:
    """Return True if the via's drill circle is fully inside ``pad_bbox``.

    Args:
        via: The via to test.  ``via.position`` is the center, ``via.drill``
            is the drill diameter.
        pad_bbox: Axis-aligned (min_x, min_y, max_x, max_y) of the pad.

    Returns:
        ``True`` when every point on the drill circle is on or inside
        the pad bounding box.  A small DRC tolerance is applied so that
        edge-touching vias (within manufacturing rounding) are not
        flagged.
    """
    cx, cy = via.position
    radius = via.drill / 2.0
    min_x, min_y, max_x, max_y = pad_bbox

    # Every point on the drill circle lies in [cx - r, cx + r] x [cy - r, cy + r].
    # The drill is fully inside the pad iff the circle's bounding box is.
    # We require strict containment minus DRC_TOLERANCE so a via whose
    # drill edge merely touches the pad edge is allowed (it would be a
    # neckdown rather than an in-pad via).
    return (
        cx - radius >= min_x - DRC_TOLERANCE
        and cx + radius <= max_x + DRC_TOLERANCE
        and cy - radius >= min_y - DRC_TOLERANCE
        and cy + radius <= max_y + DRC_TOLERANCE
    )


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

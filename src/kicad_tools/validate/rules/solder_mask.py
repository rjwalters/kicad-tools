"""Solder mask and pad dimension DRC rules.

This module implements validation rules for:
- Solder mask expansion/clearance (per-pad and board-level)
- Minimum pad size for manufacturability
- PTH pad annular ring (pad copper ring around drill hole)
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


class SolderMaskPadRules(DRCRule):
    """Check solder mask and pad dimension rules.

    Validates:
    - Solder mask expansion meets manufacturer minimum clearance
    - SMD pad dimensions meet minimum pad size
    - PTH pad annular ring (pad size - drill) / 2
    """

    rule_id = "solder_mask_pad"
    name = "Solder Mask & Pad Rules"
    description = (
        "Check solder mask clearance, minimum pad size, "
        "and PTH pad annular ring"
    )

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check all solder mask and pad rules.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile

        Returns:
            DRCResults containing violations found
        """
        results = DRCResults()

        self._check_solder_mask_clearance(pcb, design_rules, results)
        self._check_min_pad_size(pcb, design_rules, results)
        self._check_pth_annular_ring(pcb, design_rules, results)

        # 3 rule categories checked
        results.rules_checked = 3

        return results

    def _check_solder_mask_clearance(
        self,
        pcb: PCB,
        design_rules: DesignRules,
        results: DRCResults,
    ) -> None:
        """Check that solder mask expansion meets manufacturer minimum.

        The solder mask clearance (expansion) is the gap between the pad
        copper and the edge of the solder mask opening. If this is too
        small, the mask may not register properly and could cover part
        of the pad.

        Uses per-pad solder_mask_margin when available, otherwise falls
        back to the board-level pad_to_mask_clearance from PCB setup.
        """
        min_clearance = design_rules.min_solder_mask_clearance_mm

        # Board-level default mask clearance from PCB setup
        board_mask_clearance = getattr(
            getattr(pcb, "setup", None), "pad_to_mask_clearance", 0.0
        )

        for fp in pcb.footprints:
            for pad in fp.pads:
                # Only check pads that have mask layers
                has_mask = any(
                    layer.endswith(".Mask") for layer in pad.layers
                )
                if not has_mask:
                    continue

                # Determine effective mask clearance for this pad
                if pad.solder_mask_margin is not None:
                    effective_clearance = pad.solder_mask_margin
                else:
                    effective_clearance = board_mask_clearance

                # A clearance of 0 means the mask opening exactly matches
                # the pad -- this is common and acceptable if the manufacturer
                # allows it.  Only flag when the effective clearance is
                # explicitly set to a value below the manufacturer minimum
                # AND is non-zero (a zero value is KiCad's default meaning
                # "use global/manufacturer default" and should not be flagged).
                if effective_clearance != 0.0 and effective_clearance < min_clearance:
                    abs_x = fp.position[0] + pad.position[0]
                    abs_y = fp.position[1] + pad.position[1]

                    results.add(
                        DRCViolation(
                            rule_id="solder_mask_clearance",
                            severity="warning",
                            message=(
                                f"Solder mask clearance {effective_clearance:.3f}mm "
                                f"< minimum {min_clearance:.3f}mm"
                            ),
                            location=(abs_x, abs_y),
                            layer=None,
                            actual_value=effective_clearance,
                            required_value=min_clearance,
                            items=(f"{fp.reference}-{pad.number}",),
                        )
                    )

    def _check_min_pad_size(
        self,
        pcb: PCB,
        design_rules: DesignRules,
        results: DRCResults,
    ) -> None:
        """Check that pad dimensions meet manufacturer minimum.

        Both width and height of the pad must meet the minimum pad size.
        This applies to SMD pads primarily, as PTH pads are typically
        larger.
        """
        min_size = design_rules.min_pad_size_mm

        for fp in pcb.footprints:
            for pad in fp.pads:
                # Skip non-plated through-holes (mounting holes)
                if pad.type == "np_thru_hole":
                    continue

                pad_w, pad_h = pad.size
                min_dim = min(pad_w, pad_h)

                if min_dim > 0 and min_dim < min_size:
                    abs_x = fp.position[0] + pad.position[0]
                    abs_y = fp.position[1] + pad.position[1]

                    results.add(
                        DRCViolation(
                            rule_id="min_pad_size",
                            severity="error",
                            message=(
                                f"Pad size {pad_w:.3f}x{pad_h:.3f}mm: "
                                f"smallest dimension {min_dim:.3f}mm "
                                f"< minimum {min_size:.3f}mm"
                            ),
                            location=(abs_x, abs_y),
                            layer=fp.layer,
                            actual_value=min_dim,
                            required_value=min_size,
                            items=(f"{fp.reference}-{pad.number}",),
                        )
                    )

    def _check_pth_annular_ring(
        self,
        pcb: PCB,
        design_rules: DesignRules,
        results: DRCResults,
    ) -> None:
        """Check PTH pad annular ring.

        For through-hole pads, the annular ring is the copper ring
        around the drill hole: (min(pad_width, pad_height) - drill) / 2.
        This must meet the manufacturer's minimum annular ring.

        Note: This reuses min_annular_ring_mm from the design rules,
        which is the same constraint used for vias. Manufacturers
        typically apply the same minimum to both.
        """
        min_annular = design_rules.min_annular_ring_mm

        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.type != "thru_hole":
                    continue
                if pad.drill <= 0:
                    continue

                pad_w, pad_h = pad.size
                min_pad_dim = min(pad_w, pad_h)

                # Annular ring = (pad dimension - drill) / 2
                annular_ring = (min_pad_dim - pad.drill) / 2

                if annular_ring < min_annular:
                    abs_x = fp.position[0] + pad.position[0]
                    abs_y = fp.position[1] + pad.position[1]
                    net_name = pad.net_name or f"net:{pad.net_number}"

                    results.add(
                        DRCViolation(
                            rule_id="pth_annular_ring",
                            severity="error",
                            message=(
                                f"PTH annular ring {annular_ring:.3f}mm "
                                f"< minimum {min_annular:.3f}mm "
                                f"(pad {min_pad_dim:.3f}mm, drill {pad.drill:.3f}mm)"
                            ),
                            location=(abs_x, abs_y),
                            layer=None,
                            actual_value=annular_ring,
                            required_value=min_annular,
                            items=(
                                f"{fp.reference}-{pad.number}",
                                net_name,
                            ),
                        )
                    )

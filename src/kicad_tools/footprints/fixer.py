"""Footprint repair module.

Provides tools to fix common footprint issues such as pad spacing violations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from ..schema.pcb import PCB, Footprint  # noqa: F401

from .validator import FootprintValidator


@dataclass
class PadAdjustment:
    """Records an adjustment made to a pad."""

    footprint_ref: str
    pad_number: str
    old_position: Tuple[float, float]
    new_position: Tuple[float, float]
    reason: str


@dataclass
class FootprintFix:
    """Records fixes applied to a footprint."""

    footprint_ref: str
    footprint_name: str
    adjustments: List[PadAdjustment]
    old_pad_spacing: float
    new_pad_spacing: float

    def __str__(self) -> str:
        """Human-readable representation."""
        return (
            f"Fixed {self.footprint_ref} ({self.footprint_name}): "
            f"moved pads from {self.old_pad_spacing:.3f}mm to {self.new_pad_spacing:.3f}mm spacing "
            f"({len(self.adjustments)} pads adjusted)"
        )


class FootprintFixer:
    """Repairs footprint issues by adjusting pad positions.

    Fixes:
    - Pad spacing violations by moving pads outward symmetrically

    Example::

        from kicad_tools.footprints.fixer import FootprintFixer
        from kicad_tools.schema import PCB

        pcb = PCB.load("board.kicad_pcb")
        fixer = FootprintFixer(min_pad_gap=0.2)

        fixes = fixer.fix_pcb(pcb)
        for fix in fixes:
            print(fix)

        # Save the modified PCB
        pcb.save("board_fixed.kicad_pcb")
    """

    def __init__(self, min_pad_gap: float = 0.2):
        """Initialize fixer.

        Args:
            min_pad_gap: Target gap between pads in mm (default: 0.2mm)
        """
        self.min_pad_gap = min_pad_gap
        self.validator = FootprintValidator(min_pad_gap=min_pad_gap)

    def fix_footprint_pads(
        self, footprint: "Footprint", dry_run: bool = False
    ) -> Optional[FootprintFix]:
        """Fix pad spacing issues in a single footprint.

        For 2-pad components (resistors, capacitors, etc.), this moves pads
        symmetrically outward from the center to achieve the minimum gap.

        Args:
            footprint: The footprint to fix
            dry_run: If True, calculate but don't apply changes

        Returns:
            FootprintFix record if changes were made, None otherwise
        """
        issues = self.validator._check_pad_spacing(footprint)

        if not issues:
            return None

        # For now, only handle 2-pad components
        if len(footprint.pads) != 2:
            # More complex footprints would need different strategies
            return None

        # Get the two pads
        pad1, pad2 = footprint.pads[0], footprint.pads[1]

        # Calculate current spacing (center-to-center)
        x1, y1 = pad1.position
        x2, y2 = pad2.position

        # Determine if pads are arranged horizontally or vertically
        dx = x2 - x1
        dy = y2 - y1

        is_horizontal = abs(dx) > abs(dy)

        if is_horizontal:
            # Horizontal arrangement
            # Current gap = center_spacing - (pad1_width/2 + pad2_width/2)
            w1, _ = pad1.size
            w2, _ = pad2.size
            current_spacing = abs(dx)
            current_gap = current_spacing - (w1 / 2 + w2 / 2)

            if current_gap >= self.min_pad_gap:
                return None  # Already OK

            # Calculate required center-to-center spacing
            required_spacing = self.min_pad_gap + (w1 / 2 + w2 / 2)

            # Move pads symmetrically outward
            center_x = (x1 + x2) / 2
            center_y = (y1 + y2) / 2

            if x1 < x2:
                new_x1 = center_x - required_spacing / 2
                new_x2 = center_x + required_spacing / 2
            else:
                new_x1 = center_x + required_spacing / 2
                new_x2 = center_x - required_spacing / 2

            old_spacing = current_spacing
            new_spacing = required_spacing

            new_pos1 = (new_x1, y1)
            new_pos2 = (new_x2, y2)

        else:
            # Vertical arrangement
            _, h1 = pad1.size
            _, h2 = pad2.size
            current_spacing = abs(dy)
            current_gap = current_spacing - (h1 / 2 + h2 / 2)

            if current_gap >= self.min_pad_gap:
                return None  # Already OK

            # Calculate required center-to-center spacing
            required_spacing = self.min_pad_gap + (h1 / 2 + h2 / 2)

            # Move pads symmetrically outward
            center_x = (x1 + x2) / 2
            center_y = (y1 + y2) / 2

            if y1 < y2:
                new_y1 = center_y - required_spacing / 2
                new_y2 = center_y + required_spacing / 2
            else:
                new_y1 = center_y + required_spacing / 2
                new_y2 = center_y - required_spacing / 2

            old_spacing = current_spacing
            new_spacing = required_spacing

            new_pos1 = (x1, new_y1)
            new_pos2 = (x2, new_y2)

        # Record adjustments
        adjustments = [
            PadAdjustment(
                footprint_ref=footprint.reference,
                pad_number=pad1.number,
                old_position=pad1.position,
                new_position=new_pos1,
                reason=f"Increase pad spacing to {self.min_pad_gap}mm",
            ),
            PadAdjustment(
                footprint_ref=footprint.reference,
                pad_number=pad2.number,
                old_position=pad2.position,
                new_position=new_pos2,
                reason=f"Increase pad spacing to {self.min_pad_gap}mm",
            ),
        ]

        # Apply changes if not dry run
        if not dry_run:
            # Note: This modifies the Footprint object but not the underlying
            # S-expression. For full persistence, we'd need to implement
            # PCB.update_footprint_pad_position()
            pad1.position = new_pos1
            pad2.position = new_pos2

        return FootprintFix(
            footprint_ref=footprint.reference,
            footprint_name=footprint.name,
            adjustments=adjustments,
            old_pad_spacing=old_spacing,
            new_pad_spacing=new_spacing,
        )

    def fix_pcb(self, pcb: "PCB", dry_run: bool = False) -> List[FootprintFix]:
        """Fix all pad spacing issues in a PCB.

        Args:
            pcb: The PCB to fix
            dry_run: If True, calculate but don't apply changes

        Returns:
            List of fixes applied (or would be applied if dry_run)
        """
        fixes: List[FootprintFix] = []

        for footprint in pcb.footprints:
            fix = self.fix_footprint_pads(footprint, dry_run=dry_run)
            if fix:
                fixes.append(fix)

        return fixes

    def group_by_footprint_name(
        self, fixes: List[FootprintFix]
    ) -> Dict[str, List[FootprintFix]]:
        """Group fixes by footprint name.

        Useful for seeing which footprint types needed fixing.

        Args:
            fixes: List of fixes to group

        Returns:
            Dict mapping footprint name to list of fixes
        """
        grouped: Dict[str, List[FootprintFix]] = {}
        for fix in fixes:
            if fix.footprint_name not in grouped:
                grouped[fix.footprint_name] = []
            grouped[fix.footprint_name].append(fix)
        return grouped

    def summarize(self, fixes: List[FootprintFix]) -> dict:
        """Generate a summary of fixes.

        Args:
            fixes: List of fixes to summarize

        Returns:
            Summary dict with counts and groupings
        """
        by_footprint_name: Dict[str, int] = {}

        for fix in fixes:
            name_key = fix.footprint_name
            by_footprint_name[name_key] = by_footprint_name.get(name_key, 0) + 1

        return {
            "total_footprints_fixed": len(fixes),
            "total_pads_adjusted": sum(len(f.adjustments) for f in fixes),
            "by_footprint_name": by_footprint_name,
        }

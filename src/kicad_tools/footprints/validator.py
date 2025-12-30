"""Footprint validation module.

Detects common footprint issues such as overlapping pads, incorrect spacing,
and DRC-violating geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from ..schema.pcb import PCB, Footprint, Pad  # noqa: F401


class IssueSeverity(Enum):
    """Severity level for footprint issues."""

    ERROR = "error"  # Critical issues that will cause DRC failures
    WARNING = "warning"  # Issues that may cause problems
    INFO = "info"  # Informational notes


class IssueType(Enum):
    """Types of footprint issues."""

    PAD_OVERLAP = "pad_overlap"
    PAD_TOUCHING = "pad_touching"
    PAD_SPACING = "pad_spacing"
    MISSING_COURTYARD = "missing_courtyard"
    SILKSCREEN_OVERLAP = "silkscreen_overlap"
    MISSING_LAYER = "missing_layer"


@dataclass
class FootprintIssue:
    """A detected issue with a footprint."""

    footprint_ref: str
    footprint_name: str
    issue_type: IssueType
    severity: IssueSeverity
    message: str
    details: dict

    def __str__(self) -> str:
        """Human-readable representation."""
        return f"{self.footprint_ref} ({self.footprint_name}): {self.severity.value.upper()} - {self.message}"


def _calculate_pad_gap(pad1: "Pad", pad2: "Pad") -> float:
    """Calculate the gap between two pads.

    Takes into account pad positions and sizes to determine the minimum
    distance between pad edges.

    Args:
        pad1: First pad
        pad2: Second pad

    Returns:
        Gap in mm. Negative means overlap, 0 means touching.
    """
    # Get pad centers
    x1, y1 = pad1.position
    x2, y2 = pad2.position

    # Get pad sizes (width, height)
    w1, h1 = pad1.size
    w2, h2 = pad2.size

    # Calculate center-to-center distance
    dx = abs(x2 - x1)
    dy = abs(y2 - y1)

    # For axis-aligned rectangular pads, calculate edge-to-edge distance
    # along each axis separately
    gap_x = dx - (w1 / 2 + w2 / 2)
    gap_y = dy - (h1 / 2 + h2 / 2)

    # If pads don't overlap on either axis, gap is the hypotenuse
    if gap_x > 0 and gap_y > 0:
        return math.sqrt(gap_x**2 + gap_y**2)

    # If overlap on one axis, gap is the other axis
    if gap_x > 0:
        return gap_x
    if gap_y > 0:
        return gap_y

    # Both axes overlap - return the larger (less negative) gap
    # This represents how much they overlap
    return max(gap_x, gap_y)


class FootprintValidator:
    """Validates footprints for common issues.

    Detects:
    - Pads that overlap (negative gap)
    - Pads that touch (0 gap)
    - Pads with spacing less than minimum clearance
    - (Future) Missing courtyard, silkscreen overlap, etc.

    Example::

        from kicad_tools.footprints.validator import FootprintValidator
        from kicad_tools.schema import PCB

        pcb = PCB.load("board.kicad_pcb")
        validator = FootprintValidator(min_pad_gap=0.15)

        issues = validator.validate_pcb(pcb)
        for issue in issues:
            print(issue)
    """

    def __init__(self, min_pad_gap: float = 0.15):
        """Initialize validator.

        Args:
            min_pad_gap: Minimum required gap between pads in mm (default: 0.15mm)
        """
        self.min_pad_gap = min_pad_gap

    def validate_footprint(self, footprint: "Footprint") -> List[FootprintIssue]:
        """Validate a single footprint for issues.

        Args:
            footprint: The footprint to validate

        Returns:
            List of detected issues
        """
        issues: List[FootprintIssue] = []

        # Check pad spacing
        issues.extend(self._check_pad_spacing(footprint))

        return issues

    def validate_pcb(self, pcb: "PCB") -> List[FootprintIssue]:
        """Validate all footprints in a PCB.

        Args:
            pcb: The PCB to validate

        Returns:
            List of all detected issues across all footprints
        """
        issues: List[FootprintIssue] = []

        for footprint in pcb.footprints:
            issues.extend(self.validate_footprint(footprint))

        return issues

    def _check_pad_spacing(self, footprint: "Footprint") -> List[FootprintIssue]:
        """Check for pad spacing issues.

        Args:
            footprint: The footprint to check

        Returns:
            List of pad spacing issues
        """
        issues: List[FootprintIssue] = []
        pads = footprint.pads

        # Check all pairs of pads
        for i, pad1 in enumerate(pads):
            for pad2 in pads[i + 1 :]:
                gap = _calculate_pad_gap(pad1, pad2)

                if gap < 0:
                    # Pads overlap
                    issues.append(
                        FootprintIssue(
                            footprint_ref=footprint.reference,
                            footprint_name=footprint.name,
                            issue_type=IssueType.PAD_OVERLAP,
                            severity=IssueSeverity.ERROR,
                            message=f"Pad {pad1.number} and Pad {pad2.number} are overlapping ({abs(gap):.3f}mm overlap)",
                            details={
                                "pad1": pad1.number,
                                "pad2": pad2.number,
                                "gap_mm": gap,
                                "pad1_position": pad1.position,
                                "pad2_position": pad2.position,
                                "pad1_size": pad1.size,
                                "pad2_size": pad2.size,
                            },
                        )
                    )
                elif gap == 0 or gap < 0.001:  # Allow for floating point
                    # Pads are touching
                    issues.append(
                        FootprintIssue(
                            footprint_ref=footprint.reference,
                            footprint_name=footprint.name,
                            issue_type=IssueType.PAD_TOUCHING,
                            severity=IssueSeverity.WARNING,
                            message=f"Pad {pad1.number} and Pad {pad2.number} are touching ({gap:.3f}mm gap)",
                            details={
                                "pad1": pad1.number,
                                "pad2": pad2.number,
                                "gap_mm": gap,
                                "pad1_position": pad1.position,
                                "pad2_position": pad2.position,
                                "pad1_size": pad1.size,
                                "pad2_size": pad2.size,
                            },
                        )
                    )
                elif gap < self.min_pad_gap:
                    # Pads are too close
                    issues.append(
                        FootprintIssue(
                            footprint_ref=footprint.reference,
                            footprint_name=footprint.name,
                            issue_type=IssueType.PAD_SPACING,
                            severity=IssueSeverity.WARNING,
                            message=f"Pad {pad1.number} and Pad {pad2.number} have insufficient spacing ({gap:.3f}mm gap, need {self.min_pad_gap:.3f}mm)",
                            details={
                                "pad1": pad1.number,
                                "pad2": pad2.number,
                                "gap_mm": gap,
                                "required_gap_mm": self.min_pad_gap,
                                "pad1_position": pad1.position,
                                "pad2_position": pad2.position,
                                "pad1_size": pad1.size,
                                "pad2_size": pad2.size,
                            },
                        )
                    )

        return issues

    def group_by_footprint_name(
        self, issues: List[FootprintIssue]
    ) -> dict[str, List[FootprintIssue]]:
        """Group issues by footprint name.

        Useful for identifying which footprint types have issues.

        Args:
            issues: List of issues to group

        Returns:
            Dict mapping footprint name to list of issues
        """
        grouped: dict[str, List[FootprintIssue]] = {}
        for issue in issues:
            if issue.footprint_name not in grouped:
                grouped[issue.footprint_name] = []
            grouped[issue.footprint_name].append(issue)
        return grouped

    def summarize(self, issues: List[FootprintIssue]) -> dict:
        """Generate a summary of issues.

        Args:
            issues: List of issues to summarize

        Returns:
            Summary dict with counts and groupings
        """
        by_type: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_footprint_name: dict[str, int] = {}

        for issue in issues:
            type_key = issue.issue_type.value
            severity_key = issue.severity.value
            name_key = issue.footprint_name

            by_type[type_key] = by_type.get(type_key, 0) + 1
            by_severity[severity_key] = by_severity.get(severity_key, 0) + 1
            by_footprint_name[name_key] = by_footprint_name.get(name_key, 0) + 1

        return {
            "total": len(issues),
            "by_type": by_type,
            "by_severity": by_severity,
            "by_footprint_name": by_footprint_name,
            "footprints_with_issues": len(set(i.footprint_ref for i in issues)),
        }

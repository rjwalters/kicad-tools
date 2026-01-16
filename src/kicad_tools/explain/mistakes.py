"""Common PCB design mistake detection with educational explanations.

This module provides tools for detecting common PCB design mistakes that
experienced designers avoid, along with educational explanations and fix
suggestions.

Example:
    >>> from kicad_tools.explain.mistakes import detect_mistakes
    >>> from kicad_tools.schema.pcb import PCB
    >>> pcb = PCB.load("design.kicad_pcb")
    >>> mistakes = detect_mistakes(pcb)
    >>> for m in mistakes:
    ...     print(f"[{m.severity}] {m.title}")
    ...     print(f"  Location: {m.components}")
    ...     print(f"  Problem: {m.explanation}")
    ...     print(f"  Fix: {m.fix_suggestion}")
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..schema.pcb import PCB


class MistakeCategory(Enum):
    """Categories of PCB design mistakes."""

    BYPASS_CAP = "bypass_capacitor"
    CRYSTAL = "crystal_oscillator"
    DIFFERENTIAL_PAIR = "differential_pair"
    POWER_TRACE = "power_trace"
    THERMAL = "thermal_management"
    EMI = "emi_shielding"
    DECOUPLING = "decoupling"
    GROUNDING = "grounding"
    VIA = "via_placement"
    MANUFACTURABILITY = "manufacturability"


@dataclass
class Mistake:
    """A detected PCB design mistake.

    Attributes:
        category: The category of mistake
        severity: Severity level ("error", "warning", "info")
        title: Short descriptive title
        components: List of component references involved (e.g., ["C5", "U1"])
        location: Optional (x, y) coordinates in mm
        explanation: Detailed explanation of why this is a problem
        fix_suggestion: Actionable suggestion for fixing the issue
        learn_more_url: Optional URL or path to educational documentation
    """

    category: MistakeCategory
    severity: str  # "error", "warning", "info"
    title: str
    components: list[str]
    explanation: str
    fix_suggestion: str
    location: tuple[float, float] | None = None
    learn_more_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "category": self.category.value,
            "severity": self.severity,
            "title": self.title,
            "components": self.components,
            "location": self.location,
            "explanation": self.explanation,
            "fix_suggestion": self.fix_suggestion,
            "learn_more_url": self.learn_more_url,
        }

    def format_tree(self) -> str:
        """Format as a tree structure for terminal output."""
        lines = [f"[{self.severity.upper()}] {self.title}"]
        lines.append(f"├─ Components: {', '.join(self.components)}")
        if self.location:
            lines.append(f"├─ Location: ({self.location[0]:.2f}, {self.location[1]:.2f}) mm")
        lines.append(f"├─ Problem: {self.explanation}")
        lines.append(f"├─ Fix: {self.fix_suggestion}")
        if self.learn_more_url:
            lines.append(f"└─ Learn more: {self.learn_more_url}")
        return "\n".join(lines)


class MistakeCheck(Protocol):
    """Protocol for mistake detection checks.

    Each check implementation must provide:
    - category: The MistakeCategory this check relates to
    - check(pcb): Method that returns list of detected mistakes
    """

    category: MistakeCategory

    def check(self, pcb: PCB) -> list[Mistake]:
        """Run the check on a PCB and return detected mistakes."""
        ...


class MistakeDetector:
    """Detect common PCB design mistakes.

    Runs a configurable set of checks against a PCB and returns
    all detected mistakes sorted by severity.

    Example:
        >>> detector = MistakeDetector()
        >>> mistakes = detector.detect(pcb)
        >>> for m in mistakes:
        ...     print(f"[{m.severity}] {m.title}")
    """

    def __init__(self, checks: list[MistakeCheck] | None = None):
        """Initialize detector with optional custom checks.

        Args:
            checks: List of MistakeCheck implementations. If None,
                    uses the default set of built-in checks.
        """
        if checks is None:
            checks = get_default_checks()
        self._checks = checks

    @property
    def checks(self) -> list[MistakeCheck]:
        """The list of checks this detector runs."""
        return self._checks

    def detect(self, pcb: PCB) -> list[Mistake]:
        """Run all checks and return detected mistakes.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects sorted by severity (error > warning > info)
        """
        mistakes: list[Mistake] = []
        for check in self._checks:
            mistakes.extend(check.check(pcb))

        # Sort by severity: error > warning > info
        severity_order = {"error": 0, "warning": 1, "info": 2}
        return sorted(mistakes, key=lambda m: severity_order.get(m.severity, 99))

    def detect_by_category(
        self,
        pcb: PCB,
        category: MistakeCategory,
    ) -> list[Mistake]:
        """Run only checks in a specific category.

        Args:
            pcb: The PCB to analyze
            category: Only run checks in this category

        Returns:
            List of Mistake objects from checks in the specified category
        """
        mistakes: list[Mistake] = []
        for check in self._checks:
            if check.category == category:
                mistakes.extend(check.check(pcb))
        return mistakes


def detect_mistakes(pcb: PCB) -> list[Mistake]:
    """Convenience function to detect all mistakes in a PCB.

    This is the main entry point for mistake detection.

    Args:
        pcb: The PCB to analyze

    Returns:
        List of Mistake objects sorted by severity

    Example:
        >>> from kicad_tools.schema.pcb import PCB
        >>> from kicad_tools.explain.mistakes import detect_mistakes
        >>> pcb = PCB.load("my_board.kicad_pcb")
        >>> mistakes = detect_mistakes(pcb)
        >>> print(f"Found {len(mistakes)} potential issues")
    """
    detector = MistakeDetector()
    return detector.detect(pcb)


def get_default_checks() -> list[MistakeCheck]:
    """Get the default set of mistake checks.

    Returns:
        List of MistakeCheck implementations
    """
    # Import checks here to avoid circular imports
    from .checks import (
        AcidTrapCheck,
        BypassCapDistanceCheck,
        CrystalNoiseProximityCheck,
        CrystalTraceLengthCheck,
        DifferentialPairSkewCheck,
        PowerTraceWidthCheck,
        ThermalPadConnectionCheck,
        TombstoningRiskCheck,
        ViaInPadCheck,
    )

    return [
        BypassCapDistanceCheck(),
        CrystalTraceLengthCheck(),
        CrystalNoiseProximityCheck(),
        DifferentialPairSkewCheck(),
        PowerTraceWidthCheck(),
        ThermalPadConnectionCheck(),
        ViaInPadCheck(),
        AcidTrapCheck(),
        TombstoningRiskCheck(),
    ]


# =============================================================================
# Utility functions for checks
# =============================================================================


def distance(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Calculate Euclidean distance between two points.

    Args:
        p1: First point (x, y) in mm
        p2: Second point (x, y) in mm

    Returns:
        Distance in mm
    """
    return math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)


def trace_length(segments: list[Any]) -> float:
    """Calculate total length of trace segments.

    Args:
        segments: List of Segment objects

    Returns:
        Total length in mm
    """
    total = 0.0
    for seg in segments:
        total += distance(seg.start, seg.end)
    return total


def is_power_net(net_name: str) -> bool:
    """Check if a net name appears to be a power net.

    Args:
        net_name: The net name to check

    Returns:
        True if the net appears to be a power net
    """
    power_patterns = [
        "VCC",
        "VDD",
        "VIN",
        "VOUT",
        "3V3",
        "3.3V",
        "5V",
        "12V",
        "VBAT",
        "VSYS",
        "+",
        "PWR",
        "POWER",
    ]
    upper_name = net_name.upper()
    return any(pattern in upper_name for pattern in power_patterns)


def is_ground_net(net_name: str) -> bool:
    """Check if a net name appears to be a ground net.

    Args:
        net_name: The net name to check

    Returns:
        True if the net appears to be a ground net
    """
    ground_patterns = ["GND", "GROUND", "VSS", "AGND", "DGND", "PGND", "SGND"]
    upper_name = net_name.upper()
    return any(pattern in upper_name for pattern in ground_patterns)


def is_bypass_cap(reference: str, value: str) -> bool:
    """Check if a component appears to be a bypass capacitor.

    Args:
        reference: Component reference (e.g., "C5")
        value: Component value (e.g., "100nF")

    Returns:
        True if the component appears to be a bypass capacitor
    """
    if not reference.upper().startswith("C"):
        return False

    # Check for typical bypass cap values
    bypass_values = ["100n", "0.1u", "10n", "1u", "4.7u", "10u"]
    lower_value = value.lower().replace(" ", "").replace("f", "")
    return any(bv in lower_value for bv in bypass_values)


def is_crystal(reference: str, footprint: str) -> bool:
    """Check if a component appears to be a crystal or oscillator.

    Args:
        reference: Component reference (e.g., "Y1")
        footprint: Footprint name

    Returns:
        True if the component appears to be a crystal
    """
    ref_upper = reference.upper()
    fp_lower = footprint.lower()

    return (
        ref_upper.startswith("Y")
        or ref_upper.startswith("X")
        or "crystal" in fp_lower
        or "oscillator" in fp_lower
        or "xtal" in fp_lower
    )


def is_differential_pair_net(net_name: str) -> tuple[bool, str | None]:
    """Check if a net is part of a differential pair.

    Args:
        net_name: The net name to check

    Returns:
        Tuple of (is_diff_pair, pair_base_name)
        pair_base_name is None if not a differential pair
    """
    upper_name = net_name.upper()

    # Common differential pair patterns
    patterns = [
        ("USB_D+", "USB_D-", "USB_D"),
        ("USB_DP", "USB_DM", "USB_D"),
        ("D+", "D-", "USB"),
        ("DP", "DM", "USB"),
        ("TX+", "TX-", "TX"),
        ("TXP", "TXN", "TX"),
        ("RX+", "RX-", "RX"),
        ("RXP", "RXN", "RX"),
        ("LVDS+", "LVDS-", "LVDS"),
    ]

    for pos, neg, base in patterns:
        if pos in upper_name or neg in upper_name:
            return True, base

    # Check for generic _P/_N suffix
    if upper_name.endswith("_P") or upper_name.endswith("_N"):
        base = upper_name[:-2]
        return True, base

    return False, None

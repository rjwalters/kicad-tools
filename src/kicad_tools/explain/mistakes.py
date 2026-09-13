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
from typing import TYPE_CHECKING, Any, Literal, Protocol

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
    # Issue #4899: net-new categories added for the Konnect design-review
    # audit parity pass (audit_connections / check_bom_health).
    CONNECTIVITY = "connectivity"
    BOM_HEALTH = "bom_health"


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


class CheckIncomplete(Exception):
    """Raised by :meth:`MistakeCheck.check` to report partial coverage.

    Issue #4899 mirrors the #4011 ``lvs`` vacuity-guard discipline for
    ``detect_mistakes``: a check that lacked the input data it needed to
    reach a verdict (e.g. a schematic-dependent check with no sibling
    schematic file next to the PCB) must say so explicitly rather than
    silently returning an empty ``list[Mistake]`` -- which is
    indistinguishable from "I looked and found nothing wrong".

    This is deliberately **not** the same signal as "I ran and found zero
    applicable components" (e.g. a board with no crystals) -- that is a
    legitimate clean run and should return ``[]`` normally, not raise.
    ``CheckIncomplete`` is reserved for "I could not evaluate this at
    all".

    Args:
        reason: Human-readable explanation of what prerequisite was
            missing, surfaced verbatim in :attr:`CheckCoverage.reason`.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class CheckCoverage:
    """Coverage outcome for a single :class:`MistakeCheck` run (issue #4899).

    Mirrors the ``SubCheckResult`` pattern used by ``kct check``'s meta
    rollup (#3750/#4011): findings (``Mistake``) and coverage
    (``CheckCoverage``) are reported on separate channels so an
    ``incomplete`` check can never be mistaken for "ran clean".
    """

    check_name: str
    category: MistakeCategory
    status: Literal["ran", "incomplete"]
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "check_name": self.check_name,
            "category": self.category.value,
            "status": self.status,
            "reason": self.reason,
        }


class MistakeCheck(Protocol):
    """Protocol for mistake detection checks.

    Each check implementation must provide:
    - category: The MistakeCategory this check relates to
    - check(pcb): Method that returns list of detected mistakes

    A check that cannot reach a verdict because required input data is
    unavailable (e.g. no sibling schematic for a schematic-dependent
    check) should raise :class:`CheckIncomplete` instead of returning
    ``[]`` -- see that class's docstring for the "incomplete" vs.
    "ran clean" distinction.
    """

    category: MistakeCategory

    def check(self, pcb: PCB) -> list[Mistake]:
        """Run the check on a PCB and return detected mistakes.

        Raises:
            CheckIncomplete: If the check could not run to completion
                because required input data was unavailable.
        """
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

        Note:
            A check that raised :class:`CheckIncomplete` contributes no
            mistakes here and is silently skipped -- this method keeps its
            original ``list[Mistake]``-only signature for backward
            compatibility. Callers that need the honest "did every check
            actually run" signal (issue #4899, mirroring #4011) should use
            :meth:`detect_with_coverage` instead, which is what
            ``kct detect-mistakes`` and the ``detect_mistakes`` MCP tool
            use by default.
        """
        mistakes, _coverage = self.detect_with_coverage(pcb)
        return mistakes

    def detect_with_coverage(self, pcb: PCB) -> tuple[list[Mistake], list[CheckCoverage]]:
        """Run all checks and return both mistakes and per-check coverage.

        Args:
            pcb: The PCB to analyze

        Returns:
            ``(mistakes, coverage)`` -- mistakes sorted by severity, and one
            :class:`CheckCoverage` entry per registered check recording
            whether it ran or was ``incomplete`` (issue #4899).
        """
        return self._run(self._checks, pcb)

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
        mistakes, _coverage = self.detect_by_category_with_coverage(pcb, category)
        return mistakes

    def detect_by_category_with_coverage(
        self,
        pcb: PCB,
        category: MistakeCategory,
    ) -> tuple[list[Mistake], list[CheckCoverage]]:
        """Run only checks in a specific category, with coverage (issue #4899).

        Args:
            pcb: The PCB to analyze
            category: Only run checks in this category

        Returns:
            ``(mistakes, coverage)`` for checks in the specified category.
        """
        checks = [c for c in self._checks if c.category == category]
        return self._run(checks, pcb)

    @staticmethod
    def _run(checks: list[MistakeCheck], pcb: PCB) -> tuple[list[Mistake], list[CheckCoverage]]:
        """Run *checks* against *pcb*, catching :class:`CheckIncomplete` per-check.

        A check that raises ``CheckIncomplete`` contributes a ``"incomplete"``
        :class:`CheckCoverage` entry (and no mistakes); every other check
        -- including one that ran cleanly and found nothing -- contributes a
        ``"ran"`` entry.
        """
        mistakes: list[Mistake] = []
        coverage: list[CheckCoverage] = []
        for check in checks:
            check_name = type(check).__name__
            try:
                found = check.check(pcb)
            except CheckIncomplete as exc:
                coverage.append(
                    CheckCoverage(
                        check_name=check_name,
                        category=check.category,
                        status="incomplete",
                        reason=exc.reason,
                    )
                )
                continue
            mistakes.extend(found)
            coverage.append(
                CheckCoverage(
                    check_name=check_name,
                    category=check.category,
                    status="ran",
                    reason=None,
                )
            )

        # Sort by severity: error > warning > info
        severity_order = {"error": 0, "warning": 1, "info": 2}
        mistakes.sort(key=lambda m: severity_order.get(m.severity, 99))
        return mistakes, coverage


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


def detect_mistakes_with_coverage(pcb: PCB) -> tuple[list[Mistake], list[CheckCoverage]]:
    """Detect mistakes *and* report per-check coverage (issue #4899).

    This is the recommended entry point when the caller needs to honor the
    #4011 vacuity-guard discipline: a check that could not run (e.g. a
    schematic-dependent check with no sibling schematic file) must not
    silently look like a clean pass. ``kct detect-mistakes`` and the
    ``detect_mistakes`` MCP tool use this function.

    Args:
        pcb: The PCB to analyze

    Returns:
        ``(mistakes, coverage)`` -- see :meth:`MistakeDetector.detect_with_coverage`.

    Example:
        >>> from kicad_tools.schema.pcb import PCB
        >>> from kicad_tools.explain.mistakes import detect_mistakes_with_coverage
        >>> pcb = PCB.load("my_board.kicad_pcb")
        >>> mistakes, coverage = detect_mistakes_with_coverage(pcb)
        >>> incomplete = [c for c in coverage if c.status == "incomplete"]
        >>> if incomplete:
        ...     print(f"{len(incomplete)} check(s) could not run")
    """
    detector = MistakeDetector()
    return detector.detect_with_coverage(pcb)


def get_default_checks() -> list[MistakeCheck]:
    """Get the default set of mistake checks.

    Returns:
        List of MistakeCheck implementations
    """
    # Import checks here to avoid circular imports
    from .checks import (
        AcidTrapCheck,
        BomFieldHealthCheck,
        BypassCapDistanceCheck,
        CrystalNoiseProximityCheck,
        CrystalTraceLengthCheck,
        DifferentialPairSkewCheck,
        LedSeriesResistorCheck,
        MissingDecouplingCapCheck,
        PowerTraceWidthCheck,
        PullUpResistorCheck,
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
        # Issue #4899: Konnect design-review audit parity (net-new checks).
        MissingDecouplingCapCheck(),
        PullUpResistorCheck(),
        LedSeriesResistorCheck(),
        BomFieldHealthCheck(),
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

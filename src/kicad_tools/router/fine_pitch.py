"""
Fine-pitch IC detection and routing compatibility warnings.

This module provides pre-routing analysis to detect fine-pitch ICs (TSSOP, QFN, etc.)
that may cause routing difficulties due to grid/clearance constraints. It warns users
upfront about problematic components before routing begins.

Example::

    from kicad_tools.router.fine_pitch import analyze_fine_pitch_components

    # Analyze components before routing
    report = analyze_fine_pitch_components(
        pads=router.pads,
        grid_resolution=0.05,
        trace_width=0.2,
        clearance=0.2,
    )

    # Show warnings
    if report.has_warnings:
        print(report.format_warnings())
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Pad


class FinePitchSeverity(Enum):
    """Severity level for fine-pitch routing warnings."""

    OK = auto()  # No issues expected
    LOW = auto()  # Minor concerns, likely routable
    MEDIUM = auto()  # Significant concerns, may need adjustments
    HIGH = auto()  # Severe concerns, routing will be difficult
    CRITICAL = auto()  # Likely unroutable without changes


@dataclass
class OffGridPad:
    """Information about a pad that doesn't align with the routing grid."""

    ref: str
    pin: str
    x: float
    y: float
    offset_x: float  # Distance to nearest grid point in X
    offset_y: float  # Distance to nearest grid point in Y
    max_offset: float  # Maximum offset from grid

    @property
    def position(self) -> tuple[float, float]:
        """Return (x, y) position."""
        return (self.x, self.y)


@dataclass
class ComponentGridAnalysis:
    """Analysis results for a single component's routing compatibility.

    Attributes:
        ref: Component reference (e.g., "U1")
        package_type: Detected package type (e.g., "TSSOP-20", "QFN-32")
        pin_count: Number of pins
        pin_pitch: Minimum pin pitch in mm
        off_grid_pads: List of pads that don't align with the grid
        off_grid_count: Number of off-grid pads
        off_grid_percentage: Percentage of pads that are off-grid
        affected_nets: List of net names affected by off-grid pads
        severity: Overall severity for this component
        recommendations: List of recommended actions
    """

    ref: str
    package_type: str
    pin_count: int
    pin_pitch: float
    off_grid_pads: list[OffGridPad] = field(default_factory=list)
    off_grid_count: int = 0
    off_grid_percentage: float = 0.0
    affected_nets: list[str] = field(default_factory=list)
    severity: FinePitchSeverity = FinePitchSeverity.OK
    recommendations: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        """True if component has any routing concerns."""
        return self.severity != FinePitchSeverity.OK

    def format_summary(self) -> str:
        """Format a one-line summary for this component."""
        if self.severity == FinePitchSeverity.OK:
            return f"  {self.ref} ({self.package_type}): {self.pin_pitch:.2f}mm pitch - All pads on grid ✓"

        return (
            f"  {self.ref} ({self.package_type}): {self.pin_pitch:.2f}mm pitch, {self.pin_count} pads\n"
            f"    - {self.off_grid_count} pads off-grid ({self.off_grid_percentage:.0f}%)\n"
            f"    - Recommendation: {self.recommendations[0] if self.recommendations else 'Use finer grid'}"
        )


@dataclass
class FinePitchReport:
    """Complete fine-pitch analysis report for all components.

    Attributes:
        components: List of per-component analysis results
        grid_resolution: Grid resolution used for analysis (mm)
        trace_width: Trace width (mm)
        clearance: Trace clearance (mm)
        total_pads: Total number of pads analyzed
        total_off_grid: Total number of off-grid pads
        affected_net_count: Number of nets with off-grid pads
    """

    components: list[ComponentGridAnalysis] = field(default_factory=list)
    grid_resolution: float = 0.0
    trace_width: float = 0.0
    clearance: float = 0.0
    total_pads: int = 0
    total_off_grid: int = 0
    affected_net_count: int = 0

    @property
    def has_warnings(self) -> bool:
        """True if any components have routing concerns."""
        return any(c.has_issues for c in self.components)

    @property
    def components_with_issues(self) -> list[ComponentGridAnalysis]:
        """Get only components that have issues."""
        return [c for c in self.components if c.has_issues]

    @property
    def max_severity(self) -> FinePitchSeverity:
        """Get the highest severity level across all components."""
        if not self.components:
            return FinePitchSeverity.OK
        severities = [c.severity for c in self.components]
        # Return the highest severity (CRITICAL > HIGH > MEDIUM > LOW > OK)
        for sev in [
            FinePitchSeverity.CRITICAL,
            FinePitchSeverity.HIGH,
            FinePitchSeverity.MEDIUM,
            FinePitchSeverity.LOW,
        ]:
            if sev in severities:
                return sev
        return FinePitchSeverity.OK

    def format_warnings(self, verbose: bool = False) -> str:
        """Format warnings for display.

        Args:
            verbose: If True, include detailed per-pad information.

        Returns:
            Formatted warning string, or empty string if no warnings.
        """
        if not self.has_warnings:
            return ""

        lines = [
            "",
            "⚠️  Warning: Fine-pitch components detected",
            f"    Grid resolution: {self.grid_resolution}mm",
            "",
        ]

        for comp in self.components_with_issues:
            severity_icon = {
                FinePitchSeverity.LOW: "⚠️ ",
                FinePitchSeverity.MEDIUM: "⚠️ ",
                FinePitchSeverity.HIGH: "🔶",
                FinePitchSeverity.CRITICAL: "🔴",
            }.get(comp.severity, "  ")

            lines.append(
                f"  {severity_icon} {comp.ref} ({comp.package_type}): "
                f"{comp.pin_pitch:.2f}mm pitch, {comp.pin_count} pads"
            )
            lines.append(
                f"      - {comp.off_grid_count} pads off-grid by "
                f"{_format_offset_range(comp.off_grid_pads)} at current {self.grid_resolution}mm grid"
            )

            if comp.recommendations:
                lines.append(f"      - Recommendation: {comp.recommendations[0]}")

            if comp.affected_nets and len(comp.affected_nets) <= 5:
                nets_str = ", ".join(comp.affected_nets)
                lines.append(f"      - Affected nets: {nets_str}")
            elif comp.affected_nets:
                nets_str = ", ".join(comp.affected_nets[:5])
                lines.append(
                    f"      - Affected nets: {nets_str}, ... ({len(comp.affected_nets)} total)"
                )

            if verbose and comp.off_grid_pads:
                lines.append("      - Off-grid pads:")
                for pad in comp.off_grid_pads[:5]:
                    lines.append(
                        f"        • {pad.ref}.{pad.pin}: ({pad.x:.3f}, {pad.y:.3f}) "
                        f"offset {pad.max_offset:.3f}mm"
                    )
                if len(comp.off_grid_pads) > 5:
                    lines.append(f"        ... and {len(comp.off_grid_pads) - 5} more")

            lines.append("")

        # Add overall recommendations if severe issues found
        if self.max_severity in (FinePitchSeverity.HIGH, FinePitchSeverity.CRITICAL):
            lines.append("  Recommendations:")
            if self.grid_resolution > 0.025:
                lines.append(
                    f"    • Use finer grid: --grid 0.025 or --grid {self.grid_resolution / 2}"
                )
            lines.append("    • Consider enabling sub-grid routing for pad connections")
            lines.append("    • Review affected nets for manual routing")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Convert report to a JSON-serializable dictionary."""
        return {
            "grid_resolution": self.grid_resolution,
            "trace_width": self.trace_width,
            "clearance": self.clearance,
            "total_pads": self.total_pads,
            "total_off_grid": self.total_off_grid,
            "affected_net_count": self.affected_net_count,
            "max_severity": self.max_severity.name,
            "has_warnings": self.has_warnings,
            "components": [
                {
                    "ref": c.ref,
                    "package_type": c.package_type,
                    "pin_count": c.pin_count,
                    "pin_pitch": c.pin_pitch,
                    "off_grid_count": c.off_grid_count,
                    "off_grid_percentage": c.off_grid_percentage,
                    "affected_nets": c.affected_nets,
                    "severity": c.severity.name,
                    "recommendations": c.recommendations,
                }
                for c in self.components_with_issues
            ],
        }


def _format_offset_range(off_grid_pads: list[OffGridPad]) -> str:
    """Format the range of offsets for display."""
    if not off_grid_pads:
        return "0mm"
    offsets = [p.max_offset for p in off_grid_pads]
    min_off = min(offsets)
    max_off = max(offsets)
    if abs(max_off - min_off) < 0.001:
        return f"{min_off:.2f}mm"
    return f"{min_off:.2f}-{max_off:.2f}mm"


def _calculate_grid_offset(value: float, resolution: float) -> float:
    """Calculate the offset from the nearest grid point.

    Args:
        value: The coordinate value to check
        resolution: The grid resolution

    Returns:
        Distance to nearest grid point (always positive)
    """
    remainder = abs(value % resolution)
    return min(remainder, resolution - remainder)


def _calculate_min_pitch(pads: list[Pad]) -> float:
    """Calculate minimum pitch between adjacent pads.

    Uses nearest-neighbor distance, which is appropriate for IC packages
    where adjacent pins are close together.

    Args:
        pads: List of pads from a single component

    Returns:
        Minimum pitch in mm, or 0 if fewer than 2 pads
    """
    if len(pads) < 2:
        return 0.0

    min_pitch = float("inf")
    for i, p1 in enumerate(pads):
        for p2 in pads[i + 1 :]:
            dist = math.sqrt((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2)
            if dist > 0.01:  # Ignore overlapping pads
                min_pitch = min(min_pitch, dist)

    return min_pitch if min_pitch != float("inf") else 0.0


def _detect_package_type_simple(pads: list[Pad], ref: str) -> str:
    """Detect package type from pad arrangement and reference.

    Args:
        pads: List of pads from a single component
        ref: Component reference (e.g., "U1", "R1")

    Returns:
        Package type string (e.g., "TSSOP-20", "QFN-32", "SOT-23")
    """
    pin_count = len(pads)
    if pin_count == 0:
        return "UNKNOWN"

    # Calculate min pitch
    min_pitch = _calculate_min_pitch(pads)

    # Check for through-hole
    through_hole_count = sum(1 for p in pads if getattr(p, "through_hole", False))
    if through_hole_count > pin_count * 0.8:
        if pin_count <= 3:
            return f"THT-{pin_count}"
        return f"DIP-{pin_count}"

    # Classify by pin count and pitch
    if pin_count <= 5:
        if min_pitch < 1.0:
            return f"SOT-{pin_count}"
        return f"SOT-{pin_count}"

    if pin_count <= 8:
        if min_pitch < 0.65:
            return f"TSSOP-{pin_count}"
        elif min_pitch < 1.0:
            return f"MSOP-{pin_count}"
        else:
            return f"SOIC-{pin_count}"

    if pin_count <= 28:
        if min_pitch < 0.5:
            return f"TSSOP-{pin_count}"
        elif min_pitch < 0.8:
            return f"SSOP-{pin_count}"
        else:
            return f"SOIC-{pin_count}"

    # Larger packages
    if min_pitch < 0.5:
        # Check for grid arrangement (BGA/QFN)
        xs = sorted({round(p.x, 2) for p in pads})
        ys = sorted({round(p.y, 2) for p in pads})
        if len(xs) > 2 and len(ys) > 2:
            # Grid pattern
            if min_pitch < 0.4:
                return f"BGA-{pin_count}"
            return f"QFN-{pin_count}"
        return f"TQFP-{pin_count}"

    if min_pitch < 0.8:
        return f"QFP-{pin_count}"

    return f"IC-{pin_count}"


def _determine_severity(
    off_grid_percentage: float,
    pin_pitch: float,
    grid_resolution: float,
    trace_width: float,
    clearance: float,
) -> FinePitchSeverity:
    """Determine severity level based on various factors.

    Args:
        off_grid_percentage: Percentage of pads that are off-grid
        pin_pitch: Minimum pin pitch in mm
        grid_resolution: Grid resolution in mm
        trace_width: Trace width in mm
        clearance: Trace clearance in mm

    Returns:
        Appropriate severity level
    """
    # Calculate routing space between pins
    space_between_pins = pin_pitch - trace_width  # Assuming pad width ~ trace_width
    required_space = 2 * clearance + trace_width

    # Check if there's enough space to route between pins
    can_route_between = space_between_pins >= required_space

    # Calculate grid alignment ratio
    grid_alignment_ratio = pin_pitch / grid_resolution if grid_resolution > 0 else float("inf")

    # Determine severity
    if off_grid_percentage == 0:
        return FinePitchSeverity.OK

    if off_grid_percentage > 50 and not can_route_between:
        return FinePitchSeverity.CRITICAL

    if off_grid_percentage > 30 or grid_alignment_ratio < 2:
        if not can_route_between:
            return FinePitchSeverity.HIGH
        return FinePitchSeverity.MEDIUM

    if off_grid_percentage > 10 or grid_alignment_ratio < 4:
        return FinePitchSeverity.MEDIUM

    return FinePitchSeverity.LOW


def _generate_recommendations(
    pin_pitch: float,
    grid_resolution: float,
    off_grid_percentage: float,
    severity: FinePitchSeverity,
) -> list[str]:
    """Generate recommendations based on analysis.

    Args:
        pin_pitch: Minimum pin pitch in mm
        grid_resolution: Current grid resolution in mm
        off_grid_percentage: Percentage of off-grid pads
        severity: Determined severity level

    Returns:
        List of recommendation strings
    """
    recommendations = []

    if severity == FinePitchSeverity.OK:
        return recommendations

    # Calculate a grid resolution that would align with the pitch
    # Try common grid values that divide evenly into the pitch
    common_grids = [0.005, 0.01, 0.0125, 0.025, 0.05, 0.1, 0.127, 0.25]
    suggested_grid = None

    for g in common_grids:
        if g < grid_resolution:
            # Check if this grid would align better
            pitch_ratio = pin_pitch / g
            if abs(pitch_ratio - round(pitch_ratio)) < 0.01:
                suggested_grid = g
                break

    if suggested_grid:
        recommendations.append(f"Use {suggested_grid}mm grid for better alignment")
    elif grid_resolution > 0.025:
        recommendations.append(f"Use {grid_resolution / 2}mm or 0.025mm grid")

    if off_grid_percentage > 30:
        recommendations.append("Enable sub-grid routing for pad entry/exit")

    if severity in (FinePitchSeverity.HIGH, FinePitchSeverity.CRITICAL):
        recommendations.append("Consider manual routing for affected nets")

    return recommendations


def analyze_fine_pitch_components(
    pads: dict[tuple[str, str], Pad] | list[Pad],
    grid_resolution: float,
    trace_width: float = 0.2,
    clearance: float = 0.2,
    grid_tolerance: float | None = None,
) -> FinePitchReport:
    """Analyze components for fine-pitch routing compatibility.

    Examines each component to detect fine-pitch ICs that may cause routing
    difficulties due to pads not aligning with the routing grid.

    Args:
        pads: Dictionary mapping (ref, pin) to Pad, or list of Pad objects
        grid_resolution: Routing grid resolution in mm
        trace_width: Trace width in mm (for routing space calculation)
        clearance: Trace clearance in mm (for routing space calculation)
        grid_tolerance: Maximum offset to consider "on-grid" (default: resolution/10)

    Returns:
        FinePitchReport with analysis results for all components.

    Example:
        >>> report = analyze_fine_pitch_components(
        ...     pads=router.pads,
        ...     grid_resolution=0.05,
        ...     trace_width=0.2,
        ...     clearance=0.2,
        ... )
        >>> if report.has_warnings:
        ...     print(report.format_warnings())
    """
    if grid_tolerance is None:
        grid_tolerance = grid_resolution / 10

    # Convert pads to list if dict
    if isinstance(pads, dict):
        pad_list = list(pads.values())
    else:
        pad_list = list(pads)

    # Group pads by component reference
    pads_by_ref: dict[str, list[Pad]] = {}
    for pad in pad_list:
        ref = getattr(pad, "ref", "")
        if ref:
            if ref not in pads_by_ref:
                pads_by_ref[ref] = []
            pads_by_ref[ref].append(pad)

    # Analyze each component
    components: list[ComponentGridAnalysis] = []
    total_pads = 0
    total_off_grid = 0
    all_affected_nets: set[str] = set()

    for ref, comp_pads in sorted(pads_by_ref.items()):
        pin_count = len(comp_pads)
        total_pads += pin_count

        if pin_count < 2:
            continue

        # Calculate min pitch
        pin_pitch = _calculate_min_pitch(comp_pads)

        # Detect package type
        package_type = _detect_package_type_simple(comp_pads, ref)

        # Check each pad for grid alignment
        off_grid_pads: list[OffGridPad] = []
        affected_nets: set[str] = set()

        for pad in comp_pads:
            offset_x = _calculate_grid_offset(pad.x, grid_resolution)
            offset_y = _calculate_grid_offset(pad.y, grid_resolution)
            max_offset = max(offset_x, offset_y)

            if max_offset > grid_tolerance:
                pin = getattr(pad, "pin", getattr(pad, "number", "?"))
                off_grid_pads.append(
                    OffGridPad(
                        ref=ref,
                        pin=str(pin),
                        x=pad.x,
                        y=pad.y,
                        offset_x=offset_x,
                        offset_y=offset_y,
                        max_offset=max_offset,
                    )
                )

                net_name = getattr(pad, "net_name", "")
                if net_name:
                    affected_nets.add(net_name)

        off_grid_count = len(off_grid_pads)
        total_off_grid += off_grid_count
        off_grid_percentage = (off_grid_count / pin_count * 100) if pin_count > 0 else 0.0

        # Determine severity
        severity = _determine_severity(
            off_grid_percentage=off_grid_percentage,
            pin_pitch=pin_pitch,
            grid_resolution=grid_resolution,
            trace_width=trace_width,
            clearance=clearance,
        )

        # Generate recommendations
        recommendations = _generate_recommendations(
            pin_pitch=pin_pitch,
            grid_resolution=grid_resolution,
            off_grid_percentage=off_grid_percentage,
            severity=severity,
        )

        all_affected_nets.update(affected_nets)

        components.append(
            ComponentGridAnalysis(
                ref=ref,
                package_type=package_type,
                pin_count=pin_count,
                pin_pitch=pin_pitch,
                off_grid_pads=off_grid_pads,
                off_grid_count=off_grid_count,
                off_grid_percentage=off_grid_percentage,
                affected_nets=sorted(affected_nets),
                severity=severity,
                recommendations=recommendations,
            )
        )

    # Sort components by severity (most severe first)
    severity_order = {
        FinePitchSeverity.CRITICAL: 0,
        FinePitchSeverity.HIGH: 1,
        FinePitchSeverity.MEDIUM: 2,
        FinePitchSeverity.LOW: 3,
        FinePitchSeverity.OK: 4,
    }
    components.sort(key=lambda c: (severity_order.get(c.severity, 5), c.ref))

    return FinePitchReport(
        components=components,
        grid_resolution=grid_resolution,
        trace_width=trace_width,
        clearance=clearance,
        total_pads=total_pads,
        total_off_grid=total_off_grid,
        affected_net_count=len(all_affected_nets),
    )


__all__ = [
    "ComponentGridAnalysis",
    "FinePitchReport",
    "FinePitchSeverity",
    "OffGridPad",
    "analyze_fine_pitch_components",
]

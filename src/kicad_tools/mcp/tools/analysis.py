"""MCP tool for analyzing KiCad PCB files.

Provides the analyze_board function that returns comprehensive
board metadata for AI agent consumption, and get_drc_violations
for design rule checking.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.mcp.types import (
    AffectedItem,
    BoardAnalysis,
    BoardDimensions,
    ComponentSummary,
    DRCResult,
    DRCViolation,
    LayerInfo,
    NetFanout,
    NetSummary,
    RoutingStatus,
    ViolationLocation,
    ZoneInfo,
)
from kicad_tools.schema.pcb import PCB

# Supported manufacturer presets for DRC
MANUFACTURER_PRESETS = ("jlcpcb", "oshpark", "pcbway", "seeed")


def analyze_board(pcb_path: str) -> BoardAnalysis:
    """Analyze a KiCad PCB file and return comprehensive summary.

    This function parses a KiCad PCB file and extracts comprehensive
    metadata about the board design, suitable for AI agent consumption.

    Args:
        pcb_path: Absolute path to .kicad_pcb file

    Returns:
        BoardAnalysis with layers, components, nets, dimensions

    Raises:
        FileNotFoundError: If the PCB file does not exist
        ParseError: If the PCB file cannot be parsed (invalid format)
    """
    path = Path(pcb_path)
    if not path.exists():
        raise KiCadFileNotFoundError(f"PCB file not found: {pcb_path}")

    if not path.suffix == ".kicad_pcb":
        raise ParseError(f"Invalid file extension: {path.suffix} (expected .kicad_pcb)")

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        raise ParseError(f"Failed to parse PCB file: {e}") from e

    return BoardAnalysis(
        file_path=str(path.absolute()),
        board_dimensions=_extract_dimensions(pcb),
        layers=_extract_layers(pcb),
        components=_extract_components(pcb),
        nets=_extract_nets(pcb),
        zones=_extract_zones(pcb),
        routing_status=_compute_routing_status(pcb),
    )


def _extract_dimensions(pcb: PCB) -> BoardDimensions:
    """Extract board dimensions from Edge.Cuts outline.

    Args:
        pcb: Loaded PCB object

    Returns:
        BoardDimensions with width, height, area, and outline type
    """
    outline = pcb.get_board_outline()

    if not outline:
        # No outline found, return zeros
        return BoardDimensions(
            width_mm=0.0,
            height_mm=0.0,
            area_mm2=0.0,
            outline_type="unknown",
        )

    # Calculate bounding box
    min_x = min(p[0] for p in outline)
    max_x = max(p[0] for p in outline)
    min_y = min(p[1] for p in outline)
    max_y = max(p[1] for p in outline)

    width = max_x - min_x
    height = max_y - min_y

    # Calculate area using shoelace formula for polygon
    area = _calculate_polygon_area(outline)

    # Determine outline type
    outline_type = _classify_outline(outline, width, height, area)

    return BoardDimensions(
        width_mm=width,
        height_mm=height,
        area_mm2=area,
        outline_type=outline_type,
    )


def _calculate_polygon_area(points: list[tuple[float, float]]) -> float:
    """Calculate polygon area using shoelace formula.

    Args:
        points: List of (x, y) polygon vertices

    Returns:
        Area in square millimeters (always positive)
    """
    n = len(points)
    if n < 3:
        return 0.0

    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += points[i][0] * points[j][1]
        area -= points[j][0] * points[i][1]

    return abs(area) / 2.0


def _classify_outline(
    points: list[tuple[float, float]],
    width: float,
    height: float,
    area: float,
) -> str:
    """Classify outline shape type.

    Args:
        points: Outline polygon points
        width: Bounding box width
        height: Bounding box height
        area: Calculated polygon area

    Returns:
        "rectangle", "polygon", or "complex"
    """
    # Check if it's approximately rectangular
    # A rectangle's area equals width * height
    rect_area = width * height
    if rect_area > 0 and abs(area - rect_area) / rect_area < 0.01:
        # Less than 1% difference from rectangular area
        return "rectangle"

    # Check for simple polygon (4-8 vertices, no self-intersections)
    if 4 <= len(points) <= 8:
        return "polygon"

    return "complex"


def _extract_layers(pcb: PCB) -> LayerInfo:
    """Extract copper layer information.

    Args:
        pcb: Loaded PCB object

    Returns:
        LayerInfo with copper layer count and names
    """
    copper_layers = pcb.copper_layers
    layer_names = [layer.name for layer in copper_layers]

    # Check for internal planes (power/ground layers)
    has_internal_planes = any(layer.type == "power" for layer in copper_layers)

    return LayerInfo(
        copper_layers=len(copper_layers),
        layer_names=layer_names,
        has_internal_planes=has_internal_planes,
    )


def _extract_components(pcb: PCB) -> ComponentSummary:
    """Extract component summary statistics.

    Args:
        pcb: Loaded PCB object

    Returns:
        ComponentSummary with counts by type and placement status
    """
    footprints = list(pcb.footprints)

    total_count = 0
    smd_count = 0
    through_hole_count = 0
    fixed_count = 0
    unplaced_count = 0
    by_type: dict[str, int] = defaultdict(int)

    for fp in footprints:
        # Skip non-component footprints (logos, mounting holes without ref)
        if not fp.reference or fp.reference.startswith("#"):
            continue

        total_count += 1

        # Determine component type from reference prefix
        component_type = _classify_component_type(fp.reference, fp.name)
        by_type[component_type] += 1

        # Check if SMD or through-hole
        if fp.attr == "smd":
            smd_count += 1
        elif fp.attr == "through_hole":
            through_hole_count += 1
        else:
            # Infer from pads
            has_th_pads = any(p.type == "thru_hole" for p in fp.pads)
            has_smd_pads = any(p.type == "smd" for p in fp.pads)
            if has_th_pads and not has_smd_pads:
                through_hole_count += 1
            else:
                smd_count += 1

        # Check if placed (not at origin)
        x, y = fp.position
        if abs(x) < 0.1 and abs(y) < 0.1:
            unplaced_count += 1

    return ComponentSummary(
        total_count=total_count,
        smd_count=smd_count,
        through_hole_count=through_hole_count,
        by_type=dict(by_type),
        fixed_count=fixed_count,
        unplaced_count=unplaced_count,
    )


def _classify_component_type(reference: str, footprint_name: str) -> str:
    """Classify component type from reference designator and footprint.

    Args:
        reference: Component reference (e.g., "R1", "C5", "U3")
        footprint_name: Footprint library name

    Returns:
        Component type string (e.g., "resistor", "capacitor", "ic")
    """
    # Extract prefix from reference
    prefix_match = re.match(r"^([A-Za-z]+)", reference)
    if not prefix_match:
        return "other"

    prefix = prefix_match.group(1).upper()

    # Common reference designator mappings
    type_map = {
        "R": "resistor",
        "C": "capacitor",
        "L": "inductor",
        "D": "diode",
        "Q": "transistor",
        "U": "ic",
        "IC": "ic",
        "J": "connector",
        "P": "connector",
        "CON": "connector",
        "SW": "switch",
        "S": "switch",
        "F": "fuse",
        "FB": "ferrite_bead",
        "LED": "led",
        "LD": "led",
        "Y": "crystal",
        "X": "crystal",
        "XTAL": "crystal",
        "T": "transformer",
        "TR": "transformer",
        "K": "relay",
        "RY": "relay",
        "M": "motor",
        "TP": "test_point",
        "MH": "mounting_hole",
        "H": "mounting_hole",
        "BT": "battery",
        "BAT": "battery",
        "ANT": "antenna",
        "SP": "speaker",
        "LS": "speaker",
        "MIC": "microphone",
    }

    return type_map.get(prefix, "other")


def _extract_nets(pcb: PCB) -> NetSummary:
    """Extract net summary information.

    Args:
        pcb: Loaded PCB object

    Returns:
        NetSummary with routing status and power net identification
    """
    # Use NetStatusAnalyzer for accurate routing status
    analyzer = NetStatusAnalyzer(pcb)
    result = analyzer.analyze()

    # Identify power nets
    power_net_patterns = [
        r"^[+-]?\d+\.?\d*V",  # +3.3V, 5V, -12V
        r"^V(CC|DD|SS|EE|BAT|IN|OUT)",  # VCC, VDD, VSS, etc.
        r"^GND",  # GND, GNDPWR, etc.
        r"^AGND",  # Analog ground
        r"^DGND",  # Digital ground
        r"^PWR",  # Power nets
        r"^VBUS",  # USB power
    ]

    power_nets = []
    for net in pcb.nets.values():
        if net.number == 0:  # Skip unconnected net
            continue
        for pattern in power_net_patterns:
            if re.match(pattern, net.name, re.IGNORECASE):
                power_nets.append(net.name)
                break

    # Calculate net fanout (connections per net)
    net_connections: dict[int, int] = defaultdict(int)
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_number > 0:
                net_connections[pad.net_number] += 1

    # Find high fanout nets (>10 connections)
    high_fanout_nets = []
    for net_num, count in sorted(net_connections.items(), key=lambda x: -x[1]):
        if count > 10:
            net = pcb.get_net(net_num)
            if net:
                high_fanout_nets.append(NetFanout(net_name=net.name, connection_count=count))

    # Calculate routed vs unrouted
    routed_count = result.complete_count
    unrouted_count = result.incomplete_count + result.unrouted_count

    return NetSummary(
        total_nets=result.total_nets,
        routed_nets=routed_count,
        unrouted_nets=unrouted_count,
        power_nets=power_nets,
        high_fanout_nets=high_fanout_nets,
    )


def _extract_zones(pcb: PCB) -> list[ZoneInfo]:
    """Extract copper zone information.

    Args:
        pcb: Loaded PCB object

    Returns:
        List of ZoneInfo objects
    """
    zones = []
    for zone in pcb.zones:
        zones.append(
            ZoneInfo(
                net_name=zone.net_name,
                layer=zone.layer,
                priority=zone.priority,
                is_filled=zone.is_filled,
            )
        )
    return zones


def _compute_routing_status(pcb: PCB) -> RoutingStatus:
    """Compute overall routing status.

    Args:
        pcb: Loaded PCB object

    Returns:
        RoutingStatus with completion percentage and statistics
    """
    # Use NetStatusAnalyzer for accurate routing status
    analyzer = NetStatusAnalyzer(pcb)
    result = analyzer.analyze()

    # Calculate completion percentage
    if result.total_nets == 0:
        completion_percent = 100.0
    else:
        completion_percent = (result.complete_count / result.total_nets) * 100

    # Count airwires (unconnected pads)
    total_airwires = result.total_unconnected_pads

    # Get trace length and via count
    total_trace_length = pcb.total_trace_length()
    via_count = pcb.via_count

    return RoutingStatus(
        completion_percent=completion_percent,
        total_airwires=total_airwires,
        total_trace_length_mm=total_trace_length,
        via_count=via_count,
    )


# =============================================================================
# DRC Analysis Functions
# =============================================================================


def _generate_fix_suggestion(
    rule_id: str,
    required_value: float | None,
    actual_value: float | None,
) -> str | None:
    """Generate a fix suggestion for a violation.

    Args:
        rule_id: The rule that was violated
        required_value: The required value (if applicable)
        actual_value: The actual measured value (if applicable)

    Returns:
        Human-readable fix suggestion, or None
    """
    if required_value is None or actual_value is None:
        return None

    delta = required_value - actual_value

    if "clearance" in rule_id.lower():
        return f"Increase spacing by at least {delta:.3f}mm to meet minimum clearance"

    if "trace" in rule_id.lower() or "track" in rule_id.lower():
        return f"Increase track width to at least {required_value:.3f}mm"

    if "via" in rule_id.lower():
        if "drill" in rule_id.lower():
            return f"Increase via drill size to at least {required_value:.3f}mm"
        if "annular" in rule_id.lower():
            return "Increase via pad size to improve annular ring"
        return f"Increase via size to meet {required_value:.3f}mm minimum"

    if "edge" in rule_id.lower():
        return f"Move copper at least {required_value:.3f}mm from board edge"

    if "silk" in rule_id.lower():
        return "Move silkscreen element away from pads/copper"

    return f"Adjust to meet minimum of {required_value:.3f}mm"


def _convert_violation(
    violation,  # DRCViolation from validate.violations
    index: int,
) -> DRCViolation:
    """Convert internal DRCViolation to MCP DRCViolation.

    Args:
        violation: Internal DRCViolation object
        index: Index for generating unique ID

    Returns:
        MCP DRCViolation model
    """
    # Extract location
    location = ViolationLocation(x_mm=0.0, y_mm=0.0, layer="")
    if violation.location:
        location = ViolationLocation(
            x_mm=violation.location[0],
            y_mm=violation.location[1],
            layer=violation.layer or "",
        )

    # Extract affected items
    affected_items = []
    for item in violation.items:
        affected_items.append(
            AffectedItem(
                item_type="component",
                reference=item,
                net=None,
            )
        )

    # Generate fix suggestion
    fix_suggestion = _generate_fix_suggestion(
        violation.rule_id,
        violation.required_value,
        violation.actual_value,
    )

    return DRCViolation(
        id=f"drc-{index:04d}-{uuid.uuid4().hex[:8]}",
        type=violation.rule_id,
        severity=violation.severity,
        message=violation.message,
        location=location,
        affected_items=affected_items,
        fix_suggestion=fix_suggestion,
        required_value_mm=violation.required_value,
        actual_value_mm=violation.actual_value,
    )


def get_drc_violations(
    pcb_path: str,
    rules: str | None = None,
    severity_filter: Literal["all", "error", "warning"] = "all",
    layers: int = 4,
) -> DRCResult:
    """Run design rule check on PCB and return violations.

    Validates a PCB design against manufacturer-specific design rules
    and returns detailed violation information with locations and
    fix suggestions.

    Args:
        pcb_path: Absolute path to .kicad_pcb file
        rules: Manufacturer preset ("jlcpcb", "oshpark", "pcbway", "seeed")
               or path to custom rules file. Defaults to "jlcpcb".
        severity_filter: Filter by severity level:
                        "all" - return all violations
                        "error" - return only errors
                        "warning" - return only warnings
        layers: Number of PCB layers (2, 4, 6, etc.)

    Returns:
        DRCResult with violations, summary, and pass/fail status

    Raises:
        FileNotFoundError: If PCB file doesn't exist
        ParseError: If PCB file cannot be parsed
        ValueError: If manufacturer preset is not recognized

    Example:
        >>> result = get_drc_violations(
        ...     "/path/to/board.kicad_pcb",
        ...     rules="jlcpcb"
        ... )
        >>> if not result.passed:
        ...     for v in result.violations:
        ...         print(f"{v.type}: {v.message}")
    """
    # Validate path
    path = Path(pcb_path)
    if not path.exists():
        raise KiCadFileNotFoundError(f"PCB file not found: {pcb_path}")

    if path.suffix != ".kicad_pcb":
        raise ParseError(f"Invalid file extension: {path.suffix} (expected .kicad_pcb)")

    # Determine manufacturer
    manufacturer = rules or "jlcpcb"
    if manufacturer in MANUFACTURER_PRESETS:
        pass  # Use as-is
    elif Path(manufacturer).exists():
        # Custom rules file - not yet supported
        raise NotImplementedError("Custom rules files not yet supported")
    else:
        available = ", ".join(MANUFACTURER_PRESETS)
        raise ValueError(f"Unknown manufacturer preset: {manufacturer!r}. Available: {available}")

    # Load PCB
    pcb = PCB.load(path)

    # Run DRC
    from kicad_tools.validate import DRCChecker

    checker = DRCChecker(pcb, manufacturer=manufacturer, layers=layers)
    results = checker.check_all()

    # Convert violations to MCP format
    violations: list[DRCViolation] = []
    type_counts: Counter[str] = Counter()

    for i, v in enumerate(results.violations):
        # Apply severity filter
        if severity_filter == "error" and not v.is_error:
            continue
        if severity_filter == "warning" and v.is_error:
            continue

        mcp_violation = _convert_violation(v, i)
        violations.append(mcp_violation)
        type_counts[v.rule_id] += 1

    return DRCResult(
        passed=results.passed,
        violation_count=len(violations),
        error_count=results.error_count if severity_filter != "warning" else 0,
        warning_count=results.warning_count if severity_filter != "error" else 0,
        violations=violations,
        summary_by_type=dict(type_counts),
        manufacturer=manufacturer,
        layers=layers,
    )

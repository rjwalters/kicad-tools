"""MCP tool for analyzing KiCad PCB placement quality.

Provides the placement_analyze function that evaluates component placement
and returns actionable metrics for AI agent consumption.
"""

from __future__ import annotations

import math
from pathlib import Path

from kicad_tools.analysis.congestion import CongestionAnalyzer, Severity
from kicad_tools.analysis.signal_integrity import RiskLevel, SignalIntegrityAnalyzer
from kicad_tools.analysis.thermal import ThermalAnalyzer, ThermalSeverity
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.mcp.types import (
    PlacementAnalysis,
    PlacementCluster,
    PlacementIssue,
    PlacementScores,
    RoutingEstimate,
)
from kicad_tools.optim.clustering import ClusterDetector
from kicad_tools.optim.components import ClusterType, Component, Pin
from kicad_tools.schema.pcb import PCB


def placement_analyze(
    pcb_path: str,
    check_thermal: bool = True,
    check_signal_integrity: bool = True,
    check_manufacturing: bool = True,
) -> PlacementAnalysis:
    """Analyze current placement quality.

    Evaluates the component placement quality of a KiCad PCB file and
    returns comprehensive metrics including scores by category, identified
    issues, functional clusters, and routing difficulty estimates.

    Args:
        pcb_path: Absolute path to .kicad_pcb file
        check_thermal: Include thermal analysis (power components, heat spreading)
        check_signal_integrity: Include signal integrity hints (high-speed nets)
        check_manufacturing: Include DFM checks (clearances, assembly)

    Returns:
        PlacementAnalysis with overall score, category scores, issues,
        clusters, and routing estimate

    Raises:
        FileNotFoundError: If the PCB file does not exist
        ParseError: If the PCB file cannot be parsed (invalid format)
    """
    path = Path(pcb_path)
    if not path.exists():
        raise KiCadFileNotFoundError(f"PCB file not found: {pcb_path}")

    if path.suffix != ".kicad_pcb":
        raise ParseError(f"Invalid file extension: {path.suffix} (expected .kicad_pcb)")

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        raise ParseError(f"Failed to parse PCB file: {e}") from e

    issues: list[PlacementIssue] = []

    # Compute wire length score
    wire_length_score = _compute_wire_length_score(pcb)

    # Analyze congestion
    congestion_score, congestion_issues, congestion_hotspots, difficult_nets = _analyze_congestion(
        pcb
    )
    issues.extend(congestion_issues)

    # Analyze thermal (if enabled)
    if check_thermal:
        thermal_score, thermal_issues = _analyze_thermal(pcb)
        issues.extend(thermal_issues)
    else:
        thermal_score = 100.0  # Perfect score if not checked

    # Analyze signal integrity (if enabled)
    if check_signal_integrity:
        si_score, si_issues = _analyze_signal_integrity(pcb)
        issues.extend(si_issues)
    else:
        si_score = 100.0  # Perfect score if not checked

    # Analyze manufacturing/DFM (if enabled)
    if check_manufacturing:
        dfm_score, dfm_issues = _analyze_manufacturing(pcb)
        issues.extend(dfm_issues)
    else:
        dfm_score = 100.0  # Perfect score if not checked

    # Detect functional clusters
    clusters = _detect_clusters(pcb)

    # Compute overall score (weighted average)
    overall_score = _compute_overall_score(
        wire_length_score, congestion_score, thermal_score, si_score, dfm_score
    )

    # Estimate routability
    routing_estimate = RoutingEstimate(
        estimated_routability=_estimate_routability(congestion_score, wire_length_score),
        congestion_hotspots=congestion_hotspots,
        difficult_nets=difficult_nets,
    )

    # Sort issues by severity
    severity_order = {"critical": 0, "warning": 1, "suggestion": 2}
    issues.sort(key=lambda i: severity_order.get(i.severity, 3))

    return PlacementAnalysis(
        file_path=str(path.absolute()),
        overall_score=overall_score,
        categories=PlacementScores(
            wire_length=wire_length_score,
            congestion=congestion_score,
            thermal=thermal_score,
            signal_integrity=si_score,
            manufacturing=dfm_score,
        ),
        issues=issues,
        clusters=clusters,
        routing_estimate=routing_estimate,
    )


def _compute_wire_length_score(pcb: PCB) -> float:
    """Compute wire length score based on estimated total wire length.

    Uses Manhattan distance between connected pads as a proxy for
    expected routing length. Lower total wire length = higher score.

    Args:
        pcb: Loaded PCB object

    Returns:
        Wire length score (0-100, higher is better)
    """
    # Calculate total Manhattan distance for all nets
    total_manhattan = 0.0
    net_connections: dict[int, list[tuple[float, float]]] = {}

    # Collect pad positions by net
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_number > 0:
                if pad.net_number not in net_connections:
                    net_connections[pad.net_number] = []
                # Calculate absolute pad position
                pad_x = fp.position[0] + pad.position[0]
                pad_y = fp.position[1] + pad.position[1]
                net_connections[pad.net_number].append((pad_x, pad_y))

    # Calculate minimum spanning tree estimate using nearest neighbor
    for positions in net_connections.values():
        if len(positions) < 2:
            continue

        # Simple nearest neighbor heuristic for MST approximation
        remaining = list(positions[1:])
        current = positions[0]

        while remaining:
            # Find nearest unvisited position
            min_dist = float("inf")
            nearest_idx = 0
            for i, pos in enumerate(remaining):
                dist = abs(pos[0] - current[0]) + abs(pos[1] - current[1])
                if dist < min_dist:
                    min_dist = dist
                    nearest_idx = i

            total_manhattan += min_dist
            current = remaining.pop(nearest_idx)

    # Get board area for normalization
    outline = pcb.get_board_outline()
    if outline:
        min_x = min(p[0] for p in outline)
        max_x = max(p[0] for p in outline)
        min_y = min(p[1] for p in outline)
        max_y = max(p[1] for p in outline)
        board_diagonal = math.sqrt((max_x - min_x) ** 2 + (max_y - min_y) ** 2)
    else:
        board_diagonal = 100.0  # Default assumption

    # Normalize by component count and board size
    component_count = sum(
        1 for fp in pcb.footprints if fp.reference and not fp.reference.startswith("#")
    )
    if component_count == 0:
        return 100.0

    # Expected wire length scales with sqrt(components) * board_diagonal
    expected_wire_length = math.sqrt(component_count) * board_diagonal * 0.5
    if expected_wire_length == 0:
        return 100.0

    # Score: 100 if wire length is at expected, decreases as it gets worse
    ratio = total_manhattan / expected_wire_length
    score = max(0, min(100, 100 * (2 - ratio)))

    return score


def _analyze_congestion(
    pcb: PCB,
) -> tuple[float, list[PlacementIssue], list[tuple[float, float]], list[str]]:
    """Analyze routing congestion.

    Args:
        pcb: Loaded PCB object

    Returns:
        Tuple of (congestion_score, issues, hotspot_positions, difficult_net_names)
    """
    analyzer = CongestionAnalyzer()
    reports = analyzer.analyze(pcb)

    issues: list[PlacementIssue] = []
    hotspots: list[tuple[float, float]] = []
    difficult_nets: list[str] = []

    # Convert severity to score deduction
    severity_deductions = {
        Severity.CRITICAL: 25,
        Severity.HIGH: 15,
        Severity.MEDIUM: 8,
        Severity.LOW: 3,
    }

    total_deduction = 0
    for report in reports:
        deduction = severity_deductions.get(report.severity, 0)
        total_deduction += deduction

        hotspots.append(report.center)

        # Add nets from high-severity areas to difficult list
        if report.severity in (Severity.CRITICAL, Severity.HIGH):
            difficult_nets.extend(report.nets[:3])

        # Convert to PlacementIssue
        if report.severity == Severity.CRITICAL:
            severity = "critical"
        elif report.severity == Severity.HIGH:
            severity = "warning"
        else:
            severity = "suggestion"

        issues.append(
            PlacementIssue(
                severity=severity,
                category="routing",
                description=f"Congestion hotspot with {report.via_count} vias, density {report.track_density:.2f}mm/mm²",
                affected_components=report.components,
                location=report.center,
                suggestion=report.suggestions[0]
                if report.suggestions
                else "Spread components to reduce congestion",
            )
        )

    # Cap deduction at 100
    score = max(0, 100 - min(total_deduction, 100))

    # Remove duplicate nets
    difficult_nets = list(dict.fromkeys(difficult_nets))[:10]

    return score, issues, hotspots, difficult_nets


def _analyze_thermal(pcb: PCB) -> tuple[float, list[PlacementIssue]]:
    """Analyze thermal characteristics.

    Args:
        pcb: Loaded PCB object

    Returns:
        Tuple of (thermal_score, issues)
    """
    analyzer = ThermalAnalyzer()
    hotspots = analyzer.analyze(pcb)

    issues: list[PlacementIssue] = []

    # Convert severity to score deduction
    severity_deductions = {
        ThermalSeverity.CRITICAL: 30,
        ThermalSeverity.HOT: 15,
        ThermalSeverity.WARM: 5,
        ThermalSeverity.OK: 0,
    }

    total_deduction = 0
    for hotspot in hotspots:
        deduction = severity_deductions.get(hotspot.severity, 0)
        total_deduction += deduction

        if hotspot.severity == ThermalSeverity.OK:
            continue

        # Determine severity string
        if hotspot.severity == ThermalSeverity.CRITICAL:
            severity = "critical"
        elif hotspot.severity == ThermalSeverity.HOT:
            severity = "warning"
        else:
            severity = "suggestion"

        # Get affected component refs
        affected = [s.reference for s in hotspot.sources]

        issues.append(
            PlacementIssue(
                severity=severity,
                category="thermal",
                description=f"Thermal hotspot: {hotspot.total_power_w:.2f}W, estimated +{hotspot.max_temp_rise_c:.0f}°C rise",
                affected_components=affected,
                location=hotspot.position,
                suggestion=hotspot.suggestions[0]
                if hotspot.suggestions
                else "Add thermal vias or copper pour",
            )
        )

    score = max(0, 100 - min(total_deduction, 100))
    return score, issues


def _analyze_signal_integrity(pcb: PCB) -> tuple[float, list[PlacementIssue]]:
    """Analyze signal integrity concerns.

    Args:
        pcb: Loaded PCB object

    Returns:
        Tuple of (si_score, issues)
    """
    analyzer = SignalIntegrityAnalyzer()

    # Analyze crosstalk
    crosstalk_risks = analyzer.analyze_crosstalk(pcb)
    # Analyze impedance discontinuities
    impedance_issues = analyzer.analyze_impedance(pcb)

    issues: list[PlacementIssue] = []

    # Score deductions for crosstalk
    risk_deductions = {
        RiskLevel.HIGH: 20,
        RiskLevel.MEDIUM: 10,
        RiskLevel.LOW: 0,
    }

    total_deduction = 0

    for risk in crosstalk_risks:
        deduction = risk_deductions.get(risk.risk_level, 0)
        total_deduction += deduction

        if risk.risk_level == RiskLevel.LOW:
            continue

        severity = "warning" if risk.risk_level == RiskLevel.HIGH else "suggestion"

        issues.append(
            PlacementIssue(
                severity=severity,
                category="si",
                description=f"Crosstalk risk: {risk.aggressor_net} ↔ {risk.victim_net}, {risk.parallel_length_mm:.1f}mm parallel at {risk.spacing_mm:.2f}mm spacing",
                affected_components=[],  # Crosstalk is between nets, not specific components
                suggestion=risk.suggestion or "Increase trace spacing or add guard traces",
            )
        )

    # Score deductions for impedance issues
    for disc in impedance_issues:
        if disc.mismatch_percent < 15:
            continue

        if disc.mismatch_percent >= 25:
            severity = "warning"
            total_deduction += 10
        else:
            severity = "suggestion"
            total_deduction += 5

        issues.append(
            PlacementIssue(
                severity=severity,
                category="si",
                description=f"Impedance discontinuity on {disc.net}: {disc.mismatch_percent:.0f}% mismatch ({disc.cause})",
                affected_components=[],
                location=disc.position,
                suggestion=disc.suggestion,
            )
        )

    score = max(0, 100 - min(total_deduction, 100))
    return score, issues


def _analyze_manufacturing(pcb: PCB) -> tuple[float, list[PlacementIssue]]:
    """Analyze manufacturing/DFM concerns.

    Checks for:
    - Components placed at origin (unplaced)
    - Component overlap (simplified check)
    - Edge clearance

    Args:
        pcb: Loaded PCB object

    Returns:
        Tuple of (dfm_score, issues)
    """
    issues: list[PlacementIssue] = []
    total_deduction = 0

    # Get board outline for edge clearance check
    outline = pcb.get_board_outline()
    if outline:
        min_x = min(p[0] for p in outline)
        max_x = max(p[0] for p in outline)
        min_y = min(p[1] for p in outline)
        max_y = max(p[1] for p in outline)
    else:
        min_x, max_x, min_y, max_y = 0, 100, 0, 100

    # Check each component
    unplaced_components: list[str] = []
    edge_clearance_issues: list[str] = []

    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue

        x, y = fp.position

        # Check if at origin (likely unplaced)
        if abs(x) < 0.1 and abs(y) < 0.1:
            unplaced_components.append(fp.reference)

        # Check edge clearance (1mm minimum)
        edge_margin = 1.0
        if (
            x < min_x + edge_margin
            or x > max_x - edge_margin
            or y < min_y + edge_margin
            or y > max_y - edge_margin
        ):
            edge_clearance_issues.append(fp.reference)

    # Unplaced components issue
    if unplaced_components:
        total_deduction += min(len(unplaced_components) * 10, 50)
        issues.append(
            PlacementIssue(
                severity="critical" if len(unplaced_components) > 3 else "warning",
                category="dfm",
                description=f"{len(unplaced_components)} component(s) at origin (likely unplaced)",
                affected_components=unplaced_components[:10],
                suggestion="Move components from origin to valid board positions",
            )
        )

    # Edge clearance issues
    if edge_clearance_issues:
        total_deduction += min(len(edge_clearance_issues) * 5, 30)
        issues.append(
            PlacementIssue(
                severity="warning" if len(edge_clearance_issues) > 3 else "suggestion",
                category="dfm",
                description=f"{len(edge_clearance_issues)} component(s) too close to board edge",
                affected_components=edge_clearance_issues[:10],
                suggestion="Move components at least 1mm from board edge for manufacturing",
            )
        )

    score = max(0, 100 - min(total_deduction, 100))
    return score, issues


def _detect_clusters(pcb: PCB) -> list[PlacementCluster]:
    """Detect functional component clusters and evaluate compactness.

    Args:
        pcb: Loaded PCB object

    Returns:
        List of detected clusters with compactness scores
    """
    # Convert PCB footprints to Component objects for ClusterDetector
    components: list[Component] = []
    footprint_positions: dict[str, tuple[float, float]] = {}

    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue

        footprint_positions[fp.reference] = fp.position

        pins: list[Pin] = []
        for pad in fp.pads:
            net_name = pcb.get_net(pad.net_number).name if pcb.get_net(pad.net_number) else ""
            pins.append(
                Pin(
                    number=pad.number,
                    x=fp.position[0] + pad.position[0],
                    y=fp.position[1] + pad.position[1],
                    net=pad.net_number,
                    net_name=net_name,
                )
            )

        components.append(
            Component(
                ref=fp.reference,
                x=fp.position[0],
                y=fp.position[1],
                pins=pins,
            )
        )

    if not components:
        return []

    # Detect clusters
    detector = ClusterDetector(components)
    functional_clusters = detector.detect_power_clusters()
    functional_clusters.extend(detector.detect_timing_clusters())
    functional_clusters.extend(detector.detect_interface_clusters())

    # Convert to PlacementCluster with compactness scores
    result: list[PlacementCluster] = []

    cluster_type_names = {
        ClusterType.POWER: "power_cluster",
        ClusterType.TIMING: "timing_cluster",
        ClusterType.INTERFACE: "interface_cluster",
        ClusterType.DRIVER: "driver_cluster",
    }

    for i, cluster in enumerate(functional_clusters):
        # Get positions of all components in cluster
        positions: list[tuple[float, float]] = []
        for ref in cluster.all_components:
            if ref in footprint_positions:
                positions.append(footprint_positions[ref])

        if len(positions) < 2:
            continue

        # Calculate centroid
        cx = sum(p[0] for p in positions) / len(positions)
        cy = sum(p[1] for p in positions) / len(positions)

        # Calculate compactness (inverse of average distance from centroid)
        avg_distance = sum(math.sqrt((p[0] - cx) ** 2 + (p[1] - cy) ** 2) for p in positions) / len(
            positions
        )

        # Normalize compactness: 100 if all within max_distance_mm, decreasing as spread increases
        max_dist = cluster.max_distance_mm
        compactness = max(0, min(100, 100 * (1 - avg_distance / (max_dist * 2))))

        # Generate cluster name
        base_name = cluster_type_names.get(cluster.cluster_type, "cluster")
        name = f"{base_name}_{cluster.anchor}"

        result.append(
            PlacementCluster(
                name=name,
                components=cluster.all_components,
                centroid=(cx, cy),
                compactness_score=compactness,
            )
        )

    return result


def _compute_overall_score(
    wire_length: float,
    congestion: float,
    thermal: float,
    si: float,
    dfm: float,
) -> float:
    """Compute weighted overall placement score.

    Args:
        wire_length: Wire length score (0-100)
        congestion: Congestion score (0-100)
        thermal: Thermal score (0-100)
        si: Signal integrity score (0-100)
        dfm: Manufacturing score (0-100)

    Returns:
        Overall score (0-100)
    """
    # Weights: routing-related scores are most important
    weights = {
        "wire_length": 0.25,
        "congestion": 0.25,
        "thermal": 0.15,
        "si": 0.20,
        "dfm": 0.15,
    }

    return (
        wire_length * weights["wire_length"]
        + congestion * weights["congestion"]
        + thermal * weights["thermal"]
        + si * weights["si"]
        + dfm * weights["dfm"]
    )


def _estimate_routability(congestion_score: float, wire_length_score: float) -> float:
    """Estimate routing difficulty based on placement metrics.

    Args:
        congestion_score: Congestion analysis score (0-100)
        wire_length_score: Wire length score (0-100)

    Returns:
        Routability estimate (0-100, higher is easier to route)
    """
    # Routability is primarily determined by congestion and wire length
    return 0.6 * congestion_score + 0.4 * wire_length_score

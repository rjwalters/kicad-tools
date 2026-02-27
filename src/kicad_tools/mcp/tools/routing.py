"""MCP tools for routing operations.

Provides tools for querying unrouted nets and routing individual nets.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Literal

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.mcp.types import (
    NetRoutingStatus,
    RouteNetResult,
    UnroutedNetsResult,
)
from kicad_tools.schema.pcb import PCB

logger = logging.getLogger(__name__)


def get_unrouted_nets(
    pcb_path: str,
    include_partial: bool = True,
) -> UnroutedNetsResult:
    """List nets that need routing.

    Analyzes a PCB file to identify nets that are unrouted or partially
    routed. Provides difficulty estimates and routing recommendations.

    Args:
        pcb_path: Absolute path to .kicad_pcb file
        include_partial: Include partially routed nets in the results.
                        If False, only completely unrouted nets are returned.

    Returns:
        UnroutedNetsResult with net details including routing status,
        difficulty estimates, and recommendations.

    Raises:
        FileNotFoundError: If the PCB file does not exist
        ParseError: If the PCB file cannot be parsed (invalid format)

    Example:
        >>> result = get_unrouted_nets("/path/to/board.kicad_pcb")
        >>> for net in result.nets:
        ...     print(f"{net.name}: {net.status} ({net.difficulty})")
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

    # Use NetStatusAnalyzer for accurate routing status
    analyzer = NetStatusAnalyzer(pcb)
    result = analyzer.analyze()

    # Build pad position map for distance calculations
    pad_positions = _build_pad_positions(pcb)

    # Collect nets needing routing
    nets: list[NetRoutingStatus] = []
    unrouted_count = 0
    partial_count = 0
    complete_count = 0

    for net_status in result.nets:
        if net_status.status == "complete":
            complete_count += 1
            continue

        if net_status.status == "unrouted":
            unrouted_count += 1
        elif net_status.status == "incomplete":
            partial_count += 1
            if not include_partial:
                continue

        # Calculate estimated length and difficulty
        net_pads = pad_positions.get(net_status.net_number, [])
        estimated_length = _estimate_routing_length(net_pads)
        difficulty, reason = _estimate_difficulty(net_status, net_pads, pcb)

        # Total connections needed = pins - 1 (minimum spanning tree)
        total_connections = max(0, net_status.total_pads - 1)
        routed_connections = net_status.connected_count - 1 if net_status.connected_count > 1 else 0

        nets.append(
            NetRoutingStatus(
                name=net_status.net_name,
                status=net_status.status,
                pins=net_status.total_pads,
                routed_connections=max(0, routed_connections),
                total_connections=total_connections,
                estimated_length_mm=estimated_length,
                difficulty=difficulty,
                reason=reason if difficulty != "easy" else None,
            )
        )

    # Sort by difficulty (hard first), then by name
    difficulty_order = {"hard": 0, "medium": 1, "easy": 2}
    nets.sort(key=lambda n: (difficulty_order.get(n.difficulty, 3), n.name))

    return UnroutedNetsResult(
        total_nets=result.total_nets,
        unrouted_count=unrouted_count,
        partial_count=partial_count,
        complete_count=complete_count,
        nets=nets,
    )


def route_net(
    pcb_path: str,
    net_name: str,
    output_path: str | None = None,
    strategy: Literal["auto", "shortest", "avoid_vias"] = "auto",
    layer_preference: str | None = None,
) -> RouteNetResult:
    """Route a specific net.

    Attempts to route all unconnected pads on the specified net using
    the autorouter. The result can be saved to a new file or overwrite
    the original.

    Args:
        pcb_path: Absolute path to .kicad_pcb file
        net_name: Name of the net to route (e.g., "GND", "SPI_CLK")
        output_path: Path for output file. If None, overwrites the original.
        strategy: Routing strategy to use:
                  - "auto": Automatically choose best strategy
                  - "shortest": Minimize trace length
                  - "avoid_vias": Prefer single-layer routing
        layer_preference: Preferred layer for routing (e.g., "F.Cu", "B.Cu").
                         If None, router chooses optimal layer.

    Returns:
        RouteNetResult with routing details including success status,
        trace length, vias used, and any suggestions if routing failed.

    Raises:
        FileNotFoundError: If the PCB file does not exist
        ParseError: If the PCB file cannot be parsed
        ValueError: If the net name is not found in the design

    Example:
        >>> result = route_net("/path/to/board.kicad_pcb", "SPI_CLK")
        >>> if result.success:
        ...     print(f"Routed {result.trace_length_mm}mm of trace")
        ... else:
        ...     print(f"Failed: {result.error_message}")
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

    # Find the net
    net_number = None
    for num, net in pcb.nets.items():
        if net.name == net_name:
            net_number = num
            break

    if net_number is None:
        raise ValueError(f"Net '{net_name}' not found in design")

    # Get current net status
    analyzer = NetStatusAnalyzer(pcb)
    status_result = analyzer.analyze()
    net_status = status_result.get_net(net_name)

    if net_status is None:
        raise ValueError(f"Net '{net_name}' not found in design")

    # Check if already fully routed
    if net_status.status == "complete":
        return RouteNetResult(
            success=True,
            net_name=net_name,
            routed_connections=max(0, net_status.connected_count - 1),
            total_connections=max(0, net_status.total_pads - 1),
            trace_length_mm=_measure_existing_trace_length(pcb, net_number),
            vias_used=_count_vias_on_net(pcb, net_number),
            layers_used=_get_layers_used(pcb, net_number),
            output_path=output_path or pcb_path,
            suggestions=["Net is already fully routed"],
        )

    # Import router components
    try:
        from kicad_tools.router import Autorouter
        from kicad_tools.router.io import (
            merge_routes_into_pcb,
            parse_pcb_design_rules,
        )
    except ImportError as e:
        return RouteNetResult(
            success=False,
            net_name=net_name,
            error_message=f"Router module not available: {e}",
            suggestions=["Ensure kicad_tools router module is installed"],
        )

    # Extract design rules from PCB
    pcb_text = path.read_text()
    pcb_rules = parse_pcb_design_rules(pcb_text)
    design_rules = pcb_rules.to_design_rules()

    # Get board dimensions
    outline = pcb.get_board_outline()
    if not outline:
        return RouteNetResult(
            success=False,
            net_name=net_name,
            error_message="Could not determine board outline",
            suggestions=["Add Edge.Cuts outline to the board"],
        )

    min_x = min(p[0] for p in outline)
    max_x = max(p[0] for p in outline)
    min_y = min(p[1] for p in outline)
    max_y = max(p[1] for p in outline)
    board_width = max_x - min_x
    board_height = max_y - min_y

    # Configure router based on strategy
    if strategy == "avoid_vias":
        design_rules.cost_via = 1000.0  # Heavy penalty for vias
    elif strategy == "shortest":
        design_rules.cost_via = 1.0  # Low via cost to prioritize shortest path

    # Create autorouter
    router = Autorouter(
        width=board_width,
        height=board_height,
        origin_x=min_x,
        origin_y=min_y,
        rules=design_rules,
    )

    # Collect pads for the specific net, grouped by component
    from kicad_tools.router.layers import Layer

    component_pads: dict[str, list[dict]] = defaultdict(list)
    net_pads: list[dict] = []

    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue

        fp_x, fp_y = fp.position
        rotation = fp.rotation
        rot_rad = math.radians(-rotation)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        for pad in fp.pads:
            if pad.net_number == net_number:
                # Transform pad position to board coordinates
                px, py = pad.position
                rx = px * cos_r - py * sin_r
                ry = px * sin_r + py * cos_r

                # Determine layer from pad layers
                pad_layer = Layer.F_CU
                if (
                    layer_preference == "B.Cu"
                    or "B.Cu" in (pad.layers or [])
                    and "F.Cu" not in (pad.layers or [])
                ):
                    pad_layer = Layer.B_CU

                # Check if through-hole
                is_through_hole = "*.Cu" in (pad.layers or [])

                pad_info = {
                    "number": pad.number,
                    "x": fp_x + rx,
                    "y": fp_y + ry,
                    "width": pad.size[0] if pad.size else 0.5,
                    "height": pad.size[1] if pad.size else 0.5,
                    "net": net_number,
                    "net_name": net_name,
                    "layer": pad_layer,
                    "through_hole": is_through_hole,
                }
                component_pads[fp.reference].append(pad_info)
                net_pads.append(pad_info)

    if len(net_pads) < 2:
        return RouteNetResult(
            success=True,
            net_name=net_name,
            routed_connections=0,
            total_connections=0,
            output_path=output_path or pcb_path,
            suggestions=["Net has fewer than 2 pads, no routing needed"],
        )

    # Add components to router
    for ref, pads in component_pads.items():
        router.add_component(ref, pads)

    # Attempt to route the net
    try:
        routes = router.route_net(net_number)
    except Exception as e:
        return RouteNetResult(
            success=False,
            net_name=net_name,
            error_message=f"Routing failed: {e}",
            suggestions=_generate_suggestions(net_status, net_pads, pcb),
        )

    # Calculate results
    if routes:
        # Calculate trace length and count vias
        trace_length = 0.0
        vias_count = 0
        layers_used: set[str] = set()

        for route in routes:
            for seg in route.segments:
                trace_length += math.sqrt((seg.x2 - seg.x1) ** 2 + (seg.y2 - seg.y1) ** 2)

                layer_name = "F.Cu" if seg.layer == Layer.F_CU else "B.Cu"
                layers_used.add(layer_name)

            vias_count += len(route.vias)

        # Merge the routed traces into the PCB
        try:
            merge_routes_into_pcb(
                pcb,
                routes,
                net_map={net_name: net_number},
                trace_width=design_rules.trace_width,
                via_diameter=design_rules.via_diameter,
                via_drill=design_rules.via_drill,
            )

            # Save the result
            save_path = output_path or pcb_path
            pcb.save(save_path)

            return RouteNetResult(
                success=True,
                net_name=net_name,
                routed_connections=len(routes),
                total_connections=max(0, len(net_pads) - 1),
                trace_length_mm=trace_length,
                vias_used=vias_count,
                layers_used=sorted(layers_used),
                output_path=save_path,
            )
        except Exception as e:
            return RouteNetResult(
                success=False,
                net_name=net_name,
                routed_connections=len(routes),
                total_connections=max(0, len(net_pads) - 1),
                error_message=f"Failed to save routed PCB: {e}",
                suggestions=["Check file permissions", "Try specifying a different output_path"],
            )
    else:
        # Routing failed
        return RouteNetResult(
            success=False,
            net_name=net_name,
            total_connections=max(0, len(net_pads) - 1),
            error_message="Autorouter could not find a valid path",
            suggestions=_generate_suggestions(net_status, net_pads, pcb),
        )


def route_net_auto(
    pcb_path: str,
    net_name: str,
    output_path: str | None = None,
    strategy: str = "auto",
    enable_repair: bool = True,
    enable_via_resolution: bool = True,
) -> dict:
    """Route a specific net using the RoutingOrchestrator.

    Uses smart strategy selection via RoutingOrchestrator rather than the
    simple Autorouter used by route_net(). The orchestrator analyzes net
    characteristics (pin pitch, differential pairs, density, via conflicts)
    and automatically selects the optimal routing strategy.

    Args:
        pcb_path: Absolute path to .kicad_pcb file
        net_name: Name of the net to route (e.g., "GND", "SPI_CLK")
        output_path: Path for output file. If None, result is not saved.
        strategy: Strategy override ("auto", "global", "escape", "hierarchical",
                  "subgrid", or "via_resolution"). Use "auto" for smart selection.
        enable_repair: Whether to enable automatic clearance repair after routing
        enable_via_resolution: Whether to enable via conflict resolution

    Returns:
        Dictionary with routing result including:
        - success: Whether routing succeeded
        - net_name: Name of the net routed
        - strategy_used: Name of the strategy that was applied
        - metrics: Quantitative metrics (length, vias, layer changes, repairs)
        - repair_actions: List of repairs applied
        - warnings: Non-fatal warnings
        - performance: Timing breakdown
        - error_message: Error description if success is False
        - alternative_strategies: Suggestions if routing failed

    Raises:
        FileNotFoundError: If the PCB file does not exist
        ParseError: If the PCB file cannot be parsed
        ValueError: If the net name is not found in the design

    Example:
        >>> result = route_net_auto("/path/to/board.kicad_pcb", "SPI_CLK")
        >>> if result["success"]:
        ...     print(f"Routed with strategy: {result['strategy_used']}")
        ...     print(f"Total length: {result['metrics']['total_length_mm']:.2f}mm")
        ... else:
        ...     print(f"Failed: {result['error_message']}")
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

    # Find the net
    net_number = None
    for num, net in pcb.nets.items():
        if net.name == net_name:
            net_number = num
            break

    if net_number is None:
        raise ValueError(f"Net '{net_name}' not found in design")

    # Import router components
    try:
        from kicad_tools.router.io import parse_pcb_design_rules
        from kicad_tools.router.orchestrator import RoutingOrchestrator
        from kicad_tools.router.strategies import RoutingStrategy
    except ImportError as e:
        return {
            "success": False,
            "net_name": net_name,
            "error_message": f"Router module not available: {e}",
            "strategy_used": "unknown",
            "metrics": {},
            "repair_actions": [],
            "warnings": ["Ensure kicad_tools router module is installed"],
            "performance": {},
            "alternative_strategies": [],
        }

    # Extract design rules from PCB
    pcb_text = path.read_text()
    pcb_rules = parse_pcb_design_rules(pcb_text)
    design_rules = pcb_rules.to_design_rules()

    # Build a lightweight PCB-like object for the orchestrator
    # The orchestrator needs pcb.width, pcb.height, and optionally pcb.grid
    outline = pcb.get_board_outline()
    if outline:
        min_x = min(p[0] for p in outline)
        max_x = max(p[0] for p in outline)
        min_y = min(p[1] for p in outline)
        max_y = max(p[1] for p in outline)
        board_width = max_x - min_x
        board_height = max_y - min_y
    else:
        board_width = 100.0
        board_height = 100.0

    # Attach dimensions to the pcb object for orchestrator use
    pcb.width = board_width  # type: ignore[attr-defined]
    pcb.height = board_height  # type: ignore[attr-defined]
    pcb.path = path  # type: ignore[attr-defined]

    # Resolve strategy override
    strategy_map = {
        "global": RoutingStrategy.GLOBAL_WITH_REPAIR,
        "escape": RoutingStrategy.ESCAPE_THEN_GLOBAL,
        "hierarchical": RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
        "subgrid": RoutingStrategy.SUBGRID_ADAPTIVE,
        "via_resolution": RoutingStrategy.VIA_CONFLICT_RESOLUTION,
    }

    # Create orchestrator
    orchestrator = RoutingOrchestrator(
        pcb=pcb,  # type: ignore[arg-type]
        rules=design_rules,
        enable_repair=enable_repair,
        enable_via_conflict_resolution=enable_via_resolution,
    )

    # If a strategy override is requested, patch the strategy selection
    if strategy != "auto" and strategy in strategy_map:
        forced_strategy = strategy_map[strategy]

        def _forced_select(net, intent, pads):
            return forced_strategy

        orchestrator._select_strategy = _forced_select  # type: ignore[method-assign]

    # Route the net
    result = orchestrator.route_net(net=net_name)

    # Convert to dict with net_name included
    result_dict = result.to_dict()
    result_dict["net_name"] = net_name

    # Save output if requested and routing succeeded
    if output_path and result.success:
        try:
            pcb.save(output_path)
            result_dict["output_path"] = output_path
        except Exception as e:
            result_dict["warnings"] = result_dict.get("warnings", []) + [
                f"Routing succeeded but save failed: {e}"
            ]

    return result_dict


def _build_pad_positions(pcb: PCB) -> dict[int, list[tuple[float, float]]]:
    """Build a map of net numbers to pad positions.

    Args:
        pcb: Loaded PCB object

    Returns:
        Dict mapping net numbers to lists of (x, y) positions
    """
    positions: dict[int, list[tuple[float, float]]] = defaultdict(list)

    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue

        fp_x, fp_y = fp.position
        rotation = fp.rotation
        rot_rad = math.radians(-rotation)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        for pad in fp.pads:
            if pad.net_number > 0:
                px, py = pad.position
                rx = px * cos_r - py * sin_r
                ry = px * sin_r + py * cos_r
                positions[pad.net_number].append((fp_x + rx, fp_y + ry))

    return positions


def _estimate_routing_length(pad_positions: list[tuple[float, float]]) -> float:
    """Estimate minimum routing length using minimum spanning tree approximation.

    Args:
        pad_positions: List of (x, y) pad positions

    Returns:
        Estimated routing length in millimeters
    """
    if len(pad_positions) < 2:
        return 0.0

    # Simple approximation: sum of distances in a chain
    # This is an upper bound for MST
    total = 0.0
    remaining = list(pad_positions[1:])
    current = pad_positions[0]

    while remaining:
        # Find closest remaining pad
        min_dist = float("inf")
        min_idx = 0
        for i, pos in enumerate(remaining):
            dist = math.sqrt((pos[0] - current[0]) ** 2 + (pos[1] - current[1]) ** 2)
            if dist < min_dist:
                min_dist = dist
                min_idx = i

        total += min_dist
        current = remaining.pop(min_idx)

    return total


def _estimate_difficulty(
    net_status,
    pad_positions: list[tuple[float, float]],
    pcb: PCB,
) -> tuple[str, str | None]:
    """Estimate routing difficulty for a net.

    Args:
        net_status: NetStatus object from analyzer
        pad_positions: List of (x, y) pad positions
        pcb: Loaded PCB object

    Returns:
        Tuple of (difficulty, reason) where difficulty is "easy", "medium", or "hard"
    """
    if len(pad_positions) < 2:
        return "easy", None

    # Calculate bounding box and distances
    min_x = min(p[0] for p in pad_positions)
    max_x = max(p[0] for p in pad_positions)
    min_y = min(p[1] for p in pad_positions)
    max_y = max(p[1] for p in pad_positions)

    span_x = max_x - min_x
    span_y = max_y - min_y
    max_span = max(span_x, span_y)

    # Check for power nets (often need planes, not traces)
    power_patterns = ["GND", "VCC", "VDD", "VSS", "+", "-", "VBUS", "PWR"]
    is_power = any(p in net_status.net_name.upper() for p in power_patterns)
    if is_power and len(pad_positions) > 4:
        return "hard", "Power net with many connections - consider using copper pour"

    # Check for long distances
    if max_span > 50:
        return "hard", "Long routing distance"
    elif max_span > 25:
        return "medium", "Moderate routing distance"

    # Check for high fanout
    if len(pad_positions) > 8:
        return "hard", f"High fanout net ({len(pad_positions)} pins)"
    elif len(pad_positions) > 5:
        return "medium", f"Multiple connections ({len(pad_positions)} pins)"

    # Check for differential pair patterns
    diff_patterns = ["_P", "_N", "+", "-", "DP", "DM", "D+", "D-"]
    if any(net_status.net_name.endswith(p) for p in diff_patterns):
        return "medium", "Differential pair - length matching may be needed"

    # Check for clock/high-speed patterns
    clock_patterns = ["CLK", "CLOCK", "SCK", "SCLK"]
    if any(p in net_status.net_name.upper() for p in clock_patterns):
        return "medium", "Clock signal - routing length may be important"

    return "easy", None


def _generate_suggestions(net_status, net_pads: list[dict], pcb: PCB) -> list[str]:
    """Generate suggestions for failed routing.

    Args:
        net_status: NetStatus object from analyzer
        net_pads: List of pad info dicts
        pcb: Loaded PCB object

    Returns:
        List of actionable suggestions
    """
    suggestions = []

    # Check for obstacles
    if len(net_pads) > 2:
        suggestions.append("Consider routing in segments (partial routing)")

    # Check for congested areas
    suggestions.append("Check for component placement conflicts")

    # Power net suggestions
    power_patterns = ["GND", "VCC", "VDD", "VSS"]
    if any(p in net_status.net_name.upper() for p in power_patterns):
        suggestions.append("Consider using copper pour for this power net")
        suggestions.append("Use vias to connect to internal power plane")

    # General suggestions
    suggestions.append("Try adjusting layer_preference parameter")
    suggestions.append("Manual routing may be required for complex paths")

    return suggestions


def _measure_existing_trace_length(pcb: PCB, net_number: int) -> float:
    """Measure total trace length for a net.

    Args:
        pcb: Loaded PCB object
        net_number: Net number to measure

    Returns:
        Total trace length in millimeters
    """
    total = 0.0
    for seg in pcb.segments_in_net(net_number):
        dx = seg.end[0] - seg.start[0]
        dy = seg.end[1] - seg.start[1]
        total += math.sqrt(dx * dx + dy * dy)
    return total


def _count_vias_on_net(pcb: PCB, net_number: int) -> int:
    """Count vias on a net.

    Args:
        pcb: Loaded PCB object
        net_number: Net number to count

    Returns:
        Number of vias on the net
    """
    return len(list(pcb.vias_in_net(net_number)))


def _get_layers_used(pcb: PCB, net_number: int) -> list[str]:
    """Get list of layers used by a net.

    Args:
        pcb: Loaded PCB object
        net_number: Net number to check

    Returns:
        List of layer names with traces or vias
    """
    layers: set[str] = set()

    for seg in pcb.segments_in_net(net_number):
        layers.add(seg.layer)

    for via in pcb.vias_in_net(net_number):
        layers.update(via.layers)

    return sorted(layers)

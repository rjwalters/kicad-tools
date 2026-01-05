"""
PCB file I/O for autorouting.

This module provides:
- route_pcb: Route a PCB given component placements and net assignments
- load_pcb_for_routing: Load a KiCad PCB file and create an Autorouter
- merge_routes_into_pcb: Merge routed traces into an existing PCB file
- generate_netclass_setup: Generate KiCad 7+ compatible net class setup
- parse_pcb_design_rules: Extract design rules from a KiCad PCB file
- validate_grid_resolution: Check grid resolution vs clearance for DRC compliance
- validate_routes: Post-route validation for clearance issues

These functions handle the translation between KiCad file formats and
the autorouter's internal representations.

Note on net class metadata:
    Generated routes embed trace widths and via sizes directly in their
    S-expressions, so net class metadata is NOT required for the routing
    to work correctly. The generate_netclass_setup() function is provided
    for users who want to add net class definitions for documentation or
    DRC purposes, using the KiCad 7+ compatible format.

    DO NOT use the old KiCad 6 format with (net_settings (net_class ...))
    as this is incompatible with KiCad 7+.

DRC Compliance (v0.5.1):
    For DRC-clean output, ensure:
    1. Grid resolution <= clearance / 2 (use validate_grid_resolution)
    2. Via sizes meet PCB minimums (use parse_pcb_design_rules)
    3. Post-route validation (use validate_routes)
"""

from __future__ import annotations

import math
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

from .core import Autorouter
from .layers import Layer, LayerDefinition, LayerStack, LayerType
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules

# =============================================================================
# DRC COMPLIANCE TYPES AND FUNCTIONS
# =============================================================================


@dataclass
class PCBDesignRules:
    """Design rules extracted from a KiCad PCB file's setup section.

    These represent the board's actual constraints and should be used
    to configure the router for DRC-compliant output.
    """

    # Track constraints
    min_track_width: float = 0.2  # mm
    # Via constraints
    min_via_diameter: float = 0.6  # mm
    min_via_drill: float = 0.3  # mm
    # Clearances
    min_clearance: float = 0.2  # mm
    # Copper to edge
    copper_edge_clearance: float = 0.3  # mm

    def to_design_rules(
        self,
        grid_resolution: float | None = None,
    ) -> DesignRules:
        """Convert to DesignRules for the router.

        Args:
            grid_resolution: Override grid resolution. If None, uses
                            clearance / 2 for DRC compliance.

        Returns:
            DesignRules configured with these constraints.
        """
        # Default to clearance / 2 for DRC compliance
        if grid_resolution is None:
            grid_resolution = self.min_clearance / 2

        return DesignRules(
            trace_width=self.min_track_width,
            trace_clearance=self.min_clearance,
            via_drill=self.min_via_drill,
            via_diameter=self.min_via_diameter,
            via_clearance=self.min_clearance,
            grid_resolution=grid_resolution,
        )


@dataclass
class ClearanceViolation:
    """A potential clearance violation detected during post-route validation."""

    segment_index: int
    x1: float
    y1: float
    x2: float
    y2: float
    net: int
    obstacle_type: str  # "pad", "via", "segment"
    obstacle_net: int
    distance: float  # Actual distance in mm
    required: float  # Required clearance in mm


def parse_pcb_design_rules(pcb_text: str) -> PCBDesignRules:
    """Parse design rules from a KiCad PCB file's setup section.

    Extracts via/track minimums from the (setup ...) section of a KiCad PCB.
    This allows the router to respect the board's actual constraints.

    KiCad 7+ stores design rules in these locations:
    - (setup (pad_to_mask_clearance X)) - pad mask clearance
    - (setup (min_via_annular_width X)) - minimum via annular ring
    - Net class definitions for track widths and clearances

    Args:
        pcb_text: Contents of a .kicad_pcb file

    Returns:
        PCBDesignRules with extracted or default values.

    Example:
        >>> pcb_text = Path("board.kicad_pcb").read_text()
        >>> rules = parse_pcb_design_rules(pcb_text)
        >>> print(f"Min track: {rules.min_track_width}mm")
    """
    rules = PCBDesignRules()

    # Track whether we've found values from the PCB (vs using defaults)
    found_clearance = False
    found_track_width = False
    found_via_diameter = False
    found_via_drill = False

    # Extract setup section (optional - may not exist or be empty)
    setup_match = re.search(r"\(setup\s+(.*?)\n\s*\)", pcb_text, re.DOTALL)
    if setup_match:
        setup_text = setup_match.group(1)

        # Parse pad_to_mask_clearance (often indicates minimum clearance)
        mask_match = re.search(r"\(pad_to_mask_clearance\s+([\d.]+)\)", setup_text)
        if mask_match:
            mask_clearance = float(mask_match.group(1))
            if mask_clearance > 0:
                # Use as hint for clearance, but not definitive
                pass

        # Parse min_via_annular_width if present
        via_ann_match = re.search(r"\(min_via_annular_width\s+([\d.]+)\)", setup_text)
        if via_ann_match:
            ann_width = float(via_ann_match.group(1))
            # Via diameter = drill + 2 * annular width
            # We'll use this to calculate minimum via diameter
            pass

    # Look for net class definitions (KiCad 7+ format)
    # Note: These can exist even without a setup section
    # These are typically in the form: (net_class "Default" ...)
    # with (clearance X), (trace_width X), (via_dia X), (via_drill X)
    # Net class blocks can span multiple lines

    # Find all net_class blocks using bracket matching
    for nc_match in re.finditer(r'\(net_class\s+"([^"]+)"', pcb_text):
        class_name = nc_match.group(1)
        start_pos = nc_match.start()

        # Find the matching closing paren for this net_class block
        depth = 0
        end_pos = start_pos
        for i, char in enumerate(pcb_text[start_pos:], start_pos):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end_pos = i + 1
                    break

        nc_block = pcb_text[start_pos:end_pos]

        # Extract values from the block
        # Use min() only after we've found a value from this PCB
        clearance_match = re.search(r"\(clearance\s+([\d.]+)\)", nc_block)
        if clearance_match:
            clearance = float(clearance_match.group(1))
            if clearance > 0:
                if found_clearance:
                    rules.min_clearance = min(rules.min_clearance, clearance)
                else:
                    rules.min_clearance = clearance
                    found_clearance = True

        trace_match = re.search(r"\(trace_width\s+([\d.]+)\)", nc_block)
        if trace_match:
            trace_width = float(trace_match.group(1))
            if trace_width > 0:
                if found_track_width:
                    rules.min_track_width = min(rules.min_track_width, trace_width)
                else:
                    rules.min_track_width = trace_width
                    found_track_width = True

        via_dia_match = re.search(r"\(via_dia\s+([\d.]+)\)", nc_block)
        if via_dia_match:
            via_dia = float(via_dia_match.group(1))
            if via_dia > 0:
                if found_via_diameter:
                    rules.min_via_diameter = min(rules.min_via_diameter, via_dia)
                else:
                    rules.min_via_diameter = via_dia
                    found_via_diameter = True

        via_drill_match = re.search(r"\(via_drill\s+([\d.]+)\)", nc_block)
        if via_drill_match:
            via_drill = float(via_drill_match.group(1))
            if via_drill > 0:
                if found_via_drill:
                    rules.min_via_drill = min(rules.min_via_drill, via_drill)
                else:
                    rules.min_via_drill = via_drill
                    found_via_drill = True

    # Also check for board-level constraints (KiCad 8 format)
    # (design_settings (min_clearance X) (min_track_width X) ...)
    design_match = re.search(r"\(design_settings\s+(.*?)\n\s*\)", pcb_text, re.DOTALL)
    if design_match:
        design_text = design_match.group(1)

        min_clear_match = re.search(r"\(min_clearance\s+([\d.]+)\)", design_text)
        if min_clear_match:
            rules.min_clearance = float(min_clear_match.group(1))

        min_track_match = re.search(r"\(min_track_width\s+([\d.]+)\)", design_text)
        if min_track_match:
            rules.min_track_width = float(min_track_match.group(1))

        min_via_match = re.search(r"\(min_via_diameter\s+([\d.]+)\)", design_text)
        if min_via_match:
            rules.min_via_diameter = float(min_via_match.group(1))

        min_drill_match = re.search(r"\(min_via_drill\s+([\d.]+)\)", design_text)
        if min_drill_match:
            rules.min_via_drill = float(min_drill_match.group(1))

    return rules


def validate_grid_resolution(
    grid_resolution: float,
    clearance: float,
    warn: bool = True,
) -> list[str]:
    """Validate grid resolution against clearance for DRC compliance.

    The discrete routing grid can cause clearance violations when the grid
    resolution is too coarse relative to the required clearance. For reliable
    DRC compliance, grid_resolution should be <= clearance / 2.

    Args:
        grid_resolution: Router grid resolution in mm
        clearance: Required trace/via clearance in mm
        warn: If True, emit warnings via warnings.warn()

    Returns:
        List of warning messages (empty if compliant).

    Example:
        >>> warnings = validate_grid_resolution(0.25, 0.2)
        >>> if warnings:
        ...     print("Grid resolution may cause DRC violations")
    """
    issues: list[str] = []

    recommended = clearance / 2

    if grid_resolution > clearance:
        msg = (
            f"Grid resolution {grid_resolution}mm exceeds clearance {clearance}mm. "
            f"This WILL cause DRC violations. Use grid_resolution <= {clearance}mm."
        )
        issues.append(msg)
        if warn:
            warnings.warn(msg, stacklevel=2)

    elif grid_resolution > recommended:
        msg = (
            f"Grid resolution {grid_resolution}mm may cause clearance violations "
            f"with {clearance}mm clearance. Recommend grid_resolution <= {recommended}mm "
            f"for reliable DRC compliance."
        )
        issues.append(msg)
        if warn:
            warnings.warn(msg, stacklevel=2)

    return issues


def validate_routes(
    router: Autorouter,
    rules: DesignRules | None = None,
) -> list[ClearanceViolation]:
    """Validate routed traces for potential clearance violations.

    Performs a simplified post-route check for obvious clearance issues.
    This is not a full DRC check but can catch common problems before
    exporting to KiCad.

    Args:
        router: Autorouter instance with completed routes
        rules: DesignRules to check against (uses router.rules if None)

    Returns:
        List of potential ClearanceViolation issues.

    Note:
        This is a basic validation. For comprehensive DRC, export the
        PCB and run KiCad's built-in DRC checker.
    """
    if rules is None:
        rules = router.rules

    violations: list[ClearanceViolation] = []
    clearance = rules.trace_clearance

    # Check each route segment against pads of different nets
    for route_idx, route in enumerate(router.routes):
        route_net = route.net

        for seg_idx, segment in enumerate(route.segments):
            # Check against all pads
            for (ref, num), pad in router.pads.items():
                # Skip pads on the same net
                if pad.net == route_net:
                    continue

                # Calculate minimum distance from segment to pad center
                # (simplified - actual check would use pad geometry)
                dist = _point_to_segment_distance(
                    pad.x, pad.y, segment.x1, segment.y1, segment.x2, segment.y2
                )

                # Account for pad size (use larger dimension)
                pad_radius = max(pad.width, pad.height) / 2
                effective_dist = dist - pad_radius - rules.trace_width / 2

                if effective_dist < clearance:
                    violations.append(
                        ClearanceViolation(
                            segment_index=seg_idx,
                            x1=segment.x1,
                            y1=segment.y1,
                            x2=segment.x2,
                            y2=segment.y2,
                            net=route_net,
                            obstacle_type="pad",
                            obstacle_net=pad.net,
                            distance=effective_dist,
                            required=clearance,
                        )
                    )

    return violations


def _point_to_segment_distance(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    """Calculate minimum distance from a point to a line segment."""
    # Vector from start to end
    dx = x2 - x1
    dy = y2 - y1

    # Handle zero-length segment
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)

    # Project point onto line, clamped to segment
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / seg_len_sq))

    # Closest point on segment
    closest_x = x1 + t * dx
    closest_y = y1 + t * dy

    return math.sqrt((px - closest_x) ** 2 + (py - closest_y) ** 2)


def detect_layer_stack(pcb_text: str) -> LayerStack:
    """Auto-detect layer stack configuration from a KiCad PCB file.

    Parses the PCB file to determine:
    1. How many copper layers exist (from the (layers ...) section)
    2. Which inner layers have zone fills (likely planes)

    For inner layers with power/ground zones, they are marked as PLANE layers
    and excluded from signal routing. This allows proper handling of common
    4-layer configurations where In1.Cu and In2.Cu are GND/PWR planes.

    Args:
        pcb_text: Contents of a .kicad_pcb file

    Returns:
        LayerStack configured for the detected layer count and plane assignments.

    Example:
        >>> pcb_text = Path("board.kicad_pcb").read_text()
        >>> stack = detect_layer_stack(pcb_text)
        >>> print(f"Detected: {stack.name} ({stack.num_layers} layers)")
    """
    # Parse the (layers ...) section to find copper layers
    copper_layers: list[tuple[int, str]] = []

    layers_match = re.search(r"\(layers\s+(.*?)\n\s*\)", pcb_text, re.DOTALL)
    if layers_match:
        layers_text = layers_match.group(1)
        # Match layer definitions like: (0 "F.Cu" signal) or (31 "B.Cu" signal)
        for layer_match in re.finditer(r'\((\d+)\s+"([^"]+\.Cu)"\s+(\w+)', layers_text):
            layer_num = int(layer_match.group(1))
            layer_name = layer_match.group(2)
            # Only include copper layers (*.Cu)
            copper_layers.append((layer_num, layer_name))

    # Sort by layer number to get correct order
    copper_layers.sort(key=lambda x: x[0])
    num_copper = len(copper_layers)

    if num_copper == 0:
        # Fallback to 2-layer if no layers found
        return LayerStack.two_layer()

    # Detect which layers have zone fills (likely planes)
    zone_layers: dict[str, str] = {}  # layer_name -> net_name

    # Parse zone definitions to find layers with fills
    for zone_match in re.finditer(
        r'\(zone\s+.*?\(net_name\s+"([^"]+)"\).*?\(layer\s+"([^"]+)"\)',
        pcb_text,
        re.DOTALL,
    ):
        net_name = zone_match.group(1)
        layer_name = zone_match.group(2)
        # Track the net for this layer (prefer GND/power nets as plane indicators)
        if layer_name.endswith(".Cu"):
            # If multiple zones on same layer, prefer power/GND nets
            existing = zone_layers.get(layer_name, "")
            if not existing or net_name.upper() in ("GND", "GNDA", "GNDD"):
                zone_layers[layer_name] = net_name
            elif existing.upper() not in ("GND", "GNDA", "GNDD") and any(
                c in net_name.upper() for c in ["+", "V", "PWR", "VCC", "VDD"]
            ):
                zone_layers[layer_name] = net_name

    # Build layer definitions based on detected configuration
    if num_copper <= 2:
        return LayerStack.two_layer()

    elif num_copper == 4:
        # 4-layer board - check if inner layers are planes
        inner_layers = [name for _, name in copper_layers if name not in ("F.Cu", "B.Cu")]

        # Check if inner layers have zones (power/ground planes)
        inner_zones = {name: zone_layers.get(name, "") for name in inner_layers}
        has_inner_planes = any(inner_zones.values())

        if has_inner_planes:
            # Inner layers are planes - use SIG-GND-PWR-SIG configuration
            layers = [
                LayerDefinition(
                    "F.Cu", 0, LayerType.SIGNAL, is_outer=True, reference_plane="In1.Cu"
                ),
            ]
            # Add inner layers as planes with detected net names
            in1_net = inner_zones.get("In1.Cu", "GND")
            in2_net = inner_zones.get("In2.Cu", "+3.3V")
            layers.append(LayerDefinition("In1.Cu", 1, LayerType.PLANE, plane_net=in1_net))
            layers.append(LayerDefinition("In2.Cu", 2, LayerType.PLANE, plane_net=in2_net))
            layers.append(
                LayerDefinition(
                    "B.Cu", 3, LayerType.SIGNAL, is_outer=True, reference_plane="In2.Cu"
                )
            )

            return LayerStack(
                name="4-Layer (auto-detected)",
                description="4-layer with inner planes (auto-detected from PCB zones)",
                layers=layers,
            )
        else:
            # No zones on inner layers - treat all as signal layers
            return LayerStack.four_layer_sig_sig_gnd_pwr()

    elif num_copper == 6:
        return LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()

    else:
        # Unsupported layer count - fall back to 2-layer
        # Could be extended to support 8+ layers in the future
        return LayerStack.two_layer()


def route_pcb(
    board_width: float,
    board_height: float,
    components: list[dict],
    net_map: dict[str, int],
    rules: DesignRules | None = None,
    origin_x: float = 0,
    origin_y: float = 0,
    skip_nets: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> tuple[str, dict]:
    """
    Route a PCB given component placements and net assignments.

    Args:
        board_width: Board width in mm
        board_height: Board height in mm
        components: List of component dicts with:
            - ref: str (e.g., "U1")
            - x, y: float (placement position)
            - rotation: float (degrees)
            - pads: list of dicts with:
                - number: str (pad number)
                - x, y: float (relative to component center)
                - width, height: float
                - net: str (net name)
        net_map: Dict mapping net names to net numbers
        rules: DesignRules (optional)
        origin_x, origin_y: Board origin
        skip_nets: Net names to skip (e.g., ["GND", "+3.3V"] for plane nets)
        progress_callback: Optional callback for progress reporting.
            Signature: (progress: float, message: str, cancelable: bool) -> bool
            Returns False to cancel, True to continue.

    Returns:
        Tuple of (sexp_string, statistics_dict)
    """
    if rules is None:
        rules = DesignRules()

    skip_nets = skip_nets or []

    router = Autorouter(
        width=board_width,
        height=board_height,
        origin_x=origin_x,
        origin_y=origin_y,
        rules=rules,
    )

    # Add all component pads
    for comp in components:
        ref = comp["ref"]
        cx, cy = comp["x"], comp["y"]
        rotation = comp.get("rotation", 0)

        # Transform pad positions based on component placement
        # KiCad uses CLOCKWISE rotation (negative angle in standard math)
        rot_rad = math.radians(-rotation)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        pads: list[dict] = []
        for pad in comp.get("pads", []):
            # Rotate pad position around component center
            px, py = pad["x"], pad["y"]
            rx = px * cos_r - py * sin_r
            ry = px * sin_r + py * cos_r

            net_name = pad.get("net", "")
            if net_name in skip_nets:
                continue

            net_num = net_map.get(net_name, 0)
            if net_num == 0 and net_name:
                # Unknown net, assign a number
                net_num = len(net_map) + 1
                net_map[net_name] = net_num

            pads.append(
                {
                    "number": pad["number"],
                    "x": cx + rx,
                    "y": cy + ry,
                    "width": pad.get("width", 0.5),
                    "height": pad.get("height", 0.5),
                    "net": net_num,
                    "net_name": net_name,
                    "layer": Layer.F_CU,
                }
            )

        if pads:
            router.add_component(ref, pads)

    # Get all nets that need routing (exclude plane nets)
    nets_to_route: list[int] = []
    for net_name, net_num in net_map.items():
        if net_name and net_name not in skip_nets and net_num in router.nets:
            if len(router.nets[net_num]) >= 2:
                nets_to_route.append(net_num)

    # Route nets
    print(f"Autorouting {len(nets_to_route)} nets...")
    router.route_all(nets_to_route, progress_callback=progress_callback)

    stats = router.get_statistics()
    print(
        f"  Completed: {stats['routes']} routes, {stats['segments']} segments, {stats['vias']} vias"
    )

    return router.to_sexp(), stats


def _extract_pad_blocks(section: str) -> list[str]:
    """
    Extract complete (pad ...) S-expression blocks from a footprint section.

    KiCad 7+ uses multi-line pad definitions like:
        (pad "1" smd roundrect
          (at -0.9500 0.9000)
          (size 0.6000 1.1000)
          ...
        )

    This function finds each (pad ...) block and extracts the complete
    content by counting parentheses to find the matching closing paren.

    Args:
        section: Footprint section text from a KiCad PCB file

    Returns:
        List of complete pad block strings
    """
    pad_blocks: list[str] = []

    # Find all positions where "(pad " starts
    start_pos = 0
    while True:
        pad_start = section.find("(pad ", start_pos)
        if pad_start == -1:
            break

        # Count parentheses to find the matching closing paren
        depth = 0
        in_string = False
        i = pad_start
        while i < len(section):
            char = section[i]

            if char == '"' and (i == 0 or section[i - 1] != "\\"):
                in_string = not in_string
            elif not in_string:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        # Found the matching closing paren
                        pad_blocks.append(section[pad_start : i + 1])
                        break
            i += 1

        start_pos = i + 1

    return pad_blocks


def _extract_edge_segments(
    pcb_text: str,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Extract board edge segments from Edge.Cuts layer.

    Parses gr_rect and gr_line elements on the Edge.Cuts layer to build
    a list of line segments defining the board outline.

    Args:
        pcb_text: Contents of a .kicad_pcb file

    Returns:
        List of ((x1, y1), (x2, y2)) tuples for each edge segment.
    """
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

    # Look for gr_rect on Edge.Cuts (simple rectangular boards)
    # Use .*? with re.DOTALL to match nested parentheses in stroke/fill attributes
    for rect_match in re.finditer(
        r"\(gr_rect\s+\(start\s+([\d.]+)\s+([\d.]+)\)\s+\(end\s+([\d.]+)\s+([\d.]+)\)"
        r'.*?\(layer\s+"Edge\.Cuts"\)',
        pcb_text,
        re.DOTALL,
    ):
        x1, y1, x2, y2 = map(float, rect_match.groups())
        # Convert rectangle to 4 line segments
        segments.extend(
            [
                ((x1, y1), (x2, y1)),  # Top
                ((x2, y1), (x2, y2)),  # Right
                ((x2, y2), (x1, y2)),  # Bottom
                ((x1, y2), (x1, y1)),  # Left
            ]
        )

    # Also handle gr_rect where layer comes before coordinates
    for rect_match in re.finditer(
        r'\(gr_rect.*?\(layer\s+"Edge\.Cuts"\).*?'
        r"\(start\s+([\d.]+)\s+([\d.]+)\)\s*\(end\s+([\d.]+)\s+([\d.]+)\)",
        pcb_text,
        re.DOTALL,
    ):
        x1, y1, x2, y2 = map(float, rect_match.groups())
        segments.extend(
            [
                ((x1, y1), (x2, y1)),
                ((x2, y1), (x2, y2)),
                ((x2, y2), (x1, y2)),
                ((x1, y2), (x1, y1)),
            ]
        )

    # Look for gr_line elements on Edge.Cuts (complex board outlines)
    for line_match in re.finditer(
        r"\(gr_line\s+\(start\s+([\d.-]+)\s+([\d.-]+)\)\s+"
        r'\(end\s+([\d.-]+)\s+([\d.-]+)\).*?\(layer\s+"Edge\.Cuts"\)',
        pcb_text,
        re.DOTALL,
    ):
        x1, y1, x2, y2 = map(float, line_match.groups())
        segments.append(((x1, y1), (x2, y2)))

    return segments


def load_pcb_for_routing(
    pcb_path: str,
    skip_nets: list[str] | None = None,
    netlist: dict[str, str] | None = None,
    rules: DesignRules | None = None,
    use_pcb_rules: bool = True,
    validate_drc: bool = True,
    edge_clearance: float | None = None,
    layer_stack: LayerStack | None = None,
) -> tuple[Autorouter, dict[str, int]]:
    """
    Load a KiCad PCB file and create an Autorouter with all components.

    Args:
        pcb_path: Path to .kicad_pcb file
        skip_nets: Net names to skip (e.g., ["GND", "+3.3V"] for plane nets)
        netlist: Optional dict mapping "REF.PIN" to net name (e.g., {"U1.1": "+3.3V"})
                 If provided, overrides any net assignments in the PCB file.
        rules: DesignRules for routing (grid resolution, trace width, etc.)
               If None and use_pcb_rules=True, extracts rules from PCB.
               If None and use_pcb_rules=False, uses default rules.
        use_pcb_rules: If True and rules=None, parse design rules from the PCB
                       file's setup section and use them as defaults.
        validate_drc: If True, validate grid resolution against clearance and
                      emit warnings for potential DRC issues.
        edge_clearance: Copper-to-edge clearance in mm. If specified, blocks
                        routing within this distance of the board edge. Common
                        values are 0.25-0.5mm. If None, no edge clearance is
                        applied (default for backward compatibility).
        layer_stack: Layer stack configuration for routing. Controls how many
                     layers are available for routing and which layers are
                     planes vs signal layers. If None, defaults to 2-layer.
                     Use LayerStack.four_layer_sig_gnd_pwr_sig() for 4-layer
                     boards with GND/PWR planes, which routes signals on outer
                     layers (F.Cu, B.Cu) with vias for layer transitions.

    Returns:
        Tuple of (Autorouter instance, net_map dict)

    Example:
        >>> # Use PCB's design rules automatically
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb")
        >>>
        >>> # Override with custom rules
        >>> custom = DesignRules(grid_resolution=0.1, trace_width=0.15)
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb", rules=custom)
        >>>
        >>> # Skip DRC validation warnings
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb", validate_drc=False)
        >>>
        >>> # Apply 0.5mm edge clearance
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb", edge_clearance=0.5)
        >>>
        >>> # Use 4-layer stack with GND/PWR planes
        >>> from kicad_tools.router import LayerStack
        >>> stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb", layer_stack=stack)
    """
    pcb_text = Path(pcb_path).read_text()
    skip_nets = skip_nets or []

    # Parse PCB design rules if needed
    pcb_rules: PCBDesignRules | None = None
    if rules is None and use_pcb_rules:
        pcb_rules = parse_pcb_design_rules(pcb_text)

    # Parse board dimensions from Edge.Cuts gr_rect
    edge_match = re.search(
        r"\(gr_rect\s+\(start\s+([\d.]+)\s+([\d.]+)\)\s+\(end\s+([\d.]+)\s+([\d.]+)\)",
        pcb_text,
    )
    if edge_match:
        x1, y1, x2, y2 = map(float, edge_match.groups())
        board_width = x2 - x1
        board_height = y2 - y1
        origin_x = x1
        origin_y = y1
    else:
        # Default HAT dimensions
        board_width = 65.0
        board_height = 56.0
        origin_x = 115.0
        origin_y = 75.0

    # Parse nets
    net_map: dict[str, int] = {}
    for match in re.finditer(r'\(net\s+(\d+)\s+"([^"]+)"\)', pcb_text):
        net_num, net_name = int(match.group(1)), match.group(2)
        if net_num > 0:
            net_map[net_name] = net_num

    # Parse footprints and their pads
    components: list[dict] = []

    # Split by footprint for easier parsing
    footprint_sections = re.split(r"(?=\(footprint\s)", pcb_text)

    for section in footprint_sections:
        if not section.startswith("(footprint"):
            continue

        # Get footprint position
        at_match = re.search(r"\(at\s+([\d.]+)\s+([\d.]+)(?:\s+([\d.]+))?\)", section)
        if not at_match:
            continue

        fp_x = float(at_match.group(1))
        fp_y = float(at_match.group(2))
        fp_rot = float(at_match.group(3)) if at_match.group(3) else 0

        # Get reference
        ref_match = re.search(r'\(fp_text\s+reference\s+"([^"]+)"', section)
        if not ref_match:
            continue
        ref = ref_match.group(1)

        # Parse pads - extract complete (pad ...) blocks
        # KiCad 7+ uses multi-line pad definitions, so we need to extract
        # complete S-expression blocks rather than parsing line-by-line
        pads: list[dict] = []

        # Find all complete (pad ...) blocks using parenthesis matching
        pad_blocks = _extract_pad_blocks(section)

        for pad_block in pad_blocks:
            # Extract pad number and type
            # Handle both quoted ("A1") and unquoted (1) pad numbers
            # KiCad uses unquoted numbers for numeric pads, quoted for alphanumeric (BGA)
            pad_start = re.match(r'\(pad\s+(?:"([^"]+)"|(\S+))\s+(\w+)', pad_block)
            if not pad_start:
                continue
            pad_num = pad_start.group(1) or pad_start.group(2)
            pad_type = pad_start.group(3)  # smd or thru_hole

            # Extract at position (now searches entire multi-line block)
            at_match = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)", pad_block)
            if not at_match:
                continue
            pad_x = float(at_match.group(1))
            pad_y = float(at_match.group(2))

            # Extract size
            size_match = re.search(r"\(size\s+([\d.]+)\s+([\d.]+)\)", pad_block)
            if not size_match:
                continue
            pad_w = float(size_match.group(1))
            pad_h = float(size_match.group(2))

            # Extract net (if present)
            net_match = re.search(r'\(net\s+(\d+)\s+"([^"]+)"\)', pad_block)
            net_num = int(net_match.group(1)) if net_match else 0
            net_name = net_match.group(2) if net_match else ""

            # Extract drill size if present
            drill_match = re.search(r"\(drill\s+([\d.]+)", pad_block)
            drill_size = float(drill_match.group(1)) if drill_match else 0.0

            # Override with netlist if provided
            if netlist:
                pad_key = f"{ref}.{pad_num}"
                if pad_key in netlist:
                    net_name = netlist[pad_key]
                    # Assign net number from net_map or create new
                    if net_name in net_map:
                        net_num = net_map[net_name]
                    elif net_name:
                        net_num = max(net_map.values(), default=0) + 1
                        net_map[net_name] = net_num

            # For skipped nets (power/ground planes), still add pad as obstacle
            # but use net=0 so it blocks routing without being a routeable net
            if net_name in skip_nets:
                net_num = 0  # Treat as obstacle, not a routable net

            # Transform pad position by footprint rotation
            rot_rad = math.radians(-fp_rot)
            cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
            abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
            abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

            pads.append(
                {
                    "number": pad_num,
                    "x": abs_x,
                    "y": abs_y,
                    "width": pad_w,
                    "height": pad_h,
                    "net": net_num,
                    "net_name": net_name,
                    "through_hole": pad_type == "thru_hole",
                    "drill": drill_size,
                }
            )

        if pads:
            components.append(
                {
                    "ref": ref,
                    "x": fp_x,
                    "y": fp_y,
                    "rotation": fp_rot,
                    "pads": pads,
                }
            )

    # Create router with provided rules, PCB rules, or defaults
    if rules is None:
        if pcb_rules is not None:
            # Use rules extracted from PCB file
            rules = pcb_rules.to_design_rules()
        else:
            # Fall back to conservative defaults
            rules = DesignRules(grid_resolution=0.1)

    # Validate grid resolution for DRC compliance
    if validate_drc:
        validate_grid_resolution(
            rules.grid_resolution,
            rules.trace_clearance,
            warn=True,
        )

    router = Autorouter(
        width=board_width,
        height=board_height,
        origin_x=origin_x,
        origin_y=origin_y,
        rules=rules,
        net_class_map=DEFAULT_NET_CLASS_MAP,
        layer_stack=layer_stack,
    )

    # Add all components
    for comp in components:
        # Pads already have absolute positions
        router.add_component(comp["ref"], comp["pads"])

    # Apply edge clearance if specified
    if edge_clearance is not None and edge_clearance > 0:
        edge_segments = _extract_edge_segments(pcb_text)
        if edge_segments:
            blocked_cells = router.grid.add_edge_keepout(edge_segments, edge_clearance)
            if blocked_cells > 0:
                print(f"  Edge clearance: {edge_clearance}mm, {blocked_cells} cells blocked")

    return router, net_map


def generate_netclass_setup(
    rules: DesignRules,
    net_classes: dict[str, list[str]] | None = None,
) -> str:
    """
    Generate KiCad 7+ compatible net class setup S-expression.

    This function generates net class definitions in the format compatible
    with KiCad 7.x and 8.x. Note that this is OPTIONAL - routes generated
    by this library already embed trace widths and via sizes directly in
    segment and via S-expressions.

    IMPORTANT: Do NOT use the old KiCad 6 format:
        (net_settings
          (net_class "Default" "Default net class" ...)
        )

    This old format causes parsing errors in KiCad 7+:
        "Error loading PCB '...'. Unexpected 'net_settings' in '...'"

    Args:
        rules: DesignRules containing trace width, clearance, via parameters
        net_classes: Optional dict mapping class name to list of net names
                     e.g., {"Power": ["+5V", "GND"], "Signal": ["SDA", "SCL"]}

    Returns:
        KiCad 7+ compatible S-expression string for net class setup.
        Returns empty string if not needed (routes are self-contained).

    Example:
        >>> rules = DesignRules(trace_width=0.2, via_diameter=0.6, via_drill=0.3)
        >>> sexp = generate_netclass_setup(rules)
        >>> # Usually you don't need this - routes are self-contained
        >>> print("Routes already have correct trace/via sizes embedded")
    """
    # Routes already embed trace widths and via sizes in their S-expressions.
    # Net class setup is only needed for:
    # 1. DRC checking in KiCad
    # 2. Documentation purposes
    # 3. Manual editing after autorouting
    #
    # If you do need net class definitions, here's the KiCad 7+ format:
    #
    # The net class definitions go in the setup section as part of
    # design rules, not in a separate net_settings block.

    if not net_classes:
        # No net classes specified, and routes are self-contained
        # so no net class setup is needed
        return ""

    # Generate KiCad 7+ compatible net class assignments
    # These go in the setup section under design rules
    parts = []
    parts.append("  ; Net class definitions (KiCad 7+ format)")
    parts.append("  ; Note: Routes already have trace/via sizes embedded")

    for class_name, nets in net_classes.items():
        for net_name in nets:
            # In KiCad 7+, net-to-class assignments use this format
            parts.append(f'  (net_class "{class_name}" "{net_name}")')

    return "\n".join(parts)


def merge_routes_into_pcb(
    pcb_content: str,
    route_sexp: str,
) -> str:
    """
    Merge routed traces into an existing PCB file content.

    This function safely inserts route S-expressions into a PCB file,
    placing them before the final closing parenthesis. It does NOT
    add any net_settings or net_class blocks, as routes already have
    correct trace widths and via sizes embedded.

    Args:
        pcb_content: Original PCB file content as string
        route_sexp: Route S-expressions from Autorouter.to_sexp()

    Returns:
        Modified PCB content with routes inserted.

    Example:
        >>> original = Path("board.kicad_pcb").read_text()
        >>> routes = router.to_sexp()
        >>> merged = merge_routes_into_pcb(original, routes)
        >>> Path("board_routed.kicad_pcb").write_text(merged)

    Note:
        Routes contain embedded trace widths and via sizes, so no
        net class metadata is required. Do NOT add (net_settings ...)
        blocks with the old KiCad 6 format - this will cause parsing
        errors in KiCad 7+.
    """
    if not route_sexp:
        return pcb_content

    # Remove trailing whitespace and closing parenthesis
    content = pcb_content.rstrip()
    if content.endswith(")"):
        content = content[:-1].rstrip()

    # Insert routes and close the file
    result = content + "\n\n"
    result += "  ; Autorouted traces\n"
    result += f"  {route_sexp}\n"
    result += ")\n"

    return result

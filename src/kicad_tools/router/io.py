"""
PCB file I/O for autorouting.

This module provides:
- route_pcb: Route a PCB given component placements and net assignments
- load_pcb_for_routing: Load a KiCad PCB file and create an Autorouter
- merge_routes_into_pcb: Merge routed traces into an existing PCB file
- generate_netclass_setup: Generate KiCad 7+ compatible net class setup

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
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

from .core import Autorouter
from .layers import Layer
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules


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


def load_pcb_for_routing(
    pcb_path: str,
    skip_nets: list[str] | None = None,
    netlist: dict[str, str] | None = None,
    rules: DesignRules | None = None,
) -> tuple[Autorouter, dict[str, int]]:
    """
    Load a KiCad PCB file and create an Autorouter with all components.

    Args:
        pcb_path: Path to .kicad_pcb file
        skip_nets: Net names to skip (e.g., ["GND", "+3.3V"] for plane nets)
        netlist: Optional dict mapping "REF.PIN" to net name (e.g., {"U1.1": "+3.3V"})
                 If provided, overrides any net assignments in the PCB file.
        rules: DesignRules for routing (grid resolution, trace width, etc.)
               If None, uses default rules.

    Returns:
        Tuple of (Autorouter instance, net_map dict)
    """
    pcb_text = Path(pcb_path).read_text()
    skip_nets = skip_nets or []

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

    # Create router with provided rules or defaults
    if rules is None:
        rules = DesignRules(grid_resolution=0.25)
    router = Autorouter(
        width=board_width,
        height=board_height,
        origin_x=origin_x,
        origin_y=origin_y,
        rules=rules,
        net_class_map=DEFAULT_NET_CLASS_MAP,
    )

    # Add all components
    for comp in components:
        # Pads already have absolute positions
        router.add_component(comp["ref"], comp["pads"])

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

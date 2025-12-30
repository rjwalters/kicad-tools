"""
PCB file I/O for autorouting.

This module provides:
- route_pcb: Route a PCB given component placements and net assignments
- load_pcb_for_routing: Load a KiCad PCB file and create an Autorouter

These functions handle the translation between KiCad file formats and
the autorouter's internal representations.
"""

import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .core import Autorouter
from .layers import Layer
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules


def route_pcb(
    board_width: float,
    board_height: float,
    components: List[dict],
    net_map: Dict[str, int],
    rules: Optional[DesignRules] = None,
    origin_x: float = 0,
    origin_y: float = 0,
    skip_nets: Optional[List[str]] = None,
) -> Tuple[str, dict]:
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

        pads: List[dict] = []
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
    nets_to_route: List[int] = []
    for net_name, net_num in net_map.items():
        if net_name and net_name not in skip_nets and net_num in router.nets:
            if len(router.nets[net_num]) >= 2:
                nets_to_route.append(net_num)

    # Route nets
    print(f"Autorouting {len(nets_to_route)} nets...")
    router.route_all(nets_to_route)

    stats = router.get_statistics()
    print(
        f"  Completed: {stats['routes']} routes, {stats['segments']} segments, {stats['vias']} vias"
    )

    return router.to_sexp(), stats


def load_pcb_for_routing(
    pcb_path: str,
    skip_nets: Optional[List[str]] = None,
    netlist: Optional[Dict[str, str]] = None,
    rules: Optional[DesignRules] = None,
) -> Tuple[Autorouter, Dict[str, int]]:
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
    net_map: Dict[str, int] = {}
    for match in re.finditer(r'\(net\s+(\d+)\s+"([^"]+)"\)', pcb_text):
        net_num, net_name = int(match.group(1)), match.group(2)
        if net_num > 0:
            net_map[net_name] = net_num

    # Parse footprints and their pads
    components: List[dict] = []

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

        # Parse pads - match each pad individually
        pads: List[dict] = []
        # Find all (pad ...) blocks in the footprint section
        # Use a line-by-line approach for robustness
        for line in section.split('\n'):
            line = line.strip()
            if not line.startswith('(pad '):
                continue

            # Extract pad number and type
            pad_start = re.match(r'\(pad\s+"([^"]+)"\s+(\w+)', line)
            if not pad_start:
                continue
            pad_num = pad_start.group(1)
            pad_type = pad_start.group(2)  # smd or thru_hole

            # Extract at position
            at_match = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)", line)
            if not at_match:
                continue
            pad_x = float(at_match.group(1))
            pad_y = float(at_match.group(2))

            # Extract size
            size_match = re.search(r"\(size\s+([\d.]+)\s+([\d.]+)\)", line)
            if not size_match:
                continue
            pad_w = float(size_match.group(1))
            pad_h = float(size_match.group(2))

            # Extract net (if present)
            net_match = re.search(r'\(net\s+(\d+)\s+"([^"]+)"\)', line)
            net_num = int(net_match.group(1)) if net_match else 0
            net_name = net_match.group(2) if net_match else ""

            # Extract drill size if present
            drill_match = re.search(r"\(drill\s+([\d.]+)", line)
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

"""
Auto-add stitching vias for plane connections.

Automatically adds stitching vias to connect surface-mount component pads
to internal power/ground planes in multi-layer PCBs.

Usage:
    kicad-pcb-stitch board.kicad_pcb --net GND
    kicad-pcb-stitch board.kicad_pcb --net GND --net +3.3V
    kicad-pcb-stitch board.kicad_pcb --net GND --dry-run
    kicad-pcb-stitch board.kicad_pcb --net GND --via-size 0.45 --drill 0.2

Exit Codes:
    0 - Success
    1 - Error or no work to do
"""

import argparse
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.core.sexp_file import load_pcb, save_pcb
from kicad_tools.sexp import SExp
from kicad_tools.sexp.builders import via_node


@dataclass
class PadInfo:
    """Information about a pad."""

    reference: str  # Component reference (e.g., "C2")
    pad_number: str  # Pad number (e.g., "1", "2")
    net_number: int
    net_name: str
    x: float
    y: float
    layer: str  # "F.Cu" or "B.Cu"
    width: float  # Pad width
    height: float  # Pad height


@dataclass
class ViaPlacement:
    """Information about a via to be placed."""

    pad: PadInfo
    via_x: float
    via_y: float
    size: float
    drill: float
    layers: tuple[str, str]


@dataclass
class StitchResult:
    """Result of the stitching operation."""

    pcb_name: str
    target_nets: list[str]
    vias_added: list[ViaPlacement] = field(default_factory=list)
    pads_skipped: list[tuple[PadInfo, str]] = field(default_factory=list)  # (pad, reason)
    already_connected: int = 0
    # Per-net detected layers: {net_name: layer} for auto-detected layers
    detected_layers: dict[str, str] = field(default_factory=dict)
    # Nets that fell back to default B.Cu (no zone found)
    fallback_nets: list[str] = field(default_factory=list)


def get_net_map(sexp: SExp) -> dict[int, str]:
    """Build a mapping of net number to net name."""
    net_map = {}
    for child in sexp.iter_children():
        if child.tag == "net":
            net_num = child.get_int(0)
            net_name = child.get_string(1)
            if net_num is not None and net_name is not None:
                net_map[net_num] = net_name
    return net_map


def get_net_number(sexp: SExp, net_name: str) -> int | None:
    """Get the net number for a given net name."""
    for child in sexp.iter_children():
        if child.tag == "net":
            name = child.get_string(1)
            if name == net_name:
                return child.get_int(0)
    return None


def find_zones_for_net(sexp: SExp, net_name: str) -> list[str]:
    """Find zones matching a net name and return their layers.

    Args:
        sexp: PCB S-expression
        net_name: Net name to find zones for

    Returns:
        List of layer names where zones exist for this net (e.g., ["In1.Cu", "In2.Cu"])
    """
    layers = []
    for child in sexp.iter_children():
        if child.tag == "zone":
            zone_net_name = None
            zone_layer = None

            # Get net_name from zone
            net_name_node = child.find_child("net_name")
            if net_name_node:
                zone_net_name = net_name_node.get_string(0)

            # Get layer from zone
            layer_node = child.find_child("layer")
            if layer_node:
                zone_layer = layer_node.get_string(0)

            if zone_net_name == net_name and zone_layer:
                layers.append(zone_layer)

    return layers


def find_pads_on_nets(sexp: SExp, net_names: set[str]) -> list[PadInfo]:
    """Find all SMD pads on the specified nets."""
    net_map = get_net_map(sexp)
    target_net_nums = {num for num, name in net_map.items() if name in net_names}

    pads = []

    for fp in sexp.iter_children():
        if fp.tag != "footprint":
            continue

        # Get footprint position
        at_node = fp.find_child("at")
        if not at_node:
            continue
        fp_x = at_node.get_float(0) or 0.0
        fp_y = at_node.get_float(1) or 0.0
        fp_rotation = at_node.get_float(2) or 0.0

        # Get footprint layer
        layer_node = fp.find_child("layer")
        fp_layer = layer_node.get_string(0) if layer_node else "F.Cu"

        # Get reference
        reference = None
        for prop in fp.find_children("property"):
            if prop.get_string(0) == "Reference":
                reference = prop.get_string(1)
                break
        # Fallback to fp_text
        if reference is None:
            for fp_text in fp.find_children("fp_text"):
                if fp_text.get_string(0) == "reference":
                    reference = fp_text.get_string(1)
                    break
        if reference is None:
            reference = "??"

        # Find pads on target nets
        for pad in fp.find_children("pad"):
            pad_number = pad.get_string(0)
            pad_type = pad.get_string(1)  # smd, thru_hole, etc.

            # Only consider SMD pads (need vias for plane connection)
            if pad_type != "smd":
                continue

            # Check if pad is on a target net
            net_node = pad.find_child("net")
            if not net_node:
                continue
            net_num = net_node.get_int(0)
            if net_num not in target_net_nums:
                continue

            net_name = net_map.get(net_num, "")

            # Get pad position (relative to footprint)
            pad_at = pad.find_child("at")
            if not pad_at:
                continue
            pad_rel_x = pad_at.get_float(0) or 0.0
            pad_rel_y = pad_at.get_float(1) or 0.0

            # Transform pad position to board coordinates
            import math

            rad = math.radians(fp_rotation)
            cos_r = math.cos(rad)
            sin_r = math.sin(rad)
            pad_x = fp_x + pad_rel_x * cos_r - pad_rel_y * sin_r
            pad_y = fp_y + pad_rel_x * sin_r + pad_rel_y * cos_r

            # Get pad size
            size_node = pad.find_child("size")
            pad_width = size_node.get_float(0) or 0.5 if size_node else 0.5
            pad_height = size_node.get_float(1) or 0.5 if size_node else 0.5

            pads.append(
                PadInfo(
                    reference=reference,
                    pad_number=pad_number or "?",
                    net_number=net_num,
                    net_name=net_name,
                    x=pad_x,
                    y=pad_y,
                    layer=fp_layer,
                    width=pad_width,
                    height=pad_height,
                )
            )

    return pads


def find_existing_vias(sexp: SExp, net_numbers: set[int]) -> list[tuple[float, float, int]]:
    """Find existing vias on the specified nets. Returns list of (x, y, net_num)."""
    vias = []
    for child in sexp.iter_children():
        if child.tag == "via":
            net_node = child.find_child("net")
            if not net_node:
                continue
            net_num = net_node.get_int(0)
            if net_num not in net_numbers:
                continue

            at_node = child.find_child("at")
            if at_node:
                x = at_node.get_float(0) or 0.0
                y = at_node.get_float(1) or 0.0
                vias.append((x, y, net_num))
    return vias


def find_existing_tracks(sexp: SExp, net_numbers: set[int]) -> list[tuple[float, float, int]]:
    """Find track endpoints on the specified nets. Returns list of (x, y, net_num)."""
    points = []
    for child in sexp.iter_children():
        if child.tag == "segment":
            net_node = child.find_child("net")
            if not net_node:
                continue
            net_num = net_node.get_int(0)
            if net_num not in net_numbers:
                continue

            start_node = child.find_child("start")
            end_node = child.find_child("end")
            if start_node:
                x = start_node.get_float(0) or 0.0
                y = start_node.get_float(1) or 0.0
                points.append((x, y, net_num))
            if end_node:
                x = end_node.get_float(0) or 0.0
                y = end_node.get_float(1) or 0.0
                points.append((x, y, net_num))
    return points


def is_pad_connected(
    pad: PadInfo,
    vias: list[tuple[float, float, int]],
    track_points: list[tuple[float, float, int]],
    connection_radius: float = 0.5,
) -> bool:
    """Check if a pad has any connection (via or track) nearby."""
    import math

    # Check for nearby vias on the same net
    for vx, vy, vnet in vias:
        if vnet != pad.net_number:
            continue
        dist = math.sqrt((vx - pad.x) ** 2 + (vy - pad.y) ** 2)
        if dist < connection_radius + max(pad.width, pad.height) / 2:
            return True

    # Check for nearby track endpoints on the same net
    for tx, ty, tnet in track_points:
        if tnet != pad.net_number:
            continue
        dist = math.sqrt((tx - pad.x) ** 2 + (ty - pad.y) ** 2)
        if dist < connection_radius + max(pad.width, pad.height) / 2:
            return True

    return False


def calculate_via_position(
    pad: PadInfo,
    offset: float,
    via_size: float,
    existing_vias: list[tuple[float, float, int]],
    clearance: float,
) -> tuple[float, float] | None:
    """Calculate a valid via placement position near the pad.

    Tries to place the via offset from the pad center, checking for conflicts.
    Returns None if no valid position found.
    """
    import math

    # Try different offsets from pad center
    # Start with the direction away from pad center, try 8 directions
    directions = [
        (1, 0),
        (0, 1),
        (-1, 0),
        (0, -1),  # Cardinal
        (0.707, 0.707),
        (-0.707, 0.707),
        (-0.707, -0.707),
        (0.707, -0.707),  # Diagonal
    ]

    # Try placing at the edge of the pad first
    pad_radius = max(pad.width, pad.height) / 2
    test_offsets = [pad_radius + offset, pad_radius + offset * 1.5, pad_radius + offset * 2]

    for test_offset in test_offsets:
        for dx, dy in directions:
            via_x = pad.x + dx * test_offset
            via_y = pad.y + dy * test_offset

            # Check for conflicts with existing vias
            conflict = False
            for vx, vy, _vnet in existing_vias:
                dist = math.sqrt((vx - via_x) ** 2 + (vy - via_y) ** 2)
                if dist < via_size + clearance:
                    conflict = True
                    break

            if not conflict:
                return (via_x, via_y)

    return None


def get_via_layers(pad_layer: str, target_layer: str | None) -> tuple[str, str]:
    """Determine the layers for the via.

    Args:
        pad_layer: The layer the pad is on (F.Cu or B.Cu)
        target_layer: Optional target layer for the plane connection

    Returns:
        Tuple of (start_layer, end_layer) for the via
    """
    if target_layer:
        return (pad_layer, target_layer)

    # Default: connect surface to opposite surface (through via)
    if pad_layer == "F.Cu":
        return ("F.Cu", "B.Cu")
    else:
        return ("B.Cu", "F.Cu")


def add_via_to_pcb(sexp: SExp, placement: ViaPlacement) -> None:
    """Add a via to the PCB S-expression."""
    via = via_node(
        x=placement.via_x,
        y=placement.via_y,
        size=placement.size,
        drill=placement.drill,
        layers=placement.layers,
        net=placement.pad.net_number,
        uuid_str=str(uuid.uuid4()),
    )
    sexp.append(via)


def run_stitch(
    pcb_path: Path,
    net_names: list[str],
    via_size: float = 0.45,
    drill: float = 0.2,
    clearance: float = 0.2,
    offset: float = 0.5,
    target_layer: str | None = None,
    dry_run: bool = False,
) -> StitchResult:
    """Run the stitching operation on a PCB.

    Args:
        pcb_path: Path to the PCB file
        net_names: List of net names to add vias for
        via_size: Via pad diameter in mm
        drill: Via drill size in mm
        clearance: Minimum clearance from existing copper
        offset: Maximum distance from pad center for via placement
        target_layer: Target plane layer (auto-detect from zones if None)
        dry_run: If True, don't modify the file

    Returns:
        StitchResult with details of what was done
    """
    sexp = load_pcb(pcb_path)

    result = StitchResult(
        pcb_name=pcb_path.name,
        target_nets=net_names,
    )

    # Auto-detect target layers per net if not specified
    net_target_layers: dict[str, str | None] = {}
    if target_layer is None:
        for net_name in net_names:
            zone_layers = find_zones_for_net(sexp, net_name)
            if zone_layers:
                # Use first zone layer found (typically there's only one per net)
                net_target_layers[net_name] = zone_layers[0]
                result.detected_layers[net_name] = zone_layers[0]
            else:
                # No zone found, will fall back to B.Cu
                net_target_layers[net_name] = None
                result.fallback_nets.append(net_name)
    else:
        # Use explicit target layer for all nets
        for net_name in net_names:
            net_target_layers[net_name] = target_layer

    # Find pads on target nets
    net_name_set = set(net_names)
    pads = find_pads_on_nets(sexp, net_name_set)

    if not pads:
        return result

    # Get net numbers for filtering
    net_numbers = {p.net_number for p in pads}

    # Find existing connections
    existing_vias = find_existing_vias(sexp, net_numbers)
    track_points = find_existing_tracks(sexp, net_numbers)

    # Process each pad
    for pad in pads:
        # Check if already connected
        if is_pad_connected(pad, existing_vias, track_points):
            result.already_connected += 1
            continue

        # Calculate via position
        via_pos = calculate_via_position(
            pad,
            offset=offset,
            via_size=via_size,
            existing_vias=existing_vias,
            clearance=clearance,
        )

        if via_pos is None:
            result.pads_skipped.append((pad, "no valid via location"))
            continue

        # Determine via layers using per-net target layer
        pad_target_layer = net_target_layers.get(pad.net_name)
        layers = get_via_layers(pad.layer, pad_target_layer)

        placement = ViaPlacement(
            pad=pad,
            via_x=via_pos[0],
            via_y=via_pos[1],
            size=via_size,
            drill=drill,
            layers=layers,
        )

        result.vias_added.append(placement)

        # Add to existing vias list to prevent conflicts with subsequent placements
        existing_vias.append((via_pos[0], via_pos[1], pad.net_number))

    # Apply changes if not dry run
    if not dry_run and result.vias_added:
        for placement in result.vias_added:
            add_via_to_pcb(sexp, placement)
        save_pcb(sexp, pcb_path)

    return result


def output_result(result: StitchResult, dry_run: bool = False) -> None:
    """Output the stitching result."""
    import sys

    print(f"\nStitching vias for {result.pcb_name}")
    print("=" * 60)

    # Show warning for nets with no zone found (falling back to B.Cu)
    if result.fallback_nets:
        for net_name in result.fallback_nets:
            print(
                f"\nWarning: No zone found for net '{net_name}', defaulting to B.Cu",
                file=sys.stderr,
            )

    # Show detected layers
    if result.detected_layers:
        print("\nAuto-detected target layers from zones:")
        for net_name, layer in sorted(result.detected_layers.items()):
            print(f"  {net_name} -> {layer}")

    if not result.vias_added and not result.pads_skipped:
        if result.already_connected > 0:
            print(f"\nAll {result.already_connected} pads already connected.")
        else:
            print("\nNo unconnected pads found on target nets.")
        return

    # Group vias by net
    vias_by_net: dict[str, list[ViaPlacement]] = {}
    for via in result.vias_added:
        net = via.pad.net_name
        if net not in vias_by_net:
            vias_by_net[net] = []
        vias_by_net[net].append(via)

    # Output vias by net
    for net_name in sorted(vias_by_net.keys()):
        vias = vias_by_net[net_name]
        layer_target = vias[0].layers[1] if vias else ""
        print(f"\n{net_name} -> {layer_target}:")
        for via in vias[:10]:  # Limit output
            print(
                f"  Added via near {via.pad.reference}.{via.pad.pad_number} "
                f"@ ({via.via_x:.2f}, {via.via_y:.2f})"
            )
        if len(vias) > 10:
            print(f"  ... ({len(vias) - 10} more)")

    # Output skipped pads
    if result.pads_skipped:
        print("\nSkipped pads (manual placement needed):")
        for pad, reason in result.pads_skipped[:5]:
            print(f"  {pad.reference}.{pad.pad_number}: {reason}")
        if len(result.pads_skipped) > 5:
            print(f"  ... ({len(result.pads_skipped) - 5} more)")

    # Summary
    print(f"\n{'=' * 60}")
    print("Summary:")
    print(f"  + Added {len(result.vias_added)} stitching vias")
    if result.already_connected:
        print(f"  = {result.already_connected} pads already connected")
    if result.pads_skipped:
        print(f"  ! Skipped {len(result.pads_skipped)} pads (manual placement needed)")

    if dry_run:
        print("\n(dry run - no changes made)")
    else:
        print(f"\nRun DRC to verify: kicad-cli pcb drc {result.pcb_name}")


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kicad-pcb-stitch command."""
    parser = argparse.ArgumentParser(
        prog="kicad-pcb-stitch",
        description="Auto-add stitching vias for plane connections",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "pcb",
        help="Path to .kicad_pcb file",
    )
    parser.add_argument(
        "--net",
        "-n",
        action="append",
        dest="nets",
        required=True,
        help="Net name to add vias for (can be repeated)",
    )
    parser.add_argument(
        "--via-size",
        type=float,
        default=0.45,
        help="Via pad diameter in mm (default: 0.45)",
    )
    parser.add_argument(
        "--drill",
        type=float,
        default=0.2,
        help="Via drill size in mm (default: 0.2)",
    )
    parser.add_argument(
        "--clearance",
        type=float,
        default=0.2,
        help="Minimum clearance from existing copper in mm (default: 0.2)",
    )
    parser.add_argument(
        "--offset",
        type=float,
        default=0.5,
        help="Max distance from pad center for via placement in mm (default: 0.5)",
    )
    parser.add_argument(
        "--target-layer",
        "-t",
        help="Target plane layer (e.g., In1.Cu). Default: auto-detect",
    )
    parser.add_argument(
        "--dry-run",
        "-d",
        action="store_true",
        help="Show changes without applying",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output file (default: modify in place)",
    )

    args = parser.parse_args(argv)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {pcb_path.suffix}", file=sys.stderr)
        return 1

    # If output specified, copy to output first
    if args.output and not args.dry_run:
        output_path = Path(args.output)
        import shutil

        shutil.copy(pcb_path, output_path)
        pcb_path = output_path

    try:
        result = run_stitch(
            pcb_path=pcb_path,
            net_names=args.nets,
            via_size=args.via_size,
            drill=args.drill,
            clearance=args.clearance,
            offset=args.offset,
            target_layer=args.target_layer,
            dry_run=args.dry_run,
        )

        output_result(result, dry_run=args.dry_run)

        if result.vias_added:
            return 0
        else:
            return 0 if result.already_connected else 1

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""
Auto-add stitching vias for plane connections.

Automatically adds stitching vias to connect surface-mount component pads
to internal power/ground planes in multi-layer PCBs.

Usage:
    # Auto-detect all power plane nets from zones and stitch them
    kicad-pcb-stitch board.kicad_pcb

    # Stitch specific nets
    kicad-pcb-stitch board.kicad_pcb --net GND
    kicad-pcb-stitch board.kicad_pcb --net GND --net +3.3V
    kicad-pcb-stitch board.kicad_pcb --net GND --dry-run
    kicad-pcb-stitch board.kicad_pcb --net GND --via-size 0.45 --drill 0.2

    # Stitch and run DRC (fills zones automatically via kicad-cli)
    kicad-pcb-stitch board.kicad_pcb --drc

Exit Codes:
    0 - Success
    1 - Error or no work to do
"""

import argparse
import math
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.core.sexp_file import load_pcb, save_pcb, verify_pcb_write
from kicad_tools.sexp import SExp
from kicad_tools.sexp.builders import segment_node, via_node


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
class TrackSegment:
    """A track segment with start/end points, width, layer, and net."""

    start_x: float
    start_y: float
    end_x: float
    end_y: float
    width: float
    layer: str
    net_number: int


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
class TraceSegment:
    """Information about a trace segment connecting a pad to its via.

    For straight traces, only pad position and via position are used.
    For dog-leg (L-shaped) traces, intermediate_x/y specify the corner point.
    For extended escape traces, waypoints stores a list of (x, y) tuples
    forming a multi-segment path from pad to via.
    """

    pad: PadInfo
    via_x: float
    via_y: float
    width: float
    layer: str
    # For dog-leg traces: the intermediate corner point
    intermediate_x: float | None = None
    intermediate_y: float | None = None
    # For extended escape traces: list of (x, y) waypoints between pad and via
    waypoints: list[tuple[float, float]] | None = None

    @property
    def is_dogleg(self) -> bool:
        """Return True if this is an L-shaped (dog-leg) trace."""
        return self.intermediate_x is not None and self.intermediate_y is not None

    @property
    def is_extended_escape(self) -> bool:
        """Return True if this is a multi-waypoint extended escape trace."""
        return self.waypoints is not None and len(self.waypoints) > 0


@dataclass
class StitchResult:
    """Result of the stitching operation."""

    pcb_name: str
    target_nets: list[str]
    vias_added: list[ViaPlacement] = field(default_factory=list)
    traces_added: list[TraceSegment] = field(default_factory=list)
    pads_skipped: list[tuple[PadInfo, str]] = field(default_factory=list)  # (pad, reason)
    already_connected: int = 0
    # Per-net detected layers: {net_name: layer} for auto-detected layers
    detected_layers: dict[str, str] = field(default_factory=dict)
    # Nets that fell back to default B.Cu (no zone found)
    fallback_nets: list[str] = field(default_factory=list)


@dataclass
class FilledPolygon:
    """A filled polygon from a zone fill with bounding box for fast pre-filtering.

    Represents the actual copper pour geometry from a zone fill (as opposed to
    the zone boundary polygon). These are created by KiCad's zone fill
    algorithm and account for clearances, thermal reliefs, etc.
    """

    net_number: int
    net_name: str
    layer: str
    points: list[tuple[float, float]]
    # Bounding box for fast pre-filtering
    min_x: float = 0.0
    min_y: float = 0.0
    max_x: float = 0.0
    max_y: float = 0.0

    def __post_init__(self) -> None:
        if self.points:
            xs = [p[0] for p in self.points]
            ys = [p[1] for p in self.points]
            self.min_x = min(xs)
            self.max_x = max(xs)
            self.min_y = min(ys)
            self.max_y = max(ys)


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
            else:
                # KiCad 9 name-only format: (net "GND") without separate net_name node
                net_node = child.find_child("net")
                if net_node and net_node.get_int(0) is None:
                    zone_net_name = net_node.get_string(0)

            # Get layer from zone
            layer_node = child.find_child("layer")
            if layer_node:
                zone_layer = layer_node.get_string(0)

            if zone_net_name == net_name and zone_layer:
                layers.append(zone_layer)

    return layers


def find_all_plane_nets(sexp: SExp) -> dict[str, str]:
    """Find all nets that have copper zones (power planes).

    Scans the PCB for zones and returns a mapping of net names to their
    plane layers. This is used for automatic via stitching when no
    specific nets are provided.

    Args:
        sexp: PCB S-expression

    Returns:
        Dict mapping net name to plane layer (e.g., {"GND": "In1.Cu", "+3.3V": "In2.Cu"})
    """
    plane_nets: dict[str, str] = {}

    for child in sexp.iter_children():
        if child.tag == "zone":
            zone_net_name = None
            zone_layer = None

            # Get net_name from zone
            net_name_node = child.find_child("net_name")
            if net_name_node:
                zone_net_name = net_name_node.get_string(0)
            else:
                # KiCad 9 name-only format: (net "GND") without separate net_name node
                net_node = child.find_child("net")
                if net_node and net_node.get_int(0) is None:
                    zone_net_name = net_node.get_string(0)

            # Get layer from zone
            layer_node = child.find_child("layer")
            if layer_node:
                zone_layer = layer_node.get_string(0)

            # Only include zones with valid net names (skip empty nets)
            if zone_net_name and zone_layer and zone_net_name.strip():
                # If net already has a plane, keep the first one found
                if zone_net_name not in plane_nets:
                    plane_nets[zone_net_name] = zone_layer

    return plane_nets


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


def find_all_track_segments(sexp: SExp, exclude_nets: set[int] | None = None) -> list[TrackSegment]:
    """Find all track segments in the PCB, optionally excluding specific nets.

    Unlike find_existing_tracks() which only returns endpoints for same-net
    connectivity checks, this returns full segment geometry for clearance
    checking against other nets.

    Args:
        sexp: PCB S-expression
        exclude_nets: Net numbers to exclude (e.g., the nets being stitched)

    Returns:
        List of TrackSegment objects with full geometry
    """
    segments = []
    if exclude_nets is None:
        exclude_nets = set()

    for child in sexp.iter_children():
        if child.tag == "segment":
            net_node = child.find_child("net")
            if not net_node:
                continue
            net_num = net_node.get_int(0)
            if net_num is None or net_num in exclude_nets:
                continue

            start_node = child.find_child("start")
            end_node = child.find_child("end")
            if not start_node or not end_node:
                continue

            width_node = child.find_child("width")
            width = (width_node.get_float(0) or 0.2) if width_node else 0.2

            layer_node = child.find_child("layer")
            layer = (layer_node.get_string(0) or "F.Cu") if layer_node else "F.Cu"

            segments.append(
                TrackSegment(
                    start_x=start_node.get_float(0) or 0.0,
                    start_y=start_node.get_float(1) or 0.0,
                    end_x=end_node.get_float(0) or 0.0,
                    end_y=end_node.get_float(1) or 0.0,
                    width=width,
                    layer=layer,
                    net_number=net_num,
                )
            )
    return segments


def find_all_board_vias(
    sexp: SExp, exclude_nets: set[int] | None = None
) -> list[tuple[float, float, float, int]]:
    """Find all vias in the PCB, optionally excluding specific nets.

    Returns list of (x, y, size, net_num) for clearance checking against
    copper on other nets.

    Args:
        sexp: PCB S-expression
        exclude_nets: Net numbers to exclude

    Returns:
        List of (x, y, size, net_num) tuples
    """
    vias = []
    if exclude_nets is None:
        exclude_nets = set()

    for child in sexp.iter_children():
        if child.tag == "via":
            net_node = child.find_child("net")
            if not net_node:
                continue
            net_num = net_node.get_int(0)
            if net_num is None or net_num in exclude_nets:
                continue

            at_node = child.find_child("at")
            size_node = child.find_child("size")
            if not at_node:
                continue

            x = at_node.get_float(0) or 0.0
            y = at_node.get_float(1) or 0.0
            size = (size_node.get_float(0) or 0.45) if size_node else 0.45

            vias.append((x, y, size, net_num))
    return vias


def find_all_pads(
    sexp: SExp, exclude_nets: set[int] | None = None
) -> list[tuple[float, float, float, int]]:
    """Find all pads in the PCB, optionally excluding specific nets.

    Returns list of (x, y, radius, net_num) for clearance checking against
    copper on other nets. Pad positions are transformed to board coordinates
    using the footprint's position and rotation.

    Args:
        sexp: PCB S-expression
        exclude_nets: Net numbers to exclude (e.g., the nets being stitched)

    Returns:
        List of (x, y, radius, net_num) tuples where:
        - x, y are board coordinates (mm)
        - radius is effective copper radius from pad size (mm)
        - net_num is the pad's net (0 for unconnected)
    """
    pads = []
    if exclude_nets is None:
        exclude_nets = set()

    for fp in sexp.iter_children():
        if fp.tag != "footprint":
            continue

        # Get footprint position and rotation
        at_node = fp.find_child("at")
        if not at_node:
            continue
        fp_x = at_node.get_float(0) or 0.0
        fp_y = at_node.get_float(1) or 0.0
        fp_rot = math.radians(at_node.get_float(2) or 0.0)

        cos_rot = math.cos(fp_rot)
        sin_rot = math.sin(fp_rot)

        # Extract each pad
        for pad in fp.find_children("pad"):
            net_node = pad.find_child("net")
            if not net_node:
                continue
            net_num = net_node.get_int(0)
            if net_num is None or net_num in exclude_nets:
                continue

            # Get pad position (relative to footprint)
            pad_at = pad.find_child("at")
            if not pad_at:
                continue
            rel_x = pad_at.get_float(0) or 0.0
            rel_y = pad_at.get_float(1) or 0.0

            # Transform to board coordinates
            board_x = fp_x + rel_x * cos_rot - rel_y * sin_rot
            board_y = fp_y + rel_x * sin_rot + rel_y * cos_rot

            # Get pad size for radius calculation
            size_node = pad.find_child("size")
            if not size_node:
                continue
            width = size_node.get_float(0) or 0.0
            height = size_node.get_float(1) or 0.0
            # Conservative: use largest dimension as bounding circle
            radius = max(width, height) / 2

            pads.append((board_x, board_y, radius, net_num))

    return pads


def point_to_segment_distance(
    px: float, py: float, sx: float, sy: float, ex: float, ey: float
) -> float:
    """Calculate minimum distance from point (px, py) to line segment (sx,sy)-(ex,ey).

    Uses projection of the point onto the line defined by the segment,
    clamped to the segment endpoints.

    Args:
        px, py: Point coordinates
        sx, sy: Segment start coordinates
        ex, ey: Segment end coordinates

    Returns:
        Minimum distance from point to segment
    """
    dx = ex - sx
    dy = ey - sy
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq == 0:
        # Degenerate segment (zero length)
        return math.sqrt((px - sx) ** 2 + (py - sy) ** 2)

    # Parameter t for projection of point onto line
    t = ((px - sx) * dx + (py - sy) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))

    # Nearest point on segment
    nearest_x = sx + t * dx
    nearest_y = sy + t * dy

    return math.sqrt((px - nearest_x) ** 2 + (py - nearest_y) ** 2)


def segment_to_segment_distance(
    a_sx: float,
    a_sy: float,
    a_ex: float,
    a_ey: float,
    b_sx: float,
    b_sy: float,
    b_ex: float,
    b_ey: float,
) -> float:
    """Calculate minimum distance between two line segments.

    Checks endpoints of each segment against the other segment, and also
    checks for intersection (distance = 0). This covers all cases for
    minimum distance between two finite line segments.

    Args:
        a_sx, a_sy: Segment A start
        a_ex, a_ey: Segment A end
        b_sx, b_sy: Segment B start
        b_ex, b_ey: Segment B end

    Returns:
        Minimum distance between the two segments
    """
    # Check all four endpoint-to-segment distances
    d1 = point_to_segment_distance(a_sx, a_sy, b_sx, b_sy, b_ex, b_ey)
    d2 = point_to_segment_distance(a_ex, a_ey, b_sx, b_sy, b_ex, b_ey)
    d3 = point_to_segment_distance(b_sx, b_sy, a_sx, a_sy, a_ex, a_ey)
    d4 = point_to_segment_distance(b_ex, b_ey, a_sx, a_sy, a_ex, a_ey)

    min_dist = min(d1, d2, d3, d4)

    # Check for intersection: if segments cross, distance is 0
    if min_dist > 0:
        # Use cross product method to check intersection
        d1x = a_ex - a_sx
        d1y = a_ey - a_sy
        d2x = b_ex - b_sx
        d2y = b_ey - b_sy

        denom = d1x * d2y - d1y * d2x
        if abs(denom) > 1e-12:
            t = ((b_sx - a_sx) * d2y - (b_sy - a_sy) * d2x) / denom
            u = ((b_sx - a_sx) * d1y - (b_sy - a_sy) * d1x) / denom
            if 0 <= t <= 1 and 0 <= u <= 1:
                return 0.0

    return min_dist


def is_pad_connected(
    pad: PadInfo,
    vias: list[tuple[float, float, int]],
    track_points: list[tuple[float, float, int]],
    connection_radius: float = 0.5,
) -> bool:
    """Check if a pad has any connection (via or track) nearby."""
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
    other_net_tracks: list[TrackSegment] | None = None,
    other_net_vias: list[tuple[float, float, float, int]] | None = None,
    other_net_pads: list[tuple[float, float, float, int]] | None = None,
    trace_width: float = 0.0,
    other_net_filled_polygons: list[FilledPolygon] | None = None,
) -> tuple[float, float] | None:
    """Calculate a valid via placement position near the pad.

    Tries to place the via offset from the pad center, checking for conflicts
    with both same-net vias and other-net copper (tracks, vias, pads, and
    zone fill polygons).
    When trace_width > 0, also checks the connecting trace path from pad
    center to via center for clearance violations.
    Returns None if no valid position found.

    Args:
        pad: The pad to place a via near
        offset: Distance offset from pad edge
        via_size: Via pad diameter in mm
        existing_vias: Same-net vias as (x, y, net_num) for via-to-via spacing
        clearance: Minimum clearance from existing copper in mm
        other_net_tracks: Track segments on other nets for clearance checking
        other_net_vias: Vias on other nets as (x, y, size, net_num) for clearance
        other_net_pads: Pads on other nets as (x, y, radius, net_num) for clearance
        trace_width: Width of the connecting trace from pad to via in mm.
            When > 0, the trace path is checked for clearance violations.
        other_net_filled_polygons: Filled polygons from other-net zone fills
    """
    if other_net_tracks is None:
        other_net_tracks = []
    if other_net_vias is None:
        other_net_vias = []
    if other_net_pads is None:
        other_net_pads = []
    if other_net_filled_polygons is None:
        other_net_filled_polygons = []

    via_radius = via_size / 2
    trace_half_width = trace_width / 2

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

            # Check for conflicts with existing same-net vias
            conflict = False
            for vx, vy, _vnet in existing_vias:
                dist = math.sqrt((vx - via_x) ** 2 + (vy - via_y) ** 2)
                if dist < via_size + clearance:
                    conflict = True
                    break

            if conflict:
                continue

            # Check for conflicts with other-net track segments
            for seg in other_net_tracks:
                dist = point_to_segment_distance(
                    via_x, via_y, seg.start_x, seg.start_y, seg.end_x, seg.end_y
                )
                # Clearance is from via edge to track edge
                min_dist = via_radius + seg.width / 2 + clearance
                if dist < min_dist:
                    conflict = True
                    break

            if conflict:
                continue

            # Check for conflicts with other-net vias
            for ovx, ovy, ov_size, _onet in other_net_vias:
                dist = math.sqrt((ovx - via_x) ** 2 + (ovy - via_y) ** 2)
                # Clearance is from via edge to other via edge
                min_dist = via_radius + ov_size / 2 + clearance
                if dist < min_dist:
                    conflict = True
                    break

            if conflict:
                continue

            # Check for conflicts with other-net pads
            for px, py, p_radius, _pnet in other_net_pads:
                dist = math.sqrt((px - via_x) ** 2 + (py - via_y) ** 2)
                # Clearance is from via edge to pad edge
                min_dist = via_radius + p_radius + clearance
                if dist < min_dist:
                    conflict = True
                    break

            if conflict:
                continue

            # Check for conflicts with other-net filled polygons (zone fill copper)
            if other_net_filled_polygons:
                if not _check_point_filled_polygon_clearance(
                    via_x, via_y, via_radius, other_net_filled_polygons, clearance
                ):
                    conflict = True

            if conflict:
                continue

            # Check connecting trace path (pad center -> via center) for clearance
            if trace_width > 0:
                # Check trace path against other-net track segments
                for seg in other_net_tracks:
                    dist = segment_to_segment_distance(
                        pad.x,
                        pad.y,
                        via_x,
                        via_y,
                        seg.start_x,
                        seg.start_y,
                        seg.end_x,
                        seg.end_y,
                    )
                    # Clearance from trace edge to track edge
                    min_dist = trace_half_width + seg.width / 2 + clearance
                    if dist < min_dist:
                        conflict = True
                        break

                if conflict:
                    continue

                # Check trace path against other-net vias
                for ovx, ovy, ov_size, _onet in other_net_vias:
                    dist = point_to_segment_distance(
                        ovx,
                        ovy,
                        pad.x,
                        pad.y,
                        via_x,
                        via_y,
                    )
                    # Clearance from trace edge to other via edge
                    min_dist = trace_half_width + ov_size / 2 + clearance
                    if dist < min_dist:
                        conflict = True
                        break

                if conflict:
                    continue

                # Check trace path against other-net filled polygons
                if other_net_filled_polygons:
                    if not _check_segment_filled_polygon_clearance(
                        pad.x,
                        pad.y,
                        via_x,
                        via_y,
                        trace_half_width,
                        other_net_filled_polygons,
                        clearance,
                    ):
                        continue

            return (via_x, via_y)

    return None


def _check_dogleg_path_clearance(
    pad_x: float,
    pad_y: float,
    intermediate_x: float,
    intermediate_y: float,
    via_x: float,
    via_y: float,
    trace_half_width: float,
    other_net_tracks: list[TrackSegment],
    other_net_vias: list[tuple[float, float, float, int]],
    other_net_pads: list[tuple[float, float, float, int]],
    clearance: float,
    other_net_filled_polygons: list[FilledPolygon] | None = None,
) -> bool:
    """Check if a dog-leg (L-shaped) trace path has adequate clearance.

    The path consists of two segments:
    1. Pad center -> intermediate point (first leg)
    2. Intermediate point -> via center (second leg)

    Returns True if path is clear, False if there's a conflict.
    """
    if other_net_filled_polygons is None:
        other_net_filled_polygons = []

    # Define the two path segments
    legs = [
        (pad_x, pad_y, intermediate_x, intermediate_y),  # First leg
        (intermediate_x, intermediate_y, via_x, via_y),  # Second leg
    ]

    for leg_sx, leg_sy, leg_ex, leg_ey in legs:
        # Check against other-net track segments
        for seg in other_net_tracks:
            dist = segment_to_segment_distance(
                leg_sx,
                leg_sy,
                leg_ex,
                leg_ey,
                seg.start_x,
                seg.start_y,
                seg.end_x,
                seg.end_y,
            )
            min_dist = trace_half_width + seg.width / 2 + clearance
            if dist < min_dist:
                return False

        # Check against other-net vias
        for ovx, ovy, ov_size, _onet in other_net_vias:
            dist = point_to_segment_distance(ovx, ovy, leg_sx, leg_sy, leg_ex, leg_ey)
            min_dist = trace_half_width + ov_size / 2 + clearance
            if dist < min_dist:
                return False

        # Check against other-net pads
        for px, py, p_radius, _pnet in other_net_pads:
            dist = point_to_segment_distance(px, py, leg_sx, leg_sy, leg_ex, leg_ey)
            min_dist = trace_half_width + p_radius + clearance
            if dist < min_dist:
                return False

        # Check against other-net filled polygons (zone fill copper)
        if other_net_filled_polygons:
            if not _check_segment_filled_polygon_clearance(
                leg_sx,
                leg_sy,
                leg_ex,
                leg_ey,
                trace_half_width,
                other_net_filled_polygons,
                clearance,
            ):
                return False

    return True


def calculate_dogleg_via_position(
    pad: PadInfo,
    offset: float,
    via_size: float,
    existing_vias: list[tuple[float, float, int]],
    clearance: float,
    other_net_tracks: list[TrackSegment] | None = None,
    other_net_vias: list[tuple[float, float, float, int]] | None = None,
    other_net_pads: list[tuple[float, float, float, int]] | None = None,
    trace_width: float = 0.0,
    other_net_filled_polygons: list[FilledPolygon] | None = None,
) -> tuple[float, float, float, float] | None:
    """Calculate a dog-leg (L-shaped) via placement for fine-pitch components.

    When straight-line routing fails due to adjacent pads on different nets,
    this function tries L-shaped routing: first moving along the pad row
    (axially), then perpendicular to reach clear space.

    This is useful for fine-pitch components (e.g., SSOP with 0.65mm pitch)
    where adjacent pads on different nets leave insufficient clearance for
    straight-line via placement.

    Args:
        pad: The pad to place a via near
        offset: Base distance offset from pad edge
        via_size: Via pad diameter in mm
        existing_vias: Same-net vias as (x, y, net_num) for via-to-via spacing
        clearance: Minimum clearance from existing copper in mm
        other_net_tracks: Track segments on other nets for clearance checking
        other_net_vias: Vias on other nets as (x, y, size, net_num) for clearance
        other_net_pads: Pads on other nets as (x, y, radius, net_num) for clearance
        trace_width: Width of the connecting trace in mm
        other_net_filled_polygons: Filled polygons from other-net zone fills

    Returns:
        Tuple of (via_x, via_y, intermediate_x, intermediate_y) for an L-shaped
        path, or None if no valid position found.
    """
    if other_net_tracks is None:
        other_net_tracks = []
    if other_net_vias is None:
        other_net_vias = []
    if other_net_pads is None:
        other_net_pads = []
    if other_net_filled_polygons is None:
        other_net_filled_polygons = []

    via_radius = via_size / 2
    trace_half_width = trace_width / 2

    pad_radius = max(pad.width, pad.height) / 2

    # Determine the dominant alignment direction based on nearby other-net pads
    # This helps us route along the pad row first, then escape perpendicular
    nearby_pads = [
        (px, py)
        for px, py, _r, pnet in other_net_pads
        if pnet != pad.net_number and abs(px - pad.x) < 1.5 and abs(py - pad.y) < 1.5
    ]

    # Determine primary and secondary axes based on pad row orientation
    if len(nearby_pads) >= 1:
        # Calculate spread in X and Y among nearby pads
        xs = [px for px, _ in nearby_pads]
        ys = [py for _, py in nearby_pads]
        x_spread = max(xs) - min(xs) if len(xs) > 1 else 0
        y_spread = max(ys) - min(ys) if len(ys) > 1 else 0

        # If pads are spread more horizontally, the row is horizontal
        # -> axial movement should be horizontal, escape should be vertical
        if x_spread >= y_spread:
            axial_dirs = [(1, 0), (-1, 0)]  # Move along horizontal row
            escape_dirs = [
                (0, 1),
                (0, -1),
                (0.707, 0.707),
                (-0.707, 0.707),
                (0.707, -0.707),
                (-0.707, -0.707),
            ]  # Escape vertically
        else:
            axial_dirs = [(0, 1), (0, -1)]  # Move along vertical row
            escape_dirs = [
                (1, 0),
                (-1, 0),
                (0.707, 0.707),
                (0.707, -0.707),
                (-0.707, 0.707),
                (-0.707, -0.707),
            ]  # Escape horizontally
    else:
        # No clear row orientation, try all combinations
        axial_dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        escape_dirs = [
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
            (0.707, 0.707),
            (-0.707, 0.707),
            (-0.707, -0.707),
            (0.707, -0.707),
        ]

    # Axial distances: how far to move along the row before turning
    axial_distances = [0.3, 0.5, 0.7, 1.0, 1.3]

    # Escape offsets: how far to move perpendicular after the axial step
    escape_offsets = [
        pad_radius + offset * 0.75,
        pad_radius + offset,
        pad_radius + offset * 1.5,
        pad_radius + offset * 2,
    ]

    for axial_dx, axial_dy in axial_dirs:
        for axial_dist in axial_distances:
            # Calculate the intermediate (corner) point
            intermediate_x = pad.x + axial_dx * axial_dist
            intermediate_y = pad.y + axial_dy * axial_dist

            for escape_dx, escape_dy in escape_dirs:
                # Skip if escape direction is the same as axial direction
                # (that would be a straight line, not a dog-leg)
                if abs(axial_dx * escape_dx + axial_dy * escape_dy) > 0.9:
                    continue

                for escape_offset in escape_offsets:
                    via_x = intermediate_x + escape_dx * escape_offset
                    via_y = intermediate_y + escape_dy * escape_offset

                    # Check via position clearance (same as straight-line)
                    conflict = False

                    # Check same-net via spacing
                    for vx, vy, _vnet in existing_vias:
                        dist = math.sqrt((vx - via_x) ** 2 + (vy - via_y) ** 2)
                        if dist < via_size + clearance:
                            conflict = True
                            break
                    if conflict:
                        continue

                    # Check other-net track clearance at via position
                    for seg in other_net_tracks:
                        dist = point_to_segment_distance(
                            via_x, via_y, seg.start_x, seg.start_y, seg.end_x, seg.end_y
                        )
                        min_dist = via_radius + seg.width / 2 + clearance
                        if dist < min_dist:
                            conflict = True
                            break
                    if conflict:
                        continue

                    # Check other-net via clearance at via position
                    for ovx, ovy, ov_size, _onet in other_net_vias:
                        dist = math.sqrt((ovx - via_x) ** 2 + (ovy - via_y) ** 2)
                        min_dist = via_radius + ov_size / 2 + clearance
                        if dist < min_dist:
                            conflict = True
                            break
                    if conflict:
                        continue

                    # Check other-net pad clearance at via position
                    for px, py, p_radius, _pnet in other_net_pads:
                        dist = math.sqrt((px - via_x) ** 2 + (py - via_y) ** 2)
                        min_dist = via_radius + p_radius + clearance
                        if dist < min_dist:
                            conflict = True
                            break
                    if conflict:
                        continue

                    # Check other-net filled polygon clearance at via position
                    if other_net_filled_polygons:
                        if not _check_point_filled_polygon_clearance(
                            via_x,
                            via_y,
                            via_radius,
                            other_net_filled_polygons,
                            clearance,
                        ):
                            conflict = True
                    if conflict:
                        continue

                    # Check the entire L-shaped path for clearance
                    if trace_width > 0:
                        if not _check_dogleg_path_clearance(
                            pad.x,
                            pad.y,
                            intermediate_x,
                            intermediate_y,
                            via_x,
                            via_y,
                            trace_half_width,
                            other_net_tracks,
                            other_net_vias,
                            other_net_pads,
                            clearance,
                            other_net_filled_polygons,
                        ):
                            continue

                    return (via_x, via_y, intermediate_x, intermediate_y)

    return None


def _check_multileg_path_clearance(
    points: list[tuple[float, float]],
    trace_half_width: float,
    other_net_tracks: list[TrackSegment],
    other_net_vias: list[tuple[float, float, float, int]],
    other_net_pads: list[tuple[float, float, float, int]],
    clearance: float,
) -> bool:
    """Check if a multi-segment trace path has adequate clearance.

    The path consists of segments between consecutive points in the list.

    Returns True if path is clear, False if there's a conflict.
    """
    for i in range(len(points) - 1):
        leg_sx, leg_sy = points[i]
        leg_ex, leg_ey = points[i + 1]

        # Check against other-net track segments
        for seg in other_net_tracks:
            dist = segment_to_segment_distance(
                leg_sx,
                leg_sy,
                leg_ex,
                leg_ey,
                seg.start_x,
                seg.start_y,
                seg.end_x,
                seg.end_y,
            )
            min_dist = trace_half_width + seg.width / 2 + clearance
            if dist < min_dist:
                return False

        # Check against other-net vias
        for ovx, ovy, ov_size, _onet in other_net_vias:
            dist = point_to_segment_distance(ovx, ovy, leg_sx, leg_sy, leg_ex, leg_ey)
            min_dist = trace_half_width + ov_size / 2 + clearance
            if dist < min_dist:
                return False

        # Check against other-net pads
        for px, py, p_radius, _pnet in other_net_pads:
            dist = point_to_segment_distance(px, py, leg_sx, leg_sy, leg_ex, leg_ey)
            min_dist = trace_half_width + p_radius + clearance
            if dist < min_dist:
                return False

    return True


def calculate_extended_escape_position(
    pad: PadInfo,
    offset: float,
    via_size: float,
    existing_vias: list[tuple[float, float, int]],
    clearance: float,
    escape_distance: float = 3.0,
    other_net_tracks: list[TrackSegment] | None = None,
    other_net_vias: list[tuple[float, float, float, int]] | None = None,
    other_net_pads: list[tuple[float, float, float, int]] | None = None,
    trace_width: float = 0.0,
) -> tuple[float, float, list[tuple[float, float]]] | None:
    """Calculate an extended escape route for pads in dense IC pin fields.

    When both straight-line and dog-leg placement fail (typically on dense
    QFP/BGA packages), this function searches progressively farther from
    the pad, using multi-segment escape traces that navigate between
    neighboring pins.

    The approach:
    1. Identify the best escape channel direction by analyzing nearby pads
    2. Route along that channel with increasing distance
    3. Then break out perpendicular to find open via placement space

    Args:
        pad: The pad to place a via near
        offset: Base distance offset from pad edge
        via_size: Via pad diameter in mm
        existing_vias: Same-net vias as (x, y, net_num) for via-to-via spacing
        clearance: Minimum clearance from existing copper in mm
        escape_distance: Maximum total escape trace length in mm (default 3.0)
        other_net_tracks: Track segments on other nets for clearance checking
        other_net_vias: Vias on other nets as (x, y, size, net_num) for clearance
        other_net_pads: Pads on other nets as (x, y, radius, net_num) for clearance
        trace_width: Width of the connecting trace in mm

    Returns:
        Tuple of (via_x, via_y, waypoints) where waypoints is a list of
        (x, y) intermediate points, or None if no valid position found.
    """
    if other_net_tracks is None:
        other_net_tracks = []
    if other_net_vias is None:
        other_net_vias = []
    if other_net_pads is None:
        other_net_pads = []

    via_radius = via_size / 2
    trace_half_width = trace_width / 2
    pad_radius = max(pad.width, pad.height) / 2

    # Analyze nearby other-net pads to find escape channels
    nearby_pads = [
        (px, py)
        for px, py, _r, pnet in other_net_pads
        if pnet != pad.net_number
        and math.sqrt((px - pad.x) ** 2 + (py - pad.y) ** 2) < escape_distance * 1.5
    ]

    # Determine primary and secondary axes based on pad row orientation
    if len(nearby_pads) >= 2:
        xs = [px for px, _ in nearby_pads]
        ys = [py for _, py in nearby_pads]
        x_spread = max(xs) - min(xs)
        y_spread = max(ys) - min(ys)

        # Pads spread more horizontally -> row is horizontal
        # Escape perpendicular (vertical) is preferred
        if x_spread >= y_spread:
            # Horizontal row: axial along X, escape along Y
            axial_dirs = [(1, 0), (-1, 0)]
            escape_dirs = [(0, 1), (0, -1)]
        else:
            # Vertical row: axial along Y, escape along X
            axial_dirs = [(0, 1), (0, -1)]
            escape_dirs = [(1, 0), (-1, 0)]
    else:
        axial_dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        escape_dirs = [(0, 1), (0, -1), (1, 0), (-1, 0)]

    # Progressive axial distances for the first leg (along the pin row)
    # These are larger than dogleg to escape past multiple neighboring pins
    axial_distances = [0.5, 0.8, 1.0, 1.3, 1.6, 2.0, 2.5, 3.0, 3.5, 4.0]
    axial_distances = [d for d in axial_distances if d <= escape_distance]

    # Escape offsets for the final breakout leg (perpendicular to pin row)
    breakout_offsets = [
        pad_radius + offset,
        pad_radius + offset * 1.5,
        pad_radius + offset * 2,
        pad_radius + offset * 2.5,
        pad_radius + offset * 3,
    ]
    breakout_offsets = [d for d in breakout_offsets if d <= escape_distance]

    def _check_via_position(vx: float, vy: float) -> bool:
        """Check if a via position is clear of conflicts."""
        # Check same-net via spacing
        for evx, evy, _vnet in existing_vias:
            dist = math.sqrt((evx - vx) ** 2 + (evy - vy) ** 2)
            if dist < via_size + clearance:
                return False

        # Check other-net track clearance
        for seg in other_net_tracks:
            dist = point_to_segment_distance(
                vx, vy, seg.start_x, seg.start_y, seg.end_x, seg.end_y
            )
            if dist < via_radius + seg.width / 2 + clearance:
                return False

        # Check other-net via clearance
        for ovx, ovy, ov_size, _onet in other_net_vias:
            dist = math.sqrt((ovx - vx) ** 2 + (ovy - vy) ** 2)
            if dist < via_radius + ov_size / 2 + clearance:
                return False

        # Check other-net pad clearance
        for px, py, p_radius, _pnet in other_net_pads:
            dist = math.sqrt((px - vx) ** 2 + (py - vy) ** 2)
            if dist < via_radius + p_radius + clearance:
                return False

        return True

    # Strategy 1: Escape-first L-shape (perpendicular out, then axial)
    # For dense pin rows, escape perpendicular first (between pins in the row)
    # then extend axially to find an open via position.
    for escape_dx, escape_dy in escape_dirs:
        for breakout in breakout_offsets:
            waypoint_x = pad.x + escape_dx * breakout
            waypoint_y = pad.y + escape_dy * breakout

            for axial_dx, axial_dy in axial_dirs:
                # Skip if axial is same as escape (would be straight line)
                if abs(axial_dx * escape_dx + axial_dy * escape_dy) > 0.1:
                    continue

                for axial_dist in axial_distances:
                    via_x = waypoint_x + axial_dx * axial_dist
                    via_y = waypoint_y + axial_dy * axial_dist

                    total_len = breakout + axial_dist
                    if total_len > escape_distance:
                        continue

                    if not _check_via_position(via_x, via_y):
                        continue

                    path_points = [
                        (pad.x, pad.y),
                        (waypoint_x, waypoint_y),
                        (via_x, via_y),
                    ]

                    if trace_width > 0:
                        if not _check_multileg_path_clearance(
                            path_points,
                            trace_half_width,
                            other_net_tracks,
                            other_net_vias,
                            other_net_pads,
                            clearance,
                        ):
                            continue

                    waypoints = [(waypoint_x, waypoint_y)]
                    return (via_x, via_y, waypoints)

    # Strategy 2: Axial-first L-shape (along row, then perpendicular out)
    # Move along the pin row first to get past neighboring pins, then
    # break out perpendicular.
    for axial_dx, axial_dy in axial_dirs:
        for axial_dist in axial_distances:
            waypoint_x = pad.x + axial_dx * axial_dist
            waypoint_y = pad.y + axial_dy * axial_dist

            for escape_dx, escape_dy in escape_dirs:
                if abs(axial_dx * escape_dx + axial_dy * escape_dy) > 0.1:
                    continue

                for breakout in breakout_offsets:
                    via_x = waypoint_x + escape_dx * breakout
                    via_y = waypoint_y + escape_dy * breakout

                    total_len = axial_dist + breakout
                    if total_len > escape_distance:
                        continue

                    if not _check_via_position(via_x, via_y):
                        continue

                    path_points = [
                        (pad.x, pad.y),
                        (waypoint_x, waypoint_y),
                        (via_x, via_y),
                    ]

                    if trace_width > 0:
                        if not _check_multileg_path_clearance(
                            path_points,
                            trace_half_width,
                            other_net_tracks,
                            other_net_vias,
                            other_net_pads,
                            clearance,
                        ):
                            continue

                    waypoints = [(waypoint_x, waypoint_y)]
                    return (via_x, via_y, waypoints)

    # Strategy 3: Z-shaped (3-segment) escape for very dense areas
    # Escape -> axial -> escape (perpendicular step out, axial travel, final breakout)
    # This allows navigating around rows of blocking pads
    step_distances = [0.3, 0.5, 0.7, 1.0]
    step_distances = [d for d in step_distances if d <= escape_distance / 3]

    for escape_dx, escape_dy in escape_dirs:
        for step_dist in step_distances:
            wp1_x = pad.x + escape_dx * step_dist
            wp1_y = pad.y + escape_dy * step_dist

            for axial_dx, axial_dy in axial_dirs:
                if abs(axial_dx * escape_dx + axial_dy * escape_dy) > 0.1:
                    continue

                for axial_dist in axial_distances[:5]:
                    wp2_x = wp1_x + axial_dx * axial_dist
                    wp2_y = wp1_y + axial_dy * axial_dist

                    # Final breakout perpendicular
                    for breakout in breakout_offsets:
                        via_x = wp2_x + escape_dx * breakout
                        via_y = wp2_y + escape_dy * breakout

                        total_len = step_dist + axial_dist + breakout
                        if total_len > escape_distance:
                            continue

                        if not _check_via_position(via_x, via_y):
                            continue

                        path_points = [
                            (pad.x, pad.y),
                            (wp1_x, wp1_y),
                            (wp2_x, wp2_y),
                            (via_x, via_y),
                        ]

                        if trace_width > 0:
                            if not _check_multileg_path_clearance(
                                path_points,
                                trace_half_width,
                                other_net_tracks,
                                other_net_vias,
                                other_net_pads,
                                clearance,
                            ):
                                continue

                        waypoints = [
                            (wp1_x, wp1_y),
                            (wp2_x, wp2_y),
                        ]
                        return (via_x, via_y, waypoints)

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


def add_trace_to_pcb(sexp: SExp, trace: TraceSegment) -> None:
    """Add trace segment(s) from pad center to via center.

    For straight traces, adds a single segment.
    For dog-leg (L-shaped) traces, adds two segments: pad -> corner -> via.
    For extended escape traces, adds multiple segments through waypoints.
    """
    if trace.is_extended_escape:
        # Extended escape trace: multiple segments through waypoints
        # Build the full path: pad -> waypoints -> via
        points = [(trace.pad.x, trace.pad.y)]
        points.extend(trace.waypoints)
        points.append((trace.via_x, trace.via_y))

        for i in range(len(points) - 1):
            seg = segment_node(
                start_x=points[i][0],
                start_y=points[i][1],
                end_x=points[i + 1][0],
                end_y=points[i + 1][1],
                width=trace.width,
                layer=trace.layer,
                net=trace.pad.net_number,
                uuid_str=str(uuid.uuid4()),
            )
            sexp.append(seg)
    elif trace.is_dogleg:
        # Dog-leg trace: two segments forming an L-shape
        # First segment: pad center to intermediate corner point
        seg1 = segment_node(
            start_x=trace.pad.x,
            start_y=trace.pad.y,
            end_x=trace.intermediate_x,
            end_y=trace.intermediate_y,
            width=trace.width,
            layer=trace.layer,
            net=trace.pad.net_number,
            uuid_str=str(uuid.uuid4()),
        )
        sexp.append(seg1)

        # Second segment: intermediate corner point to via center
        seg2 = segment_node(
            start_x=trace.intermediate_x,
            start_y=trace.intermediate_y,
            end_x=trace.via_x,
            end_y=trace.via_y,
            width=trace.width,
            layer=trace.layer,
            net=trace.pad.net_number,
            uuid_str=str(uuid.uuid4()),
        )
        sexp.append(seg2)
    else:
        # Straight trace: single segment
        seg = segment_node(
            start_x=trace.pad.x,
            start_y=trace.pad.y,
            end_x=trace.via_x,
            end_y=trace.via_y,
            width=trace.width,
            layer=trace.layer,
            net=trace.pad.net_number,
            uuid_str=str(uuid.uuid4()),
        )
        sexp.append(seg)


@dataclass
class ZonePolygon:
    """A zone boundary polygon with its net and layer."""

    net_name: str
    layer: str
    points: list[tuple[float, float]]


def extract_zone_polygons(sexp: SExp, net_name: str) -> list[ZonePolygon]:
    """Extract zone boundary polygons for a given net.

    Parses the S-expression structure:
        (zone ... (polygon (pts (xy X Y) (xy X Y) ...)))

    Supports both KiCad 8 (net_name node) and KiCad 9 (name-only net node)
    formats.

    Args:
        sexp: PCB S-expression
        net_name: Net name to find zone polygons for

    Returns:
        List of ZonePolygon objects with boundary points
    """
    polygons = []
    for child in sexp.iter_children():
        if child.tag != "zone":
            continue

        # Get net name from zone (same dual-format logic as find_zones_for_net)
        zone_net_name = None
        net_name_node = child.find_child("net_name")
        if net_name_node:
            zone_net_name = net_name_node.get_string(0)
        else:
            # KiCad 9 name-only format: (net "GND") without separate net_name node
            net_node = child.find_child("net")
            if net_node and net_node.get_int(0) is None:
                zone_net_name = net_node.get_string(0)

        if zone_net_name != net_name:
            continue

        # Get layer
        layer_node = child.find_child("layer")
        if not layer_node:
            continue
        zone_layer = layer_node.get_string(0)
        if not zone_layer:
            continue

        # Get polygon boundary
        polygon_node = child.find_child("polygon")
        if not polygon_node:
            continue

        pts_node = polygon_node.find_child("pts")
        if not pts_node:
            continue

        points: list[tuple[float, float]] = []
        for xy_node in pts_node.find_children("xy"):
            x = xy_node.get_float(0) or 0.0
            y = xy_node.get_float(1) or 0.0
            points.append((x, y))

        if len(points) >= 3:
            polygons.append(ZonePolygon(net_name=net_name, layer=zone_layer, points=points))

    return polygons


def find_all_filled_polygons(
    sexp: SExp, exclude_nets: set[int] | None = None
) -> list[FilledPolygon]:
    """Extract filled polygon geometry from zone fills for clearance checking.

    After KiCad fills zones (via ``kicad-cli pcb drc``), each zone contains
    ``(filled_polygon ...)`` nodes representing the actual copper pour.  These
    must be checked during via placement to avoid clearance violations with
    other-net zone fill copper.

    Args:
        sexp: PCB S-expression
        exclude_nets: Net numbers to exclude (e.g., the nets being stitched)

    Returns:
        List of FilledPolygon objects with point lists and bounding boxes
    """
    polygons: list[FilledPolygon] = []
    if exclude_nets is None:
        exclude_nets = set()

    net_map = get_net_map(sexp)

    for child in sexp.iter_children():
        if child.tag != "zone":
            continue

        # Get net number from zone
        net_node = child.find_child("net")
        if not net_node:
            continue
        net_num = net_node.get_int(0)
        # KiCad 9 name-only format: (net "GND") -- get_int returns None
        if net_num is None:
            net_name_str = net_node.get_string(0)
            if net_name_str:
                # Reverse-lookup net number from name
                for num, name in net_map.items():
                    if name == net_name_str:
                        net_num = num
                        break
            if net_num is None:
                continue
        if net_num in exclude_nets:
            continue

        # Get net name
        zone_net_name = net_map.get(net_num, "")
        net_name_node = child.find_child("net_name")
        if net_name_node:
            zone_net_name = net_name_node.get_string(0) or zone_net_name

        # Get layer
        layer_node = child.find_child("layer")
        if not layer_node:
            continue
        zone_layer = layer_node.get_string(0)
        if not zone_layer:
            continue

        # Extract filled_polygon nodes
        for filled_poly_node in child.find_children("filled_polygon"):
            pts_node = filled_poly_node.find_child("pts")
            if not pts_node:
                continue

            points: list[tuple[float, float]] = []
            for xy_node in pts_node.find_children("xy"):
                x = xy_node.get_float(0) or 0.0
                y = xy_node.get_float(1) or 0.0
                points.append((x, y))

            if len(points) >= 3:
                polygons.append(
                    FilledPolygon(
                        net_number=net_num,
                        net_name=zone_net_name,
                        layer=zone_layer,
                        points=points,
                    )
                )

    return polygons


def _check_point_filled_polygon_clearance(
    px: float,
    py: float,
    radius: float,
    filled_polygons: list[FilledPolygon],
    clearance: float,
) -> bool:
    """Check if a circular copper object clears all filled polygons.

    Args:
        px, py: Center of the copper object
        radius: Radius of the copper object (via_radius or trace_half_width)
        filled_polygons: Other-net filled polygons to check against
        clearance: Required clearance from filled polygon copper

    Returns:
        True if clearance is satisfied, False if there is a violation
    """
    required = radius + clearance
    for fp in filled_polygons:
        # Bounding-box pre-filter
        if (
            px + required < fp.min_x
            or px - required > fp.max_x
            or py + required < fp.min_y
            or py - required > fp.max_y
        ):
            continue

        # Check if point is inside the filled polygon (immediate violation)
        if point_in_polygon(px, py, fp.points):
            return False

        # Check distance to every edge of the polygon
        n = len(fp.points)
        for i in range(n):
            j = (i + 1) % n
            dist = point_to_segment_distance(
                px, py, fp.points[i][0], fp.points[i][1], fp.points[j][0], fp.points[j][1]
            )
            if dist < required:
                return False

    return True


def _check_segment_filled_polygon_clearance(
    sx: float,
    sy: float,
    ex: float,
    ey: float,
    half_width: float,
    filled_polygons: list[FilledPolygon],
    clearance: float,
) -> bool:
    """Check if a trace segment clears all filled polygons.

    Checks both endpoints inside polygon and segment-to-edge distances.

    Args:
        sx, sy: Segment start
        ex, ey: Segment end
        half_width: Half the trace width
        filled_polygons: Other-net filled polygons to check against
        clearance: Required clearance

    Returns:
        True if clearance is satisfied, False if there is a violation
    """
    required = half_width + clearance
    for fp in filled_polygons:
        # Bounding-box pre-filter for the segment
        seg_min_x = min(sx, ex) - required
        seg_max_x = max(sx, ex) + required
        seg_min_y = min(sy, ey) - required
        seg_max_y = max(sy, ey) + required
        if seg_max_x < fp.min_x or seg_min_x > fp.max_x:
            continue
        if seg_max_y < fp.min_y or seg_min_y > fp.max_y:
            continue

        # Check if either endpoint is inside the polygon
        if point_in_polygon(sx, sy, fp.points) or point_in_polygon(ex, ey, fp.points):
            return False

        # Check segment-to-edge distances
        n = len(fp.points)
        for i in range(n):
            j = (i + 1) % n
            dist = segment_to_segment_distance(
                sx,
                sy,
                ex,
                ey,
                fp.points[i][0],
                fp.points[i][1],
                fp.points[j][0],
                fp.points[j][1],
            )
            if dist < required:
                return False

    return True


def point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Test if a point is inside a polygon using ray casting.

    Casts a ray from the point in the +X direction and counts the number
    of polygon edge crossings. An odd count means inside.

    Args:
        x, y: Point to test
        polygon: List of (x, y) vertices forming the polygon boundary

    Returns:
        True if the point is inside the polygon
    """
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def generate_grid_positions(
    polygon: list[tuple[float, float]],
    spacing: float,
    margin: float,
) -> list[tuple[float, float]]:
    """Generate grid positions inside a zone polygon.

    1. Computes the axis-aligned bounding box of the polygon
    2. Generates a regular grid at `spacing` mm intervals aligned to round coords
    3. Filters: keeps only points inside the polygon with margin from edges

    Args:
        polygon: List of (x, y) vertices
        spacing: Grid spacing in mm
        margin: Inset from polygon edges (via_size/2 + clearance)

    Returns:
        List of (x, y) grid positions inside the polygon
    """
    if not polygon or spacing <= 0:
        return []

    # Compute bounding box
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    # Align grid to round coordinates
    # E.g., if min_x=10.7 and spacing=3.0, start at 12.0
    import math as _math

    start_x = _math.ceil(min_x / spacing) * spacing
    start_y = _math.ceil(min_y / spacing) * spacing

    positions = []
    gx = start_x
    while gx <= max_x:
        gy = start_y
        while gy <= max_y:
            if point_in_polygon(gx, gy, polygon):
                # Check margin from all polygon edges
                if _point_has_edge_margin(gx, gy, polygon, margin):
                    positions.append((gx, gy))
            gy += spacing
        gx += spacing

    return positions


def _point_has_edge_margin(
    x: float, y: float, polygon: list[tuple[float, float]], margin: float
) -> bool:
    """Check if a point has at least `margin` distance from all polygon edges.

    Args:
        x, y: Point to check
        polygon: Polygon vertices
        margin: Required minimum distance from edges

    Returns:
        True if the point is at least `margin` from all edges
    """
    n = len(polygon)
    for i in range(n):
        j = (i + 1) % n
        dist = point_to_segment_distance(
            x, y, polygon[i][0], polygon[i][1], polygon[j][0], polygon[j][1]
        )
        if dist < margin:
            return False
    return True


def check_via_clearance(
    x: float,
    y: float,
    via_size: float,
    clearance: float,
    other_net_tracks: list[TrackSegment],
    other_net_vias: list[tuple[float, float, float, int]],
    other_net_pads: list[tuple[float, float, float, int]],
    same_net_vias: list[tuple[float, float]],
    other_net_filled_polygons: list[FilledPolygon] | None = None,
) -> bool:
    """Check if a via at (x, y) passes all clearance checks.

    Args:
        x, y: Proposed via position
        via_size: Via pad diameter in mm
        clearance: Minimum clearance from existing copper in mm
        other_net_tracks: Track segments on other nets
        other_net_vias: Vias on other nets as (x, y, size, net_num)
        other_net_pads: Pads on other nets as (x, y, radius, net_num)
        same_net_vias: Existing same-net vias as (x, y) for stacking prevention
        other_net_filled_polygons: Filled polygons from other-net zone fills

    Returns:
        True if the position is clear for via placement
    """
    via_radius = via_size / 2

    # Check against same-net vias (prevent stacking)
    for vx, vy in same_net_vias:
        dist = math.sqrt((vx - x) ** 2 + (vy - y) ** 2)
        if dist < via_size + clearance:
            return False

    # Check against other-net track segments
    for seg in other_net_tracks:
        dist = point_to_segment_distance(x, y, seg.start_x, seg.start_y, seg.end_x, seg.end_y)
        min_dist = via_radius + seg.width / 2 + clearance
        if dist < min_dist:
            return False

    # Check against other-net vias
    for ovx, ovy, ov_size, _onet in other_net_vias:
        dist = math.sqrt((ovx - x) ** 2 + (ovy - y) ** 2)
        min_dist = via_radius + ov_size / 2 + clearance
        if dist < min_dist:
            return False

    # Check against other-net pads
    for px, py, p_radius, _pnet in other_net_pads:
        dist = math.sqrt((px - x) ** 2 + (py - y) ** 2)
        min_dist = via_radius + p_radius + clearance
        if dist < min_dist:
            return False

    # Check against other-net filled polygons (zone fill copper)
    if other_net_filled_polygons:
        if not _check_point_filled_polygon_clearance(
            x, y, via_radius, other_net_filled_polygons, clearance
        ):
            return False

    return True


def run_blanket_stitch(
    pcb_path: Path,
    net_names: list[str],
    via_size: float = 0.45,
    drill: float = 0.2,
    clearance: float = 0.2,
    spacing: float = 3.0,
    target_layer: str | None = None,
    dry_run: bool = False,
) -> StitchResult:
    """Run blanket (grid-based) stitching on a PCB.

    Places vias on a regular grid pattern within zone polygons.
    Unlike pad-based stitching, blanket vias connect through the zone
    fill itself and do not need pad-to-via trace segments.

    Args:
        pcb_path: Path to the PCB file
        net_names: List of net names to stitch
        via_size: Via pad diameter in mm
        drill: Via drill size in mm
        clearance: Minimum clearance from existing copper in mm
        spacing: Grid spacing in mm
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

    # Margin from polygon edges: via must be fully inside zone
    margin = via_size / 2 + clearance

    # Get net numbers for the target nets
    net_map = get_net_map(sexp)
    target_net_nums = {num for num, name in net_map.items() if name in set(net_names)}

    # Collect other-net copper for clearance checking
    other_net_tracks = find_all_track_segments(sexp, exclude_nets=target_net_nums)
    other_net_vias = find_all_board_vias(sexp, exclude_nets=target_net_nums)
    other_net_pads = find_all_pads(sexp, exclude_nets=target_net_nums)
    other_net_filled_polys = find_all_filled_polygons(sexp, exclude_nets=target_net_nums)

    # Collect all existing same-net vias (to prevent stacking)
    all_same_net_vias = find_existing_vias(sexp, target_net_nums)
    same_net_via_positions: list[tuple[float, float]] = [
        (vx, vy) for vx, vy, _vn in all_same_net_vias
    ]

    # Also collect same-net pads to avoid placing vias on top of pads
    same_net_pads = find_all_pads(sexp, exclude_nets=set())
    same_net_pad_positions: list[tuple[float, float, float]] = [
        (px, py, pr) for px, py, pr, pnet in same_net_pads if pnet in target_net_nums
    ]

    for net_name in net_names:
        # Extract zone polygons for this net
        zone_polygons = extract_zone_polygons(sexp, net_name)

        if not zone_polygons:
            print(
                f"Warning: No zone polygon found for net '{net_name}', skipping blanket stitch",
                file=sys.stderr,
            )
            continue

        # Get net number
        net_number = get_net_number(sexp, net_name)
        if net_number is None:
            continue

        # Determine target layer
        if target_layer:
            net_target_layer = target_layer
        else:
            zone_layers = find_zones_for_net(sexp, net_name)
            if zone_layers:
                net_target_layer = zone_layers[0]
                result.detected_layers[net_name] = zone_layers[0]
            else:
                net_target_layer = None
                result.fallback_nets.append(net_name)

        for zone_poly in zone_polygons:
            # Generate grid positions inside this zone polygon
            grid_positions = generate_grid_positions(zone_poly.points, spacing, margin)

            for gx, gy in grid_positions:
                # Check clearance against all existing copper
                if not check_via_clearance(
                    gx,
                    gy,
                    via_size,
                    clearance,
                    other_net_tracks,
                    other_net_vias,
                    other_net_pads,
                    same_net_via_positions,
                    other_net_filled_polys,
                ):
                    continue

                # Check against same-net pads (avoid placing via on a pad)
                pad_conflict = False
                for px, py, pr in same_net_pad_positions:
                    dist = math.sqrt((px - gx) ** 2 + (py - gy) ** 2)
                    if dist < via_size / 2 + pr + clearance:
                        pad_conflict = True
                        break
                if pad_conflict:
                    continue

                # Determine via layers
                # For blanket vias, use F.Cu -> target or F.Cu -> B.Cu
                surface_layer = "F.Cu"
                if zone_poly.layer.startswith("B."):
                    surface_layer = "B.Cu"
                layers = get_via_layers(surface_layer, net_target_layer)

                # Create a dummy PadInfo for ViaPlacement compatibility
                dummy_pad = PadInfo(
                    reference="blanket",
                    pad_number="0",
                    net_number=net_number,
                    net_name=net_name,
                    x=gx,
                    y=gy,
                    layer=surface_layer,
                    width=via_size,
                    height=via_size,
                )

                placement = ViaPlacement(
                    pad=dummy_pad,
                    via_x=gx,
                    via_y=gy,
                    size=via_size,
                    drill=drill,
                    layers=layers,
                )
                result.vias_added.append(placement)

                # Track newly placed vias to prevent stacking
                same_net_via_positions.append((gx, gy))

    # Apply changes if not dry run
    if not dry_run and result.vias_added:
        for placement in result.vias_added:
            add_via_to_pcb(sexp, placement)
        save_pcb(sexp, pcb_path)

        # Post-write verification
        verify_pcb_write(pcb_path, expected_vias=len(result.vias_added))

    return result


def run_stitch(
    pcb_path: Path,
    net_names: list[str],
    via_size: float = 0.45,
    drill: float = 0.2,
    clearance: float = 0.2,
    offset: float = 0.5,
    target_layer: str | None = None,
    trace_width: float = 0.2,
    dry_run: bool = False,
    escape_distance: float = 3.0,
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
        trace_width: Width of pad-to-via trace segments in mm
        dry_run: If True, don't modify the file
        escape_distance: Maximum escape trace length in mm for dense IC pads (default 3.0)

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

    # Find existing connections (same-net, for connectivity checking)
    existing_vias = find_existing_vias(sexp, net_numbers)
    track_points = find_existing_tracks(sexp, net_numbers)

    # Find other-net copper for clearance checking to prevent shorts
    other_net_tracks = find_all_track_segments(sexp, exclude_nets=net_numbers)
    other_net_vias = find_all_board_vias(sexp, exclude_nets=net_numbers)
    other_net_pads = find_all_pads(sexp, exclude_nets=net_numbers)
    other_net_filled_polys = find_all_filled_polygons(sexp, exclude_nets=net_numbers)

    # Process each pad
    for pad in pads:
        # Check if already connected
        if is_pad_connected(pad, existing_vias, track_points):
            result.already_connected += 1
            continue

        # Calculate via position with clearance checking against all copper,
        # including the connecting trace path from pad to via
        via_pos = calculate_via_position(
            pad,
            offset=offset,
            via_size=via_size,
            existing_vias=existing_vias,
            clearance=clearance,
            other_net_tracks=other_net_tracks,
            other_net_vias=other_net_vias,
            other_net_pads=other_net_pads,
            trace_width=trace_width,
            other_net_filled_polygons=other_net_filled_polys,
        )

        # Track if we're using dog-leg or extended escape routing
        dogleg_pos: tuple[float, float, float, float] | None = None
        extended_pos: tuple[float, float, list[tuple[float, float]]] | None = None

        if via_pos is None:
            # Straight-line failed - try dog-leg (L-shaped) routing
            # This is especially useful for fine-pitch components like SSOP
            # where adjacent pads on different nets block straight-line escape
            dogleg_pos = calculate_dogleg_via_position(
                pad,
                offset=offset,
                via_size=via_size,
                existing_vias=existing_vias,
                clearance=clearance,
                other_net_tracks=other_net_tracks,
                other_net_vias=other_net_vias,
                other_net_pads=other_net_pads,
                trace_width=trace_width,
                other_net_filled_polygons=other_net_filled_polys,
            )

            if dogleg_pos is None:
                # Dog-leg also failed - try extended escape routing
                # This handles dense IC packages (QFP, BGA) where pads need
                # longer multi-segment escape traces to reach open board area
                extended_pos = calculate_extended_escape_position(
                    pad,
                    offset=offset,
                    via_size=via_size,
                    existing_vias=existing_vias,
                    clearance=clearance,
                    escape_distance=escape_distance,
                    other_net_tracks=other_net_tracks,
                    other_net_vias=other_net_vias,
                    other_net_pads=other_net_pads,
                    trace_width=trace_width,
                )

                if extended_pos is None:
                    result.pads_skipped.append(
                        (
                            pad,
                            "no valid via location (clearance conflict, dog-leg and "
                            f"extended escape up to {escape_distance}mm also failed)",
                        )
                    )
                    continue

        # Determine via layers using per-net target layer
        pad_target_layer = net_target_layers.get(pad.net_name)
        layers = get_via_layers(pad.layer, pad_target_layer)

        if extended_pos is not None:
            # Extended escape placement: (via_x, via_y, waypoints)
            via_x, via_y, waypoints = extended_pos

            placement = ViaPlacement(
                pad=pad,
                via_x=via_x,
                via_y=via_y,
                size=via_size,
                drill=drill,
                layers=layers,
            )

            result.vias_added.append(placement)

            # Create a multi-waypoint trace segment
            trace = TraceSegment(
                pad=pad,
                via_x=via_x,
                via_y=via_y,
                width=trace_width,
                layer=pad.layer,
                waypoints=waypoints,
            )
            result.traces_added.append(trace)

            # Add to existing vias list
            existing_vias.append((via_x, via_y, pad.net_number))
        elif dogleg_pos is not None:
            # Dog-leg placement: (via_x, via_y, intermediate_x, intermediate_y)
            via_x, via_y, intermediate_x, intermediate_y = dogleg_pos

            placement = ViaPlacement(
                pad=pad,
                via_x=via_x,
                via_y=via_y,
                size=via_size,
                drill=drill,
                layers=layers,
            )

            result.vias_added.append(placement)

            # Create an L-shaped trace segment
            trace = TraceSegment(
                pad=pad,
                via_x=via_x,
                via_y=via_y,
                width=trace_width,
                layer=pad.layer,
                intermediate_x=intermediate_x,
                intermediate_y=intermediate_y,
            )
            result.traces_added.append(trace)

            # Add to existing vias list
            existing_vias.append((via_x, via_y, pad.net_number))
        else:
            # Straight-line placement
            placement = ViaPlacement(
                pad=pad,
                via_x=via_pos[0],
                via_y=via_pos[1],
                size=via_size,
                drill=drill,
                layers=layers,
            )

            result.vias_added.append(placement)

            # Create a straight trace segment from pad center to via center
            trace = TraceSegment(
                pad=pad,
                via_x=via_pos[0],
                via_y=via_pos[1],
                width=trace_width,
                layer=pad.layer,
            )
            result.traces_added.append(trace)

            # Add to existing vias list
            existing_vias.append((via_pos[0], via_pos[1], pad.net_number))

    # Apply changes if not dry run
    if not dry_run and result.vias_added:
        for placement in result.vias_added:
            add_via_to_pcb(sexp, placement)
        for trace in result.traces_added:
            add_trace_to_pcb(sexp, trace)
        save_pcb(sexp, pcb_path)

        # Post-write verification
        verify_pcb_write(
            pcb_path,
            expected_vias=len(result.vias_added),
            expected_segments=len(result.traces_added),
        )

    return result


def run_post_stitch_drc(pcb_path: Path) -> int:
    """Run DRC on the PCB after stitching and display summary.

    kicad-cli pcb drc automatically fills zones before checking,
    so this handles both zone fill and DRC validation in one step.

    Returns:
        0 if DRC ran successfully (regardless of violations),
        1 if DRC could not be run.
    """
    from .runner import find_kicad_cli, run_drc

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print(
            "\nWarning: kicad-cli not found, skipping DRC.",
            file=sys.stderr,
        )
        print(
            "Install KiCad 8 from: https://www.kicad.org/download/",
            file=sys.stderr,
        )
        print(
            f"\nRun DRC manually: kicad-cli pcb drc {pcb_path}",
            file=sys.stderr,
        )
        return 1

    print(f"\nRunning DRC on {pcb_path.name} (zones will be filled automatically)...")

    result = run_drc(pcb_path, format="json", kicad_cli=kicad_cli)

    if not result.success:
        print(f"\nDRC failed to run: {result.stderr}", file=sys.stderr)
        return 1

    # Parse the report
    from ..drc import DRCReport

    try:
        report = DRCReport.load(result.output_path)
    except Exception as e:
        print(f"\nError parsing DRC report: {e}", file=sys.stderr)
        return 1
    finally:
        # Clean up temp file
        if result.output_path:
            result.output_path.unlink(missing_ok=True)

    # Display summary
    error_count = report.error_count
    warning_count = report.warning_count

    print(f"\n{'=' * 60}")
    print("POST-STITCH DRC RESULTS")
    print(f"{'=' * 60}")
    print(f"  Errors:   {error_count}")
    print(f"  Warnings: {warning_count}")

    if error_count == 0 and warning_count == 0:
        print("\nDRC PASSED - No violations found")
    else:
        # Group by type
        by_type = report.violations_by_type()
        print(f"\n{'-' * 60}")
        print("BY TYPE:")
        for vtype, violations in sorted(by_type.items(), key=lambda x: -len(x[1])):
            errors = sum(1 for v in violations if v.is_error)
            warnings = len(violations) - errors
            parts = []
            if errors:
                parts.append(f"{errors} error{'s' if errors != 1 else ''}")
            if warnings:
                parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
            print(f"  {vtype.value}: {', '.join(parts)}")

        if error_count > 0:
            print(f"\n{'-' * 60}")
            print("ERRORS (must fix):")
            for v in report.errors[:10]:
                print(f"  [X] {v.type_str}: {v.message}")
            if error_count > 10:
                print(f"  ... and {error_count - 10} more errors")

        print(f"\n{'=' * 60}")
        if error_count > 0:
            print("DRC FAILED - Fix errors before manufacturing")
        else:
            print("DRC PASSED with warnings")

        print(f"\nFor detailed results: kicad-drc {pcb_path}")

    return 0


def output_result(result: StitchResult, dry_run: bool = False, run_drc: bool = False) -> None:
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

    # Count trace types
    dogleg_traces = [t for t in result.traces_added if t.is_dogleg]
    extended_traces = [t for t in result.traces_added if t.is_extended_escape]
    straight_traces = len(result.traces_added) - len(dogleg_traces) - len(extended_traces)

    # Summary
    print(f"\n{'=' * 60}")
    print("Summary:")
    print(f"  + Added {len(result.vias_added)} stitching vias")
    print(f"  + Added {len(result.traces_added)} pad-to-via traces")
    if dogleg_traces or extended_traces:
        print(f"    - {straight_traces} straight traces")
        print(f"    - {len(dogleg_traces)} dog-leg (L-shaped) traces for fine-pitch pads")
        if extended_traces:
            print(
                f"    - {len(extended_traces)} extended escape traces for dense IC pads"
            )
    if result.already_connected:
        print(f"  = {result.already_connected} pads already connected")
    if result.pads_skipped:
        print(f"  ! Skipped {len(result.pads_skipped)} pads (manual placement needed)")

    if dry_run:
        print("\n(dry run - no changes made)")
    elif not run_drc:
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
        help="Net name to add vias for (can be repeated). If not specified, "
        "auto-detects all power plane nets from zones.",
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
        "--trace-width",
        type=float,
        default=0.2,
        help="Width of pad-to-via trace segments in mm (default: 0.2)",
    )
    parser.add_argument(
        "--escape-distance",
        type=float,
        default=3.0,
        help="Maximum escape trace length in mm for dense IC pads (default: 3.0). "
        "When both straight-line and dog-leg placement fail, extended escape traces "
        "up to this length are tried to navigate between dense pin fields.",
    )
    parser.add_argument(
        "--blanket",
        "-b",
        action="store_true",
        help="Place vias on a grid pattern across zone polygons (blanket stitching)",
    )
    parser.add_argument(
        "--spacing",
        type=float,
        default=3.0,
        help="Grid spacing for blanket stitching in mm (default: 3.0)",
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
    parser.add_argument(
        "--drc",
        action="store_true",
        help="Run DRC after stitching (fills zones automatically via kicad-cli)",
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

        # Also copy project file for DRC compatibility
        pro_path = pcb_path.with_suffix(".kicad_pro")
        if pro_path.exists():
            output_pro = output_path.with_suffix(".kicad_pro")
            shutil.copy(pro_path, output_pro)

        pcb_path = output_path

    # Auto-detect power plane nets if none specified
    net_names = args.nets
    if not net_names:
        # Load PCB to find zones
        from kicad_tools.core.sexp_file import load_pcb as _load_pcb

        sexp = _load_pcb(pcb_path)
        plane_nets = find_all_plane_nets(sexp)
        if not plane_nets:
            print("No power plane nets found (no zones with assigned nets)", file=sys.stderr)
            return 1
        net_names = list(plane_nets.keys())
        print(f"Auto-detected {len(net_names)} power plane nets: {', '.join(sorted(net_names))}")

    try:
        if args.blanket:
            result = run_blanket_stitch(
                pcb_path=pcb_path,
                net_names=net_names,
                via_size=args.via_size,
                drill=args.drill,
                clearance=args.clearance,
                spacing=args.spacing,
                target_layer=args.target_layer,
                dry_run=args.dry_run,
            )
        else:
            result = run_stitch(
                pcb_path=pcb_path,
                net_names=net_names,
                via_size=args.via_size,
                drill=args.drill,
                clearance=args.clearance,
                offset=args.offset,
                target_layer=args.target_layer,
                trace_width=args.trace_width,
                dry_run=args.dry_run,
                escape_distance=args.escape_distance,
            )

        output_result(result, dry_run=args.dry_run, run_drc=args.drc)

        # Run DRC if requested (and not dry run, and vias were actually added)
        if args.drc and not args.dry_run and result.vias_added:
            run_post_stitch_drc(pcb_path)

        if result.vias_added:
            return 0
        else:
            return 0 if result.already_connected else 1

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

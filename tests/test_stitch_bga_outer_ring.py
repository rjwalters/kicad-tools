"""Regression tests for outer-ring BGA GND stitch (issue #2912).

The original failure mode: on board 06's BGA-49 simulator (1.27mm pitch,
7x7 grid), five outer-ring GND pads (U2.A1, B1, C7, F1, G5) could not
be stitched because USB 3.0 escape tracks colonised every copper layer
in a tight band around the perimeter and the extended-escape search
was both too small (3.0mm) and too narrow (4 cardinal directions).

The fix raises the default escape radius to 4.0mm and adds Strategy 4
(polar-grid via sampler with layer-aware trace clearance) to
``calculate_extended_escape_position``.

These tests build a synthetic 7x7 1.27mm-pitch BGA in code, mark the
outer ring as GND, place inner-layer obstacles at varying clearance
deficits along three of four escape corridors, and assert that
``calculate_extended_escape_position`` returns a placement for at least
4 of the 5 outer-ring corner/edge pads named in the issue.
"""

from __future__ import annotations

import math

from kicad_tools.cli.stitch_cmd import (
    PadInfo,
    TrackSegment,
    calculate_extended_escape_position,
)

# BGA layout — matches board 06 / U2 geometry
BGA_PITCH = 1.27
BGA_ROWS = 7
BGA_COLS = 7
BGA_PAD_SIZE = 0.45
BGA_CENTER_X = 100.0
BGA_CENTER_Y = 100.0
BGA_NET_GND = 5
BGA_NET_SIG_BASE = 10  # signal nets 10, 11, 12, ...

ROW_LETTERS = "ABCDEFG"


def _pad_xy(row_letter: str, col: int) -> tuple[float, float]:
    """Return the (x, y) of a BGA pad given its row letter and column number."""
    row_idx = ROW_LETTERS.index(row_letter)
    px = BGA_CENTER_X + (col - 4) * BGA_PITCH
    py = BGA_CENTER_Y + (row_idx - 3) * BGA_PITCH
    return px, py


def _make_bga_pads() -> dict[str, PadInfo]:
    """Build a 7x7 BGA where the outer ring is GND and inner pads are signals."""
    pads: dict[str, PadInfo] = {}
    signal_id = 0
    for row_letter in ROW_LETTERS:
        for col in range(1, BGA_COLS + 1):
            px, py = _pad_xy(row_letter, col)
            row_idx = ROW_LETTERS.index(row_letter)
            on_outer = row_idx == 0 or row_idx == BGA_ROWS - 1 or col == 1 or col == BGA_COLS
            if on_outer:
                net_number = BGA_NET_GND
                net_name = "GND"
            else:
                net_number = BGA_NET_SIG_BASE + signal_id
                net_name = f"SIG_{signal_id}"
                signal_id += 1
            pads[f"{row_letter}{col}"] = PadInfo(
                reference="U2",
                pad_number=f"{row_letter}{col}",
                net_number=net_number,
                net_name=net_name,
                x=px,
                y=py,
                layer="F.Cu",
                width=BGA_PAD_SIZE,
                height=BGA_PAD_SIZE,
                pad_type="smd",
            )
    return pads


def _other_net_pads(
    pads: dict[str, PadInfo], target: PadInfo
) -> list[tuple[float, float, float, int]]:
    """Return all pads other than ``target`` formatted for clearance checks."""
    out: list[tuple[float, float, float, int]] = []
    for p in pads.values():
        if p is target:
            continue
        out.append((p.x, p.y, max(p.width, p.height) / 2, p.net_number))
    return out


def _obstacle_track(
    pad: PadInfo,
    direction: tuple[float, float],
    gap_mm: float,
    layer: str,
    net_number: int,
    width: float = 0.15,
    length: float = 6.0,
) -> TrackSegment:
    """Build a track segment that grazes the pad in the given direction.

    The track is placed so the closest distance from pad center to track
    centerline is ``pad_radius + clearance + gap_mm`` (gap_mm negative
    means the track sits inside the required clearance zone, simulating
    a tight escape corridor).
    """
    dx, dy = direction
    # Normalise direction
    n = math.hypot(dx, dy) or 1.0
    dx /= n
    dy /= n

    pad_radius = max(pad.width, pad.height) / 2
    clearance = 0.2  # default stitch clearance
    via_radius = 0.45 / 2

    # offset from pad center to track centerline along the direction normal
    offset = pad_radius + via_radius + clearance + gap_mm
    # The track runs PERPENDICULAR to the direction, centered at the offset
    perp = (-dy, dx)
    cx = pad.x + dx * offset
    cy = pad.y + dy * offset
    sx = cx + perp[0] * length / 2
    sy = cy + perp[1] * length / 2
    ex = cx - perp[0] * length / 2
    ey = cy - perp[1] * length / 2
    return TrackSegment(
        start_x=sx,
        start_y=sy,
        end_x=ex,
        end_y=ey,
        width=width,
        layer=layer,
        net_number=net_number,
    )


def _build_obstacles_for_corner(
    pad: PadInfo, gap_mm: float, layers: list[str]
) -> list[TrackSegment]:
    """Place obstacle tracks on three of four cardinal escape corridors.

    Mirrors the board-06 failure mode: signal escape tracks colonise
    every copper layer in a tight band, leaving only diagonal/non-cardinal
    escape paths free.
    """
    # Place obstacles north, south, east on the requested layers.  West is
    # left clear so a layer-aware diagonal/polar search can still find an
    # opening.
    directions = [(0.0, -1.0), (0.0, 1.0), (1.0, 0.0)]  # N, S, E
    tracks: list[TrackSegment] = []
    for i, dir_v in enumerate(directions):
        layer = layers[i % len(layers)]
        net = 100 + i
        tracks.append(_obstacle_track(pad, dir_v, gap_mm, layer, net))
    return tracks


# The 5 named pads from the issue
NAMED_PADS = ["A1", "B1", "C7", "F1", "G5"]


def test_synthetic_bga_outer_ring_stitches_at_least_4_of_5() -> None:
    """The fix must place at least 4 of 5 named outer-ring pads.

    Obstacles span In1.Cu, In2.Cu, and B.Cu at gap=-0.05 to -0.30mm,
    simulating the corridor congestion observed on board 06.
    """
    pads = _make_bga_pads()
    via_size = 0.45
    clearance = 0.2
    offset = 0.5
    trace_width = 0.2

    # Different gap deficits for different pads — match the issue diagnostic
    pad_gaps = {
        "A1": -0.26,  # board 06: In1.Cu gap=-0.26
        "B1": -0.30,  # board 06: In2.Cu gap=-0.32 (clamp to test envelope)
        "C7": -0.05,  # board 06: B.Cu gap=-0.06 (tight)
        "F1": -0.28,  # board 06: In1.Cu gap=-0.30 (clamp)
        "G5": -0.05,  # board 06: B.Cu gap=-0.06 (tight)
    }
    pad_layers = {
        "A1": ["In1.Cu", "In2.Cu", "F.Cu"],
        "B1": ["In2.Cu", "In1.Cu", "B.Cu"],
        "C7": ["B.Cu", "In1.Cu", "In2.Cu"],
        "F1": ["In1.Cu", "In2.Cu", "B.Cu"],
        "G5": ["B.Cu", "In2.Cu", "In1.Cu"],
    }

    placements: dict[str, tuple[float, float, list[tuple[float, float]]] | None] = {}

    for name in NAMED_PADS:
        pad = pads[name]
        obstacles = _build_obstacles_for_corner(pad, pad_gaps[name], pad_layers[name])
        result = calculate_extended_escape_position(
            pad,
            offset=offset,
            via_size=via_size,
            existing_vias=[],
            clearance=clearance,
            escape_distance=4.0,  # new default
            other_net_tracks=obstacles,
            other_net_vias=[],
            other_net_pads=_other_net_pads(pads, pad),
            trace_width=trace_width,
        )
        placements[name] = result

    placed = [k for k, v in placements.items() if v is not None]
    assert len(placed) >= 4, (
        f"Expected at least 4 of 5 named pads to stitch, got {len(placed)}: {placements}"
    )


def test_synthetic_bga_outer_ring_pads_have_clearance_valid_vias() -> None:
    """For each placed via, confirm it's actually clear of the obstacles.

    Prevents the search from returning a placement that violates the
    clearance budget — a regression in ``_check_via_position`` would
    return phantom placements that DRC would then flag.
    """
    pads = _make_bga_pads()
    via_size = 0.45
    via_radius = via_size / 2
    clearance = 0.2
    offset = 0.5
    trace_width = 0.2

    for name in NAMED_PADS:
        pad = pads[name]
        obstacles = _build_obstacles_for_corner(pad, -0.20, ["In1.Cu", "In2.Cu", "B.Cu"])
        result = calculate_extended_escape_position(
            pad,
            offset=offset,
            via_size=via_size,
            existing_vias=[],
            clearance=clearance,
            escape_distance=4.0,
            other_net_tracks=obstacles,
            other_net_vias=[],
            other_net_pads=_other_net_pads(pads, pad),
            trace_width=trace_width,
        )
        if result is None:
            continue
        via_x, via_y, _waypoints = result

        # Confirm the via does not collide with the obstacle tracks
        for seg in obstacles:
            # Closest distance from via center to track segment
            dx = seg.end_x - seg.start_x
            dy = seg.end_y - seg.start_y
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq < 1e-12:
                dist = math.hypot(via_x - seg.start_x, via_y - seg.start_y)
            else:
                t = max(
                    0.0,
                    min(
                        1.0,
                        ((via_x - seg.start_x) * dx + (via_y - seg.start_y) * dy) / seg_len_sq,
                    ),
                )
                cx = seg.start_x + t * dx
                cy = seg.start_y + t * dy
                dist = math.hypot(via_x - cx, via_y - cy)
            min_required = via_radius + seg.width / 2 + clearance
            assert dist >= min_required - 1e-6, (
                f"Pad {name}: via at ({via_x:.3f}, {via_y:.3f}) too close to "
                f"obstacle on {seg.layer} (dist={dist:.4f}, need={min_required:.4f})"
            )

        # Confirm the via does not collide with neighboring pads
        for other in pads.values():
            if other is pad:
                continue
            other_radius = max(other.width, other.height) / 2
            dist = math.hypot(other.x - via_x, other.y - via_y)
            min_required = via_radius + other_radius + clearance
            assert dist >= min_required - 1e-6, (
                f"Pad {name}: via at ({via_x:.3f}, {via_y:.3f}) collides with "
                f"pad {other.reference}.{other.pad_number} (dist={dist:.4f}, "
                f"need={min_required:.4f})"
            )


def test_clear_bga_pad_still_uses_short_escape() -> None:
    """A BGA outer-ring pad with no obstacles must still stitch via the
    first available strategy.  Guards against the new polar search
    short-circuiting earlier strategies in the no-obstacle case.
    """
    pads = _make_bga_pads()
    pad = pads["A1"]

    result = calculate_extended_escape_position(
        pad,
        offset=0.5,
        via_size=0.45,
        existing_vias=[],
        clearance=0.2,
        escape_distance=4.0,
        other_net_tracks=[],  # No obstacles
        other_net_vias=[],
        other_net_pads=_other_net_pads(pads, pad),
        trace_width=0.2,
    )
    assert result is not None, "Open BGA outer-ring pad must stitch"
    via_x, via_y, _wp = result
    radius = math.hypot(via_x - pad.x, via_y - pad.y)
    # Should not need the full 4mm when no obstacles — earlier strategies
    # should find a closer location.
    assert radius <= 3.0, f"Open pad placed via at unexpectedly long distance {radius:.3f}mm"


def test_escape_distance_default_is_4mm() -> None:
    """Lock the default to 4.0mm — board 06 requires this to clear the BGA
    corner-to-clear distance (~4mm for 7-row 1.27mm-pitch).
    """
    import inspect

    sig = inspect.signature(calculate_extended_escape_position)
    assert sig.parameters["escape_distance"].default == 4.0, (
        "Default escape_distance must be 4.0mm (issue #2912)"
    )

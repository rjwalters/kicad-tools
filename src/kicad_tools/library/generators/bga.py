"""
BGA (Ball Grid Array) footprint generator.

Generates BGA footprints with configurable grid layout, ball naming,
and support for depopulated balls and thermal pads.
"""

import string

from ..footprint import Footprint
from .standards import BGA_STANDARDS


def _get_row_letter(row: int) -> str:
    """
    Get the row letter for a BGA ball, skipping I and O per standard convention.

    Args:
        row: 0-indexed row number

    Returns:
        Row letter (A, B, C, D, E, F, G, H, J, K, ...)
    """
    # Standard BGA letters skip I and O to avoid confusion with 1 and 0
    letters = [c for c in string.ascii_uppercase if c not in ("I", "O")]
    if row < len(letters):
        return letters[row]
    # For rows beyond Z, use AA, AB, etc.
    first = row // len(letters) - 1
    second = row % len(letters)
    return letters[first] + letters[second]


def _get_ball_name(row: int, col: int) -> str:
    """
    Get the ball name for a BGA position.

    Args:
        row: 0-indexed row number
        col: 0-indexed column number

    Returns:
        Ball name (e.g., "A1", "B2", "J10")
    """
    return f"{_get_row_letter(row)}{col + 1}"


def create_bga(
    rows: int,
    cols: int,
    pitch: float = 0.8,
    ball_diameter: float | None = None,
    body_size: float | None = None,
    depopulated: list[str] | None = None,
    thermal_pad: float | None = None,
    name: str | None = None,
) -> Footprint:
    """
    Create a BGA footprint with configurable grid layout.

    Args:
        rows: Number of rows in the ball grid
        cols: Number of columns in the ball grid
        pitch: Ball pitch in mm (default: 0.8mm)
        ball_diameter: Pad diameter in mm (default: pitch * 0.5)
        body_size: Package body size in mm (default: calculated from grid)
        depopulated: List of ball names to skip (e.g., ["A1", "J10"])
        thermal_pad: Size of center thermal pad in mm (optional)
        name: Custom footprint name (auto-generated if not specified)

    Returns:
        Footprint object ready for export

    Example:
        >>> fp = create_bga(rows=10, cols=10, pitch=0.8)
        >>> fp.save("MyLib.pretty/BGA-100_10x10_0.8mm.kicad_mod")
    """
    if rows < 1:
        raise ValueError(f"BGA must have at least 1 row, got {rows}")
    if cols < 1:
        raise ValueError(f"BGA must have at least 1 column, got {cols}")
    if pitch <= 0:
        raise ValueError(f"Pitch must be positive, got {pitch}")

    # Calculate defaults
    ball_diameter = ball_diameter or pitch * 0.5
    grid_width = (cols - 1) * pitch
    grid_height = (rows - 1) * pitch
    body_size = body_size or max(grid_width, grid_height) + pitch * 2

    # Normalize depopulated list
    depopulated_set = set(depopulated or [])

    # Count actual pins (excluding depopulated)
    total_balls = rows * cols - len(depopulated_set)

    # Generate IPC-7351 compliant name
    if name is None:
        ep_suffix = "_EP" if thermal_pad else ""
        name = f"BGA-{total_balls}_{body_size}x{body_size}mm_P{pitch}mm{ep_suffix}"

    desc = f"BGA, {rows}x{cols} grid, pitch {pitch}mm, {body_size}x{body_size}mm"
    if thermal_pad:
        desc += f", thermal pad {thermal_pad}x{thermal_pad}mm"
    if depopulated_set:
        desc += f", {len(depopulated_set)} depopulated"

    fp = Footprint(
        name=name,
        description=desc,
        tags=["BGA", f"P{pitch}mm", f"{rows}x{cols}"],
        attr="smd",
    )

    # Calculate grid origin (center of grid at 0,0)
    origin_x = -grid_width / 2
    origin_y = -grid_height / 2

    # Add pads for each ball
    for row in range(rows):
        for col in range(cols):
            ball_name = _get_ball_name(row, col)
            if ball_name in depopulated_set:
                continue

            x = origin_x + col * pitch
            y = origin_y + row * pitch

            fp.add_pad(
                name=ball_name,
                x=x,
                y=y,
                width=ball_diameter,
                height=ball_diameter,
                shape="circle",
            )

    # Add thermal pad if specified
    if thermal_pad:
        fp.add_pad(
            name=str(total_balls + 1),
            x=0,
            y=0,
            width=thermal_pad,
            height=thermal_pad,
            shape="rect",
            layers=("F.Cu", "F.Paste", "F.Mask"),
        )

    # Silkscreen - corners to avoid ball overlap
    silk = body_size / 2 + 0.1
    corner_len = min(1.0, body_size / 6)

    # Draw corner marks
    for sx, sy in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
        cx, cy = sx * silk, sy * silk
        fp.add_line((cx, cy - sy * corner_len), (cx, cy), "F.SilkS", 0.12)
        fp.add_line((cx, cy), (cx - sx * corner_len, cy), "F.SilkS", 0.12)

    # Pin A1 marker (top-left corner indicator)
    marker_x = origin_x - ball_diameter
    marker_y = origin_y - ball_diameter
    fp.add_circle((marker_x, marker_y), 0.2, "F.SilkS", 0.12, fill=True)

    # Courtyard
    crt_margin = 0.25
    crt = body_size / 2 + crt_margin
    fp.add_rect((-crt, -crt), (crt, crt), "F.CrtYd", 0.05)

    # Fab layer with pin A1 chamfer
    fab = body_size / 2
    chamfer = min(1.0, body_size / 8)
    fp.add_line((-fab + chamfer, -fab), (fab, -fab), "F.Fab", 0.1)
    fp.add_line((fab, -fab), (fab, fab), "F.Fab", 0.1)
    fp.add_line((fab, fab), (-fab, fab), "F.Fab", 0.1)
    fp.add_line((-fab, fab), (-fab, -fab + chamfer), "F.Fab", 0.1)
    fp.add_line((-fab, -fab + chamfer), (-fab + chamfer, -fab), "F.Fab", 0.1)

    return fp


def create_bga_standard(package: str) -> Footprint:
    """
    Create a BGA footprint from a standard package name.

    Args:
        package: Standard package name (e.g., "BGA-256_17x17_0.8mm")

    Returns:
        Footprint object ready for export

    Example:
        >>> fp = create_bga_standard("BGA-256_17x17_0.8mm")
    """
    if package not in BGA_STANDARDS:
        available = ", ".join(sorted(BGA_STANDARDS.keys()))
        raise ValueError(f"Unknown BGA package '{package}'. Available: {available}")

    spec = BGA_STANDARDS[package]
    return create_bga(
        rows=spec["rows"],
        cols=spec["cols"],
        pitch=spec["pitch"],
        ball_diameter=spec.get("ball_diameter"),
        body_size=spec.get("body_size"),
        thermal_pad=spec.get("thermal_pad"),
        name=package,
    )

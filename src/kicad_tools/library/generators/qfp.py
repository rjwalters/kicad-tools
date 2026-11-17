"""
QFP (Quad Flat Package) footprint generator.

Generates LQFP and TQFP footprints following IPC-7351 naming conventions.
"""

from ..footprint import Footprint
from .standards import LQFP_STANDARDS


def create_qfp(
    pins: int,
    pitch: float | None = None,
    body_size: float | None = None,
    body_width: float | None = None,
    body_length: float | None = None,
    pad_width: float | None = None,
    pad_height: float | None = None,
    pins_x: int | None = None,
    pins_y: int | None = None,
    name: str | None = None,
) -> Footprint:
    """
    Create a QFP/LQFP footprint.

    Args:
        pins: Total number of pins (must be divisible by 4 for square QFP)
        pitch: Pin pitch in mm (default: 0.5mm)
        body_size: Package body size in mm (for square packages)
        body_width: Package body width in mm (for rectangular packages)
        body_length: Package body length in mm (for rectangular packages)
        pad_width: Pad width (length of pad) in mm
        pad_height: Pad height (perpendicular to pad length) in mm
        pins_x: Pins per horizontal side (for rectangular packages)
        pins_y: Pins per vertical side (for rectangular packages)
        name: Custom footprint name (auto-generated if not specified)

    Returns:
        Footprint object ready for export

    Example:
        >>> fp = create_qfp(pins=48, pitch=0.5, body_size=7.0)
        >>> fp.save("MyLib.pretty/LQFP-48_7x7mm.kicad_mod")
    """
    if pins < 8:
        raise ValueError(f"QFP must have at least 8 pins, got {pins}")

    # Get defaults from standards table
    std = LQFP_STANDARDS.get(pins, {})
    pitch = pitch or std.get("pitch", 0.5)
    pad_width = pad_width or std.get("pad_width", 1.2)
    pad_height = pad_height or std.get("pad_height", 0.3)

    # Handle square vs rectangular packages
    if body_size is not None:
        body_width = body_size
        body_length = body_size
    else:
        body_width = body_width or std.get("body_size", 10.0)
        body_length = body_length or body_width

    # Calculate pins per side
    if pins_x is None and pins_y is None:
        if body_width == body_length:
            # Square package - equal pins per side
            if pins % 4 != 0:
                raise ValueError(f"Square QFP must have pins divisible by 4, got {pins}")
            pins_per_side = pins // 4
            pins_x = pins_per_side
            pins_y = pins_per_side
        else:
            # Rectangular - need explicit pins_x and pins_y
            raise ValueError("Rectangular QFP requires pins_x and pins_y parameters")
    elif pins_x is None or pins_y is None:
        raise ValueError("Both pins_x and pins_y must be specified for rectangular QFP")

    # Verify pin count
    total_expected = 2 * pins_x + 2 * pins_y
    if total_expected != pins:
        raise ValueError(
            f"pins_x={pins_x} and pins_y={pins_y} give {total_expected} pins, expected {pins}"
        )

    # Generate IPC-7351 compliant name
    if name is None:
        if body_width == body_length:
            name = f"LQFP-{pins}_{body_width}x{body_length}mm_P{pitch}mm"
        else:
            name = f"LQFP-{pins}_{body_width}x{body_length}mm_P{pitch}mm"

    fp = Footprint(
        name=name,
        description=f"LQFP, {pins} Pin, pitch {pitch}mm, {body_width}x{body_length}mm",
        tags=["LQFP", "QFP", f"P{pitch}mm"],
        attr="smd",
    )

    # Calculate pad positions
    # Pin numbering: starts at bottom-left corner of pin 1 indicator, goes CCW
    # Bottom side: 1 to pins_x (left to right)
    # Right side: pins_x+1 to pins_x+pins_y (bottom to top)
    # Top side: pins_x+pins_y+1 to 2*pins_x+pins_y (right to left)
    # Left side: 2*pins_x+pins_y+1 to pins (top to bottom)

    span_x = (pins_x - 1) * pitch
    span_y = (pins_y - 1) * pitch

    pad_center_x = body_width / 2 + pad_width / 2 - 0.3
    pad_center_y = body_length / 2 + pad_width / 2 - 0.3

    pin_num = 1

    # Bottom side (pins go left to right)
    for i in range(pins_x):
        x = -span_x / 2 + i * pitch
        y = pad_center_y
        fp.add_pad(str(pin_num), x, y, pad_height, pad_width)  # Note: rotated
        pin_num += 1

    # Right side (pins go bottom to top)
    for i in range(pins_y):
        x = pad_center_x
        y = span_y / 2 - i * pitch
        fp.add_pad(str(pin_num), x, y, pad_width, pad_height)
        pin_num += 1

    # Top side (pins go right to left)
    for i in range(pins_x):
        x = span_x / 2 - i * pitch
        y = -pad_center_y
        fp.add_pad(str(pin_num), x, y, pad_height, pad_width)  # Note: rotated
        pin_num += 1

    # Left side (pins go top to bottom)
    for i in range(pins_y):
        x = -pad_center_x
        y = -span_y / 2 + i * pitch
        fp.add_pad(str(pin_num), x, y, pad_width, pad_height)
        pin_num += 1

    # Silkscreen outline
    silk_margin = 0.2
    silk_x = body_width / 2 + silk_margin
    silk_y = body_length / 2 + silk_margin

    # Draw corners only (to avoid pad overlap)
    corner_len = min(1.0, span_x / 4, span_y / 4)

    # Top-left corner
    fp.add_line((-silk_x, -silk_y + corner_len), (-silk_x, -silk_y), "F.SilkS", 0.12)
    fp.add_line((-silk_x, -silk_y), (-silk_x + corner_len, -silk_y), "F.SilkS", 0.12)

    # Top-right corner
    fp.add_line((silk_x - corner_len, -silk_y), (silk_x, -silk_y), "F.SilkS", 0.12)
    fp.add_line((silk_x, -silk_y), (silk_x, -silk_y + corner_len), "F.SilkS", 0.12)

    # Bottom-right corner
    fp.add_line((silk_x, silk_y - corner_len), (silk_x, silk_y), "F.SilkS", 0.12)
    fp.add_line((silk_x, silk_y), (silk_x - corner_len, silk_y), "F.SilkS", 0.12)

    # Bottom-left corner (with pin 1 marker)
    fp.add_line((-silk_x + corner_len, silk_y), (-silk_x, silk_y), "F.SilkS", 0.12)
    fp.add_line((-silk_x, silk_y), (-silk_x, silk_y - corner_len), "F.SilkS", 0.12)

    # Pin 1 marker
    marker_x = -span_x / 2 - 0.5
    marker_y = silk_y + 0.5
    fp.add_circle((marker_x, marker_y), 0.15, "F.SilkS", 0.12, fill=True)

    # Courtyard
    courtyard_margin = 0.25
    crt_x = pad_center_x + pad_width / 2 + courtyard_margin
    crt_y = pad_center_y + pad_width / 2 + courtyard_margin
    fp.add_rect((-crt_x, -crt_y), (crt_x, crt_y), "F.CrtYd", 0.05)

    # Fab layer with pin 1 chamfer
    fab_x = body_width / 2
    fab_y = body_length / 2
    chamfer = min(0.8, body_width / 8)

    fp.add_line((-fab_x + chamfer, fab_y), (fab_x, fab_y), "F.Fab", 0.1)
    fp.add_line((fab_x, fab_y), (fab_x, -fab_y), "F.Fab", 0.1)
    fp.add_line((fab_x, -fab_y), (-fab_x, -fab_y), "F.Fab", 0.1)
    fp.add_line((-fab_x, -fab_y), (-fab_x, fab_y - chamfer), "F.Fab", 0.1)
    fp.add_line((-fab_x, fab_y - chamfer), (-fab_x + chamfer, fab_y), "F.Fab", 0.1)

    return fp

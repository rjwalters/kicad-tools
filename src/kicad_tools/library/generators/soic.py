"""
SOIC (Small Outline Integrated Circuit) footprint generator.

Generates SOIC footprints following IPC-7351 naming conventions.
"""

from ..footprint import Footprint
from .standards import SOIC_STANDARDS


def create_soic(
    pins: int,
    pitch: float | None = None,
    body_width: float | None = None,
    body_length: float | None = None,
    pad_width: float | None = None,
    pad_height: float | None = None,
    name: str | None = None,
) -> Footprint:
    """
    Create a SOIC footprint.

    Args:
        pins: Number of pins (must be even, 8-28)
        pitch: Pin pitch in mm (default: 1.27mm)
        body_width: Package body width in mm
        body_length: Package body length in mm
        pad_width: Pad width (length of pad) in mm
        pad_height: Pad height (perpendicular to pad length) in mm
        name: Custom footprint name (auto-generated if not specified)

    Returns:
        Footprint object ready for export

    Example:
        >>> fp = create_soic(pins=8)
        >>> fp.save("MyLib.pretty/SOIC-8_Custom.kicad_mod")

        >>> fp = create_soic(pins=16, pitch=1.27)
        >>> print(fp.name)
        'SOIC-16_3.9x9.9mm_P1.27mm'
    """
    if pins % 2 != 0:
        raise ValueError(f"SOIC must have even number of pins, got {pins}")

    if pins < 4 or pins > 32:
        raise ValueError(f"SOIC pin count must be 4-32, got {pins}")

    # Get defaults from standards table
    std = SOIC_STANDARDS.get(pins, {})
    pitch = pitch or std.get("pitch", 1.27)
    body_width = body_width or std.get("body_width", 3.9)
    pad_width = pad_width or std.get("pad_width", 1.95)
    pad_height = pad_height or std.get("pad_height", 0.6)

    # Calculate body length if not specified
    if body_length is None:
        pins_per_side = pins // 2
        body_length = std.get("body_length", (pins_per_side - 1) * pitch + 2.0)

    # Generate IPC-7351 compliant name
    if name is None:
        name = f"SOIC-{pins}_{body_width}x{body_length}mm_P{pitch}mm"

    # Calculate pad positions
    # Pads are centered around origin, pins on left and right sides
    pins_per_side = pins // 2
    total_pin_span = (pins_per_side - 1) * pitch

    # Pad center X position (from body edge + pad width/2)
    pad_x = body_width / 2 + pad_width / 2 - 0.5  # Slight inset

    fp = Footprint(
        name=name,
        description=f"SOIC, {pins} Pin, pitch {pitch}mm, {body_width}x{body_length}mm",
        tags=["SOIC", "SO", f"P{pitch}mm"],
        attr="smd",
    )

    # Add pads - left side (pins 1 to pins_per_side, bottom to top)
    for i in range(pins_per_side):
        pin_num = i + 1
        y = -total_pin_span / 2 + i * pitch
        fp.add_pad(
            name=str(pin_num),
            x=-pad_x,
            y=y,
            width=pad_width,
            height=pad_height,
        )

    # Right side (pins pins_per_side+1 to pins, top to bottom)
    for i in range(pins_per_side):
        pin_num = pins_per_side + i + 1
        y = total_pin_span / 2 - i * pitch
        fp.add_pad(
            name=str(pin_num),
            x=pad_x,
            y=y,
            width=pad_width,
            height=pad_height,
        )

    # Add silkscreen outline
    silk_margin = 0.2
    silk_x = body_width / 2 + silk_margin
    silk_y = body_length / 2 + silk_margin

    # Top and bottom lines
    fp.add_line((-silk_x, -silk_y), (silk_x, -silk_y), "F.SilkS", 0.12)
    fp.add_line((-silk_x, silk_y), (silk_x, silk_y), "F.SilkS", 0.12)

    # Side lines (only where they don't overlap pads)
    pad_extent = total_pin_span / 2 + pad_height / 2 + 0.15
    if pad_extent < silk_y:
        fp.add_line((-silk_x, -silk_y), (-silk_x, -pad_extent), "F.SilkS", 0.12)
        fp.add_line((-silk_x, pad_extent), (-silk_x, silk_y), "F.SilkS", 0.12)
        fp.add_line((silk_x, -silk_y), (silk_x, -pad_extent), "F.SilkS", 0.12)
        fp.add_line((silk_x, pad_extent), (silk_x, silk_y), "F.SilkS", 0.12)

    # Pin 1 marker (triangle on silkscreen)
    marker_x = -pad_x - pad_width / 2 - 0.3
    marker_y = -total_pin_span / 2
    fp.add_circle((marker_x, marker_y), 0.15, "F.SilkS", 0.12, fill=True)

    # Courtyard (0.25mm outside pads)
    courtyard_margin = 0.25
    crt_x = pad_x + pad_width / 2 + courtyard_margin
    crt_y = max(body_length / 2, total_pin_span / 2 + pad_height / 2) + courtyard_margin
    fp.add_rect((-crt_x, -crt_y), (crt_x, crt_y), "F.CrtYd", 0.05)

    # Fab layer (body outline with pin 1 chamfer)
    fab_x = body_width / 2
    fab_y = body_length / 2
    chamfer = 0.8
    fp.add_line((-fab_x + chamfer, -fab_y), (fab_x, -fab_y), "F.Fab", 0.1)
    fp.add_line((fab_x, -fab_y), (fab_x, fab_y), "F.Fab", 0.1)
    fp.add_line((fab_x, fab_y), (-fab_x, fab_y), "F.Fab", 0.1)
    fp.add_line((-fab_x, fab_y), (-fab_x, -fab_y + chamfer), "F.Fab", 0.1)
    fp.add_line((-fab_x, -fab_y + chamfer), (-fab_x + chamfer, -fab_y), "F.Fab", 0.1)

    return fp

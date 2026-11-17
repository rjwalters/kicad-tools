"""
Through-hole footprint generators.

Generates DIP packages and pin headers.
"""

from ..footprint import Footprint
from .standards import DIP_STANDARDS


def create_dip(
    pins: int,
    pitch: float | None = None,
    row_spacing: float | None = None,
    pad_diameter: float | None = None,
    drill: float | None = None,
    name: str | None = None,
) -> Footprint:
    """
    Create a DIP (Dual In-line Package) footprint.

    Args:
        pins: Number of pins (must be even)
        pitch: Pin pitch in mm (default: 2.54mm)
        row_spacing: Distance between rows in mm (default: 7.62mm for narrow,
                     15.24mm for wide)
        pad_diameter: Pad diameter in mm
        drill: Drill hole diameter in mm
        name: Custom footprint name (auto-generated if not specified)

    Returns:
        Footprint object ready for export

    Example:
        >>> fp = create_dip(pins=8)
        >>> fp.name
        'DIP-8_W7.62mm_P2.54mm'

        >>> fp = create_dip(pins=28, row_spacing=15.24)
        >>> fp.name
        'DIP-28_W15.24mm_P2.54mm'
    """
    if pins % 2 != 0:
        raise ValueError(f"DIP must have even number of pins, got {pins}")

    if pins < 4:
        raise ValueError(f"DIP must have at least 4 pins, got {pins}")

    # Get defaults from standards
    std = DIP_STANDARDS.get(pins, {})
    pitch = pitch or std.get("pitch", 2.54)
    row_spacing = row_spacing or std.get("row_spacing", 7.62 if pins <= 22 else 15.24)
    pad_diameter = pad_diameter or std.get("pad_diameter", 1.6)
    drill = drill or std.get("drill", 0.8)

    if name is None:
        name = f"DIP-{pins}_W{row_spacing}mm_P{pitch}mm"

    fp = Footprint(
        name=name,
        description=f"DIP, {pins} Pin, pitch {pitch}mm, row spacing {row_spacing}mm",
        tags=["DIP", "THT", f"P{pitch}mm"],
        attr="through_hole",
    )

    pins_per_side = pins // 2
    span = (pins_per_side - 1) * pitch

    # Left column (pins 1 to pins_per_side)
    for i in range(pins_per_side):
        pin_num = i + 1
        y = -span / 2 + i * pitch
        fp.add_pad(
            str(pin_num),
            x=-row_spacing / 2,
            y=y,
            width=pad_diameter,
            height=pad_diameter,
            pad_type="thru_hole",
            shape="circle" if pin_num > 1 else "rect",  # Pin 1 is square
            drill=drill,
            layers=("*.Cu", "*.Mask"),
        )

    # Right column (pins pins_per_side+1 to pins, bottom to top)
    for i in range(pins_per_side):
        pin_num = pins_per_side + i + 1
        y = span / 2 - i * pitch
        fp.add_pad(
            str(pin_num),
            x=row_spacing / 2,
            y=y,
            width=pad_diameter,
            height=pad_diameter,
            pad_type="thru_hole",
            shape="circle",
            drill=drill,
            layers=("*.Cu", "*.Mask"),
        )

    # Silkscreen outline
    body_width = row_spacing - 2.0  # Body is narrower than row spacing
    body_length = span + pitch
    silk_x = body_width / 2
    silk_y = body_length / 2

    # Body outline with notch at pin 1 end
    notch_radius = 0.8
    fp.add_line((-silk_x, -silk_y), (silk_x, -silk_y), "F.SilkS", 0.12)
    fp.add_line((silk_x, -silk_y), (silk_x, silk_y), "F.SilkS", 0.12)
    fp.add_line((silk_x, silk_y), (-silk_x, silk_y), "F.SilkS", 0.12)
    fp.add_line((-silk_x, silk_y), (-silk_x, -silk_y + notch_radius), "F.SilkS", 0.12)
    fp.add_line(
        (-silk_x, -silk_y + notch_radius), (-silk_x + notch_radius, -silk_y), "F.SilkS", 0.12
    )

    # Pin 1 marker
    fp.add_circle((-row_spacing / 2, -span / 2 - 0.8), 0.2, "F.SilkS", 0.12, fill=True)

    # Courtyard
    crt_margin = 0.25
    crt_x = row_spacing / 2 + pad_diameter / 2 + crt_margin
    crt_y = span / 2 + pad_diameter / 2 + crt_margin
    fp.add_rect((-crt_x, -crt_y), (crt_x, crt_y), "F.CrtYd", 0.05)

    # Fab layer
    fp.add_rect((-silk_x, -silk_y), (silk_x, silk_y), "F.Fab", 0.1)

    return fp


def create_pin_header(
    pins: int,
    rows: int = 1,
    pitch: float = 2.54,
    pad_diameter: float = 1.7,
    drill: float = 1.0,
    name: str | None = None,
) -> Footprint:
    """
    Create a pin header footprint.

    Args:
        pins: Total number of pins
        rows: Number of rows (1 or 2)
        pitch: Pin pitch in mm (default: 2.54mm)
        pad_diameter: Pad diameter in mm
        drill: Drill hole diameter in mm
        name: Custom footprint name (auto-generated if not specified)

    Returns:
        Footprint object ready for export

    Example:
        >>> fp = create_pin_header(pins=10, rows=1)
        >>> fp.name
        'PinHeader_1x10_P2.54mm_Vertical'

        >>> fp = create_pin_header(pins=20, rows=2)
        >>> fp.name
        'PinHeader_2x10_P2.54mm_Vertical'
    """
    if rows not in (1, 2):
        raise ValueError(f"Rows must be 1 or 2, got {rows}")

    if rows == 2 and pins % 2 != 0:
        raise ValueError(f"2-row header must have even number of pins, got {pins}")

    pins_per_row = pins if rows == 1 else pins // 2

    if name is None:
        name = f"PinHeader_{rows}x{pins_per_row}_P{pitch}mm_Vertical"

    fp = Footprint(
        name=name,
        description=f"Pin Header, {rows}x{pins_per_row}, pitch {pitch}mm",
        tags=["PinHeader", "THT", f"P{pitch}mm"],
        attr="through_hole",
    )

    span = (pins_per_row - 1) * pitch
    row_offset = pitch / 2 if rows == 2 else 0

    # Single row: pins 1..N down the column.  Dual row: odd pins in the left
    # column, even pins in the right, stepping together (KiCad stock
    # PinHeader_2xN numbering; matches the Pi GPIO header).
    for idx in range(pins):
        if rows == 1:
            x, i = 0.0, idx
        else:
            x = -row_offset + (idx % 2) * pitch
            i = idx // 2
        pin_num = idx + 1
        fp.add_pad(
            str(pin_num),
            x=x,
            y=-span / 2 + i * pitch,
            width=pad_diameter,
            height=pad_diameter,
            pad_type="thru_hole",
            shape="rect" if pin_num == 1 else "circle",
            drill=drill,
            layers=("*.Cu", "*.Mask"),
        )

    # Silkscreen outline
    silk_margin = 0.3
    if rows == 1:
        silk_x = pitch / 2 - 0.1
    else:
        silk_x = pitch + 0.1
    silk_y = span / 2 + pitch / 2

    fp.add_rect(
        (-silk_x - silk_margin, -silk_y - silk_margin),
        (silk_x + silk_margin, silk_y + silk_margin),
        "F.SilkS",
        0.12,
    )

    # Pin 1 marker
    marker_x = -silk_x - silk_margin - 0.3 if rows == 1 else -row_offset - 0.5
    fp.add_circle((marker_x, -span / 2), 0.15, "F.SilkS", 0.12, fill=True)

    # Courtyard
    crt_margin = 0.25
    crt_x = silk_x + silk_margin + crt_margin
    crt_y = silk_y + silk_margin + crt_margin
    fp.add_rect((-crt_x, -crt_y), (crt_x, crt_y), "F.CrtYd", 0.05)

    # Fab layer
    fp.add_rect((-silk_x, -silk_y), (silk_x, silk_y), "F.Fab", 0.1)

    return fp


# Raspberry Pi HAT mechanical spec (HAT outline 65 x 56.5 mm, holes 58 x 49 mm).
_HAT_HOLE_DRILL = 2.75  # spec: 2.75 +/- 0.05 mm
_HAT_LAND_MIN = 6.2  # spec: mounting hole land min. 6.2 mm
_HAT_HOLE_SPACING_X = 49.0
_HAT_HOLE_SPACING_Y = 58.0
_HAT_OUTLINE = (65.0, 56.5)  # (long, short)


def _translate_graphic(g, dx: float, dy: float) -> None:
    """Shift a footprint graphic in place by (dx, dy)."""
    for attr, delta in (
        ("start_x", dx),
        ("end_x", dx),
        ("mid_x", dx),
        ("center_x", dx),
        ("start_y", dy),
        ("end_y", dy),
        ("mid_y", dy),
        ("center_y", dy),
    ):
        if hasattr(g, attr):
            setattr(g, attr, getattr(g, attr) + delta)


def create_rpi_hat(
    keepout_diameter: float = _HAT_LAND_MIN,
    name: str | None = None,
) -> Footprint:
    """
    Create a Raspberry Pi HAT part: 2x20 header plus four mounting holes.

    The header (pin 1 at the origin, as in KiCad's stock
    ``PinHeader_2x20_P2.54mm_Vertical``) and the four 2.75 mm NPTH mounting
    holes are one rigid footprint, so the holes cannot drift away from the
    header.  The holes are unnumbered pads, so the CPL centroid stays on the
    header.  A 65 x 56.5 mm HAT outline is drawn on F.Fab and a copper
    keep-out of ``keepout_diameter`` is emitted around each hole.

    Gotcha: the keep-outs are footprint-local zones.  Inside a ``.kicad_pcb``
    KiCad stores footprint-owned zones in board coordinates, so an in-place
    board edit should emit board-level rule areas instead.

    Args:
        keepout_diameter: Copper keep-out diameter around each hole in mm
            (0 disables the keep-outs)
        name: Custom footprint name

    Returns:
        Footprint object ready for export
    """
    import math

    from ..footprint import KeepoutZone

    if keepout_diameter < 0:
        raise ValueError(f"keepout_diameter must be >= 0, got {keepout_diameter}")

    pitch = 2.54
    fp = create_pin_header(
        40,
        rows=2,
        pitch=pitch,
        name=name or "RPi_HAT_2x20_P2.54mm_MountingHoles",
    )
    fp.description = "Raspberry Pi HAT: 2x20 header with four 2.75mm mounting holes"
    fp.tags = ["RPi", "HAT", "PinHeader", "THT", "MountingHole"]

    # Move pin 1 to the origin (create_pin_header centres the header).
    dx = pitch / 2
    dy = 19 * pitch / 2
    for pad in fp.pads:
        pad.x += dx
        pad.y += dy
    for g in fp.graphics:
        _translate_graphic(g, dx, dy)

    cx = pitch / 2
    x_lo = cx - _HAT_HOLE_SPACING_X
    y_lo = -4.87
    y_hi = y_lo + _HAT_HOLE_SPACING_Y
    holes = [(cx, y_lo), (x_lo, y_lo), (cx, y_hi), (x_lo, y_hi)]
    for hx, hy in holes:
        fp.add_pad(
            "",
            x=hx,
            y=hy,
            width=_HAT_HOLE_DRILL,
            height=_HAT_HOLE_DRILL,
            pad_type="np_thru_hole",
            shape="circle",
            drill=_HAT_HOLE_DRILL,
            layers=("*.Cu", "*.Mask"),
        )
        if keepout_diameter > 0:
            # Circumscribe the 16-gon so its flats keep the full land.
            r = keepout_diameter / 2 / math.cos(math.pi / 16)
            pts = [
                (hx + r * math.cos(2 * math.pi * k / 16), hy + r * math.sin(2 * math.pi * k / 16))
                for k in range(16)
            ]
            fp.zones.append(KeepoutZone(points=pts, layers=("*.Cu",), name="hat_hole_keepout"))

    # HAT outline (65 long along Y, 56.5 wide along X).  The far (-x) edge keeps
    # the spec's 3.5 mm hole margin; the extra 0.5 mm goes on the header (+x) side.
    long_side, short_side = _HAT_OUTLINE
    x0 = x_lo - 3.5
    ocy = (y_lo + y_hi) / 2
    fp.add_rect(
        (x0, ocy - long_side / 2),
        (x0 + short_side, ocy + long_side / 2),
        "F.Fab",
        0.1,
    )
    return fp

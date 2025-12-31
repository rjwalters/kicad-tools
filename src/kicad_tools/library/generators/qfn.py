"""
QFN (Quad Flat No-lead) footprint generator.

Generates QFN and DFN footprints with optional exposed thermal pads.
"""

from ..footprint import Footprint
from .standards import QFN_STANDARDS


def create_qfn(
    pins: int,
    pitch: float | None = None,
    body_size: float | None = None,
    pad_width: float | None = None,
    pad_height: float | None = None,
    exposed_pad: float | None = None,
    name: str | None = None,
) -> Footprint:
    """
    Create a QFN footprint with optional exposed thermal pad.

    Args:
        pins: Number of pins (not including exposed pad)
        pitch: Pin pitch in mm (default: 0.5mm)
        body_size: Package body size in mm (square packages)
        pad_width: Pad width (length of pad) in mm
        pad_height: Pad height (perpendicular to pad length) in mm
        exposed_pad: Size of center exposed thermal pad in mm (optional)
        name: Custom footprint name (auto-generated if not specified)

    Returns:
        Footprint object ready for export

    Example:
        >>> fp = create_qfn(pins=16, pitch=0.5, body_size=3.0, exposed_pad=1.7)
        >>> fp.save("MyLib.pretty/QFN-16_3x3mm.kicad_mod")
    """
    if pins < 4:
        raise ValueError(f"QFN must have at least 4 pins, got {pins}")

    if pins % 4 != 0:
        raise ValueError(f"QFN must have pins divisible by 4, got {pins}")

    # Get defaults from standards table
    key = (pins, body_size) if body_size else None
    std = QFN_STANDARDS.get(key, {})

    pitch = pitch or std.get("pitch", 0.5)
    body_size = body_size or 4.0  # Default 4x4mm
    pad_width = pad_width or std.get("pad_width", 0.8)
    pad_height = pad_height or std.get("pad_height", 0.3)
    exposed_pad = exposed_pad if exposed_pad is not None else std.get("exposed_pad")

    pins_per_side = pins // 4

    # Generate IPC-7351 compliant name
    if name is None:
        ep_suffix = "_EP" if exposed_pad else ""
        name = f"QFN-{pins}_{body_size}x{body_size}mm_P{pitch}mm{ep_suffix}"

    desc = f"QFN, {pins} Pin, pitch {pitch}mm, {body_size}x{body_size}mm"
    if exposed_pad:
        desc += f", exposed pad {exposed_pad}x{exposed_pad}mm"

    fp = Footprint(
        name=name,
        description=desc,
        tags=["QFN", f"P{pitch}mm", "DFN"],
        attr="smd",
    )

    # Calculate pad positions
    span = (pins_per_side - 1) * pitch
    pad_center = body_size / 2 - pad_width / 2 + 0.1  # Slight extension

    pin_num = 1

    # Bottom side (left to right)
    for i in range(pins_per_side):
        x = -span / 2 + i * pitch
        y = pad_center
        fp.add_pad(str(pin_num), x, y, pad_height, pad_width)
        pin_num += 1

    # Right side (bottom to top)
    for i in range(pins_per_side):
        x = pad_center
        y = span / 2 - i * pitch
        fp.add_pad(str(pin_num), x, y, pad_width, pad_height)
        pin_num += 1

    # Top side (right to left)
    for i in range(pins_per_side):
        x = span / 2 - i * pitch
        y = -pad_center
        fp.add_pad(str(pin_num), x, y, pad_height, pad_width)
        pin_num += 1

    # Left side (top to bottom)
    for i in range(pins_per_side):
        x = -pad_center
        y = -span / 2 + i * pitch
        fp.add_pad(str(pin_num), x, y, pad_width, pad_height)
        pin_num += 1

    # Exposed thermal pad
    if exposed_pad:
        fp.add_pad(
            name=str(pins + 1),
            x=0,
            y=0,
            width=exposed_pad,
            height=exposed_pad,
            shape="rect",
            layers=("F.Cu", "F.Paste", "F.Mask"),
        )

    # Silkscreen - just corners to avoid pad overlap
    silk_x = body_size / 2 + 0.1
    silk_y = body_size / 2 + 0.1
    corner_len = min(0.6, span / 3)

    # Draw corner marks
    for sx, sy in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
        cx, cy = sx * silk_x, sy * silk_y
        fp.add_line((cx, cy - sy * corner_len), (cx, cy), "F.SilkS", 0.12)
        fp.add_line((cx, cy), (cx - sx * corner_len, cy), "F.SilkS", 0.12)

    # Pin 1 marker
    marker_x = -span / 2 - 0.3
    marker_y = silk_y + 0.4
    fp.add_circle((marker_x, marker_y), 0.15, "F.SilkS", 0.12, fill=True)

    # Courtyard
    crt_margin = 0.25
    crt = body_size / 2 + crt_margin
    fp.add_rect((-crt, -crt), (crt, crt), "F.CrtYd", 0.05)

    # Fab layer with pin 1 chamfer
    fab = body_size / 2
    chamfer = min(0.5, body_size / 8)
    fp.add_line((-fab + chamfer, fab), (fab, fab), "F.Fab", 0.1)
    fp.add_line((fab, fab), (fab, -fab), "F.Fab", 0.1)
    fp.add_line((fab, -fab), (-fab, -fab), "F.Fab", 0.1)
    fp.add_line((-fab, -fab), (-fab, fab - chamfer), "F.Fab", 0.1)
    fp.add_line((-fab, fab - chamfer), (-fab + chamfer, fab), "F.Fab", 0.1)

    return fp

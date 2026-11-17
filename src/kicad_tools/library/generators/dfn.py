"""
DFN (Dual Flat No-lead) footprint generator.

Generates DFN footprints with optional exposed thermal pads and wettable flanks.
DFN packages have leads on two sides (unlike QFN which has leads on all four sides).
"""

from ..footprint import Footprint
from .standards import DFN_STANDARDS


def create_dfn(
    pins: int,
    pitch: float = 0.5,
    body_width: float | None = None,
    body_length: float | None = None,
    pad_width: float | None = None,
    pad_height: float | None = None,
    exposed_pad: tuple[float, float] | float | bool | None = None,
    wettable_flanks: bool = False,
    name: str | None = None,
) -> Footprint:
    """
    Create a DFN footprint with optional exposed thermal pad.

    DFN packages have leads on two opposing sides (top and bottom), unlike
    QFN which has leads on all four sides.

    Args:
        pins: Number of pins (not including exposed pad, must be even)
        pitch: Pin pitch in mm (default: 0.5mm)
        body_width: Package body width in mm (perpendicular to leads)
        body_length: Package body length in mm (parallel to leads)
        pad_width: Pad width (length extending from body) in mm
        pad_height: Pad height (perpendicular to pad length) in mm
        exposed_pad: Exposed thermal pad specification:
            - tuple (width, height): Custom pad dimensions
            - float: Square pad with given size
            - True: Auto-calculate based on body size
            - None/False: No exposed pad
        wettable_flanks: If True, increase pad size for wettable flank packages
        name: Custom footprint name (auto-generated if not specified)

    Returns:
        Footprint object ready for export

    Example:
        >>> fp = create_dfn(pins=8, pitch=0.5, body_width=3.0, body_length=3.0)
        >>> fp.save("MyLib.pretty/DFN-8_3x3mm.kicad_mod")
    """
    if pins < 2:
        raise ValueError(f"DFN must have at least 2 pins, got {pins}")
    if pins % 2 != 0:
        raise ValueError(f"DFN must have even number of pins, got {pins}")
    if pitch <= 0:
        raise ValueError(f"Pitch must be positive, got {pitch}")

    pins_per_side = pins // 2

    # Get defaults from standards table if available
    key = (pins, body_width, body_length) if body_width and body_length else None
    std = DFN_STANDARDS.get(key, {})

    # Apply defaults
    body_width = body_width or std.get("body_width", 3.0)
    body_length = body_length or std.get("body_length", 3.0)
    pad_width = pad_width or std.get("pad_width", 0.8)
    pad_height = pad_height or std.get("pad_height", 0.3)

    # Wettable flanks increase pad size for better solder wetting
    if wettable_flanks:
        pad_width = pad_width * 1.2
        pad_height = pad_height * 1.1

    # Process exposed pad specification
    ep_width = None
    ep_height = None
    if exposed_pad is True:
        # Auto-calculate: roughly 60% of body area
        ep_width = body_width * 0.6
        ep_height = body_length * 0.5
    elif isinstance(exposed_pad, (int, float)):
        ep_width = ep_height = float(exposed_pad)
    elif isinstance(exposed_pad, tuple) and len(exposed_pad) == 2:
        ep_width, ep_height = exposed_pad

    has_exposed_pad = ep_width is not None and ep_height is not None

    # Generate IPC-7351 compliant name
    if name is None:
        ep_suffix = "_EP" if has_exposed_pad else ""
        wf_suffix = "_WF" if wettable_flanks else ""
        name = f"DFN-{pins}_{body_width}x{body_length}mm_P{pitch}mm{ep_suffix}{wf_suffix}"

    desc = f"DFN, {pins} Pin, pitch {pitch}mm, {body_width}x{body_length}mm"
    if has_exposed_pad:
        desc += f", exposed pad {ep_width}x{ep_height}mm"
    if wettable_flanks:
        desc += ", wettable flanks"

    tags = ["DFN", f"P{pitch}mm"]
    if wettable_flanks:
        tags.append("wettable-flanks")

    fp = Footprint(
        name=name,
        description=desc,
        tags=tags,
        attr="smd",
    )

    # Calculate pad positions
    span = (pins_per_side - 1) * pitch
    pad_center_y = body_length / 2 - pad_width / 2 + 0.1  # Slight extension

    pin_num = 1

    # Bottom side (left to right) - pins 1 to pins_per_side
    for i in range(pins_per_side):
        x = -span / 2 + i * pitch
        y = pad_center_y
        fp.add_pad(str(pin_num), x, y, pad_height, pad_width)
        pin_num += 1

    # Top side (right to left) - pins pins_per_side+1 to pins
    for i in range(pins_per_side):
        x = span / 2 - i * pitch
        y = -pad_center_y
        fp.add_pad(str(pin_num), x, y, pad_height, pad_width)
        pin_num += 1

    # Exposed thermal pad
    if has_exposed_pad:
        fp.add_pad(
            name=str(pins + 1),
            x=0,
            y=0,
            width=ep_width,
            height=ep_height,
            shape="rect",
            layers=("F.Cu", "F.Paste", "F.Mask"),
        )

    # Silkscreen - lines on left and right sides (avoiding pads on top/bottom)
    silk_x = body_width / 2 + 0.1
    silk_y = body_length / 2 + 0.1

    # Left and right edge lines
    fp.add_line((-silk_x, -silk_y), (-silk_x, silk_y), "F.SilkS", 0.12)
    fp.add_line((silk_x, -silk_y), (silk_x, silk_y), "F.SilkS", 0.12)

    # Pin 1 marker
    marker_x = -span / 2 - 0.3
    marker_y = silk_y + 0.4
    fp.add_circle((marker_x, marker_y), 0.15, "F.SilkS", 0.12, fill=True)

    # Courtyard
    crt_margin = 0.25
    crt_x = body_width / 2 + crt_margin
    crt_y = body_length / 2 + crt_margin
    fp.add_rect((-crt_x, -crt_y), (crt_x, crt_y), "F.CrtYd", 0.05)

    # Fab layer with pin 1 chamfer
    fab_x = body_width / 2
    fab_y = body_length / 2
    chamfer = min(0.5, min(body_width, body_length) / 8)
    fp.add_line((-fab_x + chamfer, fab_y), (fab_x, fab_y), "F.Fab", 0.1)
    fp.add_line((fab_x, fab_y), (fab_x, -fab_y), "F.Fab", 0.1)
    fp.add_line((fab_x, -fab_y), (-fab_x, -fab_y), "F.Fab", 0.1)
    fp.add_line((-fab_x, -fab_y), (-fab_x, fab_y - chamfer), "F.Fab", 0.1)
    fp.add_line((-fab_x, fab_y - chamfer), (-fab_x + chamfer, fab_y), "F.Fab", 0.1)

    return fp


def create_dfn_standard(package: str) -> Footprint:
    """
    Create a DFN footprint from a standard package name.

    Args:
        package: Standard package name (e.g., "DFN-8_3x3_0.5mm")

    Returns:
        Footprint object ready for export

    Example:
        >>> fp = create_dfn_standard("DFN-8_3x3_0.5mm")
    """
    if package not in DFN_STANDARDS:
        # Only list string keys (package names), not tuple keys (for internal lookup)
        available = ", ".join(sorted(k for k in DFN_STANDARDS.keys() if isinstance(k, str)))
        raise ValueError(f"Unknown DFN package '{package}'. Available: {available}")

    spec = DFN_STANDARDS[package]
    return create_dfn(
        pins=spec["pins"],
        pitch=spec["pitch"],
        body_width=spec["body_width"],
        body_length=spec["body_length"],
        pad_width=spec.get("pad_width"),
        pad_height=spec.get("pad_height"),
        exposed_pad=spec.get("exposed_pad"),
        wettable_flanks=spec.get("wettable_flanks", False),
        name=package,
    )

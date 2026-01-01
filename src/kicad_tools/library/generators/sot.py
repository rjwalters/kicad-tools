"""
SOT (Small Outline Transistor) footprint generator.

Generates SOT-23, SOT-223, SOT-89, and variant footprints.
"""

from ..footprint import Footprint
from .standards import SOT_STANDARDS


def create_sot(
    variant: str | None = None,
    pins: int | None = None,
    pitch: float | None = None,
    body_width: float | None = None,
    body_length: float | None = None,
    name: str | None = None,
) -> Footprint:
    """
    Create a SOT footprint.

    Args:
        variant: Standard variant name ("SOT-23", "SOT-23-5", "SOT-23-6",
                 "SOT-223", "SOT-89")
        pins: Number of pins (used with custom dimensions)
        pitch: Pin pitch in mm (used with custom dimensions)
        body_width: Package body width in mm (used with custom dimensions)
        body_length: Package body length in mm (used with custom dimensions)
        name: Custom footprint name (auto-generated if not specified)

    Returns:
        Footprint object ready for export

    Example:
        >>> fp = create_sot("SOT-23")
        >>> fp = create_sot("SOT-23-5")
        >>> fp = create_sot("SOT-223")
    """
    if variant is not None:
        if variant not in SOT_STANDARDS:
            raise ValueError(
                f"Unknown SOT variant: {variant}. Valid variants: {list(SOT_STANDARDS.keys())}"
            )
        std = SOT_STANDARDS[variant]
        pins = std["pins"]
        pitch = std["pitch"]
        body_width = std["body_width"]
        body_length = std["body_length"]
        pad_width = std["pad_width"]
        pad_height = std["pad_height"]
        pad_positions = std["pad_positions"]
        tab_width = std.get("tab_width")
        tab_height = std.get("tab_height")
    else:
        if pins is None or pitch is None or body_width is None or body_length is None:
            raise ValueError(
                "Either specify a variant or provide pins, pitch, body_width, body_length"
            )
        # Use SOT-23 style layout for custom
        pad_width = 1.0
        pad_height = 0.6
        pad_positions = None
        tab_width = None
        tab_height = None

    if name is None:
        if variant:
            name = variant
        else:
            name = f"SOT-{pins}_{body_width}x{body_length}mm"

    fp = Footprint(
        name=name,
        description=f"{name}, {pins} Pin, {body_width}x{body_length}mm",
        tags=["SOT", name.replace("-", "")],
        attr="smd",
    )

    # Add pads
    if pad_positions is not None:
        for i, (x, y) in enumerate(pad_positions):
            pin_num = i + 1
            # SOT-223 has a larger tab for the last pin
            if variant == "SOT-223" and pin_num == pins:
                fp.add_pad(str(pin_num), x, y, tab_width, tab_height, shape="rect")
            elif variant == "SOT-89" and pin_num == 2:
                # SOT-89 has an extended center tab
                fp.add_pad(str(pin_num), x, y - 0.7, tab_width, tab_height + 1.4, shape="rect")
            else:
                fp.add_pad(str(pin_num), x, y, pad_width, pad_height)
    else:
        # Generic SOT-23 style layout
        if pins == 3:
            positions = [
                (-pitch / 2, body_length / 2),
                (pitch / 2, body_length / 2),
                (0, -body_length / 2),
            ]
        else:
            raise ValueError(f"Custom SOT layout not implemented for {pins} pins")

        for i, (x, y) in enumerate(positions):
            fp.add_pad(str(i + 1), x, y, pad_width, pad_height)

    # Silkscreen outline
    silk_margin = 0.15
    silk_x = body_width / 2 + silk_margin
    silk_y = body_length / 2 + silk_margin

    # For SOT-23 variants, draw lines that avoid pads
    if variant and variant.startswith("SOT-23"):
        # Left and right edges
        fp.add_line((-silk_x, -silk_y), (-silk_x, silk_y), "F.SilkS", 0.12)
        fp.add_line((silk_x, -silk_y), (silk_x, silk_y), "F.SilkS", 0.12)

        # Pin 1 marker
        fp.add_circle((-body_width / 2 - 0.5, body_length / 2), 0.15, "F.SilkS", 0.12, fill=True)

    elif variant == "SOT-223":
        # Draw corner marks to avoid the large tab
        corner = 0.8
        # Top corners
        fp.add_line((-silk_x, -silk_y + corner), (-silk_x, -silk_y), "F.SilkS", 0.12)
        fp.add_line((-silk_x, -silk_y), (-silk_x + corner, -silk_y), "F.SilkS", 0.12)
        fp.add_line((silk_x - corner, -silk_y), (silk_x, -silk_y), "F.SilkS", 0.12)
        fp.add_line((silk_x, -silk_y), (silk_x, -silk_y + corner), "F.SilkS", 0.12)

        # Bottom corners (near pins)
        fp.add_line((-silk_x, silk_y), (-silk_x, silk_y - corner), "F.SilkS", 0.12)
        fp.add_line((silk_x, silk_y), (silk_x, silk_y - corner), "F.SilkS", 0.12)

        # Pin 1 marker
        fp.add_circle((-pitch - 0.5, silk_y + 0.3), 0.15, "F.SilkS", 0.12, fill=True)

    elif variant == "SOT-89":
        # Similar to SOT-223
        corner = 0.6
        fp.add_line((-silk_x, -silk_y + corner), (-silk_x, -silk_y), "F.SilkS", 0.12)
        fp.add_line((-silk_x, -silk_y), (-silk_x + corner, -silk_y), "F.SilkS", 0.12)
        fp.add_line((silk_x - corner, -silk_y), (silk_x, -silk_y), "F.SilkS", 0.12)
        fp.add_line((silk_x, -silk_y), (silk_x, -silk_y + corner), "F.SilkS", 0.12)

        fp.add_circle((-pitch - 0.4, silk_y + 0.3), 0.15, "F.SilkS", 0.12, fill=True)

    # Courtyard
    crt_margin = 0.25
    # Account for pad extensions
    max_y = max(abs(p.y) + p.height / 2 for p in fp.pads)
    max_x = max(abs(p.x) + p.width / 2 for p in fp.pads)
    crt_x = max(body_width / 2, max_x) + crt_margin
    crt_y = max(body_length / 2, max_y) + crt_margin
    fp.add_rect((-crt_x, -crt_y), (crt_x, crt_y), "F.CrtYd", 0.05)

    # Fab layer
    fab_x = body_width / 2
    fab_y = body_length / 2
    fp.add_rect((-fab_x, -fab_y), (fab_x, fab_y), "F.Fab", 0.1)

    return fp

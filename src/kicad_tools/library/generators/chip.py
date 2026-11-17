"""
Chip component footprint generator.

Generates footprints for chip resistors, capacitors, and other 2-terminal
passive components following IPC-7351 naming conventions.
"""

from ..footprint import Footprint
from .standards import CHIP_SIZES


def create_chip(
    size: str,
    prefix: str = "",
    metric: bool = False,
    name: str | None = None,
) -> Footprint:
    """
    Create a chip component footprint (resistor, capacitor, etc.).

    Args:
        size: Imperial size code ("0201", "0402", "0603", "0805", "1206", etc.)
        prefix: Component prefix for naming ("R", "C", "L", etc.)
        metric: Use metric naming convention (e.g., "1005" instead of "0402")
        name: Custom footprint name (auto-generated if not specified)

    Returns:
        Footprint object ready for export

    Example:
        >>> fp = create_chip("0603", prefix="R")
        >>> fp.name
        'R_0603_1608Metric'

        >>> fp = create_chip("0402", prefix="C", metric=True)
        >>> fp.name
        'C_1005Metric'
    """
    if size not in CHIP_SIZES:
        valid_sizes = ", ".join(sorted(CHIP_SIZES.keys()))
        raise ValueError(f"Unknown chip size: {size}. Valid sizes: {valid_sizes}")

    std = CHIP_SIZES[size]
    length = std["length"]
    width = std["width"]
    pad_width = std["pad_width"]
    pad_height = std["pad_height"]
    pad_gap = std["pad_gap"]
    metric_size = std["metric"]

    # Generate IPC-7351 compliant name
    if name is None:
        if metric:
            name = f"{prefix}_{metric_size}Metric" if prefix else f"{metric_size}Metric"
        else:
            name = (
                f"{prefix}_{size}_{metric_size}Metric" if prefix else f"{size}_{metric_size}Metric"
            )

    # Determine component type for description
    if prefix == "R":
        comp_type = "Resistor"
    elif prefix == "C":
        comp_type = "Capacitor"
    elif prefix == "L":
        comp_type = "Inductor"
    else:
        comp_type = "Chip"

    fp = Footprint(
        name=name,
        description=f"{comp_type} SMD {size} ({length}x{width}mm)",
        tags=[size, metric_size, "SMD", comp_type.lower()],
        attr="smd",
    )

    # Calculate pad positions
    # Pads are centered on the component, gap between them
    pad_x = (pad_gap + pad_width) / 2

    # Pad 1 on left (cathode for diodes/LEDs, negative for caps)
    fp.add_pad("1", -pad_x, 0, pad_width, pad_height)
    # Pad 2 on right
    fp.add_pad("2", pad_x, 0, pad_width, pad_height)

    # Silkscreen - just small marks at ends to avoid pad overlap
    silk_y = width / 2 + 0.15
    if pad_height < width * 0.9:  # Only if there's room
        fp.add_line((-length / 2 - 0.1, -silk_y), (-length / 2 - 0.1, silk_y), "F.SilkS", 0.12)
        fp.add_line((length / 2 + 0.1, -silk_y), (length / 2 + 0.1, silk_y), "F.SilkS", 0.12)

    # Courtyard
    crt_margin = 0.15
    crt_x = pad_x + pad_width / 2 + crt_margin
    crt_y = max(width / 2, pad_height / 2) + crt_margin
    fp.add_rect((-crt_x, -crt_y), (crt_x, crt_y), "F.CrtYd", 0.05)

    # Fab layer - component body outline
    fab_x = length / 2
    fab_y = width / 2
    fp.add_rect((-fab_x, -fab_y), (fab_x, fab_y), "F.Fab", 0.1)

    return fp

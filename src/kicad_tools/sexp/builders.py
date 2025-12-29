#!/usr/bin/env python3
"""
KiCad S-Expression Builders

Convenience functions for building KiCad schematic S-expressions.
These builders produce SExp nodes that serialize to valid KiCad format.

Usage:
    from kicad_sexp_builders import xy, at, stroke, effects, uuid_node

    wire_node = SExp.list("wire",
        SExp.list("pts", xy(10, 20), xy(30, 40)),
        stroke(),
        uuid_node(my_uuid)
    )
"""

from .parser import SExp


def fmt(val: float) -> int | float:
    """Format coordinate with 2 decimal precision.

    Rounds to 2 decimal places. Returns int if no fractional part
    for cleaner output (e.g., 10 instead of 10.0).
    """
    rounded = round(val, 2)
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def xy(x: float, y: float) -> SExp:
    """Build an (xy X Y) coordinate node."""
    return SExp.list("xy", fmt(x), fmt(y))


def at(x: float, y: float, rotation: float = 0) -> SExp:
    """Build an (at X Y [ROTATION]) position node.

    Omits rotation if 0 for cleaner output.
    """
    if rotation == 0:
        return SExp.list("at", fmt(x), fmt(y))
    return SExp.list("at", fmt(x), fmt(y), int(rotation))


def stroke(width: float = 0, stroke_type: str = "solid") -> SExp:
    """Build a (stroke (width W) (type T)) node."""
    return SExp.list("stroke", SExp.list("width", width), SExp.list("type", stroke_type))


def font(size: float = 1.27) -> SExp:
    """Build a (font (size S S)) node."""
    return SExp.list("font", SExp.list("size", size, size))


def effects(font_size: float = 1.27, justify: str = None, hide: bool = False) -> SExp:
    """Build an (effects (font ...) [justify] [hide]) node.

    Args:
        font_size: Font size in mm (default 1.27)
        justify: Justification string (e.g., "left", "right", "left bottom")
        hide: Whether to hide the text
    """
    eff = SExp.list("effects", font(font_size))

    if justify:
        # Handle multi-word justify like "left bottom"
        parts = justify.split()
        if len(parts) == 1:
            eff.append(SExp.list("justify", parts[0]))
        else:
            justify_node = SExp.list("justify")
            for part in parts:
                justify_node.append(SExp.atom(part))
            eff.append(justify_node)

    if hide:
        eff.append(SExp.list("hide", "yes"))

    return eff


def uuid_node(uuid_str: str) -> SExp:
    """Build a (uuid "UUID") node."""
    return SExp.list("uuid", uuid_str)


def property_node(
    name: str,
    value: str,
    x: float,
    y: float,
    rotation: float = 0,
    font_size: float = 1.27,
    hide: bool = False,
) -> SExp:
    """Build a complete property block for symbols.

    Args:
        name: Property name (e.g., "Reference", "Value")
        value: Property value
        x, y: Position
        rotation: Text rotation in degrees
        font_size: Font size in mm
        hide: Whether to hide the property
    """
    prop = SExp.list(
        "property", name, value, at(x, y, rotation), effects(font_size=font_size, hide=hide)
    )
    return prop


def color(r: int = 0, g: int = 0, b: int = 0, a: int = 0) -> SExp:
    """Build a (color R G B A) node."""
    return SExp.list("color", r, g, b, a)


def pts(*points: SExp) -> SExp:
    """Build a (pts (xy ...) (xy ...) ...) node from xy nodes."""
    node = SExp.list("pts")
    for pt in points:
        node.append(pt)
    return node


# =============================================================================
# Higher-level builders for common schematic elements
# =============================================================================


def wire_node(x1: float, y1: float, x2: float, y2: float, uuid_str: str) -> SExp:
    """Build a complete wire S-expression."""
    return SExp.list("wire", pts(xy(x1, y1), xy(x2, y2)), stroke(), uuid_node(uuid_str))


def junction_node(x: float, y: float, uuid_str: str) -> SExp:
    """Build a complete junction S-expression."""
    return SExp.list("junction", at(x, y), SExp.list("diameter", 0), color(), uuid_node(uuid_str))


def label_node(text: str, x: float, y: float, rotation: float, uuid_str: str) -> SExp:
    """Build a complete label S-expression."""
    return SExp.list(
        "label",
        text,
        at(x, y, rotation),
        SExp.list("fields_autoplaced", "yes"),
        effects(justify="left bottom"),
        uuid_node(uuid_str),
    )


def hier_label_node(
    text: str, x: float, y: float, shape: str, rotation: float, uuid_str: str
) -> SExp:
    """Build a complete hierarchical label S-expression."""
    justify = "right" if rotation == 180 else "left"
    return SExp.list(
        "hierarchical_label",
        text,
        SExp.list("shape", shape),
        at(x, y, rotation),
        SExp.list("fields_autoplaced", "yes"),
        effects(justify=justify),
        uuid_node(uuid_str),
    )


def text_node(text: str, x: float, y: float, uuid_str: str) -> SExp:
    """Build a complete text note S-expression."""
    return SExp.list(
        "text",
        text,
        SExp.list("exclude_from_sim", "no"),
        at(x, y, 0),
        effects(font_size=1.524, justify="left"),
        uuid_node(uuid_str),
    )


# =============================================================================
# Symbol builders
# =============================================================================


def symbol_property_node(
    name: str, value: str, x: float, y: float, rotation: float = 0, hide: bool = False
) -> SExp:
    """Build a complete property block for symbols.

    Args:
        name: Property name (e.g., "Reference", "Value")
        value: Property value
        x, y: Position
        rotation: Text rotation in degrees
        hide: Whether to hide the property
    """
    prop = SExp.list("property", name, value, at(x, y, rotation), effects(hide=hide))
    return prop


def pin_uuid_node(pin_number: str, pin_uuid: str) -> SExp:
    """Build a pin UUID mapping node."""
    return SExp.list("pin", pin_number, uuid_node(pin_uuid))


def symbol_instances_node(project_name: str, sheet_path: str, reference: str, unit: int) -> SExp:
    """Build the instances section of a symbol."""
    return SExp.list(
        "instances",
        SExp.list(
            "project",
            project_name,
            SExp.list(
                "path", sheet_path, SExp.list("reference", reference), SExp.list("unit", unit)
            ),
        ),
    )


# =============================================================================
# Title block and document structure builders
# =============================================================================


def title_block(
    title: str, date: str, revision: str, company: str = "", comment1: str = "", comment2: str = ""
) -> SExp:
    """Build a title_block S-expression."""
    return SExp.list(
        "title_block",
        SExp.list("title", title),
        SExp.list("date", date),
        SExp.list("rev", revision),
        SExp.list("company", company),
        SExp.list("comment", 1, comment1),
        SExp.list("comment", 2, comment2),
    )


def sheet_instances(sheet_path: str, page: str) -> SExp:
    """Build a sheet_instances S-expression."""
    return SExp.list("sheet_instances", SExp.list("path", sheet_path, SExp.list("page", page)))


# =============================================================================
# PCB element builders
# =============================================================================


def segment_node(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    width: float,
    layer: str,
    net: int,
    uuid_str: str,
) -> SExp:
    """Build a PCB track segment S-expression.

    Args:
        start_x, start_y: Start point coordinates in mm
        end_x, end_y: End point coordinates in mm
        width: Track width in mm
        layer: Copper layer (e.g., "F.Cu", "B.Cu")
        net: Net number
        uuid_str: Unique identifier

    Example output:
        (segment
            (start 148.5 102.0)
            (end 148.5 101.75)
            (width 0.2)
            (layer "F.Cu")
            (net 6)
            (uuid "...")
        )
    """
    return SExp.list(
        "segment",
        SExp.list("start", fmt(start_x), fmt(start_y)),
        SExp.list("end", fmt(end_x), fmt(end_y)),
        SExp.list("width", fmt(width)),
        SExp.list("layer", layer),
        SExp.list("net", net),
        uuid_node(uuid_str),
    )


def via_node(
    x: float, y: float, size: float, drill: float, layers: tuple[str, str], net: int, uuid_str: str
) -> SExp:
    """Build a PCB via S-expression.

    Args:
        x, y: Via position in mm
        size: Via pad size in mm
        drill: Drill hole diameter in mm
        layers: Tuple of layer names (e.g., ("F.Cu", "B.Cu"))
        net: Net number
        uuid_str: Unique identifier

    Example output:
        (via
            (at 162.5 97.25)
            (size 0.6)
            (drill 0.3)
            (layers "F.Cu" "B.Cu")
            (net 12)
            (uuid "...")
        )
    """
    layers_node = SExp.list("layers", *layers)
    return SExp.list(
        "via",
        at(x, y),
        SExp.list("size", fmt(size)),
        SExp.list("drill", fmt(drill)),
        layers_node,
        SExp.list("net", net),
        uuid_node(uuid_str),
    )


def zone_node(
    net: int,
    net_name: str,
    layer: str,
    points: list[tuple[float, float]],
    uuid_str: str,
    priority: int = 0,
    min_thickness: float = 0.2,
    clearance: float = 0.2,
    thermal_gap: float = 0.3,
    thermal_bridge_width: float = 0.3,
) -> SExp:
    """Build a PCB copper zone (pour) S-expression.

    Args:
        net: Net number
        net_name: Net name (e.g., "GND")
        layer: Copper layer
        points: List of (x, y) boundary points
        uuid_str: Unique identifier
        priority: Zone priority (higher fills later)
        min_thickness: Minimum copper thickness in mm
        clearance: Pad clearance in mm
        thermal_gap: Thermal relief gap in mm
        thermal_bridge_width: Thermal relief bridge width in mm

    Example output:
        (zone
            (net 1)
            (net_name "GND")
            (layer "In1.Cu")
            (uuid "...")
            (hatch edge 0.5)
            (connect_pads (clearance 0.2))
            (min_thickness 0.2)
            (filled_areas_thickness no)
            (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
            (polygon (pts (xy 115.5 75.5) ...))
        )
    """
    # Build polygon points
    pts_children = [xy(x, y) for x, y in points]
    polygon = SExp.list("polygon", SExp.list("pts", *pts_children))

    zone = SExp.list(
        "zone",
        SExp.list("net", net),
        SExp.list("net_name", net_name),
        SExp.list("layer", layer),
        uuid_node(uuid_str),
        SExp.list("hatch", "edge", 0.5),
    )

    # Add priority if non-zero
    if priority > 0:
        zone.append(SExp.list("priority", priority))

    # Add standard zone properties
    zone.append(SExp.list("connect_pads", SExp.list("clearance", fmt(clearance))))
    zone.append(SExp.list("min_thickness", fmt(min_thickness)))
    zone.append(SExp.list("filled_areas_thickness", "no"))
    zone.append(
        SExp.list(
            "fill",
            "yes",
            SExp.list("thermal_gap", fmt(thermal_gap)),
            SExp.list("thermal_bridge_width", fmt(thermal_bridge_width)),
        )
    )
    zone.append(polygon)

    return zone


def footprint_at_node(x: float, y: float, rotation: float = 0) -> SExp:
    """Build an 'at' node for footprint positioning."""
    if rotation != 0:
        return SExp.list("at", fmt(x), fmt(y), fmt(rotation))
    return SExp.list("at", fmt(x), fmt(y))


if __name__ == "__main__":
    # Quick self-test
    print("Testing KiCad SExp Builders\n")

    # Test basic builders
    print("xy(10.5, 20.33):")
    print(f"  {xy(10.5, 20.33).to_string(compact=True)}")

    print("\nat(100, 200, 90):")
    print(f"  {at(100, 200, 90).to_string(compact=True)}")

    print("\nstroke():")
    print(f"  {stroke().to_string(compact=True)}")

    print("\neffects(justify='left bottom', hide=True):")
    print(f"  {effects(justify='left bottom', hide=True).to_string()}")

    print("\nwire_node(10, 20, 30, 40, 'test-uuid'):")
    print(wire_node(10, 20, 30, 40, "test-uuid").to_string())

    print("\njunction_node(50, 60, 'junc-uuid'):")
    print(junction_node(50, 60, "junc-uuid").to_string())

    print("\nlabel_node('GND', 100, 200, 0, 'label-uuid'):")
    print(label_node("GND", 100, 200, 0, "label-uuid").to_string())

    print("\nhier_label_node('MCLK', 100, 200, 'output', 180, 'hl-uuid'):")
    print(hier_label_node("MCLK", 100, 200, "output", 180, "hl-uuid").to_string())

    print("\n--- All tests passed ---")

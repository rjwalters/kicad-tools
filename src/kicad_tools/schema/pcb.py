"""KiCad PCB data models.

Provides classes for parsing and manipulating KiCad PCB files (.kicad_pcb).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.sexp import SExp

from ..core.sexp_file import load_footprint, load_pcb, save_pcb
from ..footprints.library_path import (
    detect_kicad_library_path,
    guess_standard_library,
    parse_library_id,
)

if TYPE_CHECKING:
    from ..manufacturers import DesignRules
    from ..query.footprints import FootprintList


@dataclass
class Layer:
    """PCB layer definition."""

    number: int
    name: str
    type: str  # signal, power, user


@dataclass
class Net:
    """PCB net definition."""

    number: int
    name: str


@dataclass
class Pad:
    """Component pad."""

    number: str
    type: str  # smd, thru_hole
    shape: str  # roundrect, rect, circle, oval
    position: tuple[float, float]
    size: tuple[float, float]
    layers: list[str]
    net_number: int = 0
    net_name: str = ""
    drill: float = 0.0
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Pad | None:
        """Parse pad from S-expression."""
        pad = cls(
            number=sexp.get_string(0) or "",
            type=sexp.get_string(1) or "",
            shape=sexp.get_string(2) or "",
            position=(0.0, 0.0),
            size=(0.0, 0.0),
            layers=[],
        )

        # Position
        if at := sexp.find("at"):
            x = at.get_float(0) or 0.0
            y = at.get_float(1) or 0.0
            pad.position = (x, y)

        # Size
        if size := sexp.find("size"):
            w = size.get_float(0) or 0.0
            h = size.get_float(1) or w
            pad.size = (w, h)

        # Layers
        if layers := sexp.find("layers"):
            pad.layers = [
                layers.get_string(i) or ""
                for i in range(len(layers.values))
                if isinstance(layers.values[i], str)
            ]

        # Net
        if net := sexp.find("net"):
            pad.net_number = net.get_int(0) or 0
            pad.net_name = net.get_string(1) or ""

        # Drill
        if drill := sexp.find("drill"):
            pad.drill = drill.get_float(0) or 0.0

        # UUID
        if uuid := sexp.find("uuid"):
            pad.uuid = uuid.get_string(0) or ""

        return pad


@dataclass
class FootprintText:
    """Text element within a footprint (fp_text).

    Used for reference designators, values, and user text on footprints.
    Contains font information for silkscreen validation.
    """

    text_type: str  # reference, value, user
    text: str
    position: tuple[float, float]
    layer: str
    font_size: tuple[float, float]  # (width, height) in mm
    font_thickness: float  # stroke thickness in mm
    uuid: str = ""
    hidden: bool = False

    @classmethod
    def from_sexp(cls, sexp: SExp) -> FootprintText:
        """Parse footprint text from S-expression."""
        text_type = sexp.get_string(0) or ""
        text = sexp.get_string(1) or ""

        fp_text = cls(
            text_type=text_type,
            text=text,
            position=(0.0, 0.0),
            layer="",
            font_size=(1.0, 1.0),
            font_thickness=0.15,
        )

        # Position
        if at := sexp.find("at"):
            x = at.get_float(0) or 0.0
            y = at.get_float(1) or 0.0
            fp_text.position = (x, y)

        # Layer
        if layer := sexp.find("layer"):
            fp_text.layer = layer.get_string(0) or ""

        # UUID
        if uuid := sexp.find("uuid"):
            fp_text.uuid = uuid.get_string(0) or ""

        # Effects (font size and thickness)
        if effects := sexp.find("effects"):
            if effects.find("hide"):
                fp_text.hidden = True
            if font := effects.find("font"):
                if size := font.find("size"):
                    w = size.get_float(0) or 1.0
                    h = size.get_float(1) or w
                    fp_text.font_size = (w, h)
                if thickness := font.find("thickness"):
                    fp_text.font_thickness = thickness.get_float(0) or 0.15

        return fp_text

    @classmethod
    def _from_property_sexp(cls, sexp: SExp, text_type: str) -> FootprintText:
        """Parse footprint text from property S-expression (KiCad 8+ format).

        Property nodes have a different structure than fp_text nodes:
        (property "Reference" "U1" (at 0 -4) (layer "F.SilkS") (effects ...))
        """
        text = sexp.get_string(1) or ""

        fp_text = cls(
            text_type=text_type,
            text=text,
            position=(0.0, 0.0),
            layer="",
            font_size=(1.0, 1.0),
            font_thickness=0.15,
        )

        # Position
        if at := sexp.find("at"):
            x = at.get_float(0) or 0.0
            y = at.get_float(1) or 0.0
            fp_text.position = (x, y)

        # Layer
        if layer := sexp.find("layer"):
            fp_text.layer = layer.get_string(0) or ""

        # UUID
        if uuid := sexp.find("uuid"):
            fp_text.uuid = uuid.get_string(0) or ""

        # Hidden check - property format uses (hide yes) directly on the property
        if hide := sexp.find("hide"):
            hide_val = hide.get_string(0)
            fp_text.hidden = hide_val == "yes"

        # Effects (font size and thickness)
        if effects := sexp.find("effects"):
            if effects.find("hide"):
                fp_text.hidden = True
            if font := effects.find("font"):
                if size := font.find("size"):
                    w = size.get_float(0) or 1.0
                    h = size.get_float(1) or w
                    fp_text.font_size = (w, h)
                if thickness := font.find("thickness"):
                    fp_text.font_thickness = thickness.get_float(0) or 0.15

        return fp_text

    @property
    def font_height(self) -> float:
        """Font height in mm (used for minimum text height checks)."""
        return self.font_size[1]


@dataclass
class FootprintGraphic:
    """Graphic element within a footprint (fp_line, fp_rect, fp_circle, fp_arc).

    Used for silkscreen outlines and markings on footprints.
    """

    graphic_type: str  # line, rect, circle, arc
    layer: str
    stroke_width: float  # in mm
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)
    center: tuple[float, float] | None = None
    radius: float | None = None
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp, graphic_type: str) -> FootprintGraphic:
        """Parse footprint graphic from S-expression."""
        graphic = cls(
            graphic_type=graphic_type,
            layer="",
            stroke_width=0.0,
        )

        # Layer
        if layer := sexp.find("layer"):
            graphic.layer = layer.get_string(0) or ""

        # Stroke width
        if stroke := sexp.find("stroke"):
            if width := stroke.find("width"):
                graphic.stroke_width = width.get_float(0) or 0.0

        # Start/end points (for line, rect)
        if start := sexp.find("start"):
            graphic.start = (start.get_float(0) or 0.0, start.get_float(1) or 0.0)
        if end := sexp.find("end"):
            graphic.end = (end.get_float(0) or 0.0, end.get_float(1) or 0.0)

        # Center/radius (for circle)
        if center := sexp.find("center"):
            graphic.center = (center.get_float(0) or 0.0, center.get_float(1) or 0.0)
        if end := sexp.find("end"):
            # For circles, end is a point on the circumference
            pass

        # UUID
        if uuid := sexp.find("uuid"):
            graphic.uuid = uuid.get_string(0) or ""

        return graphic


@dataclass
class GraphicText:
    """Board-level text element (gr_text).

    Used for board markings, labels, and silkscreen text not tied to footprints.
    """

    text: str
    position: tuple[float, float]
    layer: str
    font_size: tuple[float, float]  # (width, height) in mm
    font_thickness: float  # stroke thickness in mm
    uuid: str = ""
    hidden: bool = False

    @classmethod
    def from_sexp(cls, sexp: SExp) -> GraphicText:
        """Parse graphic text from S-expression."""
        text = sexp.get_string(0) or ""

        gr_text = cls(
            text=text,
            position=(0.0, 0.0),
            layer="",
            font_size=(1.0, 1.0),
            font_thickness=0.15,
        )

        # Position
        if at := sexp.find("at"):
            gr_text.position = (at.get_float(0) or 0.0, at.get_float(1) or 0.0)

        # Layer
        if layer := sexp.find("layer"):
            gr_text.layer = layer.get_string(0) or ""

        # UUID
        if uuid := sexp.find("uuid"):
            gr_text.uuid = uuid.get_string(0) or ""

        # Effects (font size and thickness)
        if effects := sexp.find("effects"):
            if effects.find("hide"):
                gr_text.hidden = True
            if font := effects.find("font"):
                if size := font.find("size"):
                    w = size.get_float(0) or 1.0
                    h = size.get_float(1) or w
                    gr_text.font_size = (w, h)
                if thickness := font.find("thickness"):
                    gr_text.font_thickness = thickness.get_float(0) or 0.15

        return gr_text

    @property
    def font_height(self) -> float:
        """Font height in mm (used for minimum text height checks)."""
        return self.font_size[1]


@dataclass
class BoardGraphic:
    """Board-level graphic element (gr_line, gr_rect, gr_circle, gr_arc).

    Used for board outlines, silkscreen graphics, and other board-level drawings.
    """

    graphic_type: str  # line, rect, circle, arc
    layer: str
    stroke_width: float  # in mm
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)
    center: tuple[float, float] | None = None
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp, graphic_type: str) -> BoardGraphic:
        """Parse board graphic from S-expression."""
        graphic = cls(
            graphic_type=graphic_type,
            layer="",
            stroke_width=0.0,
        )

        # Layer
        if layer := sexp.find("layer"):
            graphic.layer = layer.get_string(0) or ""

        # Stroke width
        if stroke := sexp.find("stroke"):
            if width := stroke.find("width"):
                graphic.stroke_width = width.get_float(0) or 0.0

        # Start/end points
        if start := sexp.find("start"):
            graphic.start = (start.get_float(0) or 0.0, start.get_float(1) or 0.0)
        if end := sexp.find("end"):
            graphic.end = (end.get_float(0) or 0.0, end.get_float(1) or 0.0)

        # Center (for circle/arc)
        if center := sexp.find("center"):
            graphic.center = (center.get_float(0) or 0.0, center.get_float(1) or 0.0)

        # UUID
        if uuid := sexp.find("uuid"):
            graphic.uuid = uuid.get_string(0) or ""

        return graphic


@dataclass
class Footprint:
    """PCB component footprint."""

    name: str
    layer: str
    position: tuple[float, float]
    rotation: float
    reference: str
    value: str
    pads: list[Pad] = field(default_factory=list)
    texts: list[FootprintText] = field(default_factory=list)
    graphics: list[FootprintGraphic] = field(default_factory=list)
    uuid: str = ""
    description: str = ""
    tags: str = ""
    attr: str = ""  # smd, through_hole

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Footprint:
        """Parse footprint from S-expression."""
        name = sexp.get_string(0) or ""

        fp = cls(
            name=name,
            layer="F.Cu",
            position=(0.0, 0.0),
            rotation=0.0,
            reference="",
            value="",
            pads=[],
            texts=[],
            graphics=[],
        )

        # Layer
        if layer := sexp.find("layer"):
            fp.layer = layer.get_string(0) or "F.Cu"

        # Position
        if at := sexp.find("at"):
            x = at.get_float(0) or 0.0
            y = at.get_float(1) or 0.0
            rot = at.get_float(2) or 0.0
            fp.position = (x, y)
            fp.rotation = rot

        # UUID
        if uuid := sexp.find("uuid"):
            fp.uuid = uuid.get_string(0) or ""

        # Description and tags
        if descr := sexp.find("descr"):
            fp.description = descr.get_string(0) or ""
        if tags := sexp.find("tags"):
            fp.tags = tags.get_string(0) or ""
        if attr := sexp.find("attr"):
            fp.attr = attr.get_string(0) or ""

        # Reference and value from fp_text (KiCad 7 format)
        for fp_text_sexp in sexp.find_all("fp_text"):
            fp_text = FootprintText.from_sexp(fp_text_sexp)
            fp.texts.append(fp_text)
            # Also set reference/value for convenience
            if fp_text.text_type == "reference":
                fp.reference = fp_text.text
            elif fp_text.text_type == "value":
                fp.value = fp_text.text

        # Reference and value from property (KiCad 8+ format)
        for prop in sexp.find_all("property"):
            prop_name = prop.get_string(0)
            prop_value = prop.get_string(1) or ""
            if prop_name == "Reference":
                fp.reference = prop_value
                # Also create FootprintText for validation
                fp_text = FootprintText._from_property_sexp(prop, "reference")
                fp.texts.append(fp_text)
            elif prop_name == "Value":
                fp.value = prop_value
                # Also create FootprintText for validation
                fp_text = FootprintText._from_property_sexp(prop, "value")
                fp.texts.append(fp_text)

        # Pads
        for pad_sexp in sexp.find_all("pad"):
            pad = Pad.from_sexp(pad_sexp)
            if pad:
                fp.pads.append(pad)

        # Graphics (fp_line, fp_rect, fp_circle, fp_arc)
        for graphic_type in ("line", "rect", "circle", "arc"):
            for graphic_sexp in sexp.find_all(f"fp_{graphic_type}"):
                graphic = FootprintGraphic.from_sexp(graphic_sexp, graphic_type)
                fp.graphics.append(graphic)

        return fp


@dataclass
class Segment:
    """PCB trace segment."""

    start: tuple[float, float]
    end: tuple[float, float]
    width: float
    layer: str
    net_number: int
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Segment:
        """Parse segment from S-expression."""
        seg = cls(
            start=(0.0, 0.0),
            end=(0.0, 0.0),
            width=0.0,
            layer="",
            net_number=0,
        )

        if start := sexp.find("start"):
            seg.start = (start.get_float(0) or 0.0, start.get_float(1) or 0.0)
        if end := sexp.find("end"):
            seg.end = (end.get_float(0) or 0.0, end.get_float(1) or 0.0)
        if width := sexp.find("width"):
            seg.width = width.get_float(0) or 0.0
        if layer := sexp.find("layer"):
            seg.layer = layer.get_string(0) or ""
        if net := sexp.find("net"):
            seg.net_number = net.get_int(0) or 0
        if uuid := sexp.find("uuid"):
            seg.uuid = uuid.get_string(0) or ""

        return seg

    def to_sexp(self) -> SExp:
        """Convert segment to S-expression for serialization."""
        seg_sexp = SExp.list("segment")
        seg_sexp.append(SExp.list("start", self.start[0], self.start[1]))
        seg_sexp.append(SExp.list("end", self.end[0], self.end[1]))
        seg_sexp.append(SExp.list("width", self.width))
        seg_sexp.append(SExp.list("layer", self.layer))
        seg_sexp.append(SExp.list("net", self.net_number))
        if not self.uuid:
            self.uuid = str(uuid.uuid4())
        seg_sexp.append(SExp.list("uuid", self.uuid))
        return seg_sexp


@dataclass
class Via:
    """PCB via."""

    position: tuple[float, float]
    size: float
    drill: float
    layers: list[str]
    net_number: int
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Via:
        """Parse via from S-expression."""
        via = cls(
            position=(0.0, 0.0),
            size=0.0,
            drill=0.0,
            layers=[],
            net_number=0,
        )

        if at := sexp.find("at"):
            via.position = (at.get_float(0) or 0.0, at.get_float(1) or 0.0)
        if size := sexp.find("size"):
            via.size = size.get_float(0) or 0.0
        if drill := sexp.find("drill"):
            via.drill = drill.get_float(0) or 0.0
        if layers := sexp.find("layers"):
            via.layers = [
                layers.get_string(i) or ""
                for i in range(len(layers.values))
                if isinstance(layers.values[i], str)
            ]
        if net := sexp.find("net"):
            via.net_number = net.get_int(0) or 0
        if uuid := sexp.find("uuid"):
            via.uuid = uuid.get_string(0) or ""

        return via

    def to_sexp(self) -> SExp:
        """Convert via to S-expression for serialization."""
        via_sexp = SExp.list("via")
        via_sexp.append(SExp.list("at", self.position[0], self.position[1]))
        via_sexp.append(SExp.list("size", self.size))
        via_sexp.append(SExp.list("drill", self.drill))
        # Build layers list
        layers_sexp = SExp.list("layers", *self.layers)
        via_sexp.append(layers_sexp)
        via_sexp.append(SExp.list("net", self.net_number))
        if not self.uuid:
            self.uuid = str(uuid.uuid4())
        via_sexp.append(SExp.list("uuid", self.uuid))
        return via_sexp


@dataclass
class Zone:
    """PCB copper pour zone.

    Represents a copper fill zone with boundary polygon and thermal relief settings.
    Zones are used for ground planes, power planes, and copper pours.
    """

    net_number: int
    net_name: str
    layer: str
    uuid: str = ""
    name: str = ""
    # Boundary polygon points (x, y) in mm
    polygon: list[tuple[float, float]] = field(default_factory=list)
    # Filled polygon regions after DRC (may differ from boundary due to clearances)
    filled_polygons: list[list[tuple[float, float]]] = field(default_factory=list)
    # Zone fill priority (higher priority fills later, on top of lower priority)
    priority: int = 0
    # Minimum copper thickness in mm
    min_thickness: float = 0.2
    # Clearance to pads/traces of other nets in mm
    clearance: float = 0.2
    # Thermal relief gap (antipad) in mm
    thermal_gap: float = 0.3
    # Thermal relief spoke (bridge) width in mm
    thermal_bridge_width: float = 0.3
    # Pad connection type: "thermal_reliefs", "solid", "none"
    connect_pads: str = "thermal_reliefs"
    # Fill type: "solid" or "hatch"
    fill_type: str = "solid"
    # Whether zone is filled (has copper)
    is_filled: bool = False

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Zone:
        """Parse zone from S-expression.

        Parses KiCad zone definitions including:
        - Net assignment (net, net_name)
        - Layer and name
        - Boundary polygon points
        - Filled polygon regions (actual copper after DRC)
        - Thermal relief parameters (gap, bridge width)
        - Connection type (thermal, solid, none)
        - Priority and minimum thickness
        """
        zone = cls(
            net_number=0,
            net_name="",
            layer="",
        )

        # Basic properties
        if net := sexp.find("net"):
            zone.net_number = net.get_int(0) or 0
        if net_name := sexp.find("net_name"):
            zone.net_name = net_name.get_string(0) or ""
        if layer := sexp.find("layer"):
            zone.layer = layer.get_string(0) or ""
        if uuid := sexp.find("uuid"):
            zone.uuid = uuid.get_string(0) or ""
        if name := sexp.find("name"):
            zone.name = name.get_string(0) or ""

        # Priority
        if priority := sexp.find("priority"):
            zone.priority = priority.get_int(0) or 0

        # Minimum thickness
        if min_thickness := sexp.find("min_thickness"):
            zone.min_thickness = min_thickness.get_float(0) or 0.2

        # Connect pads - can be (connect_pads yes) or (connect_pads (clearance X))
        # or (connect_pads thru_hole_only (clearance X)) etc.
        if connect_pads := sexp.find("connect_pads"):
            # Check for connection type keyword
            first_val = connect_pads.get_string(0)
            if first_val == "no":
                zone.connect_pads = "none"
            elif first_val == "yes":
                zone.connect_pads = "solid"
            elif first_val == "thru_hole_only":
                zone.connect_pads = "thermal_reliefs"
            else:
                # Default thermal reliefs if just clearance specified
                zone.connect_pads = "thermal_reliefs"

            # Extract clearance if present
            if clearance := connect_pads.find("clearance"):
                zone.clearance = clearance.get_float(0) or 0.2

        # Fill settings - (fill yes/no (thermal_gap X) (thermal_bridge_width X))
        if fill := sexp.find("fill"):
            first_val = fill.get_string(0)
            zone.is_filled = first_val == "yes"

            if thermal_gap := fill.find("thermal_gap"):
                zone.thermal_gap = thermal_gap.get_float(0) or 0.3
            if thermal_bridge := fill.find("thermal_bridge_width"):
                zone.thermal_bridge_width = thermal_bridge.get_float(0) or 0.3
            if mode := fill.find("mode"):
                fill_mode = mode.get_string(0)
                if fill_mode == "hatch":
                    zone.fill_type = "hatch"

        # Parse boundary polygon - (polygon (pts (xy X Y) ...))
        if polygon := sexp.find("polygon"):
            zone.polygon = cls._parse_polygon_pts(polygon)

        # Parse filled polygons - (filled_polygon (layer X) (pts (xy X Y) ...))
        for filled_poly in sexp.find_all("filled_polygon"):
            points = cls._parse_polygon_pts(filled_poly)
            if points:
                zone.filled_polygons.append(points)

        return zone

    @staticmethod
    def _parse_polygon_pts(polygon_sexp: SExp) -> list[tuple[float, float]]:
        """Parse polygon points from (pts (xy X Y) ...) structure."""
        points: list[tuple[float, float]] = []

        if pts := polygon_sexp.find("pts"):
            for xy in pts.find_all("xy"):
                x = xy.get_float(0) or 0.0
                y = xy.get_float(1) or 0.0
                points.append((x, y))

        return points


@dataclass
class GraphicLine:
    """PCB graphic line element (gr_line).

    Used for board outlines on Edge.Cuts layer and other graphic elements.
    """

    start: tuple[float, float]
    end: tuple[float, float]
    layer: str
    width: float = 0.1
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> GraphicLine:
        """Parse graphic line from S-expression."""
        line = cls(
            start=(0.0, 0.0),
            end=(0.0, 0.0),
            layer="",
        )

        if start := sexp.find("start"):
            line.start = (start.get_float(0) or 0.0, start.get_float(1) or 0.0)
        if end := sexp.find("end"):
            line.end = (end.get_float(0) or 0.0, end.get_float(1) or 0.0)
        if layer := sexp.find("layer"):
            line.layer = layer.get_string(0) or ""
        if width := sexp.find("width"):
            line.width = width.get_float(0) or 0.1
        if stroke := sexp.find("stroke"):
            # KiCad 8+ uses stroke instead of width
            if stroke_width := stroke.find("width"):
                line.width = stroke_width.get_float(0) or 0.1
        if uuid := sexp.find("uuid"):
            line.uuid = uuid.get_string(0) or ""

        return line


@dataclass
class GraphicArc:
    """PCB graphic arc element (gr_arc).

    Used for curved board outlines on Edge.Cuts layer.
    """

    start: tuple[float, float]
    mid: tuple[float, float]
    end: tuple[float, float]
    layer: str
    width: float = 0.1
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> GraphicArc:
        """Parse graphic arc from S-expression."""
        arc = cls(
            start=(0.0, 0.0),
            mid=(0.0, 0.0),
            end=(0.0, 0.0),
            layer="",
        )

        if start := sexp.find("start"):
            arc.start = (start.get_float(0) or 0.0, start.get_float(1) or 0.0)
        if mid := sexp.find("mid"):
            arc.mid = (mid.get_float(0) or 0.0, mid.get_float(1) or 0.0)
        if end := sexp.find("end"):
            arc.end = (end.get_float(0) or 0.0, end.get_float(1) or 0.0)
        if layer := sexp.find("layer"):
            arc.layer = layer.get_string(0) or ""
        if width := sexp.find("width"):
            arc.width = width.get_float(0) or 0.1
        if stroke := sexp.find("stroke"):
            # KiCad 8+ uses stroke instead of width
            if stroke_width := stroke.find("width"):
                arc.width = stroke_width.get_float(0) or 0.1
        if uuid := sexp.find("uuid"):
            arc.uuid = uuid.get_string(0) or ""

        return arc


@dataclass
class StackupLayer:
    """Stackup layer definition."""

    name: str
    type: str  # copper, prepreg, core, solder mask, silk screen
    thickness: float = 0.0
    material: str = ""
    epsilon_r: float = 0.0


@dataclass
class Setup:
    """PCB setup/design rules."""

    stackup: list[StackupLayer] = field(default_factory=list)
    pad_to_mask_clearance: float = 0.0
    copper_finish: str = ""


# Paper sizes in mm (width, height) - KiCad uses landscape orientation
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
    "A": (279.4, 215.9),  # US Letter
    "B": (431.8, 279.4),  # US Tabloid
    "C": (558.8, 431.8),
    "D": (863.6, 558.8),
    "E": (1117.6, 863.6),
}


class PCB:
    """KiCad PCB document.

    Parses .kicad_pcb files and provides access to:
    - Board outline and dimensions
    - Layers and stackup
    - Nets
    - Footprints (components)
    - Traces (segments)
    - Vias
    - Zones (copper pours)
    """

    def __init__(self, sexp: SExp, path: str | Path | None = None):
        """Initialize from parsed S-expression data.

        Args:
            sexp: Parsed S-expression data
            path: Optional path to the PCB file (used for export operations)
        """
        self._sexp = sexp
        self._path: Path | None = Path(path) if path else None
        self._layers: dict[int, Layer] = {}
        self._nets: dict[int, Net] = {}
        self._footprints: list[Footprint] = []
        self._segments: list[Segment] = []
        self._vias: list[Via] = []
        self._zones: list[Zone] = []
        self._graphic_lines: list[GraphicLine] = []
        self._graphic_arcs: list[GraphicArc] = []
        self._texts: list[GraphicText] = []
        self._graphics: list[BoardGraphic] = []
        self._setup: Setup | None = None
        self._title_block: dict[str, str] = {}
        self._parse()

    @classmethod
    def load(cls, path: str | Path) -> PCB:
        """Load PCB from file.

        Args:
            path: Path to .kicad_pcb file

        Returns:
            PCB instance with path stored for export operations
        """
        path = Path(path)
        sexp = load_pcb(str(path))
        return cls(sexp, path)

    @classmethod
    def create(
        cls,
        width: float = 100.0,
        height: float = 100.0,
        layers: int = 2,
        title: str = "",
        revision: str = "1.0",
        company: str = "",
        board_date: str | None = None,
        paper: str = "A4",
        center: bool = True,
    ) -> PCB:
        """Create a new blank PCB from scratch.

        This creates a minimal but valid KiCad PCB file with:
        - Board outline on Edge.Cuts layer (centered on drawing sheet by default)
        - Layer definitions (2 or 4 copper layers)
        - Basic design rules
        - Title block information

        Args:
            width: Board width in mm (default 100.0)
            height: Board height in mm (default 100.0)
            layers: Number of copper layers (2 or 4, default 2)
            title: Board title for title block
            revision: Board revision (default "1.0")
            company: Company name for title block
            board_date: Date string (default: today's date in YYYY-MM-DD format)
            paper: Paper size for drawing sheet (default "A4"). Supported sizes:
                   A4, A3, A2, A1, A0, A (US Letter), B, C, D, E
            center: If True, center the board on the drawing sheet (default True).
                    If False, place the board at origin (0, 0).

        Returns:
            A new PCB instance ready for adding footprints and traces.

        Raises:
            ValueError: If layers is not 2 or 4
            ValueError: If paper size is not recognized

        Example:
            >>> pcb = PCB.create(width=160, height=100, layers=4, title="My Board")
            >>> pcb.save("my_board.kicad_pcb")
        """
        if layers not in (2, 4):
            raise ValueError(f"Layers must be 2 or 4, got {layers}")

        if paper not in PAPER_SIZES:
            raise ValueError(
                f"Unknown paper size '{paper}'. "
                f"Supported sizes: {', '.join(sorted(PAPER_SIZES.keys()))}"
            )

        if board_date is None:
            board_date = date.today().isoformat()

        # Calculate board origin based on centering preference
        if center:
            paper_width, paper_height = PAPER_SIZES[paper]
            origin_x = (paper_width - width) / 2
            origin_y = (paper_height - height) / 2
        else:
            origin_x, origin_y = 0.0, 0.0

        sexp = cls._build_blank_pcb_sexp(
            width=width,
            height=height,
            layers=layers,
            title=title,
            revision=revision,
            company=company,
            board_date=board_date,
            paper=paper,
            origin_x=origin_x,
            origin_y=origin_y,
        )
        return cls(sexp)

    @staticmethod
    def _build_blank_pcb_sexp(
        width: float,
        height: float,
        layers: int,
        title: str,
        revision: str,
        company: str,
        board_date: str,
        paper: str,
        origin_x: float,
        origin_y: float,
    ) -> SExp:
        """Build the S-expression for a blank PCB."""
        pcb = SExp.list("kicad_pcb")

        # Version and generator info
        pcb.append(SExp.list("version", 20240108))
        pcb.append(SExp.list("generator", "kicad_tools"))
        pcb.append(SExp.list("generator_version", "8.0"))

        # General settings
        pcb.append(
            SExp.list(
                "general",
                SExp.list("thickness", 1.6),
                SExp.list("legacy_teardrops", "no"),
            )
        )

        # Paper size
        pcb.append(SExp.list("paper", paper))

        # Title block
        pcb.append(
            SExp.list(
                "title_block",
                SExp.list("title", title),
                SExp.list("date", board_date),
                SExp.list("rev", revision),
                SExp.list("company", company),
            )
        )

        # Layers
        pcb.append(PCB._build_layers_sexp(layers))

        # Setup with design rules
        pcb.append(PCB._build_setup_sexp(layers))

        # Empty net (required)
        pcb.append(SExp.list("net", 0, ""))

        # Board outline on Edge.Cuts
        pcb.append(PCB._build_board_outline_sexp(width, height, origin_x, origin_y))

        return pcb

    @staticmethod
    def _build_layers_sexp(num_layers: int) -> SExp:
        """Build the layers definition S-expression."""
        layers_node = SExp.list("layers")

        # Copper layers
        layers_node.append(SExp.list("0", "F.Cu", "signal"))
        if num_layers == 4:
            layers_node.append(SExp.list("1", "In1.Cu", "signal"))
            layers_node.append(SExp.list("2", "In2.Cu", "signal"))
        layers_node.append(SExp.list("31", "B.Cu", "signal"))

        # Technical layers (always present)
        layers_node.append(SExp.list("32", "B.Adhes", "user", "B.Adhesive"))
        layers_node.append(SExp.list("33", "F.Adhes", "user", "F.Adhesive"))
        layers_node.append(SExp.list("34", "B.Paste", "user"))
        layers_node.append(SExp.list("35", "F.Paste", "user"))
        layers_node.append(SExp.list("36", "B.SilkS", "user", "B.Silkscreen"))
        layers_node.append(SExp.list("37", "F.SilkS", "user", "F.Silkscreen"))
        layers_node.append(SExp.list("38", "B.Mask", "user"))
        layers_node.append(SExp.list("39", "F.Mask", "user"))
        layers_node.append(SExp.list("40", "Dwgs.User", "user", "User.Drawings"))
        layers_node.append(SExp.list("44", "Edge.Cuts", "user"))
        layers_node.append(SExp.list("46", "B.CrtYd", "user", "B.Courtyard"))
        layers_node.append(SExp.list("47", "F.CrtYd", "user", "F.Courtyard"))
        layers_node.append(SExp.list("48", "B.Fab", "user"))
        layers_node.append(SExp.list("49", "F.Fab", "user"))

        return layers_node

    @staticmethod
    def _build_setup_sexp(num_layers: int) -> SExp:
        """Build the setup/design rules S-expression."""
        setup = SExp.list("setup")

        # Stackup for multi-layer boards
        if num_layers == 4:
            stackup = SExp.list("stackup")
            stackup.append(SExp.list("layer", "F.SilkS", SExp.list("type", "Top Silk Screen")))
            stackup.append(SExp.list("layer", "F.Paste", SExp.list("type", "Top Solder Paste")))
            stackup.append(
                SExp.list(
                    "layer",
                    "F.Mask",
                    SExp.list("type", "Top Solder Mask"),
                    SExp.list("thickness", 0.01),
                )
            )
            stackup.append(
                SExp.list(
                    "layer", "F.Cu", SExp.list("type", "copper"), SExp.list("thickness", 0.035)
                )
            )
            stackup.append(
                SExp.list(
                    "layer",
                    "dielectric 1",
                    SExp.list("type", "prepreg"),
                    SExp.list("thickness", 0.2),
                    SExp.list("material", "FR4"),
                    SExp.list("epsilon_r", 4.5),
                    SExp.list("loss_tangent", 0.02),
                )
            )
            stackup.append(
                SExp.list(
                    "layer", "In1.Cu", SExp.list("type", "copper"), SExp.list("thickness", 0.035)
                )
            )
            stackup.append(
                SExp.list(
                    "layer",
                    "dielectric 2",
                    SExp.list("type", "core"),
                    SExp.list("thickness", 1.0),
                    SExp.list("material", "FR4"),
                    SExp.list("epsilon_r", 4.5),
                    SExp.list("loss_tangent", 0.02),
                )
            )
            stackup.append(
                SExp.list(
                    "layer", "In2.Cu", SExp.list("type", "copper"), SExp.list("thickness", 0.035)
                )
            )
            stackup.append(
                SExp.list(
                    "layer",
                    "dielectric 3",
                    SExp.list("type", "prepreg"),
                    SExp.list("thickness", 0.2),
                    SExp.list("material", "FR4"),
                    SExp.list("epsilon_r", 4.5),
                    SExp.list("loss_tangent", 0.02),
                )
            )
            stackup.append(
                SExp.list(
                    "layer", "B.Cu", SExp.list("type", "copper"), SExp.list("thickness", 0.035)
                )
            )
            stackup.append(
                SExp.list(
                    "layer",
                    "B.Mask",
                    SExp.list("type", "Bottom Solder Mask"),
                    SExp.list("thickness", 0.01),
                )
            )
            stackup.append(SExp.list("layer", "B.Paste", SExp.list("type", "Bottom Solder Paste")))
            stackup.append(SExp.list("layer", "B.SilkS", SExp.list("type", "Bottom Silk Screen")))
            stackup.append(SExp.list("copper_finish", "ENIG"))
            stackup.append(SExp.list("dielectric_constraints", "no"))
            setup.append(stackup)

        # Basic design rules
        setup.append(SExp.list("pad_to_mask_clearance", 0))

        return setup

    @staticmethod
    def _build_board_outline_sexp(
        width: float, height: float, origin_x: float, origin_y: float
    ) -> SExp:
        """Build a rectangular board outline on Edge.Cuts layer."""
        return SExp.list(
            "gr_rect",
            SExp.list("start", origin_x, origin_y),
            SExp.list("end", origin_x + width, origin_y + height),
            SExp.list("stroke", SExp.list("width", 0.1), SExp.list("type", "default")),
            SExp.list("fill", "none"),
            SExp.list("layer", "Edge.Cuts"),
            SExp.list("uuid", str(uuid.uuid4())),
        )

    def _parse(self):
        """Parse the PCB data structure."""
        for child in self._sexp.iter_children():
            tag = child.tag

            if tag == "layers":
                self._parse_layers(child)
            elif tag == "net":
                self._parse_net(child)
            elif tag == "footprint":
                fp = Footprint.from_sexp(child)
                self._footprints.append(fp)
            elif tag == "segment":
                seg = Segment.from_sexp(child)
                self._segments.append(seg)
            elif tag == "via":
                via = Via.from_sexp(child)
                self._vias.append(via)
            elif tag == "zone":
                zone = Zone.from_sexp(child)
                self._zones.append(zone)
            elif tag == "gr_line":
                line = GraphicLine.from_sexp(child)
                self._graphic_lines.append(line)
            elif tag == "gr_arc":
                arc = GraphicArc.from_sexp(child)
                self._graphic_arcs.append(arc)
            elif tag == "setup":
                self._parse_setup(child)
            elif tag == "title_block":
                self._parse_title_block(child)
            elif tag == "gr_text":
                text = GraphicText.from_sexp(child)
                self._texts.append(text)
            elif tag in ("gr_line", "gr_rect", "gr_circle", "gr_arc"):
                graphic_type = tag[3:]  # Remove "gr_" prefix
                graphic = BoardGraphic.from_sexp(child, graphic_type)
                self._graphics.append(graphic)

    def _parse_layers(self, sexp: SExp):
        """Parse layer definitions."""
        for child in sexp.iter_children():
            # Layers are stored as (N "name" type)
            if len(child.values) >= 1:
                # The tag is the layer number as string
                try:
                    number = int(child.tag)
                except ValueError:
                    continue
                name = child.get_string(0) or ""
                layer_type = child.get_string(1) or "user"
                self._layers[number] = Layer(number, name, layer_type)

    def _parse_net(self, sexp: SExp):
        """Parse net definition."""
        net_num = sexp.get_int(0) or 0
        net_name = sexp.get_string(1) or ""
        self._nets[net_num] = Net(net_num, net_name)

    def _parse_setup(self, sexp: SExp):
        """Parse setup/design rules."""
        setup = Setup()

        if stackup := sexp.find("stackup"):
            setup.stackup = self._parse_stackup(stackup)

        if clearance := sexp.find("pad_to_mask_clearance"):
            setup.pad_to_mask_clearance = clearance.get_float(0) or 0.0

        self._setup = setup

    def _parse_stackup(self, sexp: SExp) -> list[StackupLayer]:
        """Parse stackup definition."""
        layers = []

        for child in sexp.iter_children():
            if child.tag == "layer":
                layer = StackupLayer(
                    name=child.get_string(0) or "",
                    type="",
                )

                if type_node := child.find("type"):
                    layer.type = type_node.get_string(0) or ""
                if thick := child.find("thickness"):
                    layer.thickness = thick.get_float(0) or 0.0
                if mat := child.find("material"):
                    layer.material = mat.get_string(0) or ""
                if eps := child.find("epsilon_r"):
                    layer.epsilon_r = eps.get_float(0) or 0.0

                layers.append(layer)
            elif child.tag == "copper_finish":
                pass  # Store globally if needed

        return layers

    def _parse_title_block(self, sexp: SExp):
        """Parse title block."""
        for child in sexp.iter_children():
            value = child.get_string(0) or ""
            self._title_block[child.tag] = value

    # Public accessors

    @property
    def title(self) -> str:
        """Board title."""
        return self._title_block.get("title", "")

    @property
    def revision(self) -> str:
        """Board revision."""
        return self._title_block.get("rev", "")

    @property
    def date(self) -> str:
        """Board date."""
        return self._title_block.get("date", "")

    @property
    def layers(self) -> dict[int, Layer]:
        """Layer definitions."""
        return self._layers

    @property
    def copper_layers(self) -> list[Layer]:
        """Copper layers only."""
        return [layer for layer in self._layers.values() if layer.type in ("signal", "power")]

    @property
    def nets(self) -> dict[int, Net]:
        """Net definitions."""
        return self._nets

    def get_net(self, number: int) -> Net | None:
        """Get net by number."""
        return self._nets.get(number)

    def get_net_by_name(self, name: str) -> Net | None:
        """Get net by name."""
        for net in self._nets.values():
            if net.name == name:
                return net
        return None

    @property
    def footprints(self) -> FootprintList:
        """All footprints.

        Returns a FootprintList which extends list with query methods:
            pcb.footprints.by_reference("U1")
            pcb.footprints.filter(layer="F.Cu")
            pcb.footprints.query().smd().on_top().all()

        Backward compatible - all list operations still work.
        """
        # Import here to avoid circular import
        from ..query.footprints import FootprintList

        return FootprintList(self._footprints)

    def get_footprint(self, reference: str) -> Footprint | None:
        """Get footprint by reference designator."""
        for fp in self._footprints:
            if fp.reference == reference:
                return fp
        return None

    def footprints_on_layer(self, layer: str) -> Iterator[Footprint]:
        """Get footprints on a specific layer."""
        for fp in self._footprints:
            if fp.layer == layer:
                yield fp

    @property
    def segments(self) -> list[Segment]:
        """All trace segments."""
        return self._segments

    def segments_on_layer(self, layer: str) -> Iterator[Segment]:
        """Get segments on a specific layer."""
        for seg in self._segments:
            if seg.layer == layer:
                yield seg

    def segments_in_net(self, net_number: int) -> Iterator[Segment]:
        """Get segments in a specific net."""
        for seg in self._segments:
            if seg.net_number == net_number:
                yield seg

    @property
    def vias(self) -> list[Via]:
        """All vias."""
        return self._vias

    def vias_in_net(self, net_number: int) -> Iterator[Via]:
        """Get vias in a specific net."""
        for via in self._vias:
            if via.net_number == net_number:
                yield via

    @property
    def zones(self) -> list[Zone]:
        """All zones (copper pours)."""
        return self._zones

    @property
    def graphic_lines(self) -> list[GraphicLine]:
        """All graphic lines."""
        return self._graphic_lines

    @property
    def graphic_arcs(self) -> list[GraphicArc]:
        """All graphic arcs."""
        return self._graphic_arcs

    @property
    def texts(self) -> list[GraphicText]:
        """All board-level text elements (gr_text)."""
        return self._texts

    def texts_on_layer(self, layer: str) -> Iterator[GraphicText]:
        """Get text elements on a specific layer."""
        for text in self._texts:
            if text.layer == layer:
                yield text

    @property
    def graphics(self) -> list[BoardGraphic]:
        """All board-level graphic elements (gr_line, gr_rect, etc.)."""
        return self._graphics

    @property
    def graphic_items(self) -> Iterator[GraphicLine | GraphicArc | BoardGraphic]:
        """All board-level graphic items (lines, arcs, rects, circles).

        Yields all graphic elements from Edge.Cuts and other layers.
        Used for board outline calculations and layer analysis.
        """
        yield from self._graphic_lines
        yield from self._graphic_arcs
        yield from self._graphics

    def graphics_on_layer(self, layer: str) -> Iterator[BoardGraphic]:
        """Get graphic elements on a specific layer."""
        for graphic in self._graphics:
            if graphic.layer == layer:
                yield graphic

    def get_board_outline(self) -> list[tuple[float, float]]:
        """Extract board outline polygon from Edge.Cuts layer.

        Returns an ordered list of (x, y) points forming the board outline.
        Only includes line segments on the Edge.Cuts layer.
        Arc segments are approximated by their start and end points.

        Returns:
            List of (x, y) coordinate tuples in mm. Empty list if no outline found.
        """
        # Collect all Edge.Cuts segments
        edge_lines = [line for line in self._graphic_lines if line.layer == "Edge.Cuts"]
        edge_arcs = [arc for arc in self._graphic_arcs if arc.layer == "Edge.Cuts"]

        if not edge_lines and not edge_arcs:
            return []

        # Build a list of all line segments (including arc endpoints)
        segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

        for line in edge_lines:
            segments.append((line.start, line.end))

        for arc in edge_arcs:
            # For arcs, include start->mid and mid->end as approximation
            segments.append((arc.start, arc.mid))
            segments.append((arc.mid, arc.end))

        if not segments:
            return []

        # Build ordered polygon by connecting segments
        # Start with the first segment
        polygon: list[tuple[float, float]] = [segments[0][0], segments[0][1]]
        used = {0}

        # Keep finding the next connected segment
        while len(used) < len(segments):
            current_end = polygon[-1]
            found = False

            for i, (start, end) in enumerate(segments):
                if i in used:
                    continue

                # Check if this segment connects to current end
                if self._points_close(current_end, start):
                    polygon.append(end)
                    used.add(i)
                    found = True
                    break
                elif self._points_close(current_end, end):
                    polygon.append(start)
                    used.add(i)
                    found = True
                    break

            if not found:
                # No more connected segments found
                break

        return polygon

    @staticmethod
    def _points_close(
        p1: tuple[float, float], p2: tuple[float, float], tolerance: float = 0.001
    ) -> bool:
        """Check if two points are within tolerance distance."""
        dx = p1[0] - p2[0]
        dy = p1[1] - p2[1]
        return (dx * dx + dy * dy) < (tolerance * tolerance)

    def get_board_outline_segments(
        self,
    ) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        """Get board outline as a list of line segments.

        Returns all Edge.Cuts graphic elements as line segments.
        More useful for distance calculations than the polygon.

        Returns:
            List of ((x1, y1), (x2, y2)) tuples representing line segments.
        """
        segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

        for line in self._graphic_lines:
            if line.layer == "Edge.Cuts":
                segments.append((line.start, line.end))

        for arc in self._graphic_arcs:
            if arc.layer == "Edge.Cuts":
                # Approximate arc with two segments through midpoint
                segments.append((arc.start, arc.mid))
                segments.append((arc.mid, arc.end))

        return segments

    @property
    def setup(self) -> Setup | None:
        """Board setup/design rules."""
        return self._setup

    # Statistics

    @property
    def footprint_count(self) -> int:
        """Number of footprints."""
        return len(self._footprints)

    @property
    def segment_count(self) -> int:
        """Number of trace segments."""
        return len(self._segments)

    @property
    def via_count(self) -> int:
        """Number of vias."""
        return len(self._vias)

    @property
    def net_count(self) -> int:
        """Number of nets."""
        return len(self._nets)

    def total_trace_length(self, layer: str | None = None) -> float:
        """Calculate total trace length in mm."""
        import math

        total = 0.0
        for seg in self._segments:
            if layer is None or seg.layer == layer:
                dx = seg.end[0] - seg.start[0]
                dy = seg.end[1] - seg.start[1]
                total += math.sqrt(dx * dx + dy * dy)
        return total

    def summary(self) -> dict:
        """Get board summary statistics."""
        return {
            "title": self.title,
            "revision": self.revision,
            "copper_layers": len(self.copper_layers),
            "footprints": self.footprint_count,
            "nets": self.net_count,
            "segments": self.segment_count,
            "vias": self.via_count,
            "zones": len(self._zones),
            "trace_length_mm": round(self.total_trace_length(), 2),
        }

    # Modification methods

    def update_footprint_position(
        self,
        reference: str,
        x: float,
        y: float,
        rotation: float | None = None,
    ) -> bool:
        """
        Update a footprint's position in the underlying S-expression.

        Args:
            reference: Reference designator (e.g., "U1")
            x: New X position in mm
            y: New Y position in mm
            rotation: New rotation in degrees (optional)

        Returns:
            True if footprint was found and updated
        """
        # Find the footprint in the parsed data
        fp = self.get_footprint(reference)
        if not fp:
            return False

        # Update the parsed footprint object
        fp.position = (x, y)
        if rotation is not None:
            fp.rotation = rotation

        # Find and update the footprint in the SExp tree
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            # Check if this is the right footprint by looking at reference
            ref_value = None

            # KiCad 7 format: fp_text with type "reference"
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    ref_value = fp_text.get_string(1)
                    break

            # KiCad 8+ format: property with name "Reference"
            if not ref_value:
                for prop in child.find_all("property"):
                    if prop.get_string(0) == "Reference":
                        ref_value = prop.get_string(1)
                        break

            if ref_value != reference:
                continue

            # Found the footprint, update its 'at' node
            at_node = child.find("at")
            if at_node:
                at_node.set_value(0, x)
                at_node.set_value(1, y)
                if rotation is not None:
                    # Handle cases where rotation may or may not exist
                    if len(at_node.children) >= 3:
                        at_node.set_value(2, rotation)
                    elif rotation != 0.0:
                        # Use add() instead of values.append() since values
                        # is a read-only property that returns a new list
                        at_node.add(rotation)
            return True

        return False

    # Silkscreen management methods

    def set_reference_visibility(
        self,
        reference: str | None = None,
        *,
        visible: bool = True,
        pattern: str | None = None,
    ) -> int:
        """
        Set visibility of reference designators on silkscreen.

        Can target a specific reference, all references, or references matching
        a glob pattern.

        Args:
            reference: Specific reference designator (e.g., "U1"). If None,
                      applies to all footprints (or those matching pattern).
            visible: True to show, False to hide the reference designator.
            pattern: Glob pattern to match references (e.g., "C*" for all
                    capacitors, "U?" for single-digit ICs). Ignored if
                    reference is specified.

        Returns:
            Number of references updated.

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> # Hide all reference designators
            >>> pcb.set_reference_visibility(visible=False)
            >>> # Hide just capacitors
            >>> pcb.set_reference_visibility(pattern="C*", visible=False)
            >>> # Show specific reference
            >>> pcb.set_reference_visibility("U1", visible=True)
        """
        import fnmatch

        count = 0

        # Determine which references to update
        refs_to_update: set[str] = set()
        if reference is not None:
            refs_to_update.add(reference)
        elif pattern is not None:
            for fp in self._footprints:
                if fnmatch.fnmatch(fp.reference, pattern):
                    refs_to_update.add(fp.reference)
        else:
            # All footprints
            refs_to_update = {fp.reference for fp in self._footprints}

        # Update S-expression tree
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            # Get reference from this footprint
            fp_ref = self._get_footprint_reference(child)
            if fp_ref not in refs_to_update:
                continue

            # Update visibility in fp_text nodes (KiCad 7 format)
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    self._set_text_visibility(fp_text, visible)
                    count += 1

            # Update visibility in property nodes (KiCad 8+ format)
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Reference":
                    self._set_text_visibility(prop, visible)
                    count += 1

        # Update parsed footprint objects
        for fp in self._footprints:
            if fp.reference in refs_to_update:
                for text in fp.texts:
                    if text.text_type == "reference":
                        text.hidden = not visible

        return count

    def _get_footprint_reference(self, fp_sexp: SExp) -> str:
        """Extract reference designator from footprint S-expression."""
        # Try KiCad 7 format first (fp_text)
        for fp_text in fp_sexp.find_all("fp_text"):
            if fp_text.get_string(0) == "reference":
                return fp_text.get_string(1) or ""

        # Try KiCad 8+ format (property)
        for prop in fp_sexp.find_all("property"):
            if prop.get_string(0) == "Reference":
                return prop.get_string(1) or ""

        return ""

    def _set_text_visibility(self, text_sexp: SExp, visible: bool) -> None:
        """Set visibility on a text S-expression node."""
        effects = text_sexp.find("effects")
        if effects is None:
            # Create effects node if needed
            effects = SExp.list("effects")
            font = SExp.list("font")
            font.append(SExp.list("size", 1.0, 1.0))
            font.append(SExp.list("thickness", 0.15))
            effects.append(font)
            text_sexp.append(effects)

        # Find existing hide node
        hide_node = effects.find("hide")

        if visible:
            # Remove hide node if present
            if hide_node is not None:
                effects.remove(hide_node)
        else:
            # Add hide node if not present
            if hide_node is None:
                effects.append(SExp.list("hide", "yes"))

    def move_reference(
        self,
        reference: str,
        offset: tuple[float, float] = (0.0, 0.0),
        *,
        absolute: tuple[float, float] | None = None,
        layer: str | None = None,
    ) -> bool:
        """
        Move a reference designator's silkscreen text.

        Args:
            reference: Reference designator to move (e.g., "U1").
            offset: (dx, dy) offset from current position in mm.
                   Ignored if absolute is specified.
            absolute: Absolute (x, y) position in mm, relative to the
                     footprint origin. If specified, offset is ignored.
            layer: Optional new layer (e.g., "F.SilkS", "F.Fab").
                  If None, layer is unchanged.

        Returns:
            True if reference was found and updated.

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> # Move U1 reference up by 5mm
            >>> pcb.move_reference("U1", offset=(0, -5))
            >>> # Move to absolute position
            >>> pcb.move_reference("U1", absolute=(2.0, -3.0))
            >>> # Move to fab layer (hidden from manufacturing)
            >>> pcb.move_reference("U1", layer="F.Fab")
        """
        # Find footprint in S-expression tree
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            fp_ref = self._get_footprint_reference(child)
            if fp_ref != reference:
                continue

            # Found the footprint - update reference text
            updated = False

            # Update fp_text nodes (KiCad 7 format)
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    self._move_text_element(fp_text, offset, absolute, layer)
                    updated = True

            # Update property nodes (KiCad 8+ format)
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Reference":
                    self._move_text_element(prop, offset, absolute, layer)
                    updated = True

            # Update parsed footprint object
            if updated:
                for fp in self._footprints:
                    if fp.reference == reference:
                        for text in fp.texts:
                            if text.text_type == "reference":
                                if absolute is not None:
                                    text.position = absolute
                                else:
                                    text.position = (
                                        text.position[0] + offset[0],
                                        text.position[1] + offset[1],
                                    )
                                if layer is not None:
                                    text.layer = layer
                        break

            return updated

        return False

    def _move_text_element(
        self,
        text_sexp: SExp,
        offset: tuple[float, float],
        absolute: tuple[float, float] | None,
        layer: str | None,
    ) -> None:
        """Move a text element's position and optionally change its layer."""
        # Update position
        at_node = text_sexp.find("at")
        if at_node is None:
            # Create at node with default position
            at_node = SExp.list("at", 0.0, 0.0)
            text_sexp.append(at_node)

        if absolute is not None:
            at_node.set_value(0, absolute[0])
            at_node.set_value(1, absolute[1])
        else:
            current_x = at_node.get_float(0) or 0.0
            current_y = at_node.get_float(1) or 0.0
            at_node.set_value(0, current_x + offset[0])
            at_node.set_value(1, current_y + offset[1])

        # Update layer if specified
        if layer is not None:
            layer_node = text_sexp.find("layer")
            if layer_node is not None:
                layer_node.set_value(0, layer)
            else:
                text_sexp.append(SExp.list("layer", layer))

    def set_silkscreen_font(
        self,
        size: float | tuple[float, float] = 1.0,
        thickness: float = 0.15,
        *,
        pattern: str | None = None,
        text_types: tuple[str, ...] = ("reference",),
    ) -> int:
        """
        Set font size for silkscreen text on all footprints.

        Args:
            size: Font size in mm. Can be a single value (used for both
                 width and height) or a (width, height) tuple.
            thickness: Stroke thickness in mm.
            pattern: Glob pattern to match references (e.g., "C*" for all
                    capacitors). If None, applies to all footprints.
            text_types: Which text types to update. Default is ("reference",).
                       Can include "reference", "value", "user".

        Returns:
            Number of text elements updated.

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> # Set smaller font for all references
            >>> pcb.set_silkscreen_font(size=0.8, thickness=0.15)
            >>> # Set font for just capacitor values
            >>> pcb.set_silkscreen_font(
            ...     size=0.6, pattern="C*", text_types=("value",)
            ... )
        """
        import fnmatch

        if isinstance(size, (int, float)):
            font_size = (float(size), float(size))
        else:
            font_size = size

        count = 0

        # Determine which references to update
        refs_to_update: set[str] = set()
        if pattern is not None:
            for fp in self._footprints:
                if fnmatch.fnmatch(fp.reference, pattern):
                    refs_to_update.add(fp.reference)
        else:
            refs_to_update = {fp.reference for fp in self._footprints}

        # Update S-expression tree
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            fp_ref = self._get_footprint_reference(child)
            if fp_ref not in refs_to_update:
                continue

            # Update fp_text nodes (KiCad 7 format)
            for fp_text in child.find_all("fp_text"):
                text_type = fp_text.get_string(0)
                if text_type in text_types:
                    self._set_text_font(fp_text, font_size, thickness)
                    count += 1

            # Update property nodes (KiCad 8+ format)
            for prop in child.find_all("property"):
                prop_name = prop.get_string(0)
                if (
                    (prop_name == "Reference" and "reference" in text_types)
                    or (prop_name == "Value" and "value" in text_types)
                ):
                    self._set_text_font(prop, font_size, thickness)
                    count += 1

        # Update parsed footprint objects
        for fp in self._footprints:
            if fp.reference in refs_to_update:
                for text in fp.texts:
                    if text.text_type in text_types:
                        text.font_size = font_size
                        text.font_thickness = thickness

        return count

    def _set_text_font(
        self,
        text_sexp: SExp,
        size: tuple[float, float],
        thickness: float,
    ) -> None:
        """Set font properties on a text S-expression node."""
        effects = text_sexp.find("effects")
        if effects is None:
            effects = SExp.list("effects")
            text_sexp.append(effects)

        font = effects.find("font")
        if font is None:
            font = SExp.list("font")
            effects.append(font)

        # Update or create size node
        size_node = font.find("size")
        if size_node is not None:
            size_node.set_value(0, size[0])
            size_node.set_value(1, size[1])
        else:
            font.append(SExp.list("size", size[0], size[1]))

        # Update or create thickness node
        thickness_node = font.find("thickness")
        if thickness_node is not None:
            thickness_node.set_value(0, thickness)
        else:
            font.append(SExp.list("thickness", thickness))

    def move_references_to_layer(
        self,
        layer: str,
        *,
        pattern: str | None = None,
    ) -> int:
        """
        Move all reference designators to a different layer.

        Useful for moving references to the fabrication layer (F.Fab)
        so they don't appear on manufactured silkscreen.

        Args:
            layer: Target layer (e.g., "F.Fab", "F.SilkS").
            pattern: Glob pattern to match references (e.g., "C*" for all
                    capacitors). If None, applies to all footprints.

        Returns:
            Number of references moved.

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> # Move all references to fab layer
            >>> pcb.move_references_to_layer("F.Fab")
            >>> # Move just capacitor references to fab
            >>> pcb.move_references_to_layer("F.Fab", pattern="C*")
        """
        import fnmatch

        count = 0

        # Determine which references to update
        refs_to_update: set[str] = set()
        if pattern is not None:
            for fp in self._footprints:
                if fnmatch.fnmatch(fp.reference, pattern):
                    refs_to_update.add(fp.reference)
        else:
            refs_to_update = {fp.reference for fp in self._footprints}

        # Update S-expression tree
        for child in self._sexp.iter_children():
            if child.tag != "footprint":
                continue

            fp_ref = self._get_footprint_reference(child)
            if fp_ref not in refs_to_update:
                continue

            # Update fp_text nodes (KiCad 7 format)
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    layer_node = fp_text.find("layer")
                    if layer_node is not None:
                        layer_node.set_value(0, layer)
                    else:
                        fp_text.append(SExp.list("layer", layer))
                    count += 1

            # Update property nodes (KiCad 8+ format)
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Reference":
                    layer_node = prop.find("layer")
                    if layer_node is not None:
                        layer_node.set_value(0, layer)
                    else:
                        prop.append(SExp.list("layer", layer))
                    count += 1

        # Update parsed footprint objects
        for fp in self._footprints:
            if fp.reference in refs_to_update:
                for text in fp.texts:
                    if text.text_type == "reference":
                        text.layer = layer

        return count

    def validate_silkscreen(
        self,
        design_rules: DesignRules | None = None,
    ) -> list[dict]:
        """
        Validate silkscreen elements and return issues.

        Checks for common silkscreen problems including:
        - Text height too small for manufacturing
        - Line width too thin
        - Silkscreen overlapping exposed pads

        Args:
            design_rules: Manufacturing design rules. If None, uses
                         default JLCPCB-compatible rules.

        Returns:
            List of issue dictionaries with keys:
            - type: Issue type (e.g., "text_height", "over_pad")
            - reference: Reference designator or element identifier
            - description: Human-readable description
            - location: (x, y) position in mm
            - layer: Layer name

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> issues = pcb.validate_silkscreen()
            >>> for issue in issues:
            ...     print(f"{issue['type']}: {issue['reference']} - {issue['description']}")
        """
        from ..manufacturers import DesignRules as DR
        from ..validate.rules.silkscreen import check_all_silkscreen

        if design_rules is None:
            # Use default JLCPCB-compatible rules
            design_rules = DR(
                min_trace_width_mm=0.127,
                min_clearance_mm=0.127,
                min_via_drill_mm=0.3,
                min_via_diameter_mm=0.5,
                min_annular_ring_mm=0.127,
                min_silkscreen_width_mm=0.15,
                min_silkscreen_height_mm=0.8,
            )

        results = check_all_silkscreen(self, design_rules)

        issues = []
        for violation in results.violations:
            issues.append({
                "type": violation.rule_id.replace("silkscreen_", ""),
                "reference": violation.items[0] if violation.items else "",
                "description": violation.message,
                "location": violation.location,
                "layer": violation.layer,
            })

        return issues

    def add_footprint_from_file(
        self,
        kicad_mod_path: str | Path,
        reference: str,
        x: float,
        y: float,
        rotation: float = 0.0,
        layer: str = "F.Cu",
        value: str = "",
    ) -> Footprint:
        """
        Add a footprint from a .kicad_mod file to the PCB.

        Loads a footprint from a KiCad footprint file and adds it to the PCB
        at the specified position with the given reference designator.

        Args:
            kicad_mod_path: Path to the .kicad_mod footprint file
            reference: Reference designator for the component (e.g., "U1", "C1")
            x: X position in mm
            y: Y position in mm
            rotation: Rotation angle in degrees (default: 0)
            layer: Layer to place footprint on ("F.Cu" or "B.Cu", default: "F.Cu")
            value: Component value (e.g., "100nF", "10k")

        Returns:
            The Footprint object that was added to the PCB

        Raises:
            FileNotFoundError: If the footprint file doesn't exist
            FileFormatError: If the file is not a valid footprint

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> fp = pcb.add_footprint_from_file(
            ...     "MyFootprints.pretty/SOT-23.kicad_mod",
            ...     reference="U1",
            ...     x=50.0,
            ...     y=30.0,
            ...     rotation=90,
            ...     value="LM317"
            ... )
        """
        # Load the footprint from file
        fp_sexp = load_footprint(kicad_mod_path)

        # Generate a new UUID for this footprint instance
        new_uuid = str(uuid.uuid4())

        # Update the UUID in the footprint
        uuid_node = fp_sexp.find("uuid")
        if uuid_node:
            uuid_node.set_value(0, new_uuid)
        else:
            # Add UUID if not present
            fp_sexp.append(SExp.list("uuid", new_uuid))

        # Update layer first (at node must come after layer)
        layer_node = fp_sexp.find("layer")
        if layer_node:
            layer_node.set_value(0, layer)
        else:
            # Create layer node - library footprints always have this, but be safe
            layer_node = SExp.list("layer", layer)
            fp_sexp.append(layer_node)

        # Update position (at x y rotation)
        # Library footprints (.kicad_mod) don't have a top-level (at) node -
        # that's only present in placed footprints within a PCB file.
        # We must create the (at) node and insert it immediately after (layer)
        # for KiCad to recognize it properly.
        at_node = fp_sexp.find("at")
        if at_node:
            # Remove existing at node - we'll insert a fresh one in the correct position
            fp_sexp.remove(at_node)

        # Create new at node with position
        at_sexp = SExp.list("at", x, y)
        if rotation != 0.0:
            at_sexp.add(rotation)

        # Find layer node's index and insert at node immediately after it
        layer_index = None
        for i, child in enumerate(fp_sexp.children):
            if not child.is_atom and child.name == "layer":
                layer_index = i
                break

        if layer_index is not None:
            fp_sexp.children.insert(layer_index + 1, at_sexp)
        else:
            # Fallback: append to end (shouldn't happen for valid footprints)
            fp_sexp.append(at_sexp)

        # Update reference and value - try KiCad 8+ property format first
        ref_updated = False
        val_updated = False

        for prop in fp_sexp.find_all("property"):
            prop_name = prop.get_string(0)
            if prop_name == "Reference":
                prop.set_value(1, reference)
                ref_updated = True
            elif prop_name == "Value":
                prop.set_value(1, value)
                val_updated = True

        # Fall back to KiCad 7 fp_text format
        for fp_text in fp_sexp.find_all("fp_text"):
            text_type = fp_text.get_string(0)
            if text_type == "reference" and not ref_updated:
                fp_text.set_value(1, reference)
                ref_updated = True
            elif text_type == "value" and not val_updated:
                fp_text.set_value(1, value)
                val_updated = True

        # If reference/value weren't found, add them as KiCad 8+ properties
        if not ref_updated:
            ref_prop = SExp.list("property", "Reference", reference)
            ref_prop.append(SExp.list("at", 0.0, -1.5))
            ref_prop.append(SExp.list("layer", layer.replace(".Cu", ".SilkS")))
            ref_prop.append(SExp.list("uuid", str(uuid.uuid4())))
            effects = SExp.list("effects")
            font = SExp.list("font")
            font.append(SExp.list("size", 1.0, 1.0))
            font.append(SExp.list("thickness", 0.15))
            effects.append(font)
            ref_prop.append(effects)
            fp_sexp.append(ref_prop)

        if not val_updated:
            val_prop = SExp.list("property", "Value", value)
            val_prop.append(SExp.list("at", 0.0, 1.5))
            val_prop.append(SExp.list("layer", layer.replace(".Cu", ".Fab")))
            val_prop.append(SExp.list("uuid", str(uuid.uuid4())))
            effects = SExp.list("effects")
            font = SExp.list("font")
            font.append(SExp.list("size", 1.0, 1.0))
            font.append(SExp.list("thickness", 0.15))
            effects.append(font)
            val_prop.append(effects)
            fp_sexp.append(val_prop)

        # Append footprint to PCB S-expression tree
        self._sexp.append(fp_sexp)

        # Parse and add to internal footprints list
        footprint = Footprint.from_sexp(fp_sexp)
        self._footprints.append(footprint)

        return footprint

    def add_footprint(
        self,
        library_id: str,
        reference: str,
        x: float,
        y: float,
        rotation: float = 0.0,
        layer: str = "F.Cu",
        value: str = "",
    ) -> Footprint:
        """
        Add a footprint from KiCad standard libraries to the PCB.

        Loads a footprint from KiCad's standard library installation and adds
        it to the PCB at the specified position.

        Args:
            library_id: Footprint identifier in "Library:Footprint" format
                       (e.g., "Capacitor_SMD:C_0805_2012Metric")
                       If library is omitted, it will be guessed from the footprint name.
            reference: Reference designator for the component (e.g., "U1", "C1")
            x: X position in mm
            y: Y position in mm
            rotation: Rotation angle in degrees (default: 0)
            layer: Layer to place footprint on ("F.Cu" or "B.Cu", default: "F.Cu")
            value: Component value (e.g., "100nF", "10k")

        Returns:
            The Footprint object that was added to the PCB

        Raises:
            FileNotFoundError: If the footprint cannot be found in the library
            ValueError: If the library path cannot be detected

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> fp = pcb.add_footprint(
            ...     library_id="Capacitor_SMD:C_0805_2012Metric",
            ...     reference="C1",
            ...     x=50.0,
            ...     y=30.0,
            ...     value="100nF"
            ... )

            # With automatic library guessing
            >>> fp = pcb.add_footprint(
            ...     library_id="C_0402_1005Metric",  # Will guess Capacitor_SMD
            ...     reference="C2",
            ...     x=60.0,
            ...     y=30.0,
            ...     value="10nF"
            ... )
        """
        # Parse library_id to extract library name and footprint name
        library_name, footprint_name = parse_library_id(library_id)

        # If no library specified, try to guess it
        if library_name is None:
            library_name = guess_standard_library(footprint_name)
            if library_name is None:
                raise ValueError(
                    f"Cannot determine library for footprint '{footprint_name}'. "
                    "Please specify the library explicitly using 'Library:Footprint' format."
                )

        # Detect KiCad library path
        lib_paths = detect_kicad_library_path()
        if not lib_paths.found:
            raise ValueError(
                "KiCad footprint library path not found. "
                "Set KICAD_FOOTPRINT_DIR environment variable or install KiCad."
            )

        # Get the footprint file path
        fp_path = lib_paths.get_footprint_file(library_name, footprint_name)
        if fp_path is None:
            raise FileNotFoundError(
                f"Footprint '{footprint_name}' not found in library '{library_name}'. "
                f"Searched in: {lib_paths.footprints_path}"
            )

        # Delegate to add_footprint_from_file
        return self.add_footprint_from_file(
            kicad_mod_path=fp_path,
            reference=reference,
            x=x,
            y=y,
            rotation=rotation,
            layer=layer,
            value=value,
        )

    def add_net(self, net_name: str) -> Net:
        """
        Add a new net to the PCB.

        If a net with the same name already exists, returns the existing net.

        Args:
            net_name: Name of the net (e.g., "GND", "+3V3", "Net-U1-Pad1")

        Returns:
            The Net object that was added or already existed

        Example:
            >>> pcb = PCB.create(width=100, height=100)
            >>> gnd = pcb.add_net("GND")
            >>> print(gnd.number, gnd.name)
            1 GND
        """
        # Check if net already exists
        existing = self.get_net_by_name(net_name)
        if existing:
            return existing

        # Find the next available net number
        next_num = max(self._nets.keys(), default=0) + 1

        # Create the net object
        net = Net(number=next_num, name=net_name)
        self._nets[next_num] = net

        # Add to the S-expression tree - insert after the last net, not at the end
        # KiCad requires nets to be declared before footprints
        net_sexp = SExp.list("net", next_num, net_name)

        # Find the position of the last net in the S-expression
        last_net_index = -1
        for i, child in enumerate(self._sexp.children):
            if child.name == "net":
                last_net_index = i

        if last_net_index >= 0:
            # Insert after the last net
            self._sexp.children.insert(last_net_index + 1, net_sexp)
        else:
            # No nets found (shouldn't happen since net 0 is always present)
            # Find the first footprint and insert before it
            first_footprint_index = -1
            for i, child in enumerate(self._sexp.children):
                if child.name == "footprint":
                    first_footprint_index = i
                    break

            if first_footprint_index >= 0:
                self._sexp.children.insert(first_footprint_index, net_sexp)
            else:
                # No footprints either, just append
                self._sexp.append(net_sexp)

        return net

    def get_pad_position(
        self, reference: str, pad_number: str
    ) -> tuple[float, float] | None:
        """
        Get the absolute board position of a pad on a footprint.

        Calculates the absolute position by combining the footprint position
        with the pad's local offset, accounting for footprint rotation.

        Args:
            reference: Footprint reference designator (e.g., "U1", "C1")
            pad_number: Pad number/name (e.g., "1", "2", "A1")

        Returns:
            Tuple of (x, y) in mm if found, None if footprint or pad not found

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pos = pcb.get_pad_position("U1", "1")
            >>> print(f"Pad at ({pos[0]:.2f}, {pos[1]:.2f})")
        """
        import math

        fp = self.get_footprint(reference)
        if not fp:
            return None

        for pad in fp.pads:
            if pad.number == pad_number:
                # Apply rotation to pad offset
                rot_rad = math.radians(fp.rotation)
                cos_r = math.cos(rot_rad)
                sin_r = math.sin(rot_rad)

                # Rotate pad position around footprint center
                pad_x, pad_y = pad.position
                rotated_x = pad_x * cos_r - pad_y * sin_r
                rotated_y = pad_x * sin_r + pad_y * cos_r

                # Add footprint position
                abs_x = fp.position[0] + rotated_x
                abs_y = fp.position[1] + rotated_y
                return (abs_x, abs_y)

        return None

    def add_trace(
        self,
        start: tuple[float, float] | tuple[str, str],
        end: tuple[float, float] | tuple[str, str],
        width: float = 0.25,
        layer: str = "F.Cu",
        net: str | None = None,
        waypoints: list[tuple[float, float]] | None = None,
    ) -> list[Segment]:
        """
        Add a trace (one or more segments) between two points or pads.

        Routes a trace from start to end, optionally through waypoints.
        When pad references are used, the net is automatically determined.

        Args:
            start: Start position as (x, y) tuple or pad reference as (reference, pad_number)
            end: End position as (x, y) tuple or pad reference as (reference, pad_number)
            width: Trace width in mm (default 0.25)
            layer: Copper layer name (default "F.Cu")
            net: Net name for the trace. Auto-detected from pads if not specified.
            waypoints: Optional list of (x, y) intermediate points

        Returns:
            List of Segment objects that were created

        Raises:
            ValueError: If pad references are invalid or positions cannot be determined

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> # Route between two pads
            >>> pcb.add_trace(("U1", "1"), ("C1", "1"), width=0.25, layer="F.Cu")
            >>> # Route between coordinates with waypoints
            >>> pcb.add_trace((50, 30), (60, 30), waypoints=[(55, 35)])
            >>> pcb.save("board.kicad_pcb")
        """
        # Resolve start position
        if isinstance(start[0], str):
            ref, pad = start[0], start[1]
            start_pos = self.get_pad_position(ref, pad)
            if start_pos is None:
                raise ValueError(f"Cannot find pad {pad} on footprint {ref}")
            # Auto-detect net from pad if not specified
            if net is None:
                fp = self.get_footprint(ref)
                if fp:
                    for p in fp.pads:
                        if p.number == pad and p.net_name:
                            net = p.net_name
                            break
        else:
            start_pos = (float(start[0]), float(start[1]))

        # Resolve end position
        if isinstance(end[0], str):
            ref, pad = end[0], end[1]
            end_pos = self.get_pad_position(ref, pad)
            if end_pos is None:
                raise ValueError(f"Cannot find pad {pad} on footprint {ref}")
            # Auto-detect net from pad if not specified
            if net is None:
                fp = self.get_footprint(ref)
                if fp:
                    for p in fp.pads:
                        if p.number == pad and p.net_name:
                            net = p.net_name
                            break
        else:
            end_pos = (float(end[0]), float(end[1]))

        # Get net number
        net_number = 0
        if net:
            net_obj = self.add_net(net)
            net_number = net_obj.number

        # Build list of points: start -> waypoints -> end
        points = [start_pos]
        if waypoints:
            points.extend(waypoints)
        points.append(end_pos)

        # Create segments between consecutive points
        segments = []
        for i in range(len(points) - 1):
            seg = Segment(
                start=points[i],
                end=points[i + 1],
                width=width,
                layer=layer,
                net_number=net_number,
                uuid=str(uuid.uuid4()),
            )
            segments.append(seg)
            self._segments.append(seg)
            self._sexp.append(seg.to_sexp())

        return segments

    def add_via(
        self,
        x: float,
        y: float,
        size: float = 0.6,
        drill: float = 0.3,
        layers: tuple[str, str] = ("F.Cu", "B.Cu"),
        net: str | None = None,
    ) -> Via:
        """
        Add a via at the specified position.

        Vias connect traces between copper layers. Default parameters create
        a standard through-hole via suitable for most designs.

        Args:
            x: X position in mm
            y: Y position in mm
            size: Via pad size in mm (default 0.6)
            drill: Via drill diameter in mm (default 0.3)
            layers: Tuple of layer names to connect (default ("F.Cu", "B.Cu"))
            net: Net name for the via (optional)

        Returns:
            The Via object that was created

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.add_via(50, 30, net="GND")
            >>> pcb.save("board.kicad_pcb")
        """
        net_number = 0
        if net:
            net_obj = self.add_net(net)
            net_number = net_obj.number

        via = Via(
            position=(x, y),
            size=size,
            drill=drill,
            layers=list(layers),
            net_number=net_number,
            uuid=str(uuid.uuid4()),
        )
        self._vias.append(via)
        self._sexp.append(via.to_sexp())

        return via

    def routing_status(self) -> dict:
        """
        Get routing statistics for the PCB.

        Returns information about traces, vias, and unrouted connections
        (ratsnest) that can be used to assess routing completion.

        Returns:
            Dictionary with routing statistics:
            - segments: Number of trace segments
            - vias: Number of vias
            - trace_length_mm: Total trace length in mm
            - nets_with_traces: Set of net numbers that have traces
            - unrouted_pads: List of (reference, pad, net) for pads without traces

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> status = pcb.routing_status()
            >>> print(f"Segments: {status['segments']}, Vias: {status['vias']}")
            >>> print(f"Total trace length: {status['trace_length_mm']:.1f} mm")
        """
        import math

        # Count segments and calculate total length
        total_length = 0.0
        nets_with_traces: set[int] = set()

        for seg in self._segments:
            dx = seg.end[0] - seg.start[0]
            dy = seg.end[1] - seg.start[1]
            total_length += math.sqrt(dx * dx + dy * dy)
            if seg.net_number > 0:
                nets_with_traces.add(seg.net_number)

        # Add vias to nets with traces
        for via in self._vias:
            if via.net_number > 0:
                nets_with_traces.add(via.net_number)

        # Find unrouted pads (pads with nets that have no traces)
        unrouted_pads = []
        for fp in self._footprints:
            for pad in fp.pads:
                if pad.net_number > 0 and pad.net_number not in nets_with_traces:
                    unrouted_pads.append((fp.reference, pad.number, pad.net_name))

        return {
            "segments": len(self._segments),
            "vias": len(self._vias),
            "trace_length_mm": total_length,
            "nets_with_traces": nets_with_traces,
            "unrouted_pads": unrouted_pads,
        }

    def get_ratsnest(self) -> list[dict]:
        """
        Get the ratsnest (unrouted connections) for the PCB.

        Returns a list of connections that need to be routed, showing which
        pads need to be connected together on each net.

        Returns:
            List of dictionaries, each containing:
            - net: Net name
            - net_number: Net number
            - pads: List of (reference, pad_number, x, y) tuples for pads in the net

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> for connection in pcb.get_ratsnest():
            ...     print(f"{connection['net']}: {len(connection['pads'])} pads")
        """
        # Group pads by net
        nets_pads: dict[int, list[tuple[str, str, float, float]]] = {}

        for fp in self._footprints:
            for pad in fp.pads:
                if pad.net_number > 0:
                    pos = self.get_pad_position(fp.reference, pad.number)
                    if pos:
                        if pad.net_number not in nets_pads:
                            nets_pads[pad.net_number] = []
                        nets_pads[pad.net_number].append(
                            (fp.reference, pad.number, pos[0], pos[1])
                        )

        # Build result with net names
        result = []
        for net_num, pads in nets_pads.items():
            if len(pads) >= 2:  # Only include nets with multiple pads
                net = self.get_net(net_num)
                net_name = net.name if net else ""
                result.append({
                    "net": net_name,
                    "net_number": net_num,
                    "pads": pads,
                })

        return result

    def assign_net_to_footprint_pad(
        self,
        reference: str,
        pad_number: str,
        net_name: str,
    ) -> bool:
        """
        Assign a net to a specific pad on a footprint.

        This updates both the in-memory footprint data and the underlying
        S-expression tree for persistence.

        Args:
            reference: Footprint reference designator (e.g., "U1", "C1")
            pad_number: Pad number/name (e.g., "1", "2", "A1")
            net_name: Name of the net to assign (will be created if doesn't exist)

        Returns:
            True if the pad was found and updated, False otherwise

        Example:
            >>> pcb = PCB.create(width=100, height=100)
            >>> pcb.add_footprint("Capacitor_SMD:C_0805_2012Metric", "C1", 50, 50)
            >>> pcb.assign_net_to_footprint_pad("C1", "1", "GND")
            True
        """
        # Find the footprint in parsed data
        fp = self.get_footprint(reference)
        if not fp:
            return False

        # Ensure net exists and get its number
        net = self.add_net(net_name)

        # Update the in-memory pad
        pad_found = False
        for pad in fp.pads:
            if pad.number == pad_number:
                pad.net_number = net.number
                pad.net_name = net.name
                pad_found = True
                break

        if not pad_found:
            return False

        # Update the S-expression tree
        for fp_sexp in self._sexp.find_all("footprint"):
            # Find the matching footprint by reference
            ref_value = None

            # KiCad 7 format: fp_text with type "reference"
            for fp_text in fp_sexp.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    ref_value = fp_text.get_string(1)
                    break

            # KiCad 8+ format: property with name "Reference"
            if not ref_value:
                for prop in fp_sexp.find_all("property"):
                    if prop.get_string(0) == "Reference":
                        ref_value = prop.get_string(1)
                        break

            if ref_value != reference:
                continue

            # Found the footprint, now find the pad
            for pad_sexp in fp_sexp.find_all("pad"):
                if pad_sexp.get_string(0) == pad_number:
                    # Remove existing net node if present
                    net_node = pad_sexp.find("net")
                    if net_node:
                        pad_sexp.remove(net_node)

                    # Add new net node
                    new_net_node = SExp.list("net", net.number, net.name)
                    pad_sexp.append(new_net_node)
                    return True

        return False

    def assign_nets_from_netlist(self, netlist) -> dict[str, list[str]]:
        """
        Assign nets to all footprint pads based on netlist connectivity.

        Iterates through all nets in the netlist and assigns them to the
        corresponding pads on footprints in the PCB.

        Args:
            netlist: A Netlist object containing connectivity information

        Returns:
            Dictionary with statistics:
            - "assigned": List of successfully assigned pads (format: "REF.PIN")
            - "missing_footprints": List of references not found in PCB
            - "missing_pads": List of pads not found (format: "REF.PIN")

        Example:
            >>> from kicad_tools.operations.netlist import Netlist
            >>> netlist = Netlist.load("project.kicad_net")
            >>> pcb = PCB.create(width=100, height=100)
            >>> # ... add footprints ...
            >>> result = pcb.assign_nets_from_netlist(netlist)
            >>> print(f"Assigned {len(result['assigned'])} pads")
        """
        stats: dict[str, list[str]] = {
            "assigned": [],
            "missing_footprints": [],
            "missing_pads": [],
        }

        # Track which footprints we've warned about
        warned_refs: set[str] = set()

        for net in netlist.nets:
            # Skip the empty net (net 0)
            if not net.name:
                continue

            for node in net.nodes:
                ref = node.reference
                pin = node.pin

                # Check if footprint exists
                fp = self.get_footprint(ref)
                if not fp:
                    if ref not in warned_refs:
                        stats["missing_footprints"].append(ref)
                        warned_refs.add(ref)
                    continue

                # Assign net to pad
                if self.assign_net_to_footprint_pad(ref, pin, net.name):
                    stats["assigned"].append(f"{ref}.{pin}")
                else:
                    stats["missing_pads"].append(f"{ref}.{pin}")

        return stats

    @property
    def path(self) -> Path | None:
        """Path to the PCB file (if loaded from file or saved).

        This is used by export methods to locate the PCB file for kicad-cli.
        Returns None if the PCB was created in memory and never saved.
        """
        return self._path

    def save(self, path: str | Path | None = None) -> None:
        """
        Save the PCB to a file.

        Args:
            path: Path to save to (.kicad_pcb). If None, uses the original
                  path from load() or the last save location.

        Raises:
            ValueError: If no path provided and PCB has no stored path
        """
        if path is None:
            if self._path is None:
                raise ValueError(
                    "No path specified and PCB has no stored path. "
                    "Provide a path or use PCB.load() to load from a file."
                )
            path = self._path
        else:
            path = Path(path)
            self._path = path

        save_pcb(self._sexp, path)

    # =========================================================================
    # Manufacturing Export Methods
    # =========================================================================

    def export_gerbers(
        self,
        output_dir: str | Path,
        *,
        manufacturer: str = "jlcpcb",
        layers: list[str] | None = None,
        include_drill: bool = True,
        create_zip: bool = False,
    ) -> Path:
        """
        Export Gerber files for PCB fabrication.

        Uses kicad-cli to generate Gerber files with manufacturer-specific settings.

        Args:
            output_dir: Directory for output files
            manufacturer: Manufacturer preset ("jlcpcb", "pcbway", "oshpark")
            layers: Specific layers to export (default: all copper + required layers)
            include_drill: Include drill files (default: True)
            create_zip: Create a zip archive of all files (default: False)

        Returns:
            Path to output directory (or zip file if create_zip=True)

        Raises:
            ValueError: If PCB has no stored path (save first)
            ExportError: If kicad-cli fails

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.export_gerbers("./gerbers", manufacturer="jlcpcb")
            >>> # Or with zip output
            >>> pcb.export_gerbers("./output", manufacturer="jlcpcb", create_zip=True)
        """
        from ..export import GerberConfig, GerberExporter

        pcb_path = self._require_path("export_gerbers")

        exporter = GerberExporter(pcb_path)

        if manufacturer.lower() in ("jlcpcb", "pcbway", "oshpark"):
            result = exporter.export_for_manufacturer(
                manufacturer.lower(),
                output_dir,
            )
        else:
            config = GerberConfig(
                output_dir=Path(output_dir),
                layers=layers or [],
                generate_drill=include_drill,
                create_zip=create_zip,
            )
            result = exporter.export(config, output_dir)

        return result

    def export_drill(
        self,
        output_dir: str | Path,
        *,
        format: str = "excellon",
        units: str = "mm",
        merge_pth_npth: bool = False,
    ) -> Path:
        """
        Export drill files (Excellon format).

        Args:
            output_dir: Directory for output files
            format: Drill format ("excellon" or "gerber_x2")
            units: Units ("mm" or "inch")
            merge_pth_npth: Merge plated and non-plated holes (default: False)

        Returns:
            Path to output directory containing drill files

        Raises:
            ValueError: If PCB has no stored path
            ExportError: If kicad-cli fails

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.export_drill("./gerbers", merge_pth_npth=False)
        """
        from ..export import GerberConfig, GerberExporter

        pcb_path = self._require_path("export_drill")

        config = GerberConfig(
            output_dir=Path(output_dir),
            generate_drill=True,
            drill_format=format,
            merge_pth_npth=merge_pth_npth,
            # Don't generate gerbers, only drill
            layers=[],
            include_edge_cuts=False,
            include_silkscreen=False,
            include_soldermask=False,
            include_solderpaste=False,
        )

        exporter = GerberExporter(pcb_path)
        return exporter.export(config, output_dir)

    def export_bom(
        self,
        output: str | Path,
        *,
        schematic_path: str | Path | None = None,
        format: str = "csv",
        manufacturer: str = "generic",
    ) -> Path:
        """
        Export Bill of Materials (BOM).

        Generates a BOM from the associated schematic file.

        Args:
            output: Output file path
            schematic_path: Path to schematic file. If not provided, looks for
                           a .kicad_sch file with the same name as the PCB.
            format: Output format ("csv", "jlcpcb", "pcbway", "seeed")
            manufacturer: Manufacturer format preset

        Returns:
            Path to generated BOM file

        Raises:
            ValueError: If schematic not found
            ExportError: If BOM generation fails

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.export_bom("bom.csv")
            >>> # JLCPCB format
            >>> pcb.export_bom("bom_jlcpcb.csv", format="jlcpcb")
        """
        from ..export import BOMExportConfig, export_bom as _export_bom
        from ..schema.bom import extract_bom

        # Find schematic
        if schematic_path is None:
            pcb_path = self._require_path("export_bom")
            schematic_path = pcb_path.with_suffix(".kicad_sch")
            if not schematic_path.exists():
                raise ValueError(
                    f"Schematic not found at {schematic_path}. "
                    "Provide schematic_path explicitly."
                )
        else:
            schematic_path = Path(schematic_path)
            if not schematic_path.exists():
                raise ValueError(f"Schematic not found: {schematic_path}")

        # Extract BOM from schematic
        bom = extract_bom(schematic_path)
        items = bom.items

        # Determine manufacturer format
        mfr = manufacturer.lower()
        if format.lower() in ("jlcpcb", "pcbway", "seeed"):
            mfr = format.lower()

        config = BOMExportConfig()
        bom_csv = _export_bom(items, mfr, config)

        # Write to file
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(bom_csv)

        return output_path

    def export_placement(
        self,
        output: str | Path,
        *,
        format: str = "csv",
        manufacturer: str = "generic",
        side: str | None = None,
    ) -> Path:
        """
        Export pick-and-place (CPL) file for SMT assembly.

        Args:
            output: Output file path
            format: Output format ("csv", "jlcpcb", "pcbway")
            manufacturer: Manufacturer format preset
            side: Export only "top" or "bottom" side (default: both)

        Returns:
            Path to generated placement file

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.export_placement("placement.csv")
            >>> # JLCPCB format
            >>> pcb.export_placement("cpl_jlcpcb.csv", format="jlcpcb")
        """
        from ..export import PnPExportConfig, export_pnp as _export_pnp

        footprints = list(self.footprints)

        # Filter by side if specified
        if side:
            layer = "F.Cu" if side.lower() == "top" else "B.Cu"
            footprints = [fp for fp in footprints if fp.layer == layer]

        # Determine manufacturer format
        mfr = manufacturer.lower()
        if format.lower() in ("jlcpcb", "pcbway"):
            mfr = format.lower()

        config = PnPExportConfig()
        pnp_csv = _export_pnp(footprints, mfr, config)

        # Write to file
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(pnp_csv)

        return output_path

    def export_manufacturing(
        self,
        output_dir: str | Path,
        *,
        manufacturer: str = "jlcpcb",
        schematic_path: str | Path | None = None,
        include_assembly: bool = True,
        create_zip: bool = True,
    ) -> dict[str, str | None]:
        """
        Export complete manufacturing package.

        Generates all files needed for PCB fabrication and assembly:
        - Gerber files (copper, silkscreen, solder mask, outline)
        - Drill files (PTH and NPTH)
        - BOM (if include_assembly=True)
        - Pick-and-place/CPL (if include_assembly=True)

        Args:
            output_dir: Directory for output files
            manufacturer: Target manufacturer ("jlcpcb", "pcbway", "oshpark", "seeed")
            schematic_path: Path to schematic (required for BOM/assembly)
            include_assembly: Include BOM and placement files (default: True)
            create_zip: Create zip archive ready for upload (default: True)

        Returns:
            Dictionary with paths to generated files:
            {
                "gerbers": "./output/gerbers.zip",
                "drill": "./output/gerbers.zip",  # Included in gerbers
                "bom": "./output/bom.csv",
                "placement": "./output/cpl.csv",
                "zip": "./output/manufacturing.zip"
            }

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> result = pcb.export_manufacturing("./manufacturing", manufacturer="jlcpcb")
            >>> print(f"Upload {result['zip']} to JLCPCB")
        """
        from ..export import AssemblyConfig, AssemblyPackage

        pcb_path = self._require_path("export_manufacturing")
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Find schematic if needed
        if include_assembly:
            if schematic_path is None:
                schematic_path = pcb_path.with_suffix(".kicad_sch")
            else:
                schematic_path = Path(schematic_path)

            if not schematic_path.exists():
                include_assembly = False

        # Configure and export
        config = AssemblyConfig(
            output_dir=output_path,
            include_bom=include_assembly,
            include_pnp=include_assembly,
            include_gerbers=True,
        )

        pkg = AssemblyPackage(
            pcb_path=pcb_path,
            schematic_path=schematic_path if include_assembly else None,
            manufacturer=manufacturer,
            config=config,
        )
        pkg_result = pkg.export(output_path)

        # Build result dictionary
        result: dict[str, str | None] = {
            "gerbers": str(pkg_result.gerber_path) if pkg_result.gerber_path else None,
            "drill": str(pkg_result.gerber_path) if pkg_result.gerber_path else None,
            "bom": str(pkg_result.bom_path) if pkg_result.bom_path else None,
            "placement": str(pkg_result.pnp_path) if pkg_result.pnp_path else None,
        }

        # Create combined zip if requested
        if create_zip:
            import zipfile

            zip_path = output_path / f"{manufacturer}_manufacturing.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_path in output_path.iterdir():
                    if file_path.is_file() and file_path != zip_path:
                        zf.write(file_path, file_path.name)
                # Include gerber subdirectory if exists
                gerber_dir = output_path / "gerbers"
                if gerber_dir.is_dir():
                    for file_path in gerber_dir.iterdir():
                        if file_path.is_file():
                            zf.write(file_path, f"gerbers/{file_path.name}")

            result["zip"] = str(zip_path)
        else:
            result["zip"] = None

        return result

    def export_gerbers_zip(
        self,
        output: str | Path,
        *,
        manufacturer: str = "jlcpcb",
        include_drill: bool = True,
    ) -> Path:
        """
        Export Gerbers and drill files as a single zip archive.

        Convenience method for quick export of fabrication files ready
        for upload to PCB manufacturers.

        Args:
            output: Output zip file path
            manufacturer: Manufacturer preset ("jlcpcb", "pcbway", "oshpark")
            include_drill: Include drill files in zip (default: True)

        Returns:
            Path to generated zip file

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.export_gerbers_zip("gerbers.zip", manufacturer="jlcpcb")
        """
        import tempfile

        from ..export import GerberExporter

        pcb_path = self._require_path("export_gerbers_zip")
        output_path = Path(output)

        # Export to temp directory, then zip
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            exporter = GerberExporter(pcb_path)
            exporter.export_for_manufacturer(manufacturer.lower(), temp_path)

            # Create zip
            import zipfile

            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_path in temp_path.iterdir():
                    if file_path.is_file():
                        zf.write(file_path, file_path.name)

        return output_path

    def _require_path(self, method_name: str) -> Path:
        """Ensure PCB has a stored path for export operations."""
        if self._path is None:
            raise ValueError(
                f"{method_name}() requires a PCB file path. "
                "Either load the PCB with PCB.load() or save it first with pcb.save()."
            )
        return self._path

    def import_from_netlist(
        self,
        netlist,
        placement_start: tuple[float, float] = (10.0, 10.0),
        placement_spacing: float = 15.0,
        columns: int = 10,
    ) -> dict[str, list[str]]:
        """
        Import footprints and assign nets from a netlist.

        Adds all footprints referenced in the netlist to the PCB and
        assigns net connections to their pads. Footprints are placed
        in a grid pattern starting from the placement_start position.

        Args:
            netlist: A Netlist object containing components and connectivity
            placement_start: Starting (x, y) position for footprint placement
            placement_spacing: Spacing between footprints in mm
            columns: Number of footprints per row in the grid

        Returns:
            Dictionary with statistics:
            - "footprints_added": List of references successfully added
            - "footprints_skipped": List of references skipped (no footprint spec)
            - "footprints_failed": List of references that failed to add
            - "nets_assigned": Number of pad-net assignments made
            - "nets_failed": List of failed pad-net assignments (format: "REF.PIN")

        Example:
            >>> from kicad_tools.operations.netlist import Netlist
            >>> netlist = Netlist.load("project.kicad_net")
            >>> pcb = PCB.create(width=100, height=100)
            >>> result = pcb.import_from_netlist(netlist)
            >>> print(f"Added {len(result['footprints_added'])} footprints")
        """
        stats: dict[str, list[str]] = {
            "footprints_added": [],
            "footprints_skipped": [],
            "footprints_failed": [],
            "nets_assigned": [],
            "nets_failed": [],
        }

        # Track grid position for footprint placement
        x, y = placement_start
        col = 0

        # Add footprints from netlist components
        for comp in netlist.components:
            ref = comp.reference
            value = comp.value
            footprint_id = comp.footprint

            # Skip components without footprint specification
            if not footprint_id:
                stats["footprints_skipped"].append(ref)
                continue

            # Skip if footprint already exists
            if self.get_footprint(ref):
                stats["footprints_skipped"].append(ref)
                continue

            try:
                self.add_footprint(
                    library_id=footprint_id,
                    reference=ref,
                    x=x,
                    y=y,
                    rotation=0.0,
                    layer="F.Cu",
                    value=value,
                )
                stats["footprints_added"].append(ref)

                # Advance to next grid position
                col += 1
                if col >= columns:
                    col = 0
                    x = placement_start[0]
                    y += placement_spacing
                else:
                    x += placement_spacing

            except (FileNotFoundError, ValueError) as e:
                # Footprint not found in library or invalid
                stats["footprints_failed"].append(f"{ref}: {e}")

        # Assign nets to pads
        net_result = self.assign_nets_from_netlist(netlist)
        stats["nets_assigned"] = net_result["assigned"]
        stats["nets_failed"] = net_result["missing_pads"]

        return stats

    def import_from_schematic(
        self,
        schematic_path: str | Path,
        placement_start: tuple[float, float] = (10.0, 10.0),
        placement_spacing: float = 15.0,
        columns: int = 10,
    ) -> dict[str, list[str]]:
        """
        Import footprints and assign nets from a schematic file.

        Exports a netlist from the schematic using kicad-cli, then imports
        all footprints and assigns net connections. This is the programmatic
        equivalent of KiCad's "Update PCB from Schematic" (F8) operation.

        Args:
            schematic_path: Path to the .kicad_sch schematic file
            placement_start: Starting (x, y) position for footprint placement
            placement_spacing: Spacing between footprints in mm
            columns: Number of footprints per row in the grid

        Returns:
            Dictionary with statistics (same as import_from_netlist)

        Raises:
            FileNotFoundError: If schematic file or kicad-cli not found
            RuntimeError: If netlist export fails

        Example:
            >>> pcb = PCB.create(width=160, height=100)
            >>> result = pcb.import_from_schematic("project.kicad_sch")
            >>> print(f"Added {len(result['footprints_added'])} footprints")
            >>> pcb.save("project.kicad_pcb")
        """
        from ..operations.netlist import export_netlist

        # Export netlist from schematic
        netlist = export_netlist(schematic_path)

        # Import using the netlist
        return self.import_from_netlist(
            netlist,
            placement_start=placement_start,
            placement_spacing=placement_spacing,
            columns=columns,
        )

    @classmethod
    def from_schematic(
        cls,
        schematic_path: str | Path,
        width: float = 100.0,
        height: float = 100.0,
        layers: int = 2,
        placement_start: tuple[float, float] = (10.0, 10.0),
        placement_spacing: float = 15.0,
        columns: int = 10,
    ) -> tuple[PCB, dict[str, list[str]]]:
        """
        Create a new PCB from a schematic file.

        Creates a blank PCB with the specified dimensions, then imports
        all footprints and net assignments from the schematic.

        Args:
            schematic_path: Path to the .kicad_sch schematic file
            width: Board width in mm
            height: Board height in mm
            layers: Number of copper layers (2 or 4)
            placement_start: Starting (x, y) position for footprint placement
            placement_spacing: Spacing between footprints in mm
            columns: Number of footprints per row in the grid

        Returns:
            Tuple of (PCB instance, import statistics dict)

        Raises:
            FileNotFoundError: If schematic file or kicad-cli not found
            RuntimeError: If netlist export fails
            ValueError: If layers is not 2 or 4

        Example:
            >>> pcb, stats = PCB.from_schematic(
            ...     "project.kicad_sch",
            ...     width=160,
            ...     height=100,
            ...     layers=4
            ... )
            >>> print(f"Created PCB with {len(stats['footprints_added'])} components")
            >>> pcb.save("project.kicad_pcb")
        """
        # Create blank PCB
        pcb = cls.create(width=width, height=height, layers=layers)

        # Import from schematic
        stats = pcb.import_from_schematic(
            schematic_path,
            placement_start=placement_start,
            placement_spacing=placement_spacing,
            columns=columns,
        )

        return pcb, stats

    # =========================================================================
    # Collision Detection and DRC Methods
    # =========================================================================

    def check_placement_collision(
        self,
        reference: str,
        x: float,
        y: float,
        rotation: float | None = None,
        *,
        clearance: float = 0.2,
        courtyard_margin: float = 0.25,
    ):
        """
        Check if placing a component at the given position would cause a collision.

        This temporarily updates the component's position in memory, checks for
        conflicts, then restores the original position.

        Args:
            reference: Reference designator of the component to check (e.g., "U1")
            x: Proposed X position in mm
            y: Proposed Y position in mm
            rotation: Proposed rotation in degrees (optional, uses current if None)
            clearance: Minimum pad clearance in mm (default: 0.2)
            courtyard_margin: Courtyard margin in mm (default: 0.25)

        Returns:
            CollisionResult with collision details if any, or no_collision if safe

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> result = pcb.check_placement_collision("U1", x=50, y=50)
            >>> if result.has_collision:
            ...     print(f"Would collide with: {result.other_ref}")
            ...     print(f"Clearance needed: {result.required_clearance}mm")
            ...     print(f"Actual clearance: {result.actual_clearance}mm")
        """
        from ..placement import CollisionResult, DesignRules, PlacementAnalyzer

        # Find the footprint
        fp = self.get_footprint(reference)
        if not fp:
            return CollisionResult(
                has_collision=False,
                message=f"Component {reference} not found",
            )

        # Save original position
        orig_x, orig_y = fp.position
        orig_rot = fp.rotation

        # Temporarily update position (in memory only)
        fp.position = (x, y)
        if rotation is not None:
            fp.rotation = rotation

        try:
            # Create analyzer and check conflicts
            analyzer = PlacementAnalyzer()
            rules = DesignRules(
                min_pad_clearance=clearance,
                courtyard_margin=courtyard_margin,
            )

            # Load this PCB's components
            analyzer._load_pcb_from_instance(self, courtyard_margin)

            # Check all pairs involving this component
            components = analyzer.get_components()
            target_comp = next(
                (c for c in components if c.reference == reference), None
            )

            if not target_comp:
                return CollisionResult.no_collision()

            for other_comp in components:
                if other_comp.reference == reference:
                    continue

                # Check for conflicts between target and other
                conflicts = analyzer._check_pair((target_comp, other_comp), rules)

                if conflicts:
                    # Return the first conflict found
                    return CollisionResult.from_conflict(conflicts[0])

            return CollisionResult.no_collision()

        finally:
            # Restore original position
            fp.position = (orig_x, orig_y)
            fp.rotation = orig_rot

    def validate_placements(
        self,
        placements: dict[str, tuple[float, float, float]],
        *,
        clearance: float = 0.2,
        courtyard_margin: float = 0.25,
    ):
        """
        Validate a batch of proposed placements before committing.

        Checks all proposed placements for conflicts with each other and
        with existing components.

        Args:
            placements: Dictionary mapping reference to (x, y, rotation) tuples
                e.g., {"U1": (50, 50, 0), "C1": (52, 50, 90), ...}
            clearance: Minimum pad clearance in mm (default: 0.2)
            courtyard_margin: Courtyard margin in mm (default: 0.25)

        Returns:
            PlacementValidationResult with all detected issues

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> placements = {"U1": (50, 50, 0), "C1": (52, 50, 0)}
            >>> result = pcb.validate_placements(placements)
            >>> for issue in result.collisions:
            ...     print(f"{issue.ref1} <-> {issue.ref2}: {issue.violation_type}")
        """
        from ..placement import (
            DesignRules,
            PlacementAnalyzer,
            PlacementCollision,
            PlacementValidationResult,
        )

        # Save original positions
        original_positions: dict[str, tuple[float, float, float]] = {}
        for ref in placements:
            fp = self.get_footprint(ref)
            if fp:
                original_positions[ref] = (fp.position[0], fp.position[1], fp.rotation)

        # Apply proposed positions temporarily
        for ref, (x, y, rot) in placements.items():
            fp = self.get_footprint(ref)
            if fp:
                fp.position = (x, y)
                fp.rotation = rot

        try:
            # Run conflict analysis
            analyzer = PlacementAnalyzer()
            rules = DesignRules(
                min_pad_clearance=clearance,
                courtyard_margin=courtyard_margin,
            )

            analyzer._load_pcb_from_instance(self, courtyard_margin)
            conflicts = analyzer._find_conflicts_internal(rules)

            # Build result
            collisions = [PlacementCollision.from_conflict(c) for c in conflicts]

            return PlacementValidationResult(
                is_valid=len(collisions) == 0,
                total_placements=len(placements),
                collision_count=len(collisions),
                collisions=collisions,
            )

        finally:
            # Restore original positions
            for ref, (x, y, rot) in original_positions.items():
                fp = self.get_footprint(ref)
                if fp:
                    fp.position = (x, y)
                    fp.rotation = rot

    def run_drc(
        self,
        *,
        clearance: float = 0.2,
        courtyard_margin: float = 0.25,
        edge_clearance: float = 0.3,
        hole_to_hole: float = 0.5,
    ):
        """
        Run design rule check on the current PCB state.

        Checks for placement conflicts including:
        - Pad clearance violations
        - Courtyard overlaps
        - Edge clearance violations
        - Hole-to-hole violations

        Args:
            clearance: Minimum pad clearance in mm (default: 0.2)
            courtyard_margin: Courtyard margin in mm (default: 0.25)
            edge_clearance: Minimum edge clearance in mm (default: 0.3)
            hole_to_hole: Minimum hole-to-hole distance in mm (default: 0.5)

        Returns:
            DRCResult with all violations and summary counts

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> result = pcb.run_drc()
            >>> print(f"Clearance violations: {result.clearance_count}")
            >>> print(f"Courtyard overlaps: {result.courtyard_count}")
            >>> for violation in result.violations:
            ...     print(f"{violation.type}: {violation.description}")
        """
        from ..placement import (
            ConflictType,
            DesignRules,
            DRCResult,
            DRCViolation,
            PlacementAnalyzer,
        )

        analyzer = PlacementAnalyzer()
        rules = DesignRules(
            min_pad_clearance=clearance,
            courtyard_margin=courtyard_margin,
            min_edge_clearance=edge_clearance,
            min_hole_to_hole=hole_to_hole,
        )

        analyzer._load_pcb_from_instance(self, courtyard_margin)
        conflicts = analyzer._find_conflicts_internal(rules)

        # Convert to DRC violations and count by type
        violations = []
        clearance_count = 0
        courtyard_count = 0
        edge_count = 0
        hole_count = 0

        for conflict in conflicts:
            violations.append(DRCViolation.from_conflict(conflict))

            if conflict.type == ConflictType.PAD_CLEARANCE:
                clearance_count += 1
            elif conflict.type == ConflictType.COURTYARD_OVERLAP:
                courtyard_count += 1
            elif conflict.type == ConflictType.EDGE_CLEARANCE:
                edge_count += 1
            elif conflict.type == ConflictType.HOLE_TO_HOLE:
                hole_count += 1

        return DRCResult(
            passed=len(violations) == 0,
            violation_count=len(violations),
            clearance_count=clearance_count,
            courtyard_count=courtyard_count,
            edge_clearance_count=edge_count,
            hole_to_hole_count=hole_count,
            violations=violations,
        )

    def set_design_rules(
        self,
        clearance: float = 0.2,
        courtyard_clearance: float = 0.25,
        silkscreen_clearance: float = 0.15,
        edge_clearance: float = 0.3,
        hole_to_hole: float = 0.5,
    ):
        """
        Set design rules for collision detection and DRC.

        These rules are stored on the PCB instance and used as defaults
        for collision checking and DRC operations.

        Args:
            clearance: Minimum pad clearance in mm (default: 0.2)
            courtyard_clearance: Courtyard margin in mm (default: 0.25)
            silkscreen_clearance: Silkscreen clearance in mm (default: 0.15)
            edge_clearance: Minimum edge clearance in mm (default: 0.3)
            hole_to_hole: Minimum hole-to-hole distance in mm (default: 0.5)

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> pcb.set_design_rules(
            ...     clearance=0.2,
            ...     courtyard_clearance=0.25,
            ...     silkscreen_clearance=0.15
            ... )
            >>> # Now collision checks use these rules by default
            >>> result = pcb.run_drc()
        """
        from ..placement import DesignRules

        self._design_rules = DesignRules(
            min_pad_clearance=clearance,
            courtyard_margin=courtyard_clearance,
            min_edge_clearance=edge_clearance,
            min_hole_to_hole=hole_to_hole,
        )

    def place_footprint_safe(
        self,
        reference: str,
        x: float,
        y: float,
        rotation: float | None = None,
        *,
        min_clearance: float = 0.2,
        auto_adjust: bool = True,
        max_adjustment: float = 5.0,
    ) -> tuple[bool, tuple[float, float] | None, str]:
        """
        Place a footprint with automatic collision avoidance.

        Attempts to place the footprint at the given position. If a collision
        would occur and auto_adjust is True, tries to find a nearby position
        that avoids the collision.

        Args:
            reference: Reference designator of the component (e.g., "U1")
            x: Desired X position in mm
            y: Desired Y position in mm
            rotation: Rotation in degrees (optional)
            min_clearance: Minimum clearance to maintain in mm (default: 0.2)
            auto_adjust: If True, automatically adjust position to avoid collision
            max_adjustment: Maximum distance to adjust position in mm (default: 5.0)

        Returns:
            Tuple of:
            - success: True if placement succeeded (with or without adjustment)
            - final_position: (x, y) of final position, or None if failed
            - message: Description of what happened

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> success, pos, msg = pcb.place_footprint_safe(
            ...     "C1", x=50, y=50, min_clearance=0.2
            ... )
            >>> if success:
            ...     print(f"Placed at {pos}")
            >>> else:
            ...     print(f"Failed: {msg}")
        """
        fp = self.get_footprint(reference)
        if not fp:
            return False, None, f"Component {reference} not found"

        # Check if proposed position is clear
        result = self.check_placement_collision(
            reference, x, y, rotation, clearance=min_clearance
        )

        if not result.has_collision:
            # Position is clear, apply it
            self.update_footprint_position(reference, x, y, rotation)
            return True, (x, y), "Placed at requested position"

        if not auto_adjust:
            return (
                False,
                None,
                f"Collision with {result.other_ref}: {result.message}",
            )

        # Try to find a clear position nearby
        import math

        # Try positions in expanding circles
        for radius in [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]:
            if radius > max_adjustment:
                break

            for angle in range(0, 360, 45):
                rad = math.radians(angle)
                test_x = x + radius * math.cos(rad)
                test_y = y + radius * math.sin(rad)

                test_result = self.check_placement_collision(
                    reference, test_x, test_y, rotation, clearance=min_clearance
                )

                if not test_result.has_collision:
                    self.update_footprint_position(reference, test_x, test_y, rotation)
                    return (
                        True,
                        (test_x, test_y),
                        f"Adjusted by {radius:.1f}mm to avoid collision with {result.other_ref}",
                    )

        return (
            False,
            None,
            f"Could not find clear position within {max_adjustment}mm",
        )

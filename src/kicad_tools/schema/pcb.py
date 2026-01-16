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
            elif prop_name == "Value":
                fp.value = prop_value

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

    def __init__(self, sexp: SExp):
        """Initialize from parsed S-expression data."""
        self._sexp = sexp
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
    def load(cls, path: str) -> PCB:
        """Load PCB from file."""
        sexp = load_pcb(path)
        return cls(sexp)

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
    ) -> PCB:
        """Create a new blank PCB from scratch.

        This creates a minimal but valid KiCad PCB file with:
        - Board outline on Edge.Cuts layer
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

        Returns:
            A new PCB instance ready for adding footprints and traces.

        Raises:
            ValueError: If layers is not 2 or 4

        Example:
            >>> pcb = PCB.create(width=160, height=100, layers=4, title="My Board")
            >>> pcb.save("my_board.kicad_pcb")
        """
        if layers not in (2, 4):
            raise ValueError(f"Layers must be 2 or 4, got {layers}")

        if board_date is None:
            board_date = date.today().isoformat()

        sexp = cls._build_blank_pcb_sexp(
            width=width,
            height=height,
            layers=layers,
            title=title,
            revision=revision,
            company=company,
            board_date=board_date,
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
        pcb.append(SExp.list("paper", "A4"))

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
        pcb.append(PCB._build_board_outline_sexp(width, height))

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
    def _build_board_outline_sexp(width: float, height: float) -> SExp:
        """Build a rectangular board outline on Edge.Cuts layer."""
        return SExp.list(
            "gr_rect",
            SExp.list("start", 0, 0),
            SExp.list("end", width, height),
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
                    if len(at_node.values) >= 3:
                        at_node.set_value(2, rotation)
                    elif rotation != 0.0:
                        at_node.values.append(rotation)
            return True

        return False

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

        # Update position (at x y rotation)
        at_node = fp_sexp.find("at")
        if at_node:
            at_node.set_value(0, x)
            at_node.set_value(1, y)
            if rotation != 0.0:
                if len(at_node.values) >= 3:
                    at_node.set_value(2, rotation)
                else:
                    at_node.add(rotation)
        else:
            # Create at node
            at_sexp = SExp.list("at", x, y)
            if rotation != 0.0:
                at_sexp.add(rotation)
            fp_sexp.append(at_sexp)

        # Update layer
        layer_node = fp_sexp.find("layer")
        if layer_node:
            layer_node.set_value(0, layer)
        else:
            fp_sexp.append(SExp.list("layer", layer))

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

        # Add to the S-expression tree
        net_sexp = SExp.list("net", next_num, net_name)
        self._sexp.append(net_sexp)

        return net

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

    def save(self, path: str | Path) -> None:
        """
        Save the PCB to a file.

        Args:
            path: Path to save to (.kicad_pcb)
        """
        save_pcb(self._sexp, path)

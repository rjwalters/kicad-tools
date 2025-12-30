"""KiCad PCB data models.

Provides classes for parsing and manipulating KiCad PCB files (.kicad_pcb).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterator, List, Optional, Tuple, Union

from ..core.sexp import SExp
from ..core.sexp_file import load_pcb, save_pcb

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
    position: Tuple[float, float]
    size: Tuple[float, float]
    layers: List[str]
    net_number: int = 0
    net_name: str = ""
    drill: float = 0.0
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Optional[Pad]:
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
class Footprint:
    """PCB component footprint."""

    name: str
    layer: str
    position: Tuple[float, float]
    rotation: float
    reference: str
    value: str
    pads: List[Pad] = field(default_factory=list)
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
        for fp_text in sexp.find_all("fp_text"):
            text_type = fp_text.get_string(0)
            text_value = fp_text.get_string(1) or ""
            if text_type == "reference":
                fp.reference = text_value
            elif text_type == "value":
                fp.value = text_value

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

        return fp


@dataclass
class Segment:
    """PCB trace segment."""

    start: Tuple[float, float]
    end: Tuple[float, float]
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

    position: Tuple[float, float]
    size: float
    drill: float
    layers: List[str]
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
    """PCB copper pour zone."""

    net_number: int
    net_name: str
    layer: str
    uuid: str = ""
    name: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Zone:
        """Parse zone from S-expression."""
        zone = cls(
            net_number=0,
            net_name="",
            layer="",
        )

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

        return zone


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

    stackup: List[StackupLayer] = field(default_factory=list)
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
        self._layers: Dict[int, Layer] = {}
        self._nets: Dict[int, Net] = {}
        self._footprints: List[Footprint] = []
        self._segments: List[Segment] = []
        self._vias: List[Via] = []
        self._zones: List[Zone] = []
        self._setup: Optional[Setup] = None
        self._title_block: Dict[str, str] = {}
        self._parse()

    @classmethod
    def load(cls, path: str) -> "PCB":
        """Load PCB from file."""
        sexp = load_pcb(path)
        return cls(sexp)

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
            elif tag == "setup":
                self._parse_setup(child)
            elif tag == "title_block":
                self._parse_title_block(child)

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

    def _parse_stackup(self, sexp: SExp) -> List[StackupLayer]:
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
    def layers(self) -> Dict[int, Layer]:
        """Layer definitions."""
        return self._layers

    @property
    def copper_layers(self) -> List[Layer]:
        """Copper layers only."""
        return [layer for layer in self._layers.values() if layer.type in ("signal", "power")]

    @property
    def nets(self) -> Dict[int, Net]:
        """Net definitions."""
        return self._nets

    def get_net(self, number: int) -> Optional[Net]:
        """Get net by number."""
        return self._nets.get(number)

    def get_net_by_name(self, name: str) -> Optional[Net]:
        """Get net by name."""
        for net in self._nets.values():
            if net.name == name:
                return net
        return None

    @property
    def footprints(self) -> "FootprintList":
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

    def get_footprint(self, reference: str) -> Optional[Footprint]:
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
    def segments(self) -> List[Segment]:
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
    def vias(self) -> List[Via]:
        """All vias."""
        return self._vias

    def vias_in_net(self, net_number: int) -> Iterator[Via]:
        """Get vias in a specific net."""
        for via in self._vias:
            if via.net_number == net_number:
                yield via

    @property
    def zones(self) -> List[Zone]:
        """All zones (copper pours)."""
        return self._zones

    @property
    def setup(self) -> Optional[Setup]:
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

    def total_trace_length(self, layer: Optional[str] = None) -> float:
        """Calculate total trace length in mm."""
        import math

        total = 0.0
        for seg in self._segments:
            if layer is None or seg.layer == layer:
                dx = seg.end[0] - seg.start[0]
                dy = seg.end[1] - seg.start[1]
                total += math.sqrt(dx * dx + dy * dy)
        return total

    def summary(self) -> Dict:
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
        rotation: Optional[float] = None,
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
        old_pos = fp.position
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

    def save(self, path: Union[str, Path]) -> None:
        """
        Save the PCB to a file.

        Args:
            path: Path to save to (.kicad_pcb)
        """
        save_pcb(self._sexp, path)

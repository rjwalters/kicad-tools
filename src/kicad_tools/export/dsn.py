"""
Specctra DSN export for KiCad PCB files.

Exports a .kicad_pcb file to Specctra DSN format, suitable for routing
with Freerouting or other DSN-compatible autorouters.

The DSN format is an S-expression-based interchange format consisting of:
- (pcb ...) root with (structure ...) describing layers, boundaries, rules
- (placement ...) describing component positions
- (library ...) describing padstacks and component images
- (network ...) describing nets and their pin connections
- (wiring ...) for pre-existing routes to preserve

Usage::

    from kicad_tools.export.dsn import KiCadToDSNExporter

    exporter = KiCadToDSNExporter("board.kicad_pcb")
    exporter.export("board.dsn")
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..sexp.parser import SExp, parse_file

logger = logging.getLogger(__name__)

# KiCad layer name -> DSN layer name mapping
KICAD_TO_DSN_LAYER: dict[str, str] = {
    "F.Cu": "F.Cu",
    "In1.Cu": "In1.Cu",
    "In2.Cu": "In2.Cu",
    "In3.Cu": "In3.Cu",
    "In4.Cu": "In4.Cu",
    "In5.Cu": "In5.Cu",
    "In6.Cu": "In6.Cu",
    "B.Cu": "B.Cu",
}

# Reverse mapping
DSN_TO_KICAD_LAYER: dict[str, str] = {v: k for k, v in KICAD_TO_DSN_LAYER.items()}


def mm_to_um(mm: float) -> float:
    """Convert millimeters to micrometers (DSN resolution unit)."""
    return round(mm * 1000.0, 3)


def um_to_mm(um: float) -> float:
    """Convert micrometers back to millimeters."""
    return um / 1000.0


@dataclass
class PadInfo:
    """Information about a single pad in a footprint."""

    number: str
    pad_type: str  # "smd", "thru_hole"
    shape: str  # "rect", "roundrect", "oval", "circle"
    x: float  # relative to footprint origin, mm
    y: float  # relative to footprint origin, mm
    width: float  # mm
    height: float  # mm
    drill: float  # mm, 0 for SMD
    layers: list[str]
    net_number: int
    net_name: str
    roundrect_rratio: float = 0.0


@dataclass
class FootprintInfo:
    """Information about a placed footprint."""

    reference: str
    footprint_lib: str
    layer: str
    x: float  # mm, board absolute
    y: float  # mm, board absolute
    rotation: float  # degrees
    pads: list[PadInfo] = field(default_factory=list)
    uuid: str = ""


@dataclass
class SegmentInfo:
    """Pre-existing trace segment."""

    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str
    net: int


@dataclass
class ViaInfo:
    """Pre-existing via."""

    x: float
    y: float
    size: float
    drill: float
    net: int
    layers: tuple[str, str] = ("F.Cu", "B.Cu")


class KiCadToDSNExporter:
    """Export a KiCad PCB file to Specctra DSN format.

    Args:
        pcb_path: Path to the .kicad_pcb file.
    """

    def __init__(self, pcb_path: str | Path) -> None:
        self.pcb_path = Path(pcb_path)
        self._pcb: SExp | None = None
        self._layers: list[str] = []
        self._nets: dict[int, str] = {}
        self._footprints: list[FootprintInfo] = []
        self._segments: list[SegmentInfo] = []
        self._vias: list[ViaInfo] = []
        self._board_outline: list[tuple[float, float]] = []
        self._padstacks: dict[str, str] = {}  # padstack_name -> DSN definition
        self._default_clearance: float = 0.2  # mm
        self._default_trace_width: float = 0.25  # mm
        self._default_via_size: float = 0.8  # mm
        self._default_via_drill: float = 0.4  # mm

    def _load(self) -> None:
        """Load and parse the PCB file."""
        if self._pcb is not None:
            return
        self._pcb = parse_file(str(self.pcb_path))
        self._extract_layers()
        self._extract_nets()
        self._extract_design_rules()
        self._extract_board_outline()
        self._extract_footprints()
        self._extract_existing_routes()

    def _extract_layers(self) -> None:
        """Extract copper layer names from the PCB."""
        assert self._pcb is not None
        layers_node = self._pcb.find("layers")
        if layers_node is None:
            self._layers = ["F.Cu", "B.Cu"]
            return

        self._layers = []
        for child in layers_node.children:
            # Each layer child is like (0 "F.Cu" signal)
            # The parser gives: name="0", children=[SExp(value="F.Cu"), SExp(value="signal")]
            if len(child.children) >= 2:
                layer_name = _get_str(child.children[0])
                layer_type = _get_str(child.children[1])
                if layer_name and layer_type in ("signal", "power"):
                    if layer_name in KICAD_TO_DSN_LAYER:
                        self._layers.append(layer_name)

    def _extract_nets(self) -> None:
        """Extract net definitions."""
        assert self._pcb is not None
        for child in self._pcb.children:
            if not hasattr(child, "name") or child.name != "net":
                continue
            if len(child.children) >= 2:
                net_num = _get_int(child.children[0])
                net_name = _get_str(child.children[1])
                if net_num is not None and net_name is not None:
                    self._nets[net_num] = net_name

    def _extract_design_rules(self) -> None:
        """Extract design rules from the setup section."""
        assert self._pcb is not None
        setup = self._pcb.find("setup")
        if setup is None:
            return

        for child in setup.children:
            if not hasattr(child, "name"):
                continue
            if child.name == "pad_to_mask_clearance":
                pass  # Not relevant for routing
            # Look for design rules in nested structure
            elif child.name == "pcbplotparams":
                pass  # Plot params, not routing rules

        # Try to find net class defaults
        for child in self._pcb.children:
            if not hasattr(child, "name"):
                continue
            if child.name == "net_class":
                # KiCad 6 style
                for nc_child in child.children:
                    if hasattr(nc_child, "name"):
                        if nc_child.name == "clearance" and nc_child.children:
                            self._default_clearance = _get_float(nc_child.children[0]) or 0.2
                        elif nc_child.name == "trace_width" and nc_child.children:
                            self._default_trace_width = _get_float(nc_child.children[0]) or 0.25
                        elif nc_child.name == "via_dia" and nc_child.children:
                            self._default_via_size = _get_float(nc_child.children[0]) or 0.8
                        elif nc_child.name == "via_drill" and nc_child.children:
                            self._default_via_drill = _get_float(nc_child.children[0]) or 0.4

    def _extract_board_outline(self) -> None:
        """Extract board outline from Edge.Cuts layer."""
        assert self._pcb is not None
        points: list[tuple[float, float]] = []

        for child in self._pcb.children:
            if not hasattr(child, "name"):
                continue

            # Handle gr_rect on Edge.Cuts
            if child.name == "gr_rect":
                layer_node = child.find("layer")
                if layer_node and len(layer_node.children) >= 1:
                    layer_name = _get_str(layer_node.children[0])
                    if layer_name == "Edge.Cuts":
                        start = child.find("start")
                        end = child.find("end")
                        if start and end and len(start.children) >= 2 and len(end.children) >= 2:
                            x1 = _get_float(start.children[0]) or 0.0
                            y1 = _get_float(start.children[1]) or 0.0
                            x2 = _get_float(end.children[0]) or 0.0
                            y2 = _get_float(end.children[1]) or 0.0
                            points = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]

            # Handle gr_line on Edge.Cuts
            elif child.name == "gr_line":
                layer_node = child.find("layer")
                if layer_node and len(layer_node.children) >= 1:
                    layer_name = _get_str(layer_node.children[0])
                    if layer_name == "Edge.Cuts":
                        start = child.find("start")
                        end = child.find("end")
                        if start and end and len(start.children) >= 2 and len(end.children) >= 2:
                            x1 = _get_float(start.children[0]) or 0.0
                            y1 = _get_float(start.children[1]) or 0.0
                            x2 = _get_float(end.children[0]) or 0.0
                            y2 = _get_float(end.children[1]) or 0.0
                            points.append((x1, y1))
                            points.append((x2, y2))

        self._board_outline = points

    def _extract_footprints(self) -> None:
        """Extract footprint placements and pad info."""
        assert self._pcb is not None

        for child in self._pcb.children:
            if not hasattr(child, "name") or child.name != "footprint":
                continue

            # First child is the library footprint name
            fp_lib = _get_str(child.children[0]) if child.children else "unknown"

            # Get reference
            ref = "?"
            for sub in child.children:
                if hasattr(sub, "name") and sub.name == "fp_text":
                    if sub.children and _get_str(sub.children[0]) == "reference":
                        if len(sub.children) >= 2:
                            ref = _get_str(sub.children[1]) or "?"

            # Get position
            at_node = child.find("at")
            x, y, rotation = 0.0, 0.0, 0.0
            if at_node and len(at_node.children) >= 2:
                x = _get_float(at_node.children[0]) or 0.0
                y = _get_float(at_node.children[1]) or 0.0
                if len(at_node.children) >= 3:
                    rotation = _get_float(at_node.children[2]) or 0.0

            # Get layer
            layer_node = child.find("layer")
            layer = "F.Cu"
            if layer_node and layer_node.children:
                layer = _get_str(layer_node.children[0]) or "F.Cu"

            # Get UUID
            uuid_node = child.find("uuid")
            uuid_str = ""
            if uuid_node and uuid_node.children:
                uuid_str = _get_str(uuid_node.children[0]) or ""

            fp_info = FootprintInfo(
                reference=ref,
                footprint_lib=fp_lib or "unknown",
                layer=layer,
                x=x,
                y=y,
                rotation=rotation,
                uuid=uuid_str,
            )

            # Extract pads
            for sub in child.children:
                if hasattr(sub, "name") and sub.name == "pad":
                    pad = self._parse_pad(sub)
                    if pad:
                        fp_info.pads.append(pad)

            self._footprints.append(fp_info)

    def _parse_pad(self, pad_node: SExp) -> PadInfo | None:
        """Parse a pad S-expression into PadInfo."""
        if len(pad_node.children) < 3:
            return None

        pad_number = _get_str(pad_node.children[0]) or ""
        pad_type = _get_str(pad_node.children[1]) or "smd"
        pad_shape = _get_str(pad_node.children[2]) or "rect"

        # Position relative to footprint
        at_node = pad_node.find("at")
        px, py = 0.0, 0.0
        if at_node and len(at_node.children) >= 2:
            px = _get_float(at_node.children[0]) or 0.0
            py = _get_float(at_node.children[1]) or 0.0

        # Size
        size_node = pad_node.find("size")
        width, height = 1.0, 1.0
        if size_node and len(size_node.children) >= 2:
            width = _get_float(size_node.children[0]) or 1.0
            height = _get_float(size_node.children[1]) or 1.0

        # Drill
        drill = 0.0
        drill_node = pad_node.find("drill")
        if drill_node and drill_node.children:
            drill = _get_float(drill_node.children[0]) or 0.0

        # Layers
        layers_node = pad_node.find("layers")
        pad_layers: list[str] = []
        if layers_node:
            for lc in layers_node.children:
                ln = _get_str(lc)
                if ln:
                    pad_layers.append(ln)

        # Net
        net_node = pad_node.find("net")
        net_num = 0
        net_name = ""
        if net_node and len(net_node.children) >= 2:
            net_num = _get_int(net_node.children[0]) or 0
            net_name = _get_str(net_node.children[1]) or ""

        # Roundrect ratio
        rratio = 0.0
        rratio_node = pad_node.find("roundrect_rratio")
        if rratio_node and rratio_node.children:
            rratio = _get_float(rratio_node.children[0]) or 0.0

        return PadInfo(
            number=pad_number,
            pad_type=pad_type,
            shape=pad_shape,
            x=px,
            y=py,
            width=width,
            height=height,
            drill=drill,
            layers=pad_layers,
            net_number=net_num,
            net_name=net_name,
            roundrect_rratio=rratio,
        )

    def _extract_existing_routes(self) -> None:
        """Extract existing trace segments and vias."""
        assert self._pcb is not None

        for child in self._pcb.children:
            if not hasattr(child, "name"):
                continue

            if child.name == "segment":
                seg = self._parse_segment(child)
                if seg:
                    self._segments.append(seg)
            elif child.name == "via":
                via = self._parse_via(child)
                if via:
                    self._vias.append(via)

    def _parse_segment(self, node: SExp) -> SegmentInfo | None:
        """Parse a segment S-expression."""
        start = node.find("start")
        end = node.find("end")
        width_node = node.find("width")
        layer_node = node.find("layer")
        net_node = node.find("net")

        if not all([start, end, width_node, layer_node, net_node]):
            return None

        return SegmentInfo(
            x1=_get_float(start.children[0]) or 0.0,
            y1=_get_float(start.children[1]) or 0.0,
            x2=_get_float(end.children[0]) or 0.0,
            y2=_get_float(end.children[1]) or 0.0,
            width=_get_float(width_node.children[0]) or 0.25,
            layer=_get_str(layer_node.children[0]) or "F.Cu",
            net=_get_int(net_node.children[0]) or 0,
        )

    def _parse_via(self, node: SExp) -> ViaInfo | None:
        """Parse a via S-expression."""
        at_node = node.find("at")
        size_node = node.find("size")
        drill_node = node.find("drill")
        net_node = node.find("net")
        layers_node = node.find("layers")

        if not all([at_node, size_node, net_node]):
            return None

        layers = ("F.Cu", "B.Cu")
        if layers_node and len(layers_node.children) >= 2:
            l1 = _get_str(layers_node.children[0]) or "F.Cu"
            l2 = _get_str(layers_node.children[1]) or "B.Cu"
            layers = (l1, l2)

        return ViaInfo(
            x=_get_float(at_node.children[0]) or 0.0,
            y=_get_float(at_node.children[1]) or 0.0,
            size=_get_float(size_node.children[0]) or 0.8,
            drill=_get_float(drill_node.children[0]) if drill_node and drill_node.children else 0.4,
            net=_get_int(net_node.children[0]) or 0,
            layers=layers,
        )

    def _make_padstack_name(self, pad: PadInfo) -> str:
        """Generate a unique padstack name for a pad geometry."""
        if pad.pad_type == "thru_hole":
            # Through-hole: include drill info
            w = mm_to_um(pad.width)
            h = mm_to_um(pad.height)
            d = mm_to_um(pad.drill)
            return f"Round[A]Pad_{w}x{h}_Drill{d}_um"
        else:
            # SMD: layer-specific
            w = mm_to_um(pad.width)
            h = mm_to_um(pad.height)
            layer = pad.layers[0] if pad.layers else "F.Cu"
            side = "T" if "F.Cu" in layer else "B"
            return f"Rect[{side}]Pad_{w}x{h}_um"

    def _build_padstack_def(self, name: str, pad: PadInfo) -> str:
        """Build DSN padstack definition for a pad."""
        lines: list[str] = []
        lines.append(f"    (padstack {_dsn_quote(name)}")

        if pad.pad_type == "thru_hole":
            # Through-hole pad: shapes on all copper layers
            w = mm_to_um(pad.width)
            h = mm_to_um(pad.height)
            for layer in self._layers:
                dsn_layer = KICAD_TO_DSN_LAYER.get(layer, layer)
                if pad.shape in ("rect",):
                    lines.append(f"      (shape (rect {_dsn_quote(dsn_layer)} {-w/2:.1f} {-h/2:.1f} {w/2:.1f} {h/2:.1f}))")
                else:
                    # oval/circle -> use rect approximation for DSN
                    lines.append(f"      (shape (rect {_dsn_quote(dsn_layer)} {-w/2:.1f} {-h/2:.1f} {w/2:.1f} {h/2:.1f}))")
            lines.append("      (attach off)")
        else:
            # SMD pad: shape on one layer only
            w = mm_to_um(pad.width)
            h = mm_to_um(pad.height)
            pad_layer = pad.layers[0] if pad.layers else "F.Cu"
            # Map to copper layer only
            if "F.Cu" in pad_layer:
                dsn_layer = "F.Cu"
            elif "B.Cu" in pad_layer:
                dsn_layer = "B.Cu"
            else:
                dsn_layer = pad_layer
            dsn_layer = KICAD_TO_DSN_LAYER.get(dsn_layer, dsn_layer)
            lines.append(f"      (shape (rect {_dsn_quote(dsn_layer)} {-w/2:.1f} {-h/2:.1f} {w/2:.1f} {h/2:.1f}))")
            lines.append("      (attach off)")

        lines.append("    )")
        return "\n".join(lines)

    def _build_via_padstack(self) -> str:
        """Build the default via padstack."""
        size = mm_to_um(self._default_via_size)
        drill = mm_to_um(self._default_via_drill)
        name = f"Via[0-{len(self._layers)-1}]_Pad{size:.0f}_um"

        lines: list[str] = []
        lines.append(f"    (padstack {_dsn_quote(name)}")
        for layer in self._layers:
            dsn_layer = KICAD_TO_DSN_LAYER.get(layer, layer)
            r = size / 2
            lines.append(f"      (shape (circle {_dsn_quote(dsn_layer)} {size:.1f}))")
        lines.append("      (attach off)")
        lines.append("    )")

        self._via_padstack_name = name
        return "\n".join(lines)

    def export(self, output_path: str | Path | None = None) -> str:
        """Export the PCB to DSN format.

        Args:
            output_path: Path for the DSN output file. If None, returns
                the DSN content as a string without writing to disk.

        Returns:
            The DSN content as a string.
        """
        self._load()

        dsn = self._generate_dsn()

        if output_path is not None:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(dsn, encoding="utf-8")
            logger.info("Exported DSN to %s", output_path)

        return dsn

    def _generate_dsn(self) -> str:
        """Generate the complete DSN S-expression string."""
        parts: list[str] = []

        # Header
        pcb_name = self.pcb_path.stem
        parts.append(f"(pcb {_dsn_quote(pcb_name)}")
        parts.append("  (parser")
        parts.append('    (string_quote ")')
        parts.append("    (space_in_quoted_tokens on)")
        parts.append("    (host_cad \"KiCad's Pcbnew\")")
        parts.append('    (host_version "kicad-tools")')
        parts.append("  )")
        parts.append("  (resolution um 10)")
        parts.append("  (unit um)")

        # Structure
        parts.append(self._generate_structure())

        # Placement
        parts.append(self._generate_placement())

        # Library (padstacks and images)
        parts.append(self._generate_library())

        # Network
        parts.append(self._generate_network())

        # Wiring (pre-existing routes)
        wiring = self._generate_wiring()
        if wiring:
            parts.append(wiring)

        parts.append(")")
        return "\n".join(parts)

    def _generate_structure(self) -> str:
        """Generate the (structure ...) section."""
        lines: list[str] = []
        lines.append("  (structure")

        # Layers
        for layer in self._layers:
            dsn_layer = KICAD_TO_DSN_LAYER.get(layer, layer)
            lines.append(f"    (layer {_dsn_quote(dsn_layer)}")
            lines.append("      (type signal)")
            lines.append("      (property")
            lines.append("        (index 0)")
            lines.append("      )")
            lines.append("    )")

        # Board boundary
        if self._board_outline:
            lines.append("    (boundary")
            lines.append("      (path pcb 0")
            for x, y in self._board_outline:
                lines.append(f"        {mm_to_um(x):.1f} {mm_to_um(y):.1f}")
            # Close the polygon
            if self._board_outline:
                x0, y0 = self._board_outline[0]
                lines.append(f"        {mm_to_um(x0):.1f} {mm_to_um(y0):.1f}")
            lines.append("      )")
            lines.append("    )")

        # Design rules
        clearance_um = mm_to_um(self._default_clearance)
        trace_um = mm_to_um(self._default_trace_width)
        lines.append(f"    (rule")
        lines.append(f"      (width {trace_um:.1f})")
        lines.append(f"      (clearance {clearance_um:.1f})")
        lines.append(f"      (clearance {clearance_um:.1f} (type default_smd))")
        lines.append(f"      (clearance {clearance_um:.1f} (type smd_smd))")
        lines.append(f"    )")

        lines.append("  )")
        return "\n".join(lines)

    def _generate_placement(self) -> str:
        """Generate the (placement ...) section."""
        lines: list[str] = []
        lines.append("  (placement")

        for fp in self._footprints:
            image_name = _sanitize_name(fp.footprint_lib)
            side = "front" if fp.layer == "F.Cu" else "back"
            x_um = mm_to_um(fp.x)
            y_um = mm_to_um(fp.y)
            rot = fp.rotation

            lines.append(f"    (component {_dsn_quote(image_name)}")
            lines.append(f"      (place {_dsn_quote(fp.reference)} {x_um:.1f} {y_um:.1f} {side} {rot:.1f})")
            lines.append("    )")

        lines.append("  )")
        return "\n".join(lines)

    def _generate_library(self) -> str:
        """Generate the (library ...) section with images and padstacks."""
        lines: list[str] = []
        lines.append("  (library")

        # Generate images (component footprint descriptions)
        seen_images: set[str] = set()
        padstack_defs: dict[str, str] = {}

        for fp in self._footprints:
            image_name = _sanitize_name(fp.footprint_lib)
            if image_name in seen_images:
                continue
            seen_images.add(image_name)

            lines.append(f"    (image {_dsn_quote(image_name)}")

            for pad in fp.pads:
                ps_name = self._make_padstack_name(pad)
                if ps_name not in padstack_defs:
                    padstack_defs[ps_name] = self._build_padstack_def(ps_name, pad)

                px_um = mm_to_um(pad.x)
                py_um = mm_to_um(pad.y)
                lines.append(f"      (pin {_dsn_quote(ps_name)} {_dsn_quote(pad.number)} {px_um:.1f} {py_um:.1f})")

            lines.append("    )")

        # Via padstack
        via_def = self._build_via_padstack()
        padstack_defs[self._via_padstack_name] = via_def

        # Padstack definitions
        for ps_def in padstack_defs.values():
            lines.append(ps_def)

        lines.append("  )")
        return "\n".join(lines)

    def _generate_network(self) -> str:
        """Generate the (network ...) section."""
        lines: list[str] = []
        lines.append("  (network")

        # Build net-to-pins mapping
        net_pins: dict[str, list[str]] = {}
        for fp in self._footprints:
            for pad in fp.pads:
                if pad.net_name and pad.net_number != 0:
                    net_pins.setdefault(pad.net_name, []).append(
                        f"{fp.reference}-{pad.number}"
                    )

        # Generate net definitions
        for net_name, pins in sorted(net_pins.items()):
            lines.append(f"    (net {_dsn_quote(net_name)}")
            lines.append("      (pins")
            for pin in sorted(pins):
                lines.append(f"        {_dsn_quote(pin)}")
            lines.append("      )")
            lines.append("    )")

        # Net class
        lines.append("    (class kicad_default")
        for net_name in sorted(net_pins.keys()):
            lines.append(f"      {_dsn_quote(net_name)}")
        trace_um = mm_to_um(self._default_trace_width)
        clearance_um = mm_to_um(self._default_clearance)
        lines.append(f"      (circuit")
        lines.append(f"        (use_via {_dsn_quote(self._via_padstack_name)})")
        lines.append(f"      )")
        lines.append(f"      (rule")
        lines.append(f"        (width {trace_um:.1f})")
        lines.append(f"        (clearance {clearance_um:.1f})")
        lines.append(f"      )")
        lines.append("    )")

        lines.append("  )")
        return "\n".join(lines)

    def _generate_wiring(self) -> str | None:
        """Generate the (wiring ...) section for pre-existing routes."""
        if not self._segments and not self._vias:
            return None

        lines: list[str] = []
        lines.append("  (wiring")

        for seg in self._segments:
            net_name = self._nets.get(seg.net, "")
            if not net_name:
                continue
            dsn_layer = KICAD_TO_DSN_LAYER.get(seg.layer, seg.layer)
            if dsn_layer not in [KICAD_TO_DSN_LAYER.get(l, l) for l in self._layers]:
                continue
            w_um = mm_to_um(seg.width)
            x1 = mm_to_um(seg.x1)
            y1 = mm_to_um(seg.y1)
            x2 = mm_to_um(seg.x2)
            y2 = mm_to_um(seg.y2)
            lines.append(f"    (wire")
            lines.append(f"      (path {_dsn_quote(dsn_layer)} {w_um:.1f} {x1:.1f} {y1:.1f} {x2:.1f} {y2:.1f})")
            lines.append(f"      (net {_dsn_quote(net_name)})")
            lines.append(f"      (type protect)")
            lines.append(f"    )")

        for via in self._vias:
            net_name = self._nets.get(via.net, "")
            if not net_name:
                continue
            x = mm_to_um(via.x)
            y = mm_to_um(via.y)
            lines.append(f"    (via")
            lines.append(f"      {_dsn_quote(self._via_padstack_name)} {x:.1f} {y:.1f}")
            lines.append(f"      (net {_dsn_quote(net_name)})")
            lines.append(f"      (type protect)")
            lines.append(f"    )")

        lines.append("  )")
        return "\n".join(lines)

    @property
    def layers(self) -> list[str]:
        """Return the list of copper layers found in the PCB."""
        self._load()
        return list(self._layers)

    @property
    def nets(self) -> dict[int, str]:
        """Return the net map {number: name}."""
        self._load()
        return dict(self._nets)

    @property
    def footprints(self) -> list[FootprintInfo]:
        """Return the list of footprints."""
        self._load()
        return list(self._footprints)


# -- Helpers --

def _get_str(node: SExp) -> str | None:
    """Get the string value of an atom node."""
    if node.value is not None:
        return str(node.value)
    if node.name is not None:
        return node.name
    return None


def _get_int(node: SExp) -> int | None:
    """Get the integer value of an atom node."""
    val = node.value
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            return None
    if node.name is not None:
        try:
            return int(node.name)
        except ValueError:
            return None
    return None


def _get_float(node: SExp) -> float | None:
    """Get the float value of an atom node."""
    val = node.value
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return None
    if node.name is not None:
        try:
            return float(node.name)
        except ValueError:
            return None
    return None


def _dsn_quote(s: str) -> str:
    """Quote a string for DSN format."""
    # DSN uses double quotes and requires quoting for names with special chars
    if not s or re.search(r'[\s()"\']', s):
        return f'"{s}"'
    return s


def _sanitize_name(name: str) -> str:
    """Sanitize a name for DSN, replacing characters that cause parsing issues."""
    # DSN allows most characters inside quotes, so just return as-is
    return name

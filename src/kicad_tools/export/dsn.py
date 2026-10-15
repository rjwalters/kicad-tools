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
    for line in exporter.omissions:
        print("not exported:", line)

Rules come from the board's own files (issue #6594): net classes and their
assignments from the sibling ``.kicad_pro`` (through
:class:`~kicad_tools.router.board_clearance_rules.BoardClearanceRules`),
board minima from its ``design_settings.rules``, and the unconditional
clearance / track-width / via-diameter floors of the sibling ``.kicad_dru``.
Copper pours become ``(plane ...)`` and rule areas ``(keepout ...)``,
``(wire_keepout ...)`` or ``(via_keepout ...)``.  Anything Specctra cannot
express is listed in :attr:`KiCadToDSNExporter.omissions` rather than
silently dropped or approximated.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.layers import kicad_read_via_layers, via_node_type
from ..schema.pcb import Zone, _is_footprint_tag
from ..sexp.parser import SExp, parse_file, parse_string

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
    net_name: str = ""


@dataclass
class ViaInfo:
    """Pre-existing via."""

    x: float
    y: float
    size: float
    drill: float
    net: int
    layers: tuple[str, str] = ("F.Cu", "B.Cu")
    net_name: str = ""


#: The DSN name of KiCad's ``Default`` net class.  External routers (and
#: ``tests/test_dsn_export.py``) expect this spelling.
DSN_DEFAULT_CLASS = "kicad_default"

#: ``.kicad_dru`` constraints that become DSN width / clearance / via floors.
_DRU_FLOOR_CONSTRAINTS = ("clearance", "track_width", "via_diameter")

#: Conditions that hold for *every* item a floor constraint applies to (a
#: DSN has no microvias), so the rule is as good as unconditional for it.
#: Compared after removing whitespace and case-folding.
_TRIVIAL_CONDITIONS: dict[str, frozenset[str]] = {
    "track_width": frozenset({"a.type=='track'"}),
    "via_diameter": frozenset(
        {"a.type=='via'", "a.type=='via'&&a.via_type!='micro'", "a.via_type!='micro'"}
    ),
}

#: ``.kicad_pro`` ``design_settings.rules`` minima Specctra cannot express.
#: ``min_copper_edge_clearance`` is not listed: it is carried by insetting the
#: exported ``(boundary ...)`` (issue #6784).
_INEXPRESSIBLE_PROJECT_MINIMA = (
    "min_hole_clearance",
    "min_hole_to_hole",
    "min_through_hole_diameter",
    "min_via_annular_width",
    "min_microvia_diameter",
    "min_microvia_drill",
)


@dataclass
class NetClassInfo:
    """One net class as exported to the DSN (all values in mm)."""

    name: str  # DSN class name (``kicad_default`` for KiCad's ``Default``)
    track_width: float
    clearance: float
    via_diameter: float
    via_drill: float
    nets: list[str] = field(default_factory=list)


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
        self._net_map: dict[str, int] = {}  # net name -> (possibly synthetic) id
        self._classes: dict[str, NetClassInfo] = {}  # KiCad class name -> info
        self._net_class: dict[str, str] = {}  # net name -> KiCad class name
        self._planes: list[tuple[str, str, list[tuple[float, float]]]] = []
        self._keepouts: list[tuple[str, str, list[tuple[float, float]]]] = []
        self._omissions: list[str] = []
        self._edge_clearance: float = 0.0  # mm, .kicad_pro min_copper_edge_clearance
        self._via_padstack_name: str = ""

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
        self._extract_net_classes()
        self._extract_zones()

    def _omit(self, message: str) -> None:
        if message not in self._omissions:
            self._omissions.append(message)

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
        """Extract net definitions for both KiCad net-reference dialects.

        KiCad 7/8 boards carry a top-level ``(net N "name")`` table; KiCad
        9/10 boards may reference nets by name only (``(net "+3V3")``) and
        have no table at all.  The router's resolver (#4983) handles both:
        authored ids are kept and name-only nets get a stable synthetic id.
        """
        from ..router.io import _build_net_number_map

        assert self._pcb is not None
        try:
            text = self.pcb_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        self._net_map = _build_net_number_map(text) if text else {}
        for name, number in self._net_map.items():
            self._nets.setdefault(number, name)
        # Keep any top-level declaration the map did not reach (e.g. a
        # numeric-only table entry); it can only add names, never rename.
        for child in self._pcb.children:
            if not hasattr(child, "name") or child.name != "net":
                continue
            if len(child.children) >= 2:
                net_num = _get_int(child.children[0])
                net_name = _get_str(child.children[1])
                if net_num is not None and net_name is not None:
                    self._nets.setdefault(net_num, net_name)

    def _resolve_net(self, net_node: SExp | None) -> tuple[int, str]:
        """``(number, name)`` of an inline ``(net ...)`` reference, any dialect."""
        from ..router.io import _resolve_pad_net

        if net_node is None:
            return 0, ""
        return _resolve_pad_net(net_node.to_string(compact=True), self._net_map)

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
            if not hasattr(child, "name") or not _is_footprint_tag(child.name):
                continue

            # First child is the library footprint name
            fp_lib = _get_str(child.children[0]) if child.children else "unknown"

            ref = _footprint_reference(child)

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

        # Net (numeric+name, name-only or numeric-only)
        net_num, net_name = self._resolve_net(pad_node.find("net"))

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

    # -- net classes and rules (issue #6594) ---------------------------------

    def _extract_net_classes(self) -> None:
        """Build the DSN net classes from the board's ``.kicad_pro``/``.kicad_dru``.

        Class membership uses :class:`BoardClearanceRules` (schema 3-5
        assignments and patterns); per-class width and via sizes come from
        :func:`~kicad_tools.core.project_file.get_netclass_definitions`.
        Board minima and unconditional DRU floors raise every class value.
        """
        from ..core.project_file import get_netclass_definitions
        from ..router.board_clearance_rules import BoardClearanceRules

        rules = BoardClearanceRules.from_board(self.pcb_path)
        project = rules._project
        net_names = sorted(
            {pad.net_name for fp in self._footprints for pad in fp.pads if pad.net_name}
            | {seg.net_name for seg in self._segments if seg.net_name}
            | {via.net_name for via in self._vias if via.net_name}
        )

        default = NetClassInfo(
            name=DSN_DEFAULT_CLASS,
            track_width=self._default_trace_width,
            clearance=self._default_clearance,
            via_diameter=self._default_via_size,
            via_drill=self._default_via_drill,
        )
        self._classes = {"Default": default}

        if project is None:
            self._omit(
                f"no {self.pcb_path.with_suffix('.kicad_pro').name} beside the PCB: "
                f"net classes use exporter defaults (width {default.track_width} mm, "
                f"clearance {default.clearance} mm, via "
                f"{default.via_diameter}/{default.via_drill} mm)"
            )
        else:
            # get_netclass_definitions fills in a missing Default; keep that
            # write off the project BoardClearanceRules holds.
            definitions = [
                d
                for d in get_netclass_definitions(copy.deepcopy(project))
                if isinstance(d, dict) and isinstance(d.get("name"), str) and d.get("name")
            ]
            by_name = {d["name"]: d for d in definitions}
            default_def = by_name.get("Default", {})
            default.track_width = _mm(default_def.get("track_width"), default.track_width)
            default.via_diameter = _mm(default_def.get("via_diameter"), default.via_diameter)
            default.via_drill = _mm(default_def.get("via_drill"), default.via_drill)
            default.clearance = rules.default_class_clearance()
            for name, entry in by_name.items():
                if name == "Default":
                    continue
                self._classes[name] = NetClassInfo(
                    name=name,
                    track_width=_mm(entry.get("track_width"), default.track_width),
                    clearance=rules.class_clearance.get(name, default.clearance),
                    via_diameter=_mm(entry.get("via_diameter"), default.via_diameter),
                    via_drill=_mm(entry.get("via_drill"), default.via_drill),
                )

        for net in net_names:
            self._net_class[net] = self._class_of(net, rules, project)

        self._apply_board_minima(rules, project)
        self._apply_dru_floors(rules)

        for net in net_names:
            self._classes[self._net_class[net]].nets.append(net)

        # Default keeps feeding the global (rule ...) and the legacy accessors.
        self._default_trace_width = default.track_width
        self._default_clearance = default.clearance
        self._default_via_size = default.via_diameter
        self._default_via_drill = default.via_drill

    def _class_of(self, net: str, rules: Any, project: dict[str, Any] | None) -> str:
        """The single DSN class a net is exported in (KiCad class name)."""
        classes = [c for c in (rules.netclasses_of(net) or ("Default",)) if c in self._classes]
        if not classes:
            return "Default"
        if len(classes) == 1:
            return classes[0]
        chosen = classes[0]
        if project is not None:
            from ..core.project_clearance import (
                ProjectClearanceError,
                resolve_project_clearances,
            )

            try:
                source = resolve_project_clearances(project, [net])[net].source_class
            except (ProjectClearanceError, KeyError):
                source = None
            if source in self._classes:
                chosen = source
        self._omit(
            f"net {net!r} is in several net classes ({', '.join(classes)}); "
            f"a DSN net has one class, exported in {chosen!r}"
        )
        return chosen

    def _apply_board_minima(self, rules: Any, project: dict[str, Any] | None) -> None:
        """Raise every class to the ``.kicad_pro`` board minima Specctra can express."""
        section: dict[str, Any] = {}
        if project is not None:
            board = project.get("board")
            settings = board.get("design_settings") if isinstance(board, dict) else None
            raw = settings.get("rules") if isinstance(settings, dict) else None
            if isinstance(raw, dict):
                section = raw
        floors = {
            "clearance": rules.min_clearance,
            "track_width": _mm(section.get("min_track_width"), 0.0) or None,
            "via_diameter": _mm(section.get("min_via_diameter"), 0.0) or None,
            "via_drill": _mm(section.get("min_via_hole"), 0.0) or None,
        }
        for attr, floor in floors.items():
            if floor is None:
                continue
            for info in self._classes.values():
                setattr(info, attr, max(getattr(info, attr), floor))
        self._edge_clearance = _mm(section.get("min_copper_edge_clearance"), 0.0)
        for key in _INEXPRESSIBLE_PROJECT_MINIMA:
            value = _mm(section.get(key), 0.0)
            if value > 0:
                self._omit(f".kicad_pro rule {key} {value:g} mm: no Specctra equivalent")

    def _apply_dru_floors(self, rules: Any) -> None:
        """Apply unconditional ``.kicad_dru`` floors; list every other rule."""
        dru_path = self.pcb_path.with_suffix(".kicad_dru")
        if rules.dru_error is not None:
            self._omit(f"{dru_path.name} not applied: {rules.dru_error}")
            return
        for name, condition, layer, constraints, severity in _read_dru(dru_path):
            if severity == "ignore":
                self._omit(f".kicad_dru rule {name!r}: (severity ignore) has no DSN form")
                continue
            dropped: list[str] = []
            for keyword, minimum in constraints:
                if keyword in _DRU_FLOOR_CONSTRAINTS and minimum is not None:
                    trivial = condition is not None and (
                        re.sub(r"\s+", "", condition).casefold()
                        in _TRIVIAL_CONDITIONS.get(keyword, frozenset())
                    )
                    if layer is None and (condition is None or trivial):
                        for info in self._classes.values():
                            current = getattr(info, keyword)
                            setattr(info, keyword, max(current, minimum))
                        continue
                dropped.append(keyword)
            if dropped:
                why = []
                if condition is not None:
                    why.append("conditional")
                if layer is not None:
                    why.append(f"layer {layer}")
                reason = f" ({', '.join(why)})" if why else ""
                self._omit(
                    f".kicad_dru rule {name!r}{reason}: {', '.join(dropped)} not expressible in DSN"
                )

    # -- zones and rule areas (issue #6594) -----------------------------------

    def _expand_zone_layers(self, zone: Zone) -> list[str]:
        """The board copper layers a zone covers (wildcards expanded)."""
        specs = zone.layers or ([zone.layer] if zone.layer else [])
        out: list[str] = []
        for spec in specs:
            if spec == "*.Cu":
                matched = list(self._layers)
            elif spec == "F&B.Cu":
                matched = [lay for lay in ("F.Cu", "B.Cu") if lay in self._layers]
            else:
                matched = [spec] if spec in self._layers else []
            out.extend(lay for lay in matched if lay not in out)
        return out

    def _extract_zones(self) -> None:
        """Copper pours -> planes; rule areas -> keepouts; report the rest."""
        assert self._pcb is not None
        for child in self._pcb.children:
            if not hasattr(child, "name"):
                continue
            if child.name == "zone":
                self._add_zone(child)
            elif _is_footprint_tag(child.name):
                for sub in child.children:
                    if hasattr(sub, "name") and sub.name == "zone":
                        self._omit(
                            f"zone in footprint {_footprint_reference(child)}: "
                            "footprint-embedded zones are not exported"
                        )

    def _add_zone(self, node: SExp) -> None:
        zone = Zone.from_sexp(node)
        label = zone.name or zone.net_name or zone.uuid or "unnamed"
        kind = "rule area" if zone.keepout is not None else "zone"
        layers = self._expand_zone_layers(zone)
        if not layers:
            return  # no copper layer of this board: nothing a router sees
        outlines = [p for p in node.children if hasattr(p, "name") and p.name == "polygon"]
        if not outlines:
            self._omit(f"{kind} {label!r}: no outline polygon")
            return
        if len(outlines) > 1:
            self._omit(
                f"{kind} {label!r}: {len(outlines) - 1} extra outline(s)/hole(s) not exported"
            )
        pts_node = outlines[0].find("pts")
        if pts_node is not None and any(
            hasattr(c, "name") and c.name == "arc" for c in pts_node.children
        ):
            self._omit(f"{kind} {label!r}: outline contains arcs; not approximated, not exported")
            return
        polygon = zone.polygon
        if len(polygon) < 3:
            self._omit(f"{kind} {label!r}: outline has fewer than 3 points")
            return

        if zone.keepout is None:
            net = zone.net_name or (self._nets.get(zone.net_number, "") if zone.net_number else "")
            if not net:
                self._omit(f"zone {label!r}: copper zone without a net has no DSN plane form")
                return
            for layer in layers:
                self._planes.append((net, layer, polygon))
            return

        ko = zone.keepout
        if not ko.tracks_allowed and not ko.vias_allowed:
            tag = "keepout"
        elif not ko.tracks_allowed:
            tag = "wire_keepout"
        elif not ko.vias_allowed:
            tag = "via_keepout"
        else:
            tag = ""
        restricted = [
            name
            for name, allowed in (
                ("pads", ko.pads_allowed),
                ("copperpour", ko.copperpour_allowed),
                ("footprints", ko.footprints_allowed),
            )
            if not allowed
        ]
        if restricted:
            self._omit(
                f"rule area {label!r}: {', '.join(restricted)} restriction(s) "
                "have no DSN routing form"
            )
        if not tag:
            return
        for layer in layers:
            self._keepouts.append((tag, layer, polygon))

    def _parse_segment(self, node: SExp) -> SegmentInfo | None:
        """Parse a segment S-expression."""
        start = node.find("start")
        end = node.find("end")
        width_node = node.find("width")
        layer_node = node.find("layer")
        net_node = node.find("net")

        if not all([start, end, width_node, layer_node, net_node]):
            return None
        net_num, net_name = self._resolve_net(net_node)

        return SegmentInfo(
            x1=_get_float(start.children[0]) or 0.0,
            y1=_get_float(start.children[1]) or 0.0,
            x2=_get_float(end.children[0]) or 0.0,
            y2=_get_float(end.children[1]) or 0.0,
            width=_get_float(width_node.children[0]) or 0.25,
            layer=_get_str(layer_node.children[0]) or "F.Cu",
            net=net_num,
            net_name=net_name,
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

        net_num, net_name = self._resolve_net(net_node)
        layers = ("F.Cu", "B.Cu")
        if layers_node and len(layers_node.children) >= 2:
            l1 = _get_str(layers_node.children[0]) or "F.Cu"
            l2 = _get_str(layers_node.children[1]) or "B.Cu"
            # Issue #6614: a keyword-less via is a through via to KiCad,
            # whatever its pair says -- export the span KiCad reads.
            read, _ = kicad_read_via_layers((l1, l2), via_node_type(node))
            layers = (read[0], read[-1])

        return ViaInfo(
            x=_get_float(at_node.children[0]) or 0.0,
            y=_get_float(at_node.children[1]) or 0.0,
            size=_get_float(size_node.children[0]) or 0.8,
            drill=_get_float(drill_node.children[0]) if drill_node and drill_node.children else 0.4,
            net=net_num,
            layers=layers,
            net_name=net_name,
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
                    lines.append(
                        f"      (shape (rect {_dsn_quote(dsn_layer)} {-w / 2:.1f} {-h / 2:.1f} {w / 2:.1f} {h / 2:.1f}))"
                    )
                else:
                    # oval/circle -> use rect approximation for DSN
                    lines.append(
                        f"      (shape (rect {_dsn_quote(dsn_layer)} {-w / 2:.1f} {-h / 2:.1f} {w / 2:.1f} {h / 2:.1f}))"
                    )
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
            lines.append(
                f"      (shape (rect {_dsn_quote(dsn_layer)} {-w / 2:.1f} {-h / 2:.1f} {w / 2:.1f} {h / 2:.1f}))"
            )
            lines.append("      (attach off)")

        lines.append("    )")
        return "\n".join(lines)

    def _via_padstack_name_for(self, diameter_mm: float) -> str:
        size = mm_to_um(diameter_mm)
        return f"Via[0-{len(self._layers) - 1}]_Pad{size:.0f}_um"

    def _build_via_padstack(self, diameter_mm: float | None = None) -> str:
        """Build a via padstack (the Default class's when no diameter is given)."""
        if diameter_mm is None:
            diameter_mm = self._default_via_size
        size = mm_to_um(diameter_mm)
        name = self._via_padstack_name_for(diameter_mm)

        lines: list[str] = []
        lines.append(f"    (padstack {_dsn_quote(name)}")
        for layer in self._layers:
            dsn_layer = KICAD_TO_DSN_LAYER.get(layer, layer)

            lines.append(f"      (shape (circle {_dsn_quote(dsn_layer)} {size:.1f}))")
        lines.append("      (attach off)")
        lines.append("    )")

        if diameter_mm == self._default_via_size:
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
        parts.append('    (host_cad "KiCad\'s Pcbnew")')
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

    def _boundary_points(self) -> list[tuple[float, float]]:
        """The outline exported as ``(boundary ...)``, inset by the edge clearance.

        Specctra has no copper-to-edge rule, but a router never places copper
        outside the boundary, so insetting the polygon by
        ``min_copper_edge_clearance`` carries it as plain geometry. Pads that
        already sit inside the band are fixed placement: they stay where they
        are and are listed as an omission rather than routed through. When the
        inset cannot be computed the original outline is exported and the
        clearance is listed as not carried.
        """
        outline = self._board_outline
        if not outline or self._edge_clearance <= 0:
            return outline
        try:
            from shapely.geometry import Polygon

            ring: list[tuple[float, float]] = []
            for pt in outline:  # gr_line outlines repeat each shared vertex
                if not ring or pt != ring[-1]:
                    ring.append(pt)
            if len(ring) > 1 and ring[0] == ring[-1]:
                ring.pop()
            poly = Polygon(ring)
            inset = poly.buffer(-self._edge_clearance, join_style=2) if poly.is_valid else None
        except (ImportError, ValueError):
            inset = None
        if inset is None or inset.is_empty or inset.geom_type != "Polygon":
            self._omit(
                f".kicad_pro rule min_copper_edge_clearance {self._edge_clearance:g} mm: "
                "board outline cannot be inset; boundary exported uninset"
            )
            return outline
        in_band = sorted(
            f"{fp.reference}.{pad.number}"
            for fp in self._footprints
            for pad in fp.pads
            if not inset.buffer(1e-6).contains(self._pad_center(fp, pad))
        )
        if in_band:
            self._omit(
                f"{len(in_band)} pad(s) sit inside the {self._edge_clearance:g} mm "
                f"copper-to-edge band (fixed placement): {', '.join(in_band)}"
            )
        return list(inset.exterior.coords)[:-1]

    @staticmethod
    def _pad_center(fp: FootprintInfo, pad: PadInfo) -> Any:
        import math

        from shapely.geometry import Point

        a = math.radians(-fp.rotation)  # KiCad rotation is CCW on a y-down board
        return Point(
            fp.x + pad.x * math.cos(a) - pad.y * math.sin(a),
            fp.y + pad.x * math.sin(a) + pad.y * math.cos(a),
        )

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

        # Board boundary, inset by min_copper_edge_clearance (issue #6784)
        boundary = self._boundary_points()
        if boundary:
            lines.append("    (boundary")
            lines.append("      (path pcb 0")
            for x, y in boundary:
                lines.append(f"        {mm_to_um(x):.1f} {mm_to_um(y):.1f}")
            # Close the polygon
            x0, y0 = boundary[0]
            lines.append(f"        {mm_to_um(x0):.1f} {mm_to_um(y0):.1f}")
            lines.append("      )")
            lines.append("    )")

        # Design rules
        clearance_um = mm_to_um(self._default_clearance)
        trace_um = mm_to_um(self._default_trace_width)
        lines.append("    (rule")
        lines.append(f"      (width {trace_um:.1f})")
        lines.append(f"      (clearance {clearance_um:.1f})")
        lines.append(f"      (clearance {clearance_um:.1f} (type default_smd))")
        lines.append(f"      (clearance {clearance_um:.1f} (type smd_smd))")
        lines.append("    )")

        # Copper pours and rule areas (issue #6594)
        for net, layer, polygon in self._planes:
            dsn_layer = KICAD_TO_DSN_LAYER.get(layer, layer)
            lines.append(f"    (plane {_dsn_quote(net)}")
            lines.append(_dsn_polygon(dsn_layer, polygon))
            lines.append("    )")
        for tag, layer, polygon in self._keepouts:
            dsn_layer = KICAD_TO_DSN_LAYER.get(layer, layer)
            lines.append(f'    ({tag} ""')
            lines.append(_dsn_polygon(dsn_layer, polygon))
            lines.append("    )")

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
            lines.append(
                f"      (place {_dsn_quote(fp.reference)} {x_um:.1f} {y_um:.1f} {side} {rot:.1f})"
            )
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
                lines.append(
                    f"      (pin {_dsn_quote(ps_name)} {_dsn_quote(pad.number)} {px_um:.1f} {py_um:.1f})"
                )

            lines.append("    )")

        # Via padstacks: the Default class's, one per other class via
        # diameter, and one per pre-existing via size (issue #6594).
        padstack_defs[self._via_padstack_name_for(self._default_via_size)] = (
            self._build_via_padstack()
        )
        diameters = [info.via_diameter for info in self._classes.values()]
        diameters += [via.size for via in self._vias]
        for diameter in diameters:
            name = self._via_padstack_name_for(diameter)
            if name not in padstack_defs:
                padstack_defs[name] = self._build_via_padstack(diameter)

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
                # Keyed on the net *name*: a name-only (KiCad 9/10) net is
                # still a net; only an empty name means "no net" (#6594).
                if pad.net_name:
                    net_pins.setdefault(pad.net_name, []).append(f"{fp.reference}-{pad.number}")

        # Generate net definitions
        for net_name, pins in sorted(net_pins.items()):
            lines.append(f"    (net {_dsn_quote(net_name)}")
            lines.append("      (pins")
            for pin in sorted(pins):
                lines.append(f"        {_dsn_quote(pin)}")
            lines.append("      )")
            lines.append("    )")

        # Net classes: Default (as kicad_default) always, others when they
        # hold a net of this network.
        exported = set(net_pins)
        for kicad_name, info in self._classes.items():
            members = sorted(n for n in info.nets if n in exported)
            if kicad_name != "Default" and not members:
                continue
            dsn_name = DSN_DEFAULT_CLASS if kicad_name == "Default" else info.name
            lines.append(f"    (class {_dsn_quote(dsn_name)}")
            for net_name in members:
                lines.append(f"      {_dsn_quote(net_name)}")
            via_name = self._via_padstack_name_for(info.via_diameter)
            lines.append("      (circuit")
            lines.append(f"        (use_via {_dsn_quote(via_name)})")
            lines.append("      )")
            lines.append("      (rule")
            lines.append(f"        (width {mm_to_um(info.track_width):.1f})")
            lines.append(f"        (clearance {mm_to_um(info.clearance):.1f})")
            lines.append("      )")
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
            net_name = seg.net_name or self._nets.get(seg.net, "")
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
            lines.append("    (wire")
            lines.append(
                f"      (path {_dsn_quote(dsn_layer)} {w_um:.1f} {x1:.1f} {y1:.1f} {x2:.1f} {y2:.1f})"
            )
            lines.append(f"      (net {_dsn_quote(net_name)})")
            lines.append("      (type protect)")
            lines.append("    )")

        for via in self._vias:
            net_name = via.net_name or self._nets.get(via.net, "")
            if not net_name:
                continue
            x = mm_to_um(via.x)
            y = mm_to_um(via.y)
            via_name = self._via_padstack_name_for(via.size)
            lines.append("    (via")
            lines.append(f"      {_dsn_quote(via_name)} {x:.1f} {y:.1f}")
            lines.append(f"      (net {_dsn_quote(net_name)})")
            lines.append("      (type protect)")
            lines.append("    )")

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

    @property
    def net_classes(self) -> dict[str, NetClassInfo]:
        """The exported net classes, keyed by KiCad class name (``Default``...)."""
        self._load()
        return dict(self._classes)

    @property
    def omissions(self) -> list[str]:
        """Board constraints the DSN does not carry, one human-readable line each.

        Informational: the DSN is still written.  Covers a missing
        ``.kicad_pro``, ``.kicad_dru`` rules and project minima Specctra
        cannot express, and zones/rule areas (or parts of them) not
        exported (issue #6594).
        """
        self._load()
        return list(self._omissions)


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


def _dsn_polygon(dsn_layer: str, polygon: list[tuple[float, float]]) -> str:
    """``(polygon <layer> 0 x y ...)`` for a closed outline, in um."""
    coords = " ".join(f"{mm_to_um(x):.1f} {mm_to_um(y):.1f}" for x, y in polygon)
    x0, y0 = polygon[0]
    if polygon[-1] != polygon[0]:
        coords += f" {mm_to_um(x0):.1f} {mm_to_um(y0):.1f}"
    return f"      (polygon {_dsn_quote(dsn_layer)} 0 {coords})"


def _footprint_reference(node: SExp) -> str:
    """A footprint's reference: KiCad 8+ ``(property "Reference" ..)`` first,
    then the legacy ``(fp_text reference ..)``; ``"?"`` when neither exists."""
    legacy = ""
    for sub in node.children:
        if not hasattr(sub, "name") or len(sub.children) < 2:
            continue
        if sub.name == "property" and _get_str(sub.children[0]) == "Reference":
            return _get_str(sub.children[1]) or "?"
        if sub.name == "fp_text" and _get_str(sub.children[0]) == "reference" and not legacy:
            legacy = _get_str(sub.children[1]) or ""
    return legacy or "?"


def _mm(raw: object, fallback: float) -> float:
    """A ``.kicad_pro`` length (number or ``"0.2mm"``-style) in mm, else ``fallback``."""
    from ..router.board_clearance_rules import _parse_length_mm

    if isinstance(raw, bool):
        return fallback
    value = _parse_length_mm(raw)
    return value if value is not None and value >= 0 else fallback


def _read_dru(
    dru_path: Path,
) -> list[tuple[str, str | None, str | None, list[tuple[str, float | None]], str | None]]:
    """Every rule of a ``.kicad_dru`` with all of its constraint keywords.

    ``(name, condition, layer, [(constraint, min_mm | None)], severity)`` in
    file order.  Unlike :func:`~kicad_tools.router.board_clearance_rules.parse_dru`,
    which keeps only the clearance-type constraints its gate evaluates, this
    keeps every keyword so the exporter can list what it cannot express.
    Validity (a file ``kicad-cli`` would discard) is judged by ``parse_dru``
    through :attr:`BoardClearanceRules.dru_error`; a missing or unparsable
    file gives ``[]``.
    """
    from ..router.board_clearance_rules import _parse_length_mm

    try:
        text = dru_path.read_text(encoding="utf-8", errors="replace")
        tree = parse_string("(rules " + text + ")")
    except Exception:  # missing file, or parse_dru already reported it
        return []
    out = []
    for rule in tree.find_all("rule"):
        atoms = rule.get_atoms()
        name = str(atoms[0]) if atoms else ""
        cond_node = rule.find_child("condition")
        condition = cond_node.get_first_atom() if cond_node is not None else None
        layer_node = rule.find_child("layer")
        layer = layer_node.get_first_atom() if layer_node is not None else None
        sev_node = rule.find_child("severity")
        severity = sev_node.get_first_atom() if sev_node is not None else None
        constraints: list[tuple[str, float | None]] = []
        for constraint in rule.find_all("constraint"):
            c_atoms = constraint.get_atoms()
            if not c_atoms:
                continue
            minimum = constraint.find_child("min")
            value = _parse_length_mm(minimum.get_first_atom()) if minimum is not None else None
            constraints.append((str(c_atoms[0]), value))
        out.append(
            (
                name,
                None if condition is None else str(condition),
                None if layer is None else str(layer),
                constraints,
                None if severity is None else str(severity).casefold(),
            )
        )
    return out


def _sanitize_name(name: str) -> str:
    """Sanitize a name for DSN, replacing characters that cause parsing issues."""
    # DSN allows most characters inside quotes, so just return as-is
    return name

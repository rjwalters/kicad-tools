"""
Specctra SES (Session) import for KiCad PCB files.

Parses a Specctra session (``.ses``) written by a Freerouting-compatible
router and merges the routed traces back into a KiCad ``.kicad_pcb`` file.

The standard session shape (Specctra DSN/SES specification) is::

    (session NAME
      (base_design DSN)
      (placement (resolution um 10) ...)
      (was_is ...)
      (routes
        (resolution um 10)
        (parser (string_quote ") ...)
        (library_out
          (padstack "Via[0-1]_800:400_um"
            (shape (circle F.Cu 8000 0 0))
            (shape (circle B.Cu 8000 0 0))))
        (network_out
          (net GND
            (wire (path F.Cu 2500 1410000 887700 1450000 887700))
            (via "Via[0-1]_800:400_um" 1450000 887700)))))

Wires and vias nest under the ``(net NAME ...)`` that owns them, and every
coordinate and width is an integer count of ``1/resolution`` of the declared
unit -- ``(resolution um 10)`` makes ``1410000`` equal to 141.0 mm. A legacy,
non-standard shape that carries ``(net NAME)`` *inside* each wire/via is still
accepted (issue #6593).

The Y axis is **not** flipped: sessions routed from
:class:`~kicad_tools.export.dsn.KiCadToDSNExporter` output come back in
KiCad's own frame.

Usage::

    from kicad_tools.export.ses import SESToKiCadImporter

    importer = SESToKiCadImporter("board.ses")
    importer.merge_into("board.kicad_pcb", "board_routed.kicad_pcb")
    for skip in importer.skipped:
        print(skip)
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.core.layers import COPPER_LAYER_ORDER, is_declared_copper_layer, resolve_via_type
from kicad_tools.sexp import SExp, parse_string

logger = logging.getLogger(__name__)


def um_to_mm(um: float) -> float:
    """Convert micrometers to millimeters."""
    return um / 1000.0


#: Millimetres per one declared Specctra unit.
_UNIT_TO_MM: dict[str, float] = {
    "um": 0.001,
    "mil": 0.0254,
    "mm": 1.0,
    "cm": 10.0,
    "inch": 25.4,
}

_COPPER_INDEX: dict[str, int] = {name: i for i, name in enumerate(COPPER_LAYER_ORDER)}

#: ``Via[i-j]`` index range in a padstack name, as written by our DSN exporter.
_VIA_INDEX_RANGE = re.compile(r"Via\[(\d+)-(\d+)\]")

#: Specctra ``(string_quote C)`` parser directive. The lone quote character
#: is not a string opener, so it must be removed before s-expression parsing.
_STRING_QUOTE = re.compile(r"\(\s*string_quote\s+([^\s()])\s*\)")

#: Wire shapes the importer does not convert (reported in ``skipped``).
_UNSUPPORTED_WIRE_SHAPES = ("polyline_path", "qarc", "rect", "circle", "polygon")


@dataclass
class SESWire:
    """A routed wire from the SES file."""

    net_name: str
    layer: str
    width: float  # in raw SES units (1/resolution of the declared unit)
    points: list[tuple[float, float]] = field(default_factory=list)  # raw SES units


@dataclass
class SESVia:
    """A via from the SES file."""

    net_name: str
    padstack_name: str
    x: float  # in raw SES units
    y: float  # in raw SES units


@dataclass(frozen=True)
class SESPadstack:
    """A via padstack from the session's ``(library_out ...)`` section."""

    name: str
    layers: tuple[str, ...]  # copper layers named by the padstack's shapes
    diameter: float | None  # largest circle diameter, raw SES units


@dataclass(frozen=True)
class SESSkip:
    """A session element that was not imported, and why."""

    kind: str  # "wire", "via", "was_is", ...
    net_name: str
    reason: str

    def __str__(self) -> str:
        net = f" on net {self.net_name!r}" if self.net_name else ""
        return f"{self.kind}{net}: {self.reason}"


class SESToKiCadImporter:
    """Import a Specctra SES file and merge routes into a KiCad PCB.

    Args:
        ses_path: Path to the .ses file from Freerouting or a compatible router.
    """

    def __init__(self, ses_path: str | Path) -> None:
        self.ses_path = Path(ses_path)
        self._wires: list[SESWire] = []
        self._vias: list[SESVia] = []
        self._padstacks: dict[str, SESPadstack] = {}
        self._resolution: float = 1.0  # SES units per declared unit
        self._resolution_unit: str = "um"
        self._mm_per_unit: float = _UNIT_TO_MM["um"]
        self._parse_skipped: list[SESSkip] = []
        self._merge_skipped: list[SESSkip] = []
        self._imported_wires = 0
        self._imported_vias = 0
        self._parsed = False

    def parse(self) -> None:
        """Parse the SES file and extract wires and vias.

        Raises:
            ValueError: If the session declares a resolution unit or a
                ``string_quote`` character this importer cannot interpret.
        """
        content = self.ses_path.read_text(encoding="utf-8")
        self._wires = []
        self._vias = []
        self._padstacks = {}
        self._parse_skipped = []
        self._parse_ses_content(content)
        self._parsed = True
        logger.info(
            "Parsed SES: %d wires, %d vias (resolution %s %g)",
            len(self._wires),
            len(self._vias),
            self._resolution_unit,
            self._resolution,
        )

    # -- Parsing --

    def _parse_ses_content(self, content: str) -> None:
        """Walk the parsed session tree for resolution, padstacks, wires and vias."""
        root = parse_string(_strip_string_quote(content))

        routes = root.get("routes") if root.name == "session" else None
        if routes is None:
            routes = root if root.name == "routes" else root.find("routes")
        if routes is None:
            logger.warning("No (routes ...) section found in SES file")
            return

        self._read_resolution(root, routes)

        library_out = routes.get("library_out")
        if library_out is not None:
            for node in library_out.children:
                if node.name == "padstack":
                    padstack = _parse_padstack(node)
                    if padstack is not None:
                        self._padstacks[padstack.name] = padstack

        was_is = root.get("was_is")
        if was_is is not None:
            for node in was_is.children:
                if node.name:
                    self._parse_skipped.append(
                        SESSkip("was_is", "", f"pin swap ({node.name}) is not supported")
                    )

        self._walk_routes(routes, None)

    def _read_resolution(self, root: SExp, routes: SExp) -> None:
        """Read ``(resolution UNIT N)``: routes first, then session, then placement."""
        candidates = [routes.get("resolution"), root.get("resolution")]
        placement = root.get("placement")
        if placement is not None:
            candidates.append(placement.get("resolution"))
        node = next((n for n in candidates if n is not None), None)
        if node is None:
            logger.warning("SES declares no (resolution ...); assuming 1 unit = 1 um")
            return
        atoms = [c for c in node.children if c.is_atom]
        if len(atoms) < 2:
            raise ValueError("SES (resolution ...) needs a unit and a count")
        unit = _atom_text(atoms[0]).lower()
        if unit not in _UNIT_TO_MM:
            raise ValueError(
                f"SES resolution unit {unit!r} is not supported "
                f"(expected one of {', '.join(sorted(_UNIT_TO_MM))})"
            )
        count = atoms[1].value
        if not isinstance(count, (int, float)) or count <= 0:
            raise ValueError(f"SES resolution count must be a positive number, got {count!r}")
        self._resolution_unit = unit
        self._resolution = float(count)
        self._mm_per_unit = _UNIT_TO_MM[unit] / float(count)

    def _walk_routes(self, node: SExp, net_name: str | None) -> None:
        """Collect wires/vias, inheriting the enclosing ``(net NAME ...)``."""
        for child in node.children:
            name = child.name
            if name == "wire":
                self._add_wire(child, net_name)
            elif name == "via":
                self._add_via(child, net_name)
            elif name == "network_out":
                self._walk_routes(child, net_name)
            elif name == "net":
                atoms = [c for c in child.children if c.is_atom]
                if atoms and any(not c.is_atom for c in child.children):
                    self._walk_routes(child, _atom_text(atoms[0]))
            elif name in ("test_points", "supply_pin"):
                self._parse_skipped.append(
                    SESSkip(name, net_name or "", f"({name} ...) is not supported")
                )

    def _add_wire(self, node: SExp, parent_net: str | None) -> None:
        wire = self._parse_wire_node(node, parent_net)
        if wire is not None:
            self._wires.append(wire)

    def _add_via(self, node: SExp, parent_net: str | None) -> None:
        via = self._parse_via_node(node, parent_net)
        if via is not None:
            self._vias.append(via)

    def _parse_wire_node(self, node: SExp, parent_net: str | None) -> SESWire | None:
        """Parse ``(wire (path LAYER WIDTH x y x y ...) [(net NAME)] ...)``."""
        net_name = _inline_net(node) or parent_net
        if not net_name:
            self._parse_skipped.append(SESSkip("wire", "", "no enclosing or inline (net ...)"))
            return None

        path = node.get("path")
        if path is None:
            shape = next(
                (c.name for c in node.children if c.name in _UNSUPPORTED_WIRE_SHAPES), None
            )
            reason = (
                f"wire shape ({shape} ...) is not supported"
                if shape
                else "wire has no (path ...) shape"
            )
            self._parse_skipped.append(SESSkip("wire", net_name, reason))
            return None

        atoms = [c for c in path.children if c.is_atom]
        if len(atoms) < 2:
            self._parse_skipped.append(SESSkip("wire", net_name, "path has no layer/width"))
            return None
        layer = _atom_text(atoms[0])
        nums = [_atom_number(c) for c in atoms[1:]]
        if any(v is None for v in nums):
            self._parse_skipped.append(
                SESSkip("wire", net_name, f"path on {layer} has non-numeric values")
            )
            return None
        width = nums[0] or 0.0
        coords = [v for v in nums[1:] if v is not None]
        if len(coords) % 2 or len(coords) < 4:
            self._parse_skipped.append(
                SESSkip("wire", net_name, f"path on {layer} has fewer than two points")
            )
            return None
        points = [(coords[i], coords[i + 1]) for i in range(0, len(coords), 2)]
        return SESWire(net_name=net_name, layer=layer, width=width, points=points)

    def _parse_via_node(self, node: SExp, parent_net: str | None) -> SESVia | None:
        """Parse ``(via PADSTACK x y [(net NAME)] ...)``."""
        net_name = _inline_net(node) or parent_net
        atoms = [c for c in node.children if c.is_atom]
        if not net_name:
            self._parse_skipped.append(SESSkip("via", "", "no enclosing or inline (net ...)"))
            return None
        x = _atom_number(atoms[1]) if len(atoms) >= 3 else None
        y = _atom_number(atoms[2]) if len(atoms) >= 3 else None
        if x is None or y is None:
            self._parse_skipped.append(SESSkip("via", net_name, "via has no padstack/x/y"))
            return None
        return SESVia(net_name=net_name, padstack_name=_atom_text(atoms[0]), x=x, y=y)

    # -- Merging --

    def merge_into(
        self,
        pcb_path: str | Path,
        output_path: str | Path | None = None,
        *,
        default_trace_width: float = 0.25,
        default_via_size: float = 0.8,
        default_via_drill: float = 0.4,
    ) -> str:
        """Merge SES routes into a KiCad PCB file.

        The destination file is **always** written (issue #6593): with no
        importable routes it receives the unchanged board and a warning is
        logged. Elements that could not be imported are listed in
        :attr:`skipped`.

        Args:
            pcb_path: Path to the original .kicad_pcb file.
            output_path: Path for the output file. If None, overwrites the
                input file.
            default_trace_width: Trace width in mm used when the SES width is 0.
            default_via_size: Via pad diameter in mm, used when the session
                padstack carries no circle diameter.
            default_via_drill: Via drill diameter in mm (Specctra does not
                carry drill sizes).

        Returns:
            The merged PCB content as a string.
        """
        if not self._parsed:
            self.parse()

        pcb_path = Path(pcb_path)
        pcb_content = pcb_path.read_text(encoding="utf-8")

        route_sexp = self._generate_kicad_routes(
            pcb_content, default_trace_width, default_via_size, default_via_drill
        )

        if route_sexp:
            merged = _merge_sexp_into_pcb(pcb_content, route_sexp)
        else:
            logger.warning("No routes imported from %s; writing the board unchanged", self.ses_path)
            merged = pcb_content

        for skip in self.skipped:
            logger.warning("SES import skipped %s", skip)

        dest = Path(output_path) if output_path is not None else pcb_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(merged, encoding="utf-8")
        logger.info("Wrote merged PCB to %s", dest)

        return merged

    def _generate_kicad_routes(
        self,
        pcb_content: str,
        default_trace_width: float,
        default_via_size: float,
        default_via_drill: float,
    ) -> str:
        """Convert SES wires/vias to KiCad S-expression segments/vias."""
        self._merge_skipped = []
        self._imported_wires = 0
        self._imported_vias = 0

        authored, known_names = _board_nets(pcb_content)
        copper_layers = _board_copper_layers(pcb_content)
        scale = self._mm_per_unit
        parts: list[str] = []

        def net_ref(name: str) -> str | None:
            if name in authored:
                return f"(net {authored[name]})"
            if name in known_names:
                return f"(net {_kicad_quote(name)})"
            return None

        for wire in self._wires:
            ref = net_ref(wire.net_name)
            if ref is None:
                self._merge_skipped.append(SESSkip("wire", wire.net_name, "net not on the board"))
                continue
            if wire.layer not in copper_layers:
                self._merge_skipped.append(
                    SESSkip(
                        "wire", wire.net_name, f"layer {wire.layer!r} is not a board copper layer"
                    )
                )
                continue

            width_mm = wire.width * scale
            if width_mm <= 0:
                width_mm = default_trace_width

            for (x1, y1), (x2, y2) in zip(wire.points, wire.points[1:], strict=False):
                parts.append(
                    f"  (segment (start {x1 * scale:.4f} {y1 * scale:.4f}) "
                    f"(end {x2 * scale:.4f} {y2 * scale:.4f}) "
                    f"(width {width_mm:.4f}) "
                    f'(layer "{wire.layer}") '
                    f"{ref} "
                    f'(uuid "{uuid.uuid4()}"))'
                )
            self._imported_wires += 1

        for via in self._vias:
            ref = net_ref(via.net_name)
            if ref is None:
                self._merge_skipped.append(SESSkip("via", via.net_name, "net not on the board"))
                continue
            span = self._via_span(via.padstack_name, copper_layers)
            if isinstance(span, str):
                self._merge_skipped.append(SESSkip("via", via.net_name, span))
                continue

            padstack = self._padstacks.get(via.padstack_name)
            size_mm = (
                padstack.diameter * scale
                if padstack is not None and padstack.diameter
                else default_via_size
            )
            keyword, layers = resolve_via_type(span)
            if keyword is not None:
                logger.warning(
                    "SES via %r at (%.4f, %.4f) spans %s-%s; writing (via %s ...)",
                    via.padstack_name,
                    via.x * scale,
                    via.y * scale,
                    layers[0],
                    layers[1],
                    keyword,
                )
            head = f"(via {keyword} " if keyword else "(via "
            parts.append(
                f"  {head}(at {via.x * scale:.4f} {via.y * scale:.4f}) "
                f"(size {size_mm:.4f}) "
                f"(drill {default_via_drill}) "
                f'(layers "{layers[0]}" "{layers[1]}") '
                f"{ref} "
                f'(uuid "{uuid.uuid4()}"))'
            )
            self._imported_vias += 1

        return "\n".join(parts)

    def _via_span(self, padstack_name: str, copper_layers: list[str]) -> tuple[str, str] | str:
        """Return the via's (top, bottom) copper layers, or a skip reason string.

        The session ``library_out`` padstack wins; otherwise the ``Via[i-j]``
        index range in the name (our DSN exporter's convention) is read against
        the board's front-to-back copper order. A span that cannot be
        determined is never defaulted to ``F.Cu``/``B.Cu``.
        """
        padstack = self._padstacks.get(padstack_name)
        if padstack is not None:
            unknown = [layer for layer in padstack.layers if layer not in copper_layers]
            if unknown:
                return (
                    f"padstack {padstack_name!r} names non-board copper layer(s) "
                    f"{', '.join(unknown)}"
                )
            if not padstack.layers:
                return f"padstack {padstack_name!r} has no copper shapes"
            ordered = sorted(padstack.layers, key=copper_layers.index)
            if len(ordered) < 2:
                return f"padstack {padstack_name!r} spans a single layer ({ordered[0]})"
            return ordered[0], ordered[-1]

        match = _VIA_INDEX_RANGE.search(padstack_name)
        if match is None:
            return f"padstack {padstack_name!r} is not in library_out and has no Via[i-j] range"
        lo, hi = sorted((int(match.group(1)), int(match.group(2))))
        if hi >= len(copper_layers) or lo == hi:
            return (
                f"padstack {padstack_name!r} range [{lo}-{hi}] does not fit the "
                f"board's {len(copper_layers)} copper layers"
            )
        # The DSN exporter only ever writes full-span ``Via[0-(n-1)]`` names, but
        # its layer list (signal/power layers named in KICAD_TO_DSN_LAYER) can be
        # shorter than the board's copper, so the index is not comparable to
        # ``copper_layers``.  Only full-span ranges are recoverable; treat them
        # as F.Cu-B.Cu and skip anything else rather than invent a blind/buried via.
        from .dsn import KICAD_TO_DSN_LAYER

        exportable = [layer for layer in copper_layers if layer in KICAD_TO_DSN_LAYER]
        if lo == 0 and hi in (len(copper_layers) - 1, len(exportable) - 1):
            return copper_layers[0], copper_layers[-1]
        return (
            f"padstack {padstack_name!r} range [{lo}-{hi}] is ambiguous against the "
            f"board's {len(copper_layers)} copper layers (not a full-span via)"
        )

    # -- Results --

    @property
    def wires(self) -> list[SESWire]:
        """Return parsed wires."""
        return list(self._wires)

    @property
    def vias(self) -> list[SESVia]:
        """Return parsed vias."""
        return list(self._vias)

    @property
    def padstacks(self) -> dict[str, SESPadstack]:
        """Return the session's ``library_out`` padstacks by name."""
        return dict(self._padstacks)

    @property
    def mm_per_unit(self) -> float:
        """Millimetres per raw SES coordinate unit (from ``resolution``)."""
        return self._mm_per_unit

    @property
    def skipped(self) -> list[SESSkip]:
        """Session elements that were not imported (parse- and merge-time)."""
        return [*self._parse_skipped, *self._merge_skipped]

    @property
    def imported_wires(self) -> int:
        """Wires written by the last :meth:`merge_into`."""
        return self._imported_wires

    @property
    def imported_vias(self) -> int:
        """Vias written by the last :meth:`merge_into`."""
        return self._imported_vias


# -- Helpers --


def _strip_string_quote(content: str) -> str:
    """Remove ``(string_quote ")`` so the lone quote is not read as a string opener."""

    def repl(match: re.Match[str]) -> str:
        if match.group(1) != '"':
            raise ValueError(f"SES string_quote {match.group(1)!r} is not supported (only '\"' is)")
        return "(string_quote)"

    return _STRING_QUOTE.sub(repl, content)


def _atom_text(node: SExp) -> str:
    """Text of an atom, keeping numeric-looking names verbatim (``007``)."""
    original = getattr(node, "_original_str", None)
    if original is not None:
        return str(original)
    return "" if node.value is None else str(node.value)


def _atom_number(node: SExp) -> float | None:
    """Numeric value of an atom, or ``None`` when it is not a number."""
    value = node.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _inline_net(node: SExp) -> str | None:
    """The legacy inline ``(net NAME)`` child of a wire/via, if any."""
    for child in node.children:
        if child.name == "net":
            atoms = [c for c in child.children if c.is_atom]
            if atoms:
                return _atom_text(atoms[0])
    return None


def _parse_padstack(node: SExp) -> SESPadstack | None:
    """Read a ``(padstack NAME (shape (circle LAYER DIA ...)) ...)`` node."""
    atoms = [c for c in node.children if c.is_atom]
    if not atoms:
        return None
    layers: list[str] = []
    diameter: float | None = None
    for shape in node.children:
        if shape.name != "shape":
            continue
        for geom in shape.children:
            if geom.is_atom:
                continue
            geom_atoms = [c for c in geom.children if c.is_atom]
            if not geom_atoms:
                continue
            layer = _atom_text(geom_atoms[0])
            if layer not in layers:
                layers.append(layer)
            if geom.name == "circle" and len(geom_atoms) >= 2:
                value = geom_atoms[1].value
                if isinstance(value, (int, float)):
                    diameter = max(diameter or 0.0, float(value))
    return SESPadstack(name=_atom_text(atoms[0]), layers=tuple(layers), diameter=diameter)


def _kicad_quote(text: str) -> str:
    """Quote a KiCad string atom."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _board_nets(pcb_content: str) -> tuple[dict[str, int], set[str]]:
    """Return ``(authored numeric ids by name, every net name on the board)``.

    Handles both KiCad net-reference dialects (#4983): a name with an authored
    ``(net N "NAME")`` id is emitted numerically; a name that only appears as
    KiCad-10 ``(net "NAME")`` is emitted by name, never under a synthetic id.
    """
    from kicad_tools.router.io import _iter_net_references

    authored: dict[str, int] = {}
    names: set[str] = set()
    for net_num, net_name in _iter_net_references(pcb_content):
        if not net_name:
            continue
        names.add(net_name)
        if net_num is not None and net_num > 0 and net_name not in authored:
            authored[net_name] = net_num
    return authored, names


def _board_copper_layers(pcb_content: str) -> list[str]:
    """The board's copper layers in front-to-back order (``F.Cu`` .. ``B.Cu``)."""
    match = re.search(r"\(layers\s", pcb_content)
    if match is None:
        return ["F.Cu", "B.Cu"]
    block = _extract_balanced(pcb_content, match.start()) or ""
    found = {
        name
        for name, kind in re.findall(r'\(\s*\d+\s+"([^"]+)"\s+([A-Za-z_]+)', block)
        if is_declared_copper_layer(name, kind)
    }
    if not found:
        return ["F.Cu", "B.Cu"]
    return sorted(found, key=_COPPER_INDEX.__getitem__)


def _extract_balanced(text: str, start: int) -> str | None:
    """Extract a balanced parenthesized expression starting at 'start'."""
    if start >= len(text) or text[start] != "(":
        return None

    depth = 0
    in_string = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == '"':
                in_string = False
            elif ch == "\\":
                i += 1  # skip escaped char
        else:
            if ch == '"':
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _build_net_map(pcb_content: str) -> dict[str, int]:
    """Build a mapping of net name -> authored net number from PCB content."""
    return _board_nets(pcb_content)[0]


def _merge_sexp_into_pcb(pcb_content: str, route_sexp: str) -> str:
    """Merge route S-expressions into PCB content.

    Inserts the routes before the final closing parenthesis.
    This follows the same pattern as merge_routes_into_pcb() in router/io.py.
    """
    if not route_sexp:
        return pcb_content

    content = pcb_content.rstrip()
    if content.endswith(")"):
        content = content[:-1].rstrip()

    result = content + "\n\n"
    result += route_sexp + "\n"
    result += ")\n"

    return result

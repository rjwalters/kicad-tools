"""
Netlist parsing and export operations.

Provides classes for parsing KiCad netlist files and extracting
connectivity information.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.sexp import SExp, parse_string

logger = logging.getLogger(__name__)


@dataclass
class ComponentPin:
    """A pin on a component."""

    number: str
    name: str
    pin_type: str = ""


@dataclass
class NetlistComponent:
    """A component in the netlist."""

    reference: str
    value: str
    footprint: str
    lib_id: str
    sheet_path: str = ""
    properties: dict[str, str] = field(default_factory=dict)
    pins: list[ComponentPin] = field(default_factory=list)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> NetlistComponent:
        """Parse component from netlist S-expression."""
        ref = ""
        value = ""
        footprint = ""
        lib_id = ""
        sheet_path = ""
        properties: dict[str, str] = {}

        if ref_node := sexp.find("ref"):
            ref = ref_node.get_string(0) or ""

        if value_node := sexp.find("value"):
            value = value_node.get_string(0) or ""

        if fp_node := sexp.find("footprint"):
            footprint = fp_node.get_string(0) or ""

        if lib_node := sexp.find("libsource"):
            if part := lib_node.find("part"):
                lib_id = part.get_string(0) or ""
            elif lib_node.get_string(1):
                lib_id = lib_node.get_string(1) or ""

        if (sheet_node := sexp.find("sheetpath")) and (names := sheet_node.find("names")):
            sheet_path = names.get_string(0) or ""

        # Parse properties
        for prop in sexp.find_all("property"):
            prop_name = prop.get_string(0)
            prop_value = prop.get_string(1)
            if prop_name and prop_value:
                properties[prop_name] = prop_value

        return cls(
            reference=ref,
            value=value,
            footprint=footprint,
            lib_id=lib_id,
            sheet_path=sheet_path,
            properties=properties,
        )


@dataclass
class NetNode:
    """A connection point in a net."""

    reference: str
    pin: str
    pin_function: str = ""
    pin_type: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> NetNode:
        """Parse node from netlist S-expression."""
        ref = ""
        pin = ""
        pin_function = ""
        pin_type = ""

        # Reference is in (ref "...") child node or as first positional atom
        if ref_node := sexp.find("ref"):
            ref = ref_node.get_string(0) or ""
        else:
            ref = sexp.get_string(0) or ""

        if pin_node := sexp.find("pin"):
            pin = pin_node.get_string(0) or ""

        if func_node := sexp.find("pinfunction"):
            pin_function = func_node.get_string(0) or ""

        if type_node := sexp.find("pintype"):
            pin_type = type_node.get_string(0) or ""

        return cls(
            reference=ref,
            pin=pin,
            pin_function=pin_function,
            pin_type=pin_type,
        )


@dataclass
class NetlistNet:
    """A net (electrical connection) in the netlist."""

    code: int
    name: str
    nodes: list[NetNode] = field(default_factory=list)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> NetlistNet:
        """Parse net from netlist S-expression."""
        code = 0
        name = ""
        nodes: list[NetNode] = []

        if code_node := sexp.find("code"):
            code = code_node.get_int(0) or 0

        if name_node := sexp.find("name"):
            name = name_node.get_string(0) or ""

        for node in sexp.find_all("node"):
            nodes.append(NetNode.from_sexp(node))

        return cls(code=code, name=name, nodes=nodes)

    @property
    def connection_count(self) -> int:
        """Number of pins connected to this net."""
        return len(self.nodes)


@dataclass
class SheetInfo:
    """Information about a schematic sheet."""

    number: int
    name: str
    path: str
    title: str = ""
    source: str = ""


@dataclass
class Netlist:
    """Parsed netlist data."""

    source_file: str = ""
    tool: str = ""
    date: str = ""
    sheets: list[SheetInfo] = field(default_factory=list)
    components: list[NetlistComponent] = field(default_factory=list)
    nets: list[NetlistNet] = field(default_factory=list)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Netlist:
        """Parse complete netlist from S-expression."""
        netlist = cls()

        if sexp.tag != "export":
            raise ValueError(f"Expected 'export' root, got '{sexp.tag}'")

        # Parse design info
        if design := sexp.find("design"):
            if source := design.find("source"):
                netlist.source_file = source.get_string(0) or ""
            if tool := design.find("tool"):
                netlist.tool = tool.get_string(0) or ""
            if date := design.find("date"):
                netlist.date = date.get_string(0) or ""

            # Parse sheets
            for sheet in design.find_all("sheet"):
                sheet_num = 0
                sheet_name = ""
                sheet_path = ""
                title = ""
                source_str = ""

                if num := sheet.find("number"):
                    sheet_num = num.get_int(0) or 0
                if name := sheet.find("name"):
                    sheet_name = name.get_string(0) or ""
                if tstamps := sheet.find("tstamps"):
                    sheet_path = tstamps.get_string(0) or ""

                if tb := sheet.find("title_block"):
                    if t := tb.find("title"):
                        title = t.get_string(0) or ""
                    if s := tb.find("source"):
                        source_str = s.get_string(0) or ""

                netlist.sheets.append(
                    SheetInfo(
                        number=sheet_num,
                        name=sheet_name,
                        path=sheet_path,
                        title=title,
                        source=source_str,
                    )
                )

        # Parse components
        if components := sexp.find("components"):
            for comp in components.find_all("comp"):
                netlist.components.append(NetlistComponent.from_sexp(comp))

        # Parse nets
        if nets_node := sexp.find("nets"):
            for net in nets_node.find_all("net"):
                netlist.nets.append(NetlistNet.from_sexp(net))

        return netlist

    @classmethod
    def load(cls, path: str | Path) -> Netlist:
        """Load and parse a netlist file."""
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        sexp = parse_string(text)
        return cls.from_sexp(sexp)

    def get_component(self, reference: str) -> NetlistComponent | None:
        """Get component by reference designator."""
        for comp in self.components:
            if comp.reference == reference:
                return comp
        return None

    def get_net(self, name: str) -> NetlistNet | None:
        """Get net by name."""
        for net in self.nets:
            if net.name == name:
                return net
        return None

    def get_component_nets(self, reference: str) -> list[NetlistNet]:
        """Get all nets connected to a component."""
        result = []
        for net in self.nets:
            for node in net.nodes:
                if node.reference == reference:
                    result.append(net)
                    break
        return result

    def get_net_by_pin(self, reference: str, pin: str) -> NetlistNet | None:
        """Get the net connected to a specific pin."""
        for net in self.nets:
            for node in net.nodes:
                if node.reference == reference and node.pin == pin:
                    return net
        return None

    @property
    def power_nets(self) -> list[NetlistNet]:
        """Get power nets (containing power pins)."""
        power_nets = []
        for net in self.nets:
            for node in net.nodes:
                if "power" in node.pin_type.lower():
                    power_nets.append(net)
                    break
        return power_nets

    def find_single_pin_nets(self) -> list[NetlistNet]:
        """Find nets with only one connection (potential issues).

        Single-pin nets often indicate unconnected pins or incomplete
        connectivity in the schematic.

        Returns:
            List of nets with exactly one connection.
        """
        return [net for net in self.nets if net.connection_count == 1]

    def find_floating_pins(self) -> list[tuple[str, str, str]]:
        """Find pins that appear on nets with only one connection.

        These are pins that are connected to a net but have no other
        connections, suggesting they may be unintentionally floating.

        Returns:
            List of tuples (reference, pin, net_name) for floating pins.
        """
        floating = []
        for net in self.nets:
            if len(net.nodes) == 1:
                node = net.nodes[0]
                floating.append((node.reference, node.pin, net.name))
        return floating

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "source": self.source_file,
            "tool": self.tool,
            "date": self.date,
            "sheets": [
                {
                    "number": s.number,
                    "name": s.name,
                    "path": s.path,
                    "title": s.title,
                    "source": s.source,
                }
                for s in self.sheets
            ],
            "components": [
                {
                    "reference": c.reference,
                    "value": c.value,
                    "footprint": c.footprint,
                    "lib_id": c.lib_id,
                    "sheet_path": c.sheet_path,
                    "properties": c.properties,
                }
                for c in self.components
            ],
            "nets": [
                {
                    "code": n.code,
                    "name": n.name,
                    "connections": n.connection_count,
                    "nodes": [
                        {
                            "reference": node.reference,
                            "pin": node.pin,
                            "pin_function": node.pin_function,
                            "pin_type": node.pin_type,
                        }
                        for node in n.nodes
                    ],
                }
                for n in self.nets
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    def summary(self) -> dict:
        """Get netlist summary statistics."""
        # Group components by prefix
        by_prefix: dict[str, int] = defaultdict(int)
        for comp in self.components:
            prefix = "".join(c for c in comp.reference if c.isalpha())
            by_prefix[prefix] += 1

        # Categorize nets
        power_names = {"GND", "PGND", "AGND", "VCC", "VDD", "VBUS"}
        power_nets = [n for n in self.nets if n.name.startswith("+") or n.name in power_names]

        return {
            "source_file": self.source_file,
            "tool": self.tool,
            "date": self.date,
            "sheet_count": len(self.sheets),
            "component_count": len(self.components),
            "components_by_type": dict(by_prefix),
            "net_count": len(self.nets),
            "power_net_count": len(power_nets),
            "signal_net_count": len(self.nets) - len(power_nets),
        }



def _count_hierarchy_sheets(sch_path: Path, visited: set[Path] | None = None) -> int:
    """Count the total number of sheets in a schematic hierarchy.

    Recursively traverses all sub-sheet references and returns the total
    count including the root sheet itself.  Circular references are
    detected and skipped.

    Args:
        sch_path: Path to the root .kicad_sch file.
        visited: Set of already-visited resolved paths (internal).

    Returns:
        Total number of sheets (1 for a flat schematic).
    """
    if visited is None:
        visited = set()

    resolved = sch_path.resolve()
    if resolved in visited:
        return 0
    visited.add(resolved)

    if not sch_path.exists():
        return 0

    count = 1  # This sheet
    sub_filenames = _get_sheet_filenames(sch_path)
    for filename in sub_filenames:
        sub_path = sch_path.parent / filename
        count += _count_hierarchy_sheets(sub_path, visited)
    return count


@dataclass
class _SheetEntry:
    """Parsed sheet entry with filename and pin information."""

    filename: str
    pin_names: list[str] = field(default_factory=list)
    pin_positions: list[tuple[float, float]] = field(default_factory=list)


def _get_sheet_filenames(sch_path: Path) -> list[str]:
    """Extract sub-sheet filenames from a schematic file.

    Parses the raw S-expression tree to find ``(sheet ...)`` entries and
    extracts the ``Sheetfile`` property from each.

    Args:
        sch_path: Path to the .kicad_sch file.

    Returns:
        List of sub-sheet filenames (relative to the schematic directory).
    """
    return [entry.filename for entry in _get_sheet_entries(sch_path)]


def _get_sheet_entries(sch_path: Path) -> list[_SheetEntry]:
    """Extract sub-sheet entries including pin information.

    Parses the raw S-expression tree to find ``(sheet ...)`` entries and
    extracts both the ``Sheetfile`` property and any ``(pin ...)`` children
    (sheet pins) from each.

    Sheet pins represent the interface between a parent sheet and its child
    sheet.  Each pin has a name that corresponds to a ``hierarchical_label``
    in the child schematic.  The pin's position in the parent sheet is used
    to determine which parent net it connects to.

    Args:
        sch_path: Path to the .kicad_sch file.

    Returns:
        List of :class:`_SheetEntry` objects with filename and pin data.
    """
    doc = parse_string(sch_path.read_text(encoding="utf-8"))
    entries: list[_SheetEntry] = []
    for child in doc.children:
        if getattr(child, "name", None) == "sheet" or getattr(child, "tag", None) == "sheet":
            filename = ""
            pin_names: list[str] = []
            pin_positions: list[tuple[float, float]] = []

            # Extract sheet position for computing absolute pin positions
            sheet_x, sheet_y = 0.0, 0.0
            at_node = child.find("at")
            if at_node:
                atoms = at_node.get_atoms()
                if len(atoms) >= 2:
                    sheet_x = round(float(atoms[0]), 2)
                    sheet_y = round(float(atoms[1]), 2)

            # Look for (property "Sheetfile" "filename.kicad_sch")
            for prop in child.find_all("property"):
                prop_name = prop.get_string(0)
                if prop_name == "Sheetfile":
                    fname = prop.get_string(1)
                    if fname:
                        filename = fname

            # Extract (pin "name" direction (at x y angle) ...) children.
            # Pin positions in sheet entries are relative to the sheet origin.
            for pin_node in child.find_all("pin"):
                pin_name = pin_node.get_string(0)
                if pin_name:
                    pin_names.append(pin_name)
                    pin_at = pin_node.find("at")
                    if pin_at:
                        pin_atoms = pin_at.get_atoms()
                        if len(pin_atoms) >= 2:
                            px = round(float(pin_atoms[0]), 2)
                            py = round(float(pin_atoms[1]), 2)
                            pin_positions.append((px, py))
                        else:
                            pin_positions.append((sheet_x, sheet_y))
                    else:
                        pin_positions.append((sheet_x, sheet_y))

            if filename:
                entries.append(
                    _SheetEntry(
                        filename=filename,
                        pin_names=pin_names,
                        pin_positions=pin_positions,
                    )
                )
    return entries


def _collect_hierarchy_components(
    sch_path: Path,
    sheet_path: str,
    visited: set[Path] | None = None,
) -> tuple[list[NetlistComponent], dict[str, list]]:
    """Recursively collect components and nets from a schematic hierarchy.

    Loads the schematic at *sch_path*, collects its components and
    per-sheet netlist, then recurses into any ``(sheet ...)`` sub-sheets.
    Global labels with matching names across sheets are merged into the
    same net.  Hierarchical labels in child sheets are merged with the
    corresponding parent sheet pin's net using the parent's wiring context.
    Circular references (the same file appearing again in the traversal)
    are detected and skipped with a warning.

    Args:
        sch_path: Absolute path to a ``.kicad_sch`` file.
        sheet_path: Hierarchical sheet path string for this level
            (e.g. ``"/"`` for root).
        visited: Set of already-visited *resolved* paths used for
            circular-reference detection.  Callers should pass ``None``;
            the function creates the set internally.

    Returns:
        A ``(components, net_dict)`` tuple where *components* is a flat
        list of :class:`NetlistComponent` from all sheets and *net_dict*
        maps net names to lists of :class:`PinRef`-like objects (each
        with ``symbol_ref`` and ``pin`` attributes).
    """
    from kicad_tools.schematic.models import Schematic

    if visited is None:
        visited = set()

    resolved = sch_path.resolve()
    if resolved in visited:
        logger.warning("Circular sheet reference detected, skipping: %s", sch_path)
        return [], {}
    visited.add(resolved)

    if not sch_path.exists():
        logger.warning("Sub-sheet file not found, skipping: %s", sch_path)
        return [], {}

    # Load single sheet
    sch = Schematic.load(str(sch_path))

    # Collect components from this sheet
    components: list[NetlistComponent] = []
    for sym in sch.symbols:
        footprint = ""
        if hasattr(sym, "footprint") and sym.footprint:
            footprint = sym.footprint
        elif hasattr(sym, "properties"):
            footprint = sym.properties.get("Footprint", "")

        lib_id = ""
        if sym.symbol_def and hasattr(sym.symbol_def, "lib_id"):
            lib_id = sym.symbol_def.lib_id

        components.append(
            NetlistComponent(
                reference=sym.reference,
                value=sym.value,
                footprint=footprint,
                lib_id=lib_id,
                sheet_path=sheet_path,
            )
        )

    # Extract per-sheet connectivity
    net_dict: dict[str, list] = {}
    try:
        sheet_nets = sch.extract_netlist()
        for net_name, pins in sheet_nets.items():
            net_dict.setdefault(net_name, []).extend(pins)
    except Exception:
        logger.warning("Failed to extract netlist from %s, skipping connectivity", sch_path)

    # Build a map from sheet pin position -> net name in the parent sheet.
    # This is used to resolve hierarchical label connections: the parent's
    # connectivity graph tells us what net each sheet pin is on.
    parent_connectivity, parent_net_names, _ = sch._build_connectivity_graph()

    def _find_parent(p: tuple) -> tuple:
        if p not in parent_connectivity:
            parent_connectivity[p] = p
        if parent_connectivity[p] != p:
            parent_connectivity[p] = _find_parent(parent_connectivity[p])
        return parent_connectivity[p]

    def _net_name_at(pos: tuple) -> str | None:
        """Find the net name for a position in the parent connectivity graph."""
        root = _find_parent(pos)
        # Check all points in the same connected component for net names
        for point, names in parent_net_names.items():
            if _find_parent(point) == root and names:
                return names[0]
        return None

    # Recurse into sub-sheets with hierarchical label net merging
    sheet_entries = _get_sheet_entries(sch_path)
    parent_dir = sch_path.parent
    for entry in sheet_entries:
        sub_path = parent_dir / entry.filename
        sub_sheet_path = f"{sheet_path}{entry.filename}/"
        sub_components, sub_nets = _collect_hierarchy_components(
            sub_path, sub_sheet_path, visited
        )
        components.extend(sub_components)

        # Build a mapping from hierarchical label name -> parent net name
        # using the sheet pin positions in the parent's connectivity graph.
        hlabel_to_parent_net: dict[str, str] = {}
        for pin_name, pin_pos in zip(entry.pin_names, entry.pin_positions):
            parent_net = _net_name_at(pin_pos)
            if parent_net:
                hlabel_to_parent_net[pin_name] = parent_net

        # Merge child nets into parent net_dict.  When a child net name
        # matches a hierarchical label that has a different net name in
        # the parent context, unify them under the parent's net name.
        for net_name, pins in sub_nets.items():
            target_name = hlabel_to_parent_net.get(net_name, net_name)
            net_dict.setdefault(target_name, []).extend(pins)

        # Also ensure any parent-side pins already in net_dict under the
        # hierarchical label name are merged if we renamed the net.
        for hlabel_name, parent_net in hlabel_to_parent_net.items():
            if hlabel_name != parent_net and hlabel_name in net_dict:
                net_dict.setdefault(parent_net, []).extend(net_dict.pop(hlabel_name))

    return components, net_dict


def build_netlist_from_schematic(sch_path: str | Path) -> Netlist:
    """
    Build a Netlist from a schematic using pure Python extraction.

    This provides a fallback when kicad-cli is unavailable or crashes.
    Uses the Schematic.extract_netlist() method to analyze connectivity
    directly from the schematic file.

    For hierarchical schematics the function recursively loads all
    sub-sheets referenced via ``(sheet ...)`` entries and merges their
    components and nets.  Global labels with matching names across
    sheets are unified into the same net.

    Args:
        sch_path: Path to .kicad_sch file

    Returns:
        Parsed Netlist object

    Raises:
        FileNotFoundError: If schematic not found
        ValueError: If schematic parsing fails
    """
    sch_path = Path(sch_path)
    if not sch_path.exists():
        raise FileNotFoundError(f"Schematic not found: {sch_path}")

    # Recursively collect components and nets from all sheets
    components, net_dict = _collect_hierarchy_components(sch_path, "/")

    # Build nets from merged connectivity data
    nets: list[NetlistNet] = []
    for code, (net_name, pins) in enumerate(net_dict.items(), 1):
        nodes = [NetNode(reference=pin.symbol_ref, pin=pin.pin) for pin in pins]
        nets.append(
            NetlistNet(
                code=code,
                name=net_name,
                nodes=nodes,
            )
        )

    return Netlist(
        source_file=str(sch_path),
        tool="kicad-tools (Python fallback)",
        components=components,
        nets=nets,
    )


def export_netlist(
    sch_path: str | Path,
    output_path: str | Path | None = None,
    kicad_cli: str | Path | None = None,
    format: str = "kicadsexpr",
    fallback: bool = True,
) -> Netlist:
    """
    Export netlist from schematic using kicad-cli, with optional Python fallback.

    Args:
        sch_path: Path to .kicad_sch file
        output_path: Output path for netlist (optional, uses temp)
        kicad_cli: Path to kicad-cli (auto-detected if not provided)
        format: Netlist format (kicadsexpr, kicadxml)
        fallback: If True, use pure Python extraction when kicad-cli fails

    Returns:
        Parsed Netlist object

    Raises:
        FileNotFoundError: If kicad-cli not found (and fallback=False)
        RuntimeError: If export fails (and fallback=False)
    """
    sch_path = Path(sch_path)
    if not sch_path.exists():
        raise FileNotFoundError(f"Schematic not found: {sch_path}")

    # Try to find kicad-cli
    if kicad_cli is None:
        cli = find_kicad_cli()
        if cli is None:
            if fallback:
                logger.warning("kicad-cli not found, using pure Python netlist extraction")
                return build_netlist_from_schematic(sch_path)
            raise FileNotFoundError("kicad-cli not found. Install KiCad 8.")
        kicad_cli = cli
    else:
        kicad_cli = Path(kicad_cli)

    if output_path is None:
        output_path = sch_path.parent / f"{sch_path.stem}-netlist.kicad_net"
    else:
        output_path = Path(output_path)

    # Delete existing netlist to ensure fresh export (prevents stale cache issues)
    if output_path.exists():
        output_path.unlink()

    cmd = [
        str(kicad_cli),
        "sch",
        "export",
        "netlist",
        "--format",
        format,
        "--output",
        str(output_path),
        str(sch_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        # Check for kicad-cli crash (SIGSEGV = exit code 139)
        if result.returncode == 139:
            if fallback:
                logger.warning("kicad-cli crashed (SIGSEGV), using pure Python netlist extraction")
                return build_netlist_from_schematic(sch_path)
            raise RuntimeError(
                "kicad-cli crashed (SIGSEGV). This may be caused by a problematic "
                "symbol in the schematic. Try removing recently added symbols or "
                "exporting the netlist manually from KiCad GUI. "
                "See: https://gitlab.com/kicad/code/kicad/-/issues"
            )

        # Check for other non-zero exit codes
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else f"Exit code {result.returncode}"
            if fallback:
                logger.warning(
                    f"kicad-cli failed ({error_msg}), using pure Python netlist extraction"
                )
                return build_netlist_from_schematic(sch_path)
            raise RuntimeError(f"kicad-cli failed: {error_msg}")

        if not output_path.exists():
            if fallback:
                logger.warning("kicad-cli produced no output, using pure Python netlist extraction")
                return build_netlist_from_schematic(sch_path)
            raise RuntimeError(result.stderr or "Netlist export produced no output")

        netlist = Netlist.load(output_path)

        # Validate completeness: kicad-cli sometimes omits sub-sheets
        # from hierarchical schematics.  Compare the sheet count in the
        # exported netlist against the hierarchy declared in the .kicad_sch
        # files.  When the output is incomplete, fall back to the Python
        # extractor which traverses the full hierarchy.
        if fallback:
            expected_sheets = _count_hierarchy_sheets(sch_path)
            exported_sheets = len(netlist.sheets) if netlist.sheets else 1
            if exported_sheets < expected_sheets:
                logger.warning(
                    "kicad-cli netlist is incomplete: exported %d of %d sheets, "
                    "using pure Python netlist extraction",
                    exported_sheets,
                    expected_sheets,
                )
                return build_netlist_from_schematic(sch_path)

            # Also validate component count: kicad-cli may report all sheets
            # but still omit components from some of them.  Use the Python
            # hierarchy traversal to get the expected count.
            py_components, _ = _collect_hierarchy_components(sch_path, "/")
            # Filter out power symbols (references starting with #) which
            # are not included in the kicad-cli netlist component list.
            expected_count = sum(
                1 for c in py_components if not c.reference.startswith("#")
            )
            exported_count = len(netlist.components)
            if exported_count < expected_count:
                logger.warning(
                    "kicad-cli netlist is missing components: exported %d of %d, "
                    "using pure Python netlist extraction",
                    exported_count,
                    expected_count,
                )
                return build_netlist_from_schematic(sch_path)

        return netlist

    except subprocess.CalledProcessError as e:
        if fallback:
            logger.warning(
                f"kicad-cli subprocess error ({e}), using pure Python netlist extraction"
            )
            return build_netlist_from_schematic(sch_path)
        raise RuntimeError(f"Netlist export failed: {e}")

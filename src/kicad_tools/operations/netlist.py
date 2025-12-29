"""
Netlist parsing and export operations.

Provides classes for parsing KiCad netlist files and extracting
connectivity information.
"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..core.sexp import SExp, parse_sexp


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
    properties: Dict[str, str] = field(default_factory=dict)
    pins: List[ComponentPin] = field(default_factory=list)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "NetlistComponent":
        """Parse component from netlist S-expression."""
        ref = ""
        value = ""
        footprint = ""
        lib_id = ""
        sheet_path = ""
        properties: Dict[str, str] = {}

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

        if sheet_node := sexp.find("sheetpath"):
            if names := sheet_node.find("names"):
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
    def from_sexp(cls, sexp: SExp) -> "NetNode":
        """Parse node from netlist S-expression."""
        ref = sexp.get_string(0) or ""
        pin = ""
        pin_function = ""
        pin_type = ""

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
    nodes: List[NetNode] = field(default_factory=list)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "NetlistNet":
        """Parse net from netlist S-expression."""
        code = 0
        name = ""
        nodes: List[NetNode] = []

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
    sheets: List[SheetInfo] = field(default_factory=list)
    components: List[NetlistComponent] = field(default_factory=list)
    nets: List[NetlistNet] = field(default_factory=list)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "Netlist":
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
    def load(cls, path: str | Path) -> "Netlist":
        """Load and parse a netlist file."""
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        sexp = parse_sexp(text)
        return cls.from_sexp(sexp)

    def get_component(self, reference: str) -> Optional[NetlistComponent]:
        """Get component by reference designator."""
        for comp in self.components:
            if comp.reference == reference:
                return comp
        return None

    def get_net(self, name: str) -> Optional[NetlistNet]:
        """Get net by name."""
        for net in self.nets:
            if net.name == name:
                return net
        return None

    def get_component_nets(self, reference: str) -> List[NetlistNet]:
        """Get all nets connected to a component."""
        result = []
        for net in self.nets:
            for node in net.nodes:
                if node.reference == reference:
                    result.append(net)
                    break
        return result

    def get_net_by_pin(self, reference: str, pin: str) -> Optional[NetlistNet]:
        """Get the net connected to a specific pin."""
        for net in self.nets:
            for node in net.nodes:
                if node.reference == reference and node.pin == pin:
                    return net
        return None

    @property
    def power_nets(self) -> List[NetlistNet]:
        """Get power nets (containing power pins)."""
        power_nets = []
        for net in self.nets:
            for node in net.nodes:
                if "power" in node.pin_type.lower():
                    power_nets.append(net)
                    break
        return power_nets

    def to_dict(self) -> Dict:
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

    def summary(self) -> Dict:
        """Get netlist summary statistics."""
        # Group components by prefix
        by_prefix: Dict[str, int] = defaultdict(int)
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


def find_kicad_cli() -> Optional[Path]:
    """Find kicad-cli executable."""
    locations = [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/opt/homebrew/bin/kicad-cli",
    ]

    for loc in locations:
        if Path(loc).exists():
            return Path(loc)

    try:
        result = subprocess.run(["which", "kicad-cli"], capture_output=True, text=True)
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass

    return None


def export_netlist(
    sch_path: str | Path,
    output_path: Optional[str | Path] = None,
    kicad_cli: Optional[str | Path] = None,
    format: str = "kicadsexpr",
) -> Netlist:
    """
    Export netlist from schematic using kicad-cli.

    Args:
        sch_path: Path to .kicad_sch file
        output_path: Output path for netlist (optional, uses temp)
        kicad_cli: Path to kicad-cli (auto-detected if not provided)
        format: Netlist format (kicadsexpr, kicadxml)

    Returns:
        Parsed Netlist object

    Raises:
        FileNotFoundError: If kicad-cli not found
        RuntimeError: If export fails
    """
    sch_path = Path(sch_path)
    if not sch_path.exists():
        raise FileNotFoundError(f"Schematic not found: {sch_path}")

    if kicad_cli is None:
        cli = find_kicad_cli()
        if cli is None:
            raise FileNotFoundError("kicad-cli not found. Install KiCad 8.")
        kicad_cli = cli
    else:
        kicad_cli = Path(kicad_cli)

    if output_path is None:
        output_path = sch_path.parent / f"{sch_path.stem}-netlist.kicad_net"
    else:
        output_path = Path(output_path)

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

        if not output_path.exists():
            raise RuntimeError(result.stderr or "Netlist export produced no output")

        return Netlist.load(output_path)

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Netlist export failed: {e}")

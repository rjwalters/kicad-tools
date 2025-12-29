#!/usr/bin/env python3
"""
Export and analyze schematic netlist.

Extracts connectivity information from KiCad schematics including
components, nets, and pin connections.

Usage:
    python3 scripts/kicad/export-netlist.py design.kicad_sch
    python3 scripts/kicad/export-netlist.py design.kicad_sch --format json
    python3 scripts/kicad/export-netlist.py design.kicad_sch --components
    python3 scripts/kicad/export-netlist.py design.kicad_sch --nets
    python3 scripts/kicad/export-netlist.py design.kicad_sch --component U1
    python3 scripts/kicad/export-netlist.py design.kicad_sch --net GND
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from kicad_tools.core.sexp import SExp, parse_sexp


@dataclass
class ComponentPin:
    """A pin on a component."""

    number: str
    name: str
    pin_type: str = ""


@dataclass
class Component:
    """A component in the schematic."""

    reference: str
    value: str
    footprint: str
    lib_id: str
    sheet_path: str = ""
    properties: dict = field(default_factory=dict)
    pins: list[ComponentPin] = field(default_factory=list)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "Component":
        """Parse component from netlist S-expression."""
        ref = ""
        value = ""
        footprint = ""
        lib_id = ""
        sheet_path = ""
        properties = {}

        if ref_node := sexp.find("ref"):
            ref = ref_node.get_string(0) or ""

        if value_node := sexp.find("value"):
            value = value_node.get_string(0) or ""

        if fp_node := sexp.find("footprint"):
            footprint = fp_node.get_string(0) or ""

        if lib_node := sexp.find("libsource"):
            if part := lib_node.find("part"):
                lib_id = part.get_string(0) or ""
            elif lib_node.get_string(1):  # (libsource (lib X) (part Y))
                lib_id = lib_node.get_string(1) or ""

        if sheet_node := sexp.find("sheetpath"):
            if names := sheet_node.find("names"):
                sheet_path = names.get_string(0) or ""

        # Parse properties
        if sexp.find("property"):
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
class Net:
    """A net (electrical connection) in the schematic."""

    code: int
    name: str
    nodes: list[NetNode] = field(default_factory=list)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "Net":
        """Parse net from netlist S-expression."""
        code = 0
        name = ""
        nodes = []

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
    components: list[Component] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)

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
                source = ""

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
                        source = s.get_string(0) or ""

                netlist.sheets.append(
                    SheetInfo(
                        number=sheet_num,
                        name=sheet_name,
                        path=sheet_path,
                        title=title,
                        source=source,
                    )
                )

        # Parse components
        if components := sexp.find("components"):
            for comp in components.find_all("comp"):
                netlist.components.append(Component.from_sexp(comp))

        # Parse nets
        if nets := sexp.find("nets"):
            for net in nets.find_all("net"):
                netlist.nets.append(Net.from_sexp(net))

        return netlist

    def get_component(self, reference: str) -> Optional[Component]:
        """Get component by reference designator."""
        for comp in self.components:
            if comp.reference == reference:
                return comp
        return None

    def get_net(self, name: str) -> Optional[Net]:
        """Get net by name."""
        for net in self.nets:
            if net.name == name:
                return net
        return None

    def get_component_nets(self, reference: str) -> list[Net]:
        """Get all nets connected to a component."""
        result = []
        for net in self.nets:
            for node in net.nodes:
                if node.reference == reference:
                    result.append(net)
                    break
        return result

    def get_net_by_pin(self, reference: str, pin: str) -> Optional[Net]:
        """Get the net connected to a specific pin."""
        for net in self.nets:
            for node in net.nodes:
                if node.reference == reference and node.pin == pin:
                    return net
        return None

    @property
    def power_nets(self) -> list[Net]:
        """Get power nets (containing power pins)."""
        power_nets = []
        for net in self.nets:
            for node in net.nodes:
                if "power" in node.pin_type.lower():
                    power_nets.append(net)
                    break
        return power_nets


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
    sch_path: Path,
    output_path: Path,
    kicad_cli: Path,
    format: str = "kicadsexpr",
) -> tuple[bool, str]:
    """
    Export netlist from schematic.

    Returns:
        Tuple of (success, error_message)
    """
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
            return False, result.stderr or "Netlist export produced no output"

        return True, ""

    except subprocess.CalledProcessError as e:
        return False, str(e)
    except FileNotFoundError as e:
        return False, f"kicad-cli not found: {e}"


def load_netlist(path: Path) -> Netlist:
    """Load and parse a netlist file."""
    text = path.read_text(encoding="utf-8")
    sexp = parse_sexp(text)
    return Netlist.from_sexp(sexp)


def print_summary(netlist: Netlist):
    """Print netlist summary."""
    print(f"\n{'=' * 60}")
    print("NETLIST SUMMARY")
    print(f"{'=' * 60}")

    if netlist.source_file:
        print(f"Source: {Path(netlist.source_file).name}")
    if netlist.tool:
        print(f"Tool: {netlist.tool}")
    if netlist.date:
        print(f"Date: {netlist.date}")

    print(f"\nSheets: {len(netlist.sheets)}")
    for sheet in netlist.sheets:
        title = f" - {sheet.title}" if sheet.title else ""
        print(f"  {sheet.number}. {sheet.name}{title}")

    print(f"\nComponents: {len(netlist.components)}")
    if netlist.components:
        # Group by prefix
        by_prefix = defaultdict(list)
        for comp in netlist.components:
            prefix = "".join(c for c in comp.reference if c.isalpha())
            by_prefix[prefix].append(comp)

        for prefix in sorted(by_prefix.keys()):
            comps = by_prefix[prefix]
            print(f"  {prefix}: {len(comps)}")

    print(f"\nNets: {len(netlist.nets)}")
    if netlist.nets:
        # Find key nets
        power_nets = [
            n for n in netlist.nets if n.name.startswith("+") or n.name in ("GND", "PGND", "AGND")
        ]
        signal_nets = [n for n in netlist.nets if n not in power_nets]

        if power_nets:
            print(f"  Power: {len(power_nets)}")
            for net in sorted(power_nets, key=lambda n: n.name)[:10]:
                print(f"    {net.name} ({net.connection_count} connections)")

        if signal_nets:
            print(f"  Signal: {len(signal_nets)}")
            # Show nets with most connections
            top_nets = sorted(signal_nets, key=lambda n: -n.connection_count)[:5]
            for net in top_nets:
                print(f"    {net.name} ({net.connection_count} connections)")

    if not netlist.components and not netlist.nets:
        print("\n  (No components or nets - schematic may be incomplete)")

    print(f"\n{'=' * 60}")


def print_components(netlist: Netlist, filter_ref: Optional[str] = None):
    """Print component list."""
    components = netlist.components

    if filter_ref:
        components = [c for c in components if filter_ref.upper() in c.reference.upper()]

    if not components:
        print("No components found")
        return

    print(f"\n{'=' * 60}")
    print(f"COMPONENTS ({len(components)})")
    print(f"{'=' * 60}")

    for comp in sorted(components, key=lambda c: c.reference):
        print(f"\n{comp.reference}: {comp.value}")
        if comp.footprint:
            print(f"  Footprint: {comp.footprint}")
        if comp.lib_id:
            print(f"  Library: {comp.lib_id}")
        if comp.sheet_path:
            print(f"  Sheet: {comp.sheet_path}")

        # Show connected nets
        nets = netlist.get_component_nets(comp.reference)
        if nets:
            print(f"  Nets ({len(nets)}):")
            for net in nets[:10]:
                pins = [n.pin for n in net.nodes if n.reference == comp.reference]
                print(f"    {net.name}: pins {', '.join(pins)}")
            if len(nets) > 10:
                print(f"    ... and {len(nets) - 10} more")


def print_nets(netlist: Netlist, filter_name: Optional[str] = None):
    """Print net list."""
    nets = netlist.nets

    if filter_name:
        nets = [n for n in nets if filter_name.upper() in n.name.upper()]

    if not nets:
        print("No nets found")
        return

    print(f"\n{'=' * 60}")
    print(f"NETS ({len(nets)})")
    print(f"{'=' * 60}")

    for net in sorted(nets, key=lambda n: n.name):
        print(f"\n{net.name} (code {net.code})")
        print(f"  Connections: {net.connection_count}")
        for node in net.nodes:
            func = f" ({node.pin_function})" if node.pin_function else ""
            ptype = f" [{node.pin_type}]" if node.pin_type else ""
            print(f"    {node.reference}.{node.pin}{func}{ptype}")


def print_json(netlist: Netlist):
    """Print netlist as JSON."""
    output = {
        "source": netlist.source_file,
        "tool": netlist.tool,
        "date": netlist.date,
        "sheets": [
            {
                "number": s.number,
                "name": s.name,
                "path": s.path,
                "title": s.title,
                "source": s.source,
            }
            for s in netlist.sheets
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
            for c in netlist.components
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
            for n in netlist.nets
        ],
    }

    print(json.dumps(output, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Export and analyze schematic netlist",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "schematic", nargs="?", type=Path, help="Path to KiCad schematic file (.kicad_sch)"
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["summary", "json", "components", "nets"],
        default="summary",
        help="Output format (default: summary)",
    )
    parser.add_argument(
        "--component",
        "-c",
        type=str,
        metavar="REF",
        help="Filter/show specific component by reference",
    )
    parser.add_argument(
        "--net", "-n", type=str, metavar="NAME", help="Filter/show specific net by name"
    )
    parser.add_argument("--output", "-o", type=Path, help="Save netlist to file (instead of temp)")
    parser.add_argument(
        "--keep-netlist", action="store_true", help="Keep the exported netlist file"
    )

    # Shortcut flags
    parser.add_argument(
        "--components",
        action="store_true",
        help="Show component list (same as --format components)",
    )
    parser.add_argument("--nets", action="store_true", help="Show net list (same as --format nets)")
    parser.add_argument("--json", action="store_true", help="Output JSON (same as --format json)")

    args = parser.parse_args()

    # Handle shortcut flags
    if args.components:
        args.format = "components"
    elif args.nets:
        args.format = "nets"
    elif args.json:
        args.format = "json"

    # If component or net filter specified, switch to appropriate format
    if args.component and args.format == "summary":
        args.format = "components"
    if args.net and args.format == "summary":
        args.format = "nets"

    # Validate schematic path
    if not args.schematic:
        parser.print_help()
        print("\nError: No schematic file specified")
        return 1

    if not args.schematic.exists():
        print(f"Error: Schematic not found: {args.schematic}")
        return 1

    # Find kicad-cli
    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("Error: kicad-cli not found")
        print("Install KiCad 8 from: https://www.kicad.org/download/")
        return 1

    # Export netlist
    output_path = args.output or (
        args.schematic.parent / f"{args.schematic.stem}-netlist.kicad_net"
    )

    print(f"Exporting netlist from: {args.schematic.name}")
    success, error = export_netlist(args.schematic, output_path, kicad_cli)

    if not success:
        print(f"Error exporting netlist: {error}")
        return 1

    # Parse netlist
    try:
        netlist = load_netlist(output_path)
    except Exception as e:
        print(f"Error parsing netlist: {e}")
        return 1

    # Output
    if args.format == "json":
        print_json(netlist)
    elif args.format == "components":
        print_components(netlist, args.component)
    elif args.format == "nets":
        print_nets(netlist, args.net)
    else:
        print_summary(netlist)

    # Cleanup
    if not args.keep_netlist and not args.output and output_path.exists():
        output_path.unlink()

    return 0


if __name__ == "__main__":
    sys.exit(main())

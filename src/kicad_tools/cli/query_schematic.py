#!/usr/bin/env python3
"""
Query KiCad schematic files.

Provides information about symbols, wires, labels, and connectivity
in schematic files. Essential for understanding and modifying designs.

Usage:
    python3 scripts/kicad/query-schematic.py design.kicad_sch
    python3 scripts/kicad/query-schematic.py design.kicad_sch --symbols
    python3 scripts/kicad/query-schematic.py design.kicad_sch --wires
    python3 scripts/kicad/query-schematic.py design.kicad_sch --labels
    python3 scripts/kicad/query-schematic.py design.kicad_sch --symbol U1
    python3 scripts/kicad/query-schematic.py design.kicad_sch --json
    python3 scripts/kicad/query-schematic.py design.kicad_sch --hierarchy
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
KICAD_SCRIPTS = Path(__file__).resolve().parent

# Import our S-expression parser
from kicad_tools.core.sexp import SExp, parse_sexp


# Embedded data models (to avoid import complexity)
@dataclass
class SymbolProperty:
    """A property on a symbol."""
    name: str
    value: str
    position: tuple[float, float] = (0, 0)
    rotation: float = 0
    visible: bool = True

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "SymbolProperty":
        name = sexp.get_string(0) or ""
        value = sexp.get_string(1) or ""
        pos = (0.0, 0.0)
        rot = 0.0
        if at := sexp.find('at'):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0
        visible = True
        if effects := sexp.find('effects'):
            if effects.find('hide'):
                visible = False
        return cls(name=name, value=value, position=pos, rotation=rot, visible=visible)


@dataclass
class SymbolInstance:
    """A symbol instance in a schematic."""
    lib_id: str
    uuid: str
    position: tuple[float, float] = (0, 0)
    rotation: float = 0
    mirror: str = ""
    unit: int = 1
    properties: dict = field(default_factory=dict)
    pins: list = field(default_factory=list)

    @property
    def reference(self) -> str:
        if 'Reference' in self.properties:
            return self.properties['Reference'].value
        return ""

    @property
    def value(self) -> str:
        if 'Value' in self.properties:
            return self.properties['Value'].value
        return ""

    @property
    def footprint(self) -> str:
        if 'Footprint' in self.properties:
            return self.properties['Footprint'].value
        return ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "SymbolInstance":
        lib_id = ""
        if lid := sexp.find('lib_id'):
            lib_id = lid.get_string(0) or ""
        uuid = ""
        if uuid_node := sexp.find('uuid'):
            uuid = uuid_node.get_string(0) or ""
        pos = (0.0, 0.0)
        rot = 0.0
        if at := sexp.find('at'):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0
        mirror = ""
        if m := sexp.find('mirror'):
            mirror = m.get_string(0) or ""
        unit = 1
        if u := sexp.find('unit'):
            unit = u.get_int(0) or 1
        properties = {}
        for prop in sexp.find_all('property'):
            sp = SymbolProperty.from_sexp(prop)
            properties[sp.name] = sp
        pins = []
        for pin in sexp.find_all('pin'):
            pins.append(pin.get_string(0) or "")
        return cls(lib_id=lib_id, uuid=uuid, position=pos, rotation=rot, mirror=mirror, unit=unit, properties=properties, pins=pins)


@dataclass
class Wire:
    """A wire segment."""
    start: tuple[float, float]
    end: tuple[float, float]
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "Wire":
        start = (0.0, 0.0)
        end = (0.0, 0.0)
        uuid = ""
        if pts := sexp.find('pts'):
            xy_nodes = pts.find_all('xy')
            if len(xy_nodes) >= 2:
                start = (xy_nodes[0].get_float(0) or 0, xy_nodes[0].get_float(1) or 0)
                end = (xy_nodes[1].get_float(0) or 0, xy_nodes[1].get_float(1) or 0)
        if uuid_node := sexp.find('uuid'):
            uuid = uuid_node.get_string(0) or ""
        return cls(start=start, end=end, uuid=uuid)

    @property
    def length(self) -> float:
        dx = self.end[0] - self.start[0]
        dy = self.end[1] - self.start[1]
        return (dx * dx + dy * dy) ** 0.5


@dataclass
class Bus:
    """A bus segment."""
    start: tuple[float, float]
    end: tuple[float, float]
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "Bus":
        start = (0.0, 0.0)
        end = (0.0, 0.0)
        uuid = ""
        if pts := sexp.find('pts'):
            xy_nodes = pts.find_all('xy')
            if len(xy_nodes) >= 2:
                start = (xy_nodes[0].get_float(0) or 0, xy_nodes[0].get_float(1) or 0)
                end = (xy_nodes[1].get_float(0) or 0, xy_nodes[1].get_float(1) or 0)
        if uuid_node := sexp.find('uuid'):
            uuid = uuid_node.get_string(0) or ""
        return cls(start=start, end=end, uuid=uuid)


@dataclass
class Junction:
    """A junction point."""
    position: tuple[float, float]
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "Junction":
        pos = (0.0, 0.0)
        uuid = ""
        if at := sexp.find('at'):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
        if uuid_node := sexp.find('uuid'):
            uuid = uuid_node.get_string(0) or ""
        return cls(position=pos, uuid=uuid)


@dataclass
class Label:
    """A local net label."""
    text: str
    position: tuple[float, float]
    rotation: float = 0
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "Label":
        text = sexp.get_string(0) or ""
        pos = (0.0, 0.0)
        rot = 0.0
        uuid = ""
        if at := sexp.find('at'):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0
        if uuid_node := sexp.find('uuid'):
            uuid = uuid_node.get_string(0) or ""
        return cls(text=text, position=pos, rotation=rot, uuid=uuid)


@dataclass
class HierarchicalLabel:
    """A hierarchical label."""
    text: str
    position: tuple[float, float]
    rotation: float = 0
    shape: str = "input"
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "HierarchicalLabel":
        text = sexp.get_string(0) or ""
        pos = (0.0, 0.0)
        rot = 0.0
        shape = "input"
        uuid = ""
        if at := sexp.find('at'):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0
        if s := sexp.find('shape'):
            shape = s.get_string(0) or "input"
        if uuid_node := sexp.find('uuid'):
            uuid = uuid_node.get_string(0) or ""
        return cls(text=text, position=pos, rotation=rot, shape=shape, uuid=uuid)


@dataclass
class GlobalLabel:
    """A global label."""
    text: str
    position: tuple[float, float]
    rotation: float = 0
    shape: str = "input"
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "GlobalLabel":
        text = sexp.get_string(0) or ""
        pos = (0.0, 0.0)
        rot = 0.0
        shape = "input"
        uuid = ""
        if at := sexp.find('at'):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0
        if s := sexp.find('shape'):
            shape = s.get_string(0) or "input"
        if uuid_node := sexp.find('uuid'):
            uuid = uuid_node.get_string(0) or ""
        return cls(text=text, position=pos, rotation=rot, shape=shape, uuid=uuid)


@dataclass
class PowerSymbol:
    """A power symbol."""
    lib_id: str
    position: tuple[float, float]
    rotation: float = 0
    uuid: str = ""
    value: str = ""

    @classmethod
    def from_symbol_sexp(cls, sexp: SExp) -> Optional["PowerSymbol"]:
        lib_id = ""
        if lid := sexp.find('lib_id'):
            lib_id = lid.get_string(0) or ""
        if not lib_id.startswith('power:'):
            return None
        pos = (0.0, 0.0)
        rot = 0.0
        uuid = ""
        value = ""
        if at := sexp.find('at'):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0
        if uuid_node := sexp.find('uuid'):
            uuid = uuid_node.get_string(0) or ""
        for prop in sexp.find_all('property'):
            if prop.get_string(0) == 'Value':
                value = prop.get_string(1) or ""
                break
        return cls(lib_id=lib_id, position=pos, rotation=rot, uuid=uuid, value=value)


@dataclass
class SchematicInfo:
    """Parsed schematic information."""
    path: Path
    version: int = 0
    generator: str = ""
    uuid: str = ""
    paper: str = ""
    title: str = ""
    date: str = ""
    rev: str = ""
    company: str = ""
    comments: list[str] = field(default_factory=list)

    # Contents
    symbols: list[SymbolInstance] = field(default_factory=list)
    power_symbols: list[PowerSymbol] = field(default_factory=list)
    wires: list[Wire] = field(default_factory=list)
    buses: list[Bus] = field(default_factory=list)
    junctions: list[Junction] = field(default_factory=list)
    labels: list[Label] = field(default_factory=list)
    hierarchical_labels: list[HierarchicalLabel] = field(default_factory=list)
    global_labels: list[GlobalLabel] = field(default_factory=list)

    # Embedded library symbols
    lib_symbols: dict[str, SExp] = field(default_factory=dict)

    # Hierarchical sheets
    sheets: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "SchematicInfo":
        """Load and parse a schematic file."""
        text = path.read_text(encoding="utf-8")
        sexp = parse_sexp(text)

        if sexp.tag != "kicad_sch":
            raise ValueError(f"Not a schematic: {sexp.tag}")

        info = cls(path=path)

        # Parse header info
        if v := sexp.find("version"):
            info.version = v.get_int(0) or 0
        if g := sexp.find("generator"):
            info.generator = g.get_string(0) or ""
        if u := sexp.find("uuid"):
            info.uuid = u.get_string(0) or ""
        if p := sexp.find("paper"):
            info.paper = p.get_string(0) or ""

        # Parse title block
        if tb := sexp.find("title_block"):
            if t := tb.find("title"):
                info.title = t.get_string(0) or ""
            if d := tb.find("date"):
                info.date = d.get_string(0) or ""
            if r := tb.find("rev"):
                info.rev = r.get_string(0) or ""
            if c := tb.find("company"):
                info.company = c.get_string(0) or ""

            for comment in tb.find_all("comment"):
                num = comment.get_int(0)
                val = comment.get_string(1)
                if val:
                    info.comments.append(val)

        # Parse embedded library symbols
        if lib_syms := sexp.find("lib_symbols"):
            for sym in lib_syms.find_all("symbol"):
                name = sym.get_string(0) or ""
                if name:
                    info.lib_symbols[name] = sym

        # Parse symbol instances
        for sym in sexp.find_all("symbol"):
            instance = SymbolInstance.from_sexp(sym)
            # Check if it's a power symbol
            power = PowerSymbol.from_symbol_sexp(sym)
            if power:
                info.power_symbols.append(power)
            else:
                info.symbols.append(instance)

        # Parse wires
        for wire in sexp.find_all("wire"):
            info.wires.append(Wire.from_sexp(wire))

        # Parse buses
        for bus in sexp.find_all("bus"):
            info.buses.append(Bus.from_sexp(bus))

        # Parse junctions
        for junc in sexp.find_all("junction"):
            info.junctions.append(Junction.from_sexp(junc))

        # Parse labels
        for lbl in sexp.find_all("label"):
            info.labels.append(Label.from_sexp(lbl))

        for hlbl in sexp.find_all("hierarchical_label"):
            info.hierarchical_labels.append(HierarchicalLabel.from_sexp(hlbl))

        for glbl in sexp.find_all("global_label"):
            info.global_labels.append(GlobalLabel.from_sexp(glbl))

        # Parse hierarchical sheets
        for sheet in sexp.find_all("sheet"):
            sheet_info = {"uuid": "", "name": "", "filename": "", "position": (0, 0), "size": (0, 0)}

            if u := sheet.find("uuid"):
                sheet_info["uuid"] = u.get_string(0) or ""
            if at := sheet.find("at"):
                sheet_info["position"] = (at.get_float(0) or 0, at.get_float(1) or 0)
            if sz := sheet.find("size"):
                sheet_info["size"] = (sz.get_float(0) or 0, sz.get_float(1) or 0)

            for prop in sheet.find_all("property"):
                prop_name = prop.get_string(0) or ""
                prop_val = prop.get_string(1) or ""
                if prop_name == "Sheetname":
                    sheet_info["name"] = prop_val
                elif prop_name == "Sheetfile":
                    sheet_info["filename"] = prop_val

            # Parse sheet pins
            pins = []
            for pin in sheet.find_all("pin"):
                pin_name = pin.get_string(0) or ""
                pin_shape = ""
                if shape := pin.find("shape"):
                    pin_shape = shape.get_string(0) or ""
                pins.append({"name": pin_name, "shape": pin_shape})
            sheet_info["pins"] = pins

            info.sheets.append(sheet_info)

        return info

    def get_symbol(self, reference: str) -> Optional[SymbolInstance]:
        """Get symbol by reference designator."""
        for sym in self.symbols:
            if sym.reference == reference:
                return sym
        return None

    def get_symbols_by_lib(self, lib_id: str) -> list[SymbolInstance]:
        """Get all symbols using a specific library symbol."""
        return [s for s in self.symbols if lib_id.lower() in s.lib_id.lower()]

    def get_lib_symbol(self, lib_id: str) -> Optional[SExp]:
        """Get embedded library symbol definition."""
        return self.lib_symbols.get(lib_id)


def print_summary(info: SchematicInfo):
    """Print schematic summary."""
    print(f"\n{'='*60}")
    print(f"SCHEMATIC: {info.path.name}")
    print(f"{'='*60}")

    if info.title:
        print(f"Title: {info.title}")
    if info.date:
        print(f"Date: {info.date}")
    if info.rev:
        print(f"Revision: {info.rev}")
    print(f"Paper: {info.paper}")
    print(f"Generator: {info.generator} (v{info.version})")

    if info.comments:
        print("\nComments:")
        for c in info.comments:
            print(f"  {c}")

    print(f"\n{'─'*60}")
    print("CONTENTS:")
    print(f"  Symbols:             {len(info.symbols)}")
    print(f"  Power Symbols:       {len(info.power_symbols)}")
    print(f"  Wires:               {len(info.wires)}")
    print(f"  Buses:               {len(info.buses)}")
    print(f"  Junctions:           {len(info.junctions)}")
    print(f"  Labels:              {len(info.labels)}")
    print(f"  Hierarchical Labels: {len(info.hierarchical_labels)}")
    print(f"  Global Labels:       {len(info.global_labels)}")
    print(f"  Hierarchical Sheets: {len(info.sheets)}")
    print(f"  Library Symbols:     {len(info.lib_symbols)}")

    # Symbol summary by type
    if info.symbols:
        print(f"\n{'─'*60}")
        print("SYMBOLS BY TYPE:")
        by_prefix = defaultdict(list)
        for sym in info.symbols:
            prefix = "".join(c for c in sym.reference if c.isalpha())
            by_prefix[prefix].append(sym)

        for prefix in sorted(by_prefix.keys()):
            syms = by_prefix[prefix]
            refs = ", ".join(s.reference for s in sorted(syms, key=lambda x: x.reference))
            if len(refs) > 50:
                refs = refs[:47] + "..."
            print(f"  {prefix}: {len(syms)} ({refs})")

    # Hierarchical sheets
    if info.sheets:
        print(f"\n{'─'*60}")
        print("HIERARCHICAL SHEETS:")
        for sheet in info.sheets:
            print(f"  {sheet['name']}: {sheet['filename']}")
            if sheet.get('pins'):
                for pin in sheet['pins'][:5]:
                    print(f"    - {pin['name']} ({pin['shape']})")
                if len(sheet.get('pins', [])) > 5:
                    print(f"    ... and {len(sheet['pins']) - 5} more pins")


def print_symbols(info: SchematicInfo, filter_ref: Optional[str] = None, verbose: bool = False):
    """Print symbol list."""
    symbols = info.symbols

    if filter_ref:
        symbols = [s for s in symbols if filter_ref.upper() in s.reference.upper()]

    if not symbols:
        print("No symbols found")
        return

    print(f"\n{'='*60}")
    print(f"SYMBOLS ({len(symbols)})")
    print(f"{'='*60}")

    for sym in sorted(symbols, key=lambda s: s.reference):
        print(f"\n{sym.reference}: {sym.value}")
        print(f"  Library: {sym.lib_id}")
        print(f"  Position: ({sym.position[0]:.2f}, {sym.position[1]:.2f})")
        if sym.rotation:
            print(f"  Rotation: {sym.rotation}°")
        if sym.mirror:
            print(f"  Mirror: {sym.mirror}")
        if sym.footprint:
            print(f"  Footprint: {sym.footprint}")
        print(f"  UUID: {sym.uuid}")

        if verbose:
            # Show all properties
            if sym.properties:
                print("  Properties:")
                for name, prop in sym.properties.items():
                    if name not in ("Reference", "Value", "Footprint"):
                        vis = "" if prop.visible else " [hidden]"
                        print(f"    {name}: {prop.value}{vis}")

            # Show pins
            if sym.pins:
                print(f"  Pins: {len(sym.pins)}")


def print_wires(info: SchematicInfo, verbose: bool = False):
    """Print wire list."""
    if not info.wires:
        print("No wires found")
        return

    print(f"\n{'='*60}")
    print(f"WIRES ({len(info.wires)})")
    print(f"{'='*60}")

    if verbose:
        for i, wire in enumerate(info.wires):
            print(f"\n{i+1}. ({wire.start[0]:.2f}, {wire.start[1]:.2f}) -> ({wire.end[0]:.2f}, {wire.end[1]:.2f})")
            print(f"   Length: {wire.length:.2f}")
            print(f"   UUID: {wire.uuid}")
    else:
        # Summary stats
        total_length = sum(w.length for w in info.wires)
        print(f"\nTotal wire length: {total_length:.2f}")

        # Group by approximate grid positions
        horizontal = [w for w in info.wires if abs(w.start[1] - w.end[1]) < 0.1]
        vertical = [w for w in info.wires if abs(w.start[0] - w.end[0]) < 0.1]
        diagonal = [w for w in info.wires if w not in horizontal and w not in vertical]

        print(f"  Horizontal: {len(horizontal)}")
        print(f"  Vertical:   {len(vertical)}")
        print(f"  Diagonal:   {len(diagonal)}")

    if info.junctions:
        print(f"\nJunctions: {len(info.junctions)}")
        if verbose:
            for junc in info.junctions:
                print(f"  ({junc.position[0]:.2f}, {junc.position[1]:.2f})")


def print_labels(info: SchematicInfo, verbose: bool = False):
    """Print label list."""
    total = len(info.labels) + len(info.hierarchical_labels) + len(info.global_labels) + len(info.power_symbols)

    if total == 0:
        print("No labels found")
        return

    print(f"\n{'='*60}")
    print(f"LABELS ({total})")
    print(f"{'='*60}")

    if info.labels:
        print(f"\nLocal Labels ({len(info.labels)}):")
        for lbl in sorted(info.labels, key=lambda l: l.text):
            pos = f"({lbl.position[0]:.2f}, {lbl.position[1]:.2f})" if verbose else ""
            print(f"  {lbl.text} {pos}")

    if info.hierarchical_labels:
        print(f"\nHierarchical Labels ({len(info.hierarchical_labels)}):")
        for lbl in sorted(info.hierarchical_labels, key=lambda l: l.text):
            pos = f"({lbl.position[0]:.2f}, {lbl.position[1]:.2f})" if verbose else ""
            print(f"  {lbl.text} [{lbl.shape}] {pos}")

    if info.global_labels:
        print(f"\nGlobal Labels ({len(info.global_labels)}):")
        for lbl in sorted(info.global_labels, key=lambda l: l.text):
            pos = f"({lbl.position[0]:.2f}, {lbl.position[1]:.2f})" if verbose else ""
            print(f"  {lbl.text} [{lbl.shape}] {pos}")

    if info.power_symbols:
        print(f"\nPower Symbols ({len(info.power_symbols)}):")
        by_net = defaultdict(list)
        for ps in info.power_symbols:
            by_net[ps.value].append(ps)

        for net in sorted(by_net.keys()):
            symbols = by_net[net]
            print(f"  {net}: {len(symbols)} instance(s)")


def print_hierarchy(info: SchematicInfo):
    """Print hierarchical structure."""
    print(f"\n{'='*60}")
    print(f"HIERARCHY: {info.path.name}")
    print(f"{'='*60}")

    print(f"\nThis Sheet: {info.title or info.path.stem}")

    if info.sheets:
        print(f"\nSub-sheets ({len(info.sheets)}):")
        for sheet in info.sheets:
            print(f"\n  {sheet['name']}")
            print(f"    File: {sheet['filename']}")
            print(f"    Position: {sheet['position']}")
            print(f"    Size: {sheet['size']}")

            if sheet.get('pins'):
                print(f"    Pins ({len(sheet['pins'])}):")
                by_shape = defaultdict(list)
                for pin in sheet['pins']:
                    by_shape[pin['shape']].append(pin['name'])

                for shape in ['input', 'output', 'bidirectional', 'passive']:
                    if shape in by_shape:
                        pins = by_shape[shape]
                        print(f"      {shape}: {', '.join(pins[:5])}", end="")
                        if len(pins) > 5:
                            print(f" (+{len(pins)-5} more)", end="")
                        print()

    if info.hierarchical_labels:
        print("\nHierarchical Labels (connections to parent):")
        by_shape = defaultdict(list)
        for lbl in info.hierarchical_labels:
            by_shape[lbl.shape].append(lbl.text)

        for shape in ['input', 'output', 'bidirectional', 'passive']:
            if shape in by_shape:
                labels = by_shape[shape]
                print(f"  {shape}: {', '.join(sorted(labels)[:10])}", end="")
                if len(labels) > 10:
                    print(f" (+{len(labels)-10} more)", end="")
                print()


def print_lib_symbols(info: SchematicInfo, symbol_filter: Optional[str] = None):
    """Print embedded library symbols."""
    lib_syms = info.lib_symbols

    if symbol_filter:
        lib_syms = {k: v for k, v in lib_syms.items() if symbol_filter.lower() in k.lower()}

    if not lib_syms:
        print("No embedded library symbols found")
        return

    print(f"\n{'='*60}")
    print(f"EMBEDDED LIBRARY SYMBOLS ({len(lib_syms)})")
    print(f"{'='*60}")

    for name in sorted(lib_syms.keys()):
        sym = lib_syms[name]

        # Count pins
        pin_count = 0
        for subsym in sym.find_all("symbol"):
            pin_count += len(subsym.find_all("pin"))

        # Get footprint
        footprint = ""
        for prop in sym.find_all("property"):
            if prop.get_string(0) == "Footprint":
                footprint = prop.get_string(1) or ""
                break

        print(f"\n{name}")
        print(f"  Pins: {pin_count}")
        if footprint:
            print(f"  Footprint: {footprint}")


def print_json_output(info: SchematicInfo):
    """Print schematic info as JSON."""
    output = {
        "path": str(info.path),
        "version": info.version,
        "generator": info.generator,
        "uuid": info.uuid,
        "title": info.title,
        "date": info.date,
        "revision": info.rev,
        "paper": info.paper,
        "symbols": [
            {
                "reference": s.reference,
                "value": s.value,
                "lib_id": s.lib_id,
                "footprint": s.footprint,
                "position": {"x": s.position[0], "y": s.position[1]},
                "rotation": s.rotation,
                "mirror": s.mirror,
                "uuid": s.uuid,
            }
            for s in info.symbols
        ],
        "power_symbols": [
            {
                "value": ps.value,
                "lib_id": ps.lib_id,
                "position": {"x": ps.position[0], "y": ps.position[1]},
                "uuid": ps.uuid,
            }
            for ps in info.power_symbols
        ],
        "wires": [
            {
                "start": {"x": w.start[0], "y": w.start[1]},
                "end": {"x": w.end[0], "y": w.end[1]},
                "length": w.length,
                "uuid": w.uuid,
            }
            for w in info.wires
        ],
        "junctions": [
            {
                "position": {"x": j.position[0], "y": j.position[1]},
                "uuid": j.uuid,
            }
            for j in info.junctions
        ],
        "labels": [
            {
                "text": l.text,
                "position": {"x": l.position[0], "y": l.position[1]},
                "rotation": l.rotation,
                "uuid": l.uuid,
            }
            for l in info.labels
        ],
        "hierarchical_labels": [
            {
                "text": l.text,
                "shape": l.shape,
                "position": {"x": l.position[0], "y": l.position[1]},
                "uuid": l.uuid,
            }
            for l in info.hierarchical_labels
        ],
        "global_labels": [
            {
                "text": l.text,
                "shape": l.shape,
                "position": {"x": l.position[0], "y": l.position[1]},
                "uuid": l.uuid,
            }
            for l in info.global_labels
        ],
        "sheets": info.sheets,
        "lib_symbols": list(info.lib_symbols.keys()),
    }

    print(json.dumps(output, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Query KiCad schematic files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", nargs="?", type=Path,
                        help="Path to KiCad schematic file (.kicad_sch)")
    parser.add_argument("--symbol", "-s", type=str, metavar="REF",
                        help="Filter/show specific symbol by reference")
    parser.add_argument("--lib", "-l", type=str, metavar="LIB_ID",
                        help="Filter symbols by library ID")

    # Output modes
    parser.add_argument("--symbols", action="store_true",
                        help="Show symbol list")
    parser.add_argument("--wires", action="store_true",
                        help="Show wire list")
    parser.add_argument("--labels", action="store_true",
                        help="Show label list")
    parser.add_argument("--hierarchy", action="store_true",
                        help="Show hierarchical structure")
    parser.add_argument("--lib-symbols", action="store_true",
                        help="Show embedded library symbols")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed information")

    args = parser.parse_args()

    # Find schematic if not specified
    if not args.schematic:
        defaults = [
            REPO_ROOT / "hardware/chorus-revA/kicad/amplifier.kicad_sch",
            REPO_ROOT / "hardware/chorus-revA/kicad/chorus-revA.kicad_sch",
        ]
        for default in defaults:
            if default.exists():
                args.schematic = default
                break

        if not args.schematic:
            parser.print_help()
            print("\nError: No schematic file specified")
            return 1

    if not args.schematic.exists():
        print(f"Error: Schematic not found: {args.schematic}")
        return 1

    # Load schematic
    try:
        info = SchematicInfo.load(args.schematic)
    except Exception as e:
        print(f"Error loading schematic: {e}")
        return 1

    # JSON output
    if args.json:
        print_json_output(info)
        return 0

    # Specific output modes
    if args.symbols or args.symbol or args.lib:
        filter_ref = args.symbol
        if args.lib:
            # Find symbols using this library
            matching = info.get_symbols_by_lib(args.lib)
            if matching:
                for sym in matching:
                    print_symbols(info, sym.reference, args.verbose)
            else:
                print(f"No symbols found using library: {args.lib}")
        else:
            print_symbols(info, filter_ref, args.verbose)
        return 0

    if args.wires:
        print_wires(info, args.verbose)
        return 0

    if args.labels:
        print_labels(info, args.verbose)
        return 0

    if args.hierarchy:
        print_hierarchy(info)
        return 0

    if args.lib_symbols:
        print_lib_symbols(info)
        return 0

    # Default: summary
    print_summary(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())

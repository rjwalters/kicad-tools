"""
Schematic I/O Mixin

Provides file loading and saving capabilities for Schematic class.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.sexp import SExp
from kicad_tools.sexp.builders import (
    sheet_instances,
    text_node,
    title_block,
    uuid_node,
)

from ..logging import _log_info
from .elements import HierarchicalLabel, Junction, Label, PowerSymbol, Wire
from .symbol import SymbolInstance

if TYPE_CHECKING:
    from .schematic import Schematic


class SchematicIOMixin:
    """Mixin providing I/O operations for Schematic class."""

    @classmethod
    def load(cls, path: str | Path) -> Schematic:
        """Load a schematic from a .kicad_sch file.

        This enables round-trip editing: load -> modify -> save.

        Args:
            path: Path to the .kicad_sch file

        Returns:
            A Schematic instance populated with all elements from the file

        Example:
            sch = Schematic.load("power.kicad_sch")
            sch.add_symbol("Device:R", 100, 100, "R5", "10k")
            sch.write("power.kicad_sch")
        """
        from kicad_tools.sexp import parse_file

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Schematic file not found: {path}")

        doc = parse_file(path)
        return cls._from_sexp(doc)

    @classmethod
    def _from_sexp(cls, doc: SExp) -> Schematic:
        """Create a Schematic from a parsed S-expression tree.

        This is the internal method that does the actual parsing.
        """
        from .schematic import SnapMode

        # Extract title block info
        title = ""
        date = ""
        revision = ""
        company = ""
        comment1 = ""
        comment2 = ""

        tb = doc.get("title_block")
        if tb:
            title_node = tb.get("title")
            if title_node:
                title = str(title_node.get_first_atom() or "")
            date_node = tb.get("date")
            if date_node:
                date = str(date_node.get_first_atom() or "")
            rev_node = tb.get("rev")
            if rev_node:
                revision = str(rev_node.get_first_atom() or "")
            company_node = tb.get("company")
            if company_node:
                company = str(company_node.get_first_atom() or "")
            # Comments are numbered
            for comment_node in tb.find_all("comment"):
                atoms = comment_node.get_atoms()
                if len(atoms) >= 2:
                    num = int(atoms[0])
                    text = str(atoms[1])
                    if num == 1:
                        comment1 = text
                    elif num == 2:
                        comment2 = text

        # Get paper size
        paper_node = doc.get("paper")
        paper = str(paper_node.get_first_atom()) if paper_node else "A4"

        # Get UUID
        uuid_node_elem = doc.get("uuid")
        sheet_uuid = str(uuid_node_elem.get_first_atom()) if uuid_node_elem else str(uuid.uuid4())

        # Parse lib_symbols to get embedded symbol definitions
        embedded_lib_symbols: dict[str, SExp] = {}
        lib_symbols_node = doc.get("lib_symbols")
        if lib_symbols_node:
            for sym_node in lib_symbols_node.children:
                if sym_node.name == "symbol":
                    sym_name = str(sym_node.get_first_atom())
                    embedded_lib_symbols[sym_name] = sym_node

        # Create schematic instance with minimal init
        # We disable snapping for loaded schematics to preserve coordinates
        sch = cls(
            title=title,
            date=date,
            revision=revision,
            company=company,
            comment1=comment1,
            comment2=comment2,
            paper=paper,
            sheet_uuid=sheet_uuid,
            snap_mode=SnapMode.OFF,  # Preserve original coordinates
        )

        # Store embedded lib_symbols for round-trip
        sch._embedded_lib_symbols = embedded_lib_symbols

        # Parse sheet_instances to get project name and parent info
        sheet_instances_node = doc.get("sheet_instances")
        if sheet_instances_node:
            project_node = sheet_instances_node.get("project")
            if project_node:
                sch.project_name = str(project_node.get_first_atom() or "project")
                path_node = project_node.get("path")
                if path_node:
                    page_node = path_node.get("page")
                    if page_node:
                        sch.page = str(page_node.get_first_atom() or "1")

        # Parse placed symbols (those with lib_id)
        for child in doc.children:
            if child.name == "symbol" and child.get("lib_id"):
                if PowerSymbol.is_power_symbol(child):
                    pwr = PowerSymbol.from_sexp(child)
                    sch.power_symbols.append(pwr)
                else:
                    sym = SymbolInstance.from_sexp(
                        child, symbol_defs=sch._symbol_defs, lib_symbols=embedded_lib_symbols
                    )
                    sch.symbols.append(sym)
                    # Cache the symbol def
                    sch._symbol_defs[sym.symbol_def.lib_id] = sym.symbol_def

        # Parse wires
        for child in doc.children:
            if child.name == "wire":
                sch.wires.append(Wire.from_sexp(child))

        # Parse junctions
        for child in doc.children:
            if child.name == "junction":
                sch.junctions.append(Junction.from_sexp(child))

        # Parse labels
        for child in doc.children:
            if child.name == "label":
                sch.labels.append(Label.from_sexp(child))

        # Parse hierarchical labels
        for child in doc.children:
            if child.name == "hierarchical_label":
                sch.hier_labels.append(HierarchicalLabel.from_sexp(child))

        # Parse text notes
        for child in doc.children:
            if child.name == "text":
                text = str(child.get_first_atom() or "")
                at_node = child.get("at")
                if at_node:
                    atoms = at_node.get_atoms()
                    x = round(float(atoms[0]), 2)
                    y = round(float(atoms[1]), 2)
                    sch.text_notes.append((text, x, y))

        # Update power counter based on existing power symbols
        max_pwr = 0
        for pwr in sch.power_symbols:
            # Extract number from #PWR01, #PWR02, etc.
            if pwr.reference.startswith("#PWR"):
                try:
                    num = int(pwr.reference[4:])
                    max_pwr = max(max_pwr, num)
                except ValueError:
                    pass
        sch._pwr_counter = max_pwr + 1

        _log_info(
            f"Loaded schematic: {len(sch.symbols)} symbols, "
            f"{len(sch.power_symbols)} power symbols, "
            f"{len(sch.wires)} wires"
        )

        return sch

    def _build_lib_symbols_node(self) -> SExp:
        """Build lib_symbols section as SExp node."""
        lib_symbols = SExp.list("lib_symbols")

        added_lib_ids = set()

        # First, add any embedded lib_symbols from loaded schematics
        for sym_name, sym_node in self._embedded_lib_symbols.items():
            lib_symbols.append(sym_node)
            added_lib_ids.add(sym_name)

        # Then add any new symbol defs that weren't embedded
        for sym_def in self._symbol_defs.values():
            if sym_def.lib_id not in added_lib_ids:
                for sym_node in sym_def.to_sexp_nodes():
                    lib_symbols.append(sym_node)
                    added_lib_ids.add(sym_def.lib_id)

        return lib_symbols

    def _build_text_note_node(self, text: str, x: float, y: float) -> SExp:
        """Build a text note as SExp node."""
        return text_node(text, x, y, str(uuid.uuid4()))

    def to_sexp_node(self) -> SExp:
        """Build complete schematic as SExp tree."""
        root = SExp.list(
            "kicad_sch",
            SExp.list("version", 20250114),
            SExp.list("generator", "eeschema"),
            SExp.list("generator_version", "9.0"),
            uuid_node(self.sheet_uuid),
            SExp.list("paper", self.paper),
        )

        # Title block
        root.append(
            title_block(
                title=self.title,
                date=self.date,
                revision=self.revision,
                company=self.company,
                comment1=self.comment1,
                comment2=self.comment2,
            )
        )

        # Library symbols
        root.append(self._build_lib_symbols_node())

        # Symbol instances
        for sym in self.symbols:
            root.append(sym.to_sexp_node(self.project_name, self.sheet_path))

        # Power symbols
        for pwr in self.power_symbols:
            root.append(pwr.to_sexp_node(self.project_name, self.sheet_path))

        # Wires
        for wire in self.wires:
            root.append(wire.to_sexp_node())

        # Junctions
        for junc in self.junctions:
            root.append(junc.to_sexp_node())

        # Labels
        for label in self.labels:
            root.append(label.to_sexp_node())

        # Hierarchical labels
        for hl in self.hier_labels:
            root.append(hl.to_sexp_node())

        # Text notes
        for text, x, y in self.text_notes:
            root.append(self._build_text_note_node(text, x, y))

        # Sheet instances
        root.append(sheet_instances(self.sheet_path, self.page))

        return root

    def to_sexp(self) -> str:
        """Generate complete schematic S-expression string."""
        return self.to_sexp_node().to_string()

    def write(self, path: str | Path):
        """Write schematic to file."""
        path = Path(path)
        content = self.to_sexp()
        path.write_text(content)
        _log_info(
            f"Wrote schematic to {path} ({len(self.symbols)} symbols, {len(self.wires)} wires)"
        )

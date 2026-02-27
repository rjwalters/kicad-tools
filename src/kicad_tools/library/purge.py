"""Detect unused symbols and footprints in KiCad project-local libraries.

This module scans a KiCad project directory to find symbols defined in local
``.kicad_sym`` files and footprints defined in local ``.pretty`` directories
that are not referenced by any schematic (``.kicad_sch``) or PCB
(``.kicad_pcb``) file in the project.

Usage::

    from kicad_tools.library.purge import UnusedLibraryAnalyzer

    analyzer = UnusedLibraryAnalyzer(Path("/path/to/kicad/project"))
    result = analyzer.analyze()
    for item in result.unused_symbols:
        print(f"Unused symbol: {item.library}:{item.name}")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.sexp import parse_sexp


@dataclass
class UnusedItem:
    """A single unused library item (symbol or footprint)."""

    library: str
    """Library name (e.g. ``my_project_lib``)."""

    name: str
    """Item name within the library (e.g. ``MyConnector``)."""

    file_path: Path
    """Path to the file containing this item."""

    @property
    def lib_id(self) -> str:
        """Full library identifier in ``Library:Name`` format."""
        return f"{self.library}:{self.name}"


@dataclass
class PurgeResult:
    """Result of analyzing a project for unused library items."""

    project_dir: Path
    """The project directory that was analyzed."""

    unused_symbols: list[UnusedItem] = field(default_factory=list)
    """Symbols defined in local ``.kicad_sym`` files but not referenced."""

    unused_footprints: list[UnusedItem] = field(default_factory=list)
    """Footprints defined in local ``.pretty`` dirs but not referenced."""

    @property
    def total_unused(self) -> int:
        """Total number of unused items."""
        return len(self.unused_symbols) + len(self.unused_footprints)

    def format_table(self) -> str:
        """Format the result as a human-readable table."""
        lines: list[str] = []

        if self.unused_symbols:
            lines.append("Unused Symbols")
            lines.append("-" * 60)
            for item in sorted(self.unused_symbols, key=lambda i: i.lib_id):
                lines.append(f"  {item.file_path.name}  ({item.lib_id})")
            lines.append("")

        if self.unused_footprints:
            lines.append("Unused Footprints")
            lines.append("-" * 60)
            for item in sorted(self.unused_footprints, key=lambda i: i.lib_id):
                lines.append(f"  {item.file_path.name}  ({item.lib_id})")
            lines.append("")

        if not self.unused_symbols and not self.unused_footprints:
            lines.append("No unused library items found.")

        return "\n".join(lines)

    def format_json(self) -> str:
        """Format the result as JSON."""
        data = {
            "project_dir": str(self.project_dir),
            "unused_symbols": [
                {
                    "library": item.library,
                    "name": item.name,
                    "lib_id": item.lib_id,
                    "file": str(item.file_path),
                }
                for item in self.unused_symbols
            ],
            "unused_footprints": [
                {
                    "library": item.library,
                    "name": item.name,
                    "lib_id": item.lib_id,
                    "file": str(item.file_path),
                }
                for item in self.unused_footprints
            ],
            "total_unused": self.total_unused,
        }
        return json.dumps(data, indent=2)


class UnusedLibraryAnalyzer:
    """Analyze a KiCad project to find unused symbols and footprints.

    Scans project-local ``.kicad_sym`` and ``.pretty`` directories and
    compares their contents against references in ``.kicad_sch`` and
    ``.kicad_pcb`` files.

    Parameters
    ----------
    project_dir:
        Path to the KiCad project directory.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    def analyze(self) -> PurgeResult:
        """Run the analysis and return the result."""
        result = PurgeResult(project_dir=self.project_dir)

        # Collect what is referenced
        used_symbol_ids = self._collect_used_symbols()
        used_footprint_ids = self._collect_used_footprints()

        # Collect what is available in local libraries
        available_symbols = self._collect_available_symbols()
        available_footprints = self._collect_available_footprints()

        # Compute unused = available - used
        for lib_id, (library, name, file_path) in available_symbols.items():
            if lib_id not in used_symbol_ids:
                result.unused_symbols.append(
                    UnusedItem(library=library, name=name, file_path=file_path)
                )

        for lib_id, (library, name, file_path) in available_footprints.items():
            if lib_id not in used_footprint_ids:
                result.unused_footprints.append(
                    UnusedItem(library=library, name=name, file_path=file_path)
                )

        return result

    # ------------------------------------------------------------------
    # Collecting used references
    # ------------------------------------------------------------------

    def _collect_used_symbols(self) -> set[str]:
        """Collect all symbol lib_id values referenced in schematics."""
        used: set[str] = set()
        for sch_path in self.project_dir.rglob("*.kicad_sch"):
            used.update(self._extract_symbol_refs(sch_path))
        return used

    def _collect_used_footprints(self) -> set[str]:
        """Collect all footprint references from PCB files and schematics.

        Footprint references are found in:
        - PCB files: ``(footprint "Lib:Name" ...)`` top-level entries
        - Schematic files: ``(property "Footprint" "Lib:Name" ...)`` on symbols
        """
        used: set[str] = set()
        for pcb_path in self.project_dir.rglob("*.kicad_pcb"):
            used.update(self._extract_footprint_refs_pcb(pcb_path))
        for sch_path in self.project_dir.rglob("*.kicad_sch"):
            used.update(self._extract_footprint_refs_sch(sch_path))
        return used

    @staticmethod
    def _extract_symbol_refs(sch_path: Path) -> set[str]:
        """Extract symbol lib_id values from a schematic file."""
        refs: set[str] = set()
        try:
            text = sch_path.read_text(encoding="utf-8")
            sexp = parse_sexp(text)
        except Exception:
            return refs

        # Top-level (symbol (lib_id "Lib:Name") ...) nodes
        for child in sexp.children:
            if child.name == "symbol":
                lib_id_node = child.get("lib_id")
                if lib_id_node and lib_id_node.children:
                    first = lib_id_node.children[0]
                    if first.is_atom and isinstance(first.value, str):
                        refs.add(first.value)
        return refs

    @staticmethod
    def _extract_footprint_refs_pcb(pcb_path: Path) -> set[str]:
        """Extract footprint library IDs from a PCB file.

        PCB files have top-level ``(footprint "Lib:Name" ...)`` nodes
        where the first child atom is the library ID.
        """
        refs: set[str] = set()
        try:
            text = pcb_path.read_text(encoding="utf-8")
            sexp = parse_sexp(text)
        except Exception:
            return refs

        for child in sexp.children:
            if child.name == "footprint" and child.children:
                first = child.children[0]
                if first.is_atom and isinstance(first.value, str):
                    refs.add(first.value)
        return refs

    @staticmethod
    def _extract_footprint_refs_sch(sch_path: Path) -> set[str]:
        """Extract footprint references from schematic symbol properties.

        Schematics store footprint assignments as:
        ``(property "Footprint" "Lib:Name" ...)``
        """
        refs: set[str] = set()
        try:
            text = sch_path.read_text(encoding="utf-8")
            sexp = parse_sexp(text)
        except Exception:
            return refs

        for symbol_node in sexp.children:
            if symbol_node.name != "symbol":
                continue
            for prop in symbol_node.children:
                if prop.name != "property":
                    continue
                # property children: first atom = key, second atom = value
                atoms = [c for c in prop.children if c.is_atom]
                if (
                    len(atoms) >= 2
                    and atoms[0].value == "Footprint"
                    and isinstance(atoms[1].value, str)
                    and atoms[1].value  # skip empty footprint assignments
                ):
                    refs.add(atoms[1].value)
        return refs

    # ------------------------------------------------------------------
    # Collecting available items in project-local libraries
    # ------------------------------------------------------------------

    def _collect_available_symbols(self) -> dict[str, tuple[str, str, Path]]:
        """Collect symbols from project-local ``.kicad_sym`` files.

        Returns a dict mapping ``lib_id`` to ``(library, name, file_path)``.
        The library name is derived from the filename stem.
        """
        available: dict[str, tuple[str, str, Path]] = {}

        for sym_path in self.project_dir.rglob("*.kicad_sym"):
            library_name = sym_path.stem  # e.g. "my_project_lib"
            try:
                text = sym_path.read_text(encoding="utf-8")
                sexp = parse_sexp(text)
            except Exception:
                continue

            if sexp.name != "kicad_symbol_lib":
                continue

            for child in sexp.children:
                if child.name != "symbol":
                    continue
                # The first atom child is the symbol name
                atoms = [c for c in child.children if c.is_atom]
                if atoms and isinstance(atoms[0].value, str):
                    symbol_name = atoms[0].value
                    # Skip sub-units (names like "SymbolName_0_1")
                    # Top-level symbols have a name like "Lib:Name" or just "Name"
                    # Sub-units are children of top-level symbols, so they
                    # appear nested -- we only see top-level here.
                    lib_id = f"{library_name}:{symbol_name}"
                    available[lib_id] = (library_name, symbol_name, sym_path)

        return available

    def _collect_available_footprints(self) -> dict[str, tuple[str, str, Path]]:
        """Collect footprints from project-local ``.pretty`` directories.

        Returns a dict mapping ``lib_id`` to ``(library, name, file_path)``.
        The library name is derived from the ``.pretty`` directory name stem.
        """
        available: dict[str, tuple[str, str, Path]] = {}

        for pretty_dir in self.project_dir.rglob("*.pretty"):
            if not pretty_dir.is_dir():
                continue
            library_name = pretty_dir.stem  # e.g. "my_project_lib"

            for mod_file in pretty_dir.glob("*.kicad_mod"):
                footprint_name = mod_file.stem
                lib_id = f"{library_name}:{footprint_name}"
                available[lib_id] = (library_name, footprint_name, mod_file)

        return available

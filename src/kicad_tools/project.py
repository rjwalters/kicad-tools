"""
KiCad project handling.

Provides a unified interface to work with complete KiCad projects,
cross-referencing schematics and PCBs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .schema.schematic import Schematic
from .schema.pcb import PCB
from .schema.bom import extract_bom, BOM

logger = logging.getLogger(__name__)


@dataclass
class UnplacedSymbol:
    """A symbol in schematic that has no corresponding footprint on PCB."""

    reference: str
    value: str
    lib_id: str
    footprint_name: str  # Expected footprint


@dataclass
class OrphanedFootprint:
    """A footprint on PCB with no corresponding symbol in schematic."""

    reference: str
    value: str
    footprint_name: str
    position: Tuple[float, float]


@dataclass
class MismatchedComponent:
    """A component where schematic and PCB data don't match."""

    reference: str
    schematic_value: str
    pcb_value: str
    schematic_footprint: str
    pcb_footprint: str
    mismatches: List[str] = field(default_factory=list)


@dataclass
class CrossReferenceResult:
    """Result of cross-referencing schematic and PCB."""

    matched: int = 0
    unplaced: List[UnplacedSymbol] = field(default_factory=list)
    orphaned: List[OrphanedFootprint] = field(default_factory=list)
    mismatched: List[MismatchedComponent] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """Check if cross-reference is clean (no issues)."""
        return (
            len(self.unplaced) == 0
            and len(self.orphaned) == 0
            and len(self.mismatched) == 0
        )

    def summary(self) -> Dict[str, int]:
        """Get summary counts."""
        return {
            "matched": self.matched,
            "unplaced": len(self.unplaced),
            "orphaned": len(self.orphaned),
            "mismatched": len(self.mismatched),
        }


class Project:
    """
    High-level interface to a complete KiCad project.

    Provides methods for working with project files, cross-referencing
    schematics and PCBs, and generating manufacturing outputs.

    Example::

        from kicad_tools import Project

        # Load from .kicad_pro file
        project = Project.load("myproject.kicad_pro")

        # Or specify files directly
        project = Project(
            schematic="myproject.kicad_sch",
            pcb="myproject.kicad_pcb",
        )

        # Cross-reference
        result = project.cross_reference()
        if not result.is_clean:
            print(f"Unplaced: {len(result.unplaced)}")
            print(f"Orphaned: {len(result.orphaned)}")

        # Generate BOM
        bom = project.get_bom()

        # Export for manufacturing
        project.export_assembly("output/", manufacturer="jlcpcb")
    """

    def __init__(
        self,
        schematic: Optional[str | Path] = None,
        pcb: Optional[str | Path] = None,
        project_file: Optional[str | Path] = None,
    ):
        """
        Initialize project.

        Args:
            schematic: Path to schematic file (.kicad_sch)
            pcb: Path to PCB file (.kicad_pcb)
            project_file: Path to project file (.kicad_pro)
        """
        self.project_file = Path(project_file) if project_file else None
        self._schematic_path = Path(schematic) if schematic else None
        self._pcb_path = Path(pcb) if pcb else None

        # Lazy-loaded instances
        self._schematic: Optional[Schematic] = None
        self._pcb: Optional[PCB] = None
        self._bom: Optional[BOM] = None

    @classmethod
    def load(cls, project_path: str | Path) -> "Project":
        """
        Load a KiCad project from .kicad_pro file.

        Args:
            project_path: Path to .kicad_pro file

        Returns:
            Project instance

        Raises:
            FileNotFoundError: If project file doesn't exist
        """
        project_path = Path(project_path)
        if not project_path.exists():
            raise FileNotFoundError(f"Project file not found: {project_path}")

        # Parse project file to find related files
        project_dir = project_path.parent
        project_name = project_path.stem

        # Look for schematic and PCB
        schematic_path = project_dir / f"{project_name}.kicad_sch"
        pcb_path = project_dir / f"{project_name}.kicad_pcb"

        return cls(
            schematic=schematic_path if schematic_path.exists() else None,
            pcb=pcb_path if pcb_path.exists() else None,
            project_file=project_path,
        )

    @classmethod
    def from_pcb(cls, pcb_path: str | Path) -> "Project":
        """
        Create project from PCB file, finding related schematic.

        Args:
            pcb_path: Path to .kicad_pcb file

        Returns:
            Project instance
        """
        pcb_path = Path(pcb_path)
        schematic_path = pcb_path.with_suffix(".kicad_sch")
        project_path = pcb_path.with_suffix(".kicad_pro")

        return cls(
            schematic=schematic_path if schematic_path.exists() else None,
            pcb=pcb_path,
            project_file=project_path if project_path.exists() else None,
        )

    @property
    def name(self) -> str:
        """Get project name."""
        if self.project_file:
            return self.project_file.stem
        if self._pcb_path:
            return self._pcb_path.stem
        if self._schematic_path:
            return self._schematic_path.stem
        return "unnamed"

    @property
    def directory(self) -> Optional[Path]:
        """Get project directory."""
        if self.project_file:
            return self.project_file.parent
        if self._pcb_path:
            return self._pcb_path.parent
        if self._schematic_path:
            return self._schematic_path.parent
        return None

    @property
    def schematic(self) -> Optional[Schematic]:
        """Get loaded schematic (lazy loaded)."""
        if self._schematic is None and self._schematic_path:
            if self._schematic_path.exists():
                self._schematic = Schematic.load(self._schematic_path)
        return self._schematic

    @property
    def pcb(self) -> Optional[PCB]:
        """Get loaded PCB (lazy loaded)."""
        if self._pcb is None and self._pcb_path:
            if self._pcb_path.exists():
                self._pcb = PCB.load(str(self._pcb_path))
        return self._pcb

    def get_bom(self, force_reload: bool = False) -> Optional[BOM]:
        """
        Get bill of materials from schematic.

        Args:
            force_reload: Force reload from schematic

        Returns:
            BOM object, or None if no schematic
        """
        if self._bom is None or force_reload:
            if self._schematic_path and self._schematic_path.exists():
                self._bom = extract_bom(self._schematic_path)
        return self._bom

    def cross_reference(self) -> CrossReferenceResult:
        """
        Cross-reference schematic symbols with PCB footprints.

        Checks for:
        - Symbols without footprints (unplaced)
        - Footprints without symbols (orphaned)
        - Value/footprint mismatches

        Returns:
            CrossReferenceResult with findings
        """
        result = CrossReferenceResult()

        if not self.schematic or not self.pcb:
            logger.warning("Cannot cross-reference: missing schematic or PCB")
            return result

        # Build reference sets
        sch_refs: Dict[str, dict] = {}
        for sym in self.schematic.symbols:
            if sym.reference and not sym.reference.startswith("#"):
                sch_refs[sym.reference] = {
                    "value": sym.value,
                    "lib_id": sym.lib_id,
                    "footprint": getattr(sym, "footprint", ""),
                }

        pcb_refs: Dict[str, dict] = {}
        for fp in self.pcb.footprints:
            if fp.reference and not fp.reference.startswith("#"):
                pcb_refs[fp.reference] = {
                    "value": fp.value,
                    "footprint": fp.name,
                    "position": fp.position,
                }

        # Find matches, unplaced, and orphaned
        sch_set = set(sch_refs.keys())
        pcb_set = set(pcb_refs.keys())

        matched_refs = sch_set & pcb_set
        unplaced_refs = sch_set - pcb_set
        orphaned_refs = pcb_set - sch_set

        result.matched = len(matched_refs)

        # Check for mismatches in matched components
        for ref in matched_refs:
            sch_data = sch_refs[ref]
            pcb_data = pcb_refs[ref]

            mismatches = []
            if sch_data["value"] != pcb_data["value"]:
                mismatches.append("value")

            # Compare footprints (may have library prefix)
            sch_fp = sch_data.get("footprint", "")
            pcb_fp = pcb_data["footprint"]
            if sch_fp and pcb_fp:
                # Extract footprint name without library
                sch_fp_name = sch_fp.split(":")[-1] if ":" in sch_fp else sch_fp
                pcb_fp_name = pcb_fp.split(":")[-1] if ":" in pcb_fp else pcb_fp
                if sch_fp_name != pcb_fp_name:
                    mismatches.append("footprint")

            if mismatches:
                result.mismatched.append(MismatchedComponent(
                    reference=ref,
                    schematic_value=sch_data["value"],
                    pcb_value=pcb_data["value"],
                    schematic_footprint=sch_data.get("footprint", ""),
                    pcb_footprint=pcb_data["footprint"],
                    mismatches=mismatches,
                ))

        # Record unplaced symbols
        for ref in unplaced_refs:
            sch_data = sch_refs[ref]
            result.unplaced.append(UnplacedSymbol(
                reference=ref,
                value=sch_data["value"],
                lib_id=sch_data["lib_id"],
                footprint_name=sch_data.get("footprint", ""),
            ))

        # Record orphaned footprints
        for ref in orphaned_refs:
            pcb_data = pcb_refs[ref]
            result.orphaned.append(OrphanedFootprint(
                reference=ref,
                value=pcb_data["value"],
                footprint_name=pcb_data["footprint"],
                position=pcb_data["position"],
            ))

        return result

    def find_unplaced_symbols(self) -> List[UnplacedSymbol]:
        """
        Find symbols in schematic that aren't on PCB.

        Returns:
            List of unplaced symbols
        """
        return self.cross_reference().unplaced

    def find_orphaned_footprints(self) -> List[OrphanedFootprint]:
        """
        Find footprints on PCB that aren't in schematic.

        Returns:
            List of orphaned footprints
        """
        return self.cross_reference().orphaned

    def export_assembly(
        self,
        output_dir: str | Path,
        manufacturer: str = "jlcpcb",
    ) -> "AssemblyPackageResult":
        """
        Export complete assembly package.

        Args:
            output_dir: Output directory
            manufacturer: Manufacturer ID

        Returns:
            AssemblyPackageResult with generated files
        """
        from .export import create_assembly_package

        if not self._pcb_path:
            raise ValueError("PCB path required for assembly export")

        return create_assembly_package(
            pcb=self._pcb_path,
            schematic=self._schematic_path,
            manufacturer=manufacturer,
            output_dir=output_dir,
        )

    def __repr__(self) -> str:
        parts = [f"Project({self.name!r}"]
        if self._schematic_path:
            parts.append(f"schematic={self._schematic_path.name!r}")
        if self._pcb_path:
            parts.append(f"pcb={self._pcb_path.name!r}")
        return ", ".join(parts) + ")"

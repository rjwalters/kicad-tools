"""
Assembly package generator.

Creates complete manufacturing packages including BOM, CPL, and Gerbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .bom_formats import BOMExportConfig, export_bom
from .gerber import GerberConfig, GerberExporter, MANUFACTURER_PRESETS
from .pnp import PnPExportConfig, export_pnp

logger = logging.getLogger(__name__)


@dataclass
class AssemblyConfig:
    """Configuration for assembly package generation."""

    # Output settings
    output_dir: Path = field(default_factory=lambda: Path("assembly"))

    # BOM settings
    include_bom: bool = True
    bom_filename: str = "bom_{manufacturer}.csv"
    bom_config: Optional[BOMExportConfig] = None

    # CPL/PnP settings
    include_pnp: bool = True
    pnp_filename: str = "cpl_{manufacturer}.csv"
    pnp_config: Optional[PnPExportConfig] = None

    # Gerber settings
    include_gerbers: bool = True
    gerbers_subdir: str = "gerbers"
    gerber_config: Optional[GerberConfig] = None

    # Filtering
    exclude_references: List[str] = field(default_factory=list)


@dataclass
class AssemblyPackageResult:
    """Result of assembly package generation."""

    output_dir: Path
    bom_path: Optional[Path] = None
    pnp_path: Optional[Path] = None
    gerber_path: Optional[Path] = None
    errors: List[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Check if generation was successful."""
        return len(self.errors) == 0

    def __str__(self) -> str:
        lines = [f"Assembly Package: {self.output_dir}"]
        if self.bom_path:
            lines.append(f"  BOM: {self.bom_path.name}")
        if self.pnp_path:
            lines.append(f"  CPL: {self.pnp_path.name}")
        if self.gerber_path:
            lines.append(f"  Gerbers: {self.gerber_path.name}")
        if self.errors:
            lines.append(f"  Errors: {len(self.errors)}")
            for err in self.errors:
                lines.append(f"    - {err}")
        return "\n".join(lines)


class AssemblyPackage:
    """
    Generate complete assembly packages for PCB manufacturing.

    Combines BOM, pick-and-place (CPL), and Gerber files into
    manufacturer-ready packages.

    Example::

        # Quick export for JLCPCB
        pkg = AssemblyPackage.create(
            pcb="board.kicad_pcb",
            schematic="board.kicad_sch",
            manufacturer="jlcpcb",
        )
        result = pkg.export("output/")
        print(f"Created: {result.bom_path}, {result.pnp_path}, {result.gerber_path}")

        # Custom configuration
        config = AssemblyConfig(
            include_gerbers=False,
            exclude_references=["TP*", "MH*"],
        )
        pkg = AssemblyPackage(
            pcb_path="board.kicad_pcb",
            schematic_path="board.kicad_sch",
            manufacturer="pcbway",
            config=config,
        )
        result = pkg.export()
    """

    def __init__(
        self,
        pcb_path: str | Path,
        schematic_path: Optional[str | Path] = None,
        manufacturer: str = "jlcpcb",
        config: Optional[AssemblyConfig] = None,
    ):
        """
        Initialize assembly package generator.

        Args:
            pcb_path: Path to KiCad PCB file (.kicad_pcb)
            schematic_path: Path to KiCad schematic file (.kicad_sch)
                           If not provided, will look for same name as PCB
            manufacturer: Manufacturer ID (jlcpcb, pcbway, etc.)
            config: Assembly configuration
        """
        self.pcb_path = Path(pcb_path)
        if not self.pcb_path.exists():
            raise FileNotFoundError(f"PCB file not found: {pcb_path}")

        # Find schematic
        if schematic_path:
            self.schematic_path = Path(schematic_path)
        else:
            # Look for schematic with same name
            self.schematic_path = self.pcb_path.with_suffix(".kicad_sch")

        if not self.schematic_path.exists():
            logger.warning(f"Schematic not found: {self.schematic_path}")
            self.schematic_path = None

        self.manufacturer = manufacturer.lower()
        self.config = config or AssemblyConfig()

    @classmethod
    def create(
        cls,
        pcb: str | Path,
        schematic: Optional[str | Path] = None,
        manufacturer: str = "jlcpcb",
        output_dir: Optional[str | Path] = None,
    ) -> "AssemblyPackage":
        """
        Factory method for quick assembly package creation.

        Args:
            pcb: Path to PCB file
            schematic: Path to schematic file (optional)
            manufacturer: Manufacturer ID
            output_dir: Output directory (optional)

        Returns:
            Configured AssemblyPackage instance
        """
        config = AssemblyConfig()
        if output_dir:
            config.output_dir = Path(output_dir)

        return cls(pcb, schematic, manufacturer, config)

    def export(self, output_dir: Optional[str | Path] = None) -> AssemblyPackageResult:
        """
        Generate complete assembly package.

        Args:
            output_dir: Output directory (overrides config)

        Returns:
            AssemblyPackageResult with paths and any errors
        """
        out_dir = Path(output_dir) if output_dir else self.config.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        result = AssemblyPackageResult(output_dir=out_dir)

        # Generate BOM
        if self.config.include_bom:
            try:
                result.bom_path = self._generate_bom(out_dir)
            except Exception as e:
                result.errors.append(f"BOM generation failed: {e}")
                logger.error(f"BOM generation failed: {e}")

        # Generate CPL
        if self.config.include_pnp:
            try:
                result.pnp_path = self._generate_pnp(out_dir)
            except Exception as e:
                result.errors.append(f"CPL generation failed: {e}")
                logger.error(f"CPL generation failed: {e}")

        # Generate Gerbers
        if self.config.include_gerbers:
            try:
                result.gerber_path = self._generate_gerbers(out_dir)
            except Exception as e:
                result.errors.append(f"Gerber generation failed: {e}")
                logger.error(f"Gerber generation failed: {e}")

        return result

    def _generate_bom(self, output_dir: Path) -> Path:
        """Generate BOM file."""
        if not self.schematic_path:
            raise ValueError("Schematic path required for BOM generation")

        # Import here to avoid circular imports
        from ..schema.bom import extract_bom

        bom = extract_bom(self.schematic_path)
        items = bom.items

        # Filter excluded references
        if self.config.exclude_references:
            items = self._filter_references(items)

        # Generate BOM
        bom_config = self.config.bom_config or BOMExportConfig()
        bom_csv = export_bom(items, self.manufacturer, bom_config)

        # Write to file
        filename = self.config.bom_filename.format(manufacturer=self.manufacturer)
        bom_path = output_dir / filename
        bom_path.write_text(bom_csv)

        logger.info(f"Generated BOM: {bom_path}")
        return bom_path

    def _generate_pnp(self, output_dir: Path) -> Path:
        """Generate pick-and-place file."""
        # Import here to avoid circular imports
        from ..schema.pcb import PCB

        pcb = PCB.load(str(self.pcb_path))
        footprints = list(pcb.footprints)

        # Filter excluded references
        if self.config.exclude_references:
            footprints = [
                fp for fp in footprints
                if not self._is_excluded(fp.reference)
            ]

        # Generate CPL
        pnp_config = self.config.pnp_config or PnPExportConfig()
        pnp_csv = export_pnp(footprints, self.manufacturer, pnp_config)

        # Write to file
        filename = self.config.pnp_filename.format(manufacturer=self.manufacturer)
        pnp_path = output_dir / filename
        pnp_path.write_text(pnp_csv)

        logger.info(f"Generated CPL: {pnp_path}")
        return pnp_path

    def _generate_gerbers(self, output_dir: Path) -> Path:
        """Generate Gerber files."""
        gerber_dir = output_dir / self.config.gerbers_subdir

        exporter = GerberExporter(self.pcb_path)

        if self.manufacturer in MANUFACTURER_PRESETS:
            return exporter.export_for_manufacturer(self.manufacturer, gerber_dir)
        else:
            config = self.config.gerber_config or GerberConfig()
            return exporter.export(config, gerber_dir)

    def _filter_references(self, items: list) -> list:
        """Filter items by excluded reference patterns."""
        import fnmatch
        result = []
        for item in items:
            if not self._is_excluded(item.reference):
                result.append(item)
        return result

    def _is_excluded(self, reference: str) -> bool:
        """Check if reference matches any exclusion pattern."""
        import fnmatch
        for pattern in self.config.exclude_references:
            if fnmatch.fnmatch(reference, pattern):
                return True
        return False


def create_assembly_package(
    pcb: str | Path,
    schematic: Optional[str | Path] = None,
    manufacturer: str = "jlcpcb",
    output_dir: Optional[str | Path] = None,
) -> AssemblyPackageResult:
    """
    Convenience function to create assembly package.

    Args:
        pcb: Path to PCB file
        schematic: Path to schematic file (optional)
        manufacturer: Manufacturer ID
        output_dir: Output directory

    Returns:
        AssemblyPackageResult with generated files
    """
    pkg = AssemblyPackage.create(pcb, schematic, manufacturer, output_dir)
    return pkg.export()

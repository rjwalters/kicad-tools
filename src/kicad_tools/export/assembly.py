"""
Assembly package generator.

Creates complete manufacturing packages including BOM, CPL, and Gerbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ValidationError

from .bom_enrich import EnrichmentReport, enrich_bom_lcsc
from .bom_formats import BOMExportConfig, export_bom
from .bom_spec_overlay import SpecOverlayReport, apply_spec_overlay, find_spec_file
from .gerber import MANUFACTURER_PRESETS, GerberConfig, GerberExporter
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
    bom_config: BOMExportConfig | None = None

    # CPL/PnP settings
    include_pnp: bool = True
    pnp_filename: str = "cpl_{manufacturer}.csv"
    pnp_config: PnPExportConfig | None = None

    # Gerber settings
    include_gerbers: bool = True
    gerbers_subdir: str = "gerbers"
    gerber_config: GerberConfig | None = None

    # LCSC auto-matching
    auto_lcsc: bool = True
    auto_lcsc_prefer_basic: bool = True
    auto_lcsc_min_stock: int = 100

    # Spec overlay
    no_spec: bool = False
    spec_path: Path | None = None

    # BOM source: "schematic" (default), "pcb", or "auto"
    bom_source: str = "schematic"

    # Filtering
    exclude_references: list[str] = field(default_factory=list)


@dataclass
class AssemblyPackageResult:
    """Result of assembly package generation."""

    output_dir: Path
    bom_path: Path | None = None
    pnp_path: Path | None = None
    gerber_path: Path | None = None
    spec_overlay: SpecOverlayReport | None = None
    lcsc_enrichment: EnrichmentReport | None = None
    errors: list[str] = field(default_factory=list)

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
        schematic_path: str | Path | None = None,
        manufacturer: str = "jlcpcb",
        config: AssemblyConfig | None = None,
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
            raise KiCadFileNotFoundError(
                "PCB file not found",
                context={"file": str(pcb_path)},
                suggestions=["Check that the file path is correct"],
            )

        # Find schematic
        if schematic_path:
            self.schematic_path = Path(schematic_path)
        else:
            from kicad_tools.report.utils import find_schematic

            self.schematic_path = find_schematic(self.pcb_path)

        if self.schematic_path is not None and not self.schematic_path.exists():
            logger.warning(f"Schematic not found: {self.schematic_path}")
            self.schematic_path = None

        self.manufacturer = manufacturer.lower()
        self.config = config or AssemblyConfig()

    @classmethod
    def create(
        cls,
        pcb: str | Path,
        schematic: str | Path | None = None,
        manufacturer: str = "jlcpcb",
        output_dir: str | Path | None = None,
    ) -> AssemblyPackage:
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

    def export(self, output_dir: str | Path | None = None) -> AssemblyPackageResult:
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
                result.bom_path = self._generate_bom(out_dir, result)
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

    def _generate_bom(self, output_dir: Path, result: AssemblyPackageResult | None = None) -> Path:
        """Generate BOM file.

        When ``auto_lcsc`` is enabled (the default) and the target
        manufacturer is ``jlcpcb``, missing LCSC part numbers are
        populated automatically via :func:`enrich_bom_lcsc` before
        the BOM CSV is written.

        The ``bom_source`` config field controls where BOM data is
        sourced from:

        - ``"schematic"`` (default): extract from schematic symbols
        - ``"pcb"``: extract from PCB footprints (no schematic needed)
        - ``"auto"``: use schematic by default, fall back to PCB when
          reference sets diverge significantly

        Args:
            output_dir: Directory to write the BOM CSV into.
            result: Optional result object to attach enrichment report.

        Returns:
            Path to the written BOM CSV.
        """
        bom_source = self.config.bom_source

        if bom_source == "pcb":
            from ..schema.bom import extract_bom_from_pcb

            bom = extract_bom_from_pcb(str(self.pcb_path))
            items = bom.items
        elif bom_source == "auto":
            items = self._resolve_auto_bom_source()
        else:
            # Default: schematic source
            if not self.schematic_path:
                raise ValidationError(
                    ["Schematic path required for BOM generation"],
                    context={"pcb": str(self.pcb_path)},
                    suggestions=[
                        "Provide a schematic file path when creating the AssemblyPackage",
                        "Use --bom-source pcb to generate BOM from PCB footprints instead",
                    ],
                )

            from ..schema.bom import extract_bom

            bom = extract_bom(self.schematic_path)
            items = bom.items

        # Filter excluded references
        if self.config.exclude_references:
            items = self._filter_references(items)

        # Spec overlay: apply MPN/LCSC from .kct before auto-enrichment
        spec_refs: set[str] = set()
        if not self.config.no_spec:
            try:
                spec_file = self.config.spec_path
                if spec_file is None:
                    # Auto-detect from schematic/PCB directory
                    spec_file = find_spec_file(self.pcb_path.parent)
                if spec_file is not None:
                    from ..spec.parser import load_spec

                    spec = load_spec(spec_file)
                    if spec.bom_entries:
                        overlay_report = apply_spec_overlay(items, spec.bom_entries)
                        if result is not None:
                            result.spec_overlay = overlay_report
                        for line in overlay_report.summary_lines():
                            logger.info(line)
                        # Collect refs that got an LCSC from spec
                        spec_refs = {
                            e.reference
                            for e in overlay_report.entries
                            if e.matched and e.lcsc
                        }
            except Exception as e:
                logger.warning(f"Spec overlay failed (continuing without): {e}")

        # LCSC auto-matching for JLCPCB exports
        if self.config.auto_lcsc and self.manufacturer == "jlcpcb":
            try:
                enrichment = enrich_bom_lcsc(
                    items,
                    prefer_basic=self.config.auto_lcsc_prefer_basic,
                    min_stock=self.config.auto_lcsc_min_stock,
                    spec_refs=spec_refs,
                )
                if result is not None:
                    result.lcsc_enrichment = enrichment
                for line in enrichment.summary_lines():
                    logger.info(line)
            except Exception as e:
                logger.warning(f"LCSC auto-matching failed (continuing without): {e}")

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
            footprints = [fp for fp in footprints if not self._is_excluded(fp.reference)]

        # Generate CPL – pass None when no explicit config so that the
        # formatter can apply its own defaults (e.g. JLCPCB exclude_tht=True).
        pnp_csv = export_pnp(footprints, self.manufacturer, self.config.pnp_config)

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

    def _resolve_auto_bom_source(self) -> list:
        """Resolve BOM source automatically.

        Uses schematic when available and reference sets align with PCB.
        Falls back to PCB when the mismatch exceeds 10% or 5 references.
        """
        from ..schema.bom import extract_bom_from_pcb

        pcb_bom = extract_bom_from_pcb(str(self.pcb_path))
        pcb_refs = {item.reference for item in pcb_bom.items if not item.is_virtual}

        if self.schematic_path and self.schematic_path.exists():
            from ..schema.bom import extract_bom

            sch_bom = extract_bom(self.schematic_path)
            sch_refs = {
                item.reference
                for item in sch_bom.items
                if not item.is_virtual and not item.dnp
            }

            # Compute mismatch
            mismatch = len(sch_refs.symmetric_difference(pcb_refs))
            total = max(len(sch_refs), len(pcb_refs), 1)
            mismatch_pct = mismatch / total

            if mismatch <= 5 and mismatch_pct <= 0.10:
                logger.info(
                    "Auto BOM source: using schematic (mismatch: %d refs, %.0f%%)",
                    mismatch,
                    mismatch_pct * 100,
                )
                return sch_bom.items
            else:
                logger.warning(
                    "Auto BOM source: using PCB due to significant reference "
                    "mismatch with schematic (%d refs, %.0f%%)",
                    mismatch,
                    mismatch_pct * 100,
                )
                return pcb_bom.items
        else:
            logger.info("Auto BOM source: using PCB (no schematic available)")
            return pcb_bom.items

    def _filter_references(self, items: list) -> list:
        """Filter items by excluded reference patterns."""
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
    schematic: str | Path | None = None,
    manufacturer: str = "jlcpcb",
    output_dir: str | Path | None = None,
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

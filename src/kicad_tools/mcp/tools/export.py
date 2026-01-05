"""
Export tools for MCP server.

Provides tools for exporting PCB manufacturing files (Gerbers, drill files, BOM, PnP).
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from kicad_tools.export.assembly import AssemblyPackage
from kicad_tools.export.gerber import (
    MANUFACTURER_PRESETS,
    GerberConfig,
    GerberExporter,
)
from kicad_tools.mcp.types import (
    AssemblyExportResult,
    BOMExportResult,
    GerberExportResult,
    GerberFile,
    PnPExportResult,
    get_file_type,
)

logger = logging.getLogger(__name__)

# Supported manufacturers
SUPPORTED_MANUFACTURERS = ["generic", "jlcpcb", "pcbway", "oshpark", "seeed"]


def export_gerbers(
    pcb_path: str,
    output_dir: str,
    manufacturer: str = "generic",
    include_drill: bool = True,
    zip_output: bool = True,
) -> GerberExportResult:
    """
    Export Gerber files for PCB manufacturing.

    Generates all required Gerber layers (copper, soldermask, silkscreen, outline)
    and optionally drill files in Excellon format. Supports manufacturer presets
    for JLCPCB, OSHPark, PCBWay, and Seeed.

    Args:
        pcb_path: Path to .kicad_pcb file
        output_dir: Directory for output files
        manufacturer: Manufacturer preset ("generic", "jlcpcb", "pcbway", "oshpark", "seeed")
        include_drill: Include drill files (Excellon format)
        zip_output: Create zip archive of all files

    Returns:
        GerberExportResult with file paths, layer count, and any warnings.

    Example:
        >>> result = export_gerbers(
        ...     "/path/to/board.kicad_pcb",
        ...     "/tmp/gerbers",
        ...     manufacturer="jlcpcb",
        ... )
        >>> if result.success:
        ...     print(f"Generated {len(result.files)} files")
        ...     if result.zip_file:
        ...         print(f"Zip: {result.zip_file}")
    """
    pcb = Path(pcb_path)
    out_dir = Path(output_dir)
    warnings: list[str] = []

    # Validate inputs
    if not pcb.exists():
        return GerberExportResult(
            success=False,
            output_dir=str(out_dir),
            error=f"PCB file not found: {pcb_path}",
        )

    if pcb.suffix != ".kicad_pcb":
        warnings.append(f"Unusual file extension: {pcb.suffix} (expected .kicad_pcb)")

    manufacturer_lower = manufacturer.lower()
    if manufacturer_lower not in SUPPORTED_MANUFACTURERS:
        return GerberExportResult(
            success=False,
            output_dir=str(out_dir),
            error=f"Unknown manufacturer: {manufacturer}. "
            f"Supported: {', '.join(SUPPORTED_MANUFACTURERS)}",
            warnings=warnings,
        )

    try:
        # Create exporter
        exporter = GerberExporter(pcb)

        # Configure based on manufacturer
        if manufacturer_lower in MANUFACTURER_PRESETS:
            config = MANUFACTURER_PRESETS[manufacturer_lower].config
        else:
            config = GerberConfig()

        # Override settings based on parameters
        config.generate_drill = include_drill
        config.create_zip = zip_output

        # Export
        result_path = exporter.export(config, out_dir)

        # Collect file information
        files: list[GerberFile] = []
        out_dir.mkdir(parents=True, exist_ok=True)

        for file_path in out_dir.iterdir():
            if file_path.is_file():
                # Determine layer from filename
                layer = _extract_layer_from_filename(file_path.name)
                file_type = _determine_file_type(file_path.name, layer)

                files.append(
                    GerberFile(
                        filename=file_path.name,
                        layer=layer,
                        file_type=file_type,
                        size_bytes=file_path.stat().st_size,
                    )
                )

        # Determine zip file path
        zip_file: str | None = None
        if zip_output:
            zip_path = out_dir / config.zip_name
            if zip_path.exists():
                zip_file = str(zip_path)
            elif result_path.suffix == ".zip":
                zip_file = str(result_path)

        # Count copper layers
        copper_layers = [f for f in files if f.file_type == "copper"]
        layer_count = len(copper_layers)

        return GerberExportResult(
            success=True,
            output_dir=str(out_dir),
            zip_file=zip_file,
            files=files,
            layer_count=layer_count,
            warnings=warnings,
        )

    except Exception as e:
        logger.exception("Gerber export failed")
        return GerberExportResult(
            success=False,
            output_dir=str(out_dir),
            error=str(e),
            warnings=warnings,
        )


def _extract_layer_from_filename(filename: str) -> str:
    """Extract layer name from Gerber filename."""
    # Common patterns:
    # - project-F_Cu.gbr
    # - project-B_Cu.gbr
    # - project-F_Mask.gbr
    # - project-Edge_Cuts.gbr
    # - project-PTH.drl
    # - project-NPTH.drl

    name = Path(filename).stem
    parts = name.split("-")

    if len(parts) >= 2:
        layer_part = parts[-1]
        # Convert underscore back to dot for standard KiCad names
        layer_candidates = [
            ("F_Cu", "F.Cu"),
            ("B_Cu", "B.Cu"),
            ("In1_Cu", "In1.Cu"),
            ("In2_Cu", "In2.Cu"),
            ("In3_Cu", "In3.Cu"),
            ("In4_Cu", "In4.Cu"),
            ("F_Mask", "F.Mask"),
            ("B_Mask", "B.Mask"),
            ("F_SilkS", "F.SilkS"),
            ("B_SilkS", "B.SilkS"),
            ("F_Paste", "F.Paste"),
            ("B_Paste", "B.Paste"),
            ("Edge_Cuts", "Edge.Cuts"),
            ("F_Silkscreen", "F.SilkS"),
            ("B_Silkscreen", "B.SilkS"),
        ]
        for pattern, layer in layer_candidates:
            if pattern in layer_part:
                return layer

    # Drill files - check NPTH first since "NPTH" contains "PTH"
    if "NPTH" in filename.upper():
        return "NPTH"
    if "PTH" in filename.upper():
        return "PTH"
    if filename.endswith(".drl"):
        return "drill"

    return "unknown"


def _determine_file_type(filename: str, layer: str) -> str:
    """Determine file type from filename and layer."""
    suffix = Path(filename).suffix.lower()

    if suffix in (".drl", ".xln"):
        return "drill"

    if suffix == ".zip":
        return "archive"

    file_type = get_file_type(layer)
    if file_type != "other":
        return file_type

    # Fallback based on Protel extensions
    extension_types = {
        ".gtl": "copper",  # Top copper
        ".gbl": "copper",  # Bottom copper
        ".g2": "copper",  # Inner layer 2
        ".g3": "copper",  # Inner layer 3
        ".gts": "soldermask",  # Top soldermask
        ".gbs": "soldermask",  # Bottom soldermask
        ".gto": "silkscreen",  # Top silkscreen
        ".gbo": "silkscreen",  # Bottom silkscreen
        ".gtp": "paste",  # Top paste
        ".gbp": "paste",  # Bottom paste
        ".gm1": "outline",  # Mechanical layer / outline
        ".gko": "outline",  # Keep-out / outline
    }

    return extension_types.get(suffix, "other")


# Supported assembly manufacturers (subset that supports full assembly)
ASSEMBLY_MANUFACTURERS = ["generic", "jlcpcb", "pcbway", "seeed"]


def export_assembly(
    pcb_path: str,
    schematic_path: str,
    output_dir: str,
    manufacturer: str = "jlcpcb",
) -> AssemblyExportResult:
    """
    Generate complete assembly package for manufacturing.

    Creates a comprehensive manufacturing package including Gerber files,
    bill of materials (BOM), and pick-and-place (PnP/CPL) files tailored
    to the specified manufacturer's requirements.

    Args:
        pcb_path: Path to .kicad_pcb file
        schematic_path: Path to .kicad_sch file
        output_dir: Directory for output files
        manufacturer: Target manufacturer ("jlcpcb", "pcbway", "seeed", "generic")

    Returns:
        AssemblyExportResult with paths to all generated files, component counts,
        and any warnings about missing part numbers or rotation issues.

    Example:
        >>> result = export_assembly(
        ...     "/path/to/board.kicad_pcb",
        ...     "/path/to/board.kicad_sch",
        ...     "/tmp/manufacturing",
        ...     manufacturer="jlcpcb",
        ... )
        >>> if result.success:
        ...     print(f"Package ready: {result.zip_file}")
        ...     print(f"BOM has {result.bom.component_count} components")
    """
    pcb = Path(pcb_path)
    schematic = Path(schematic_path)
    out_dir = Path(output_dir)
    warnings: list[str] = []

    # Validate PCB file
    if not pcb.exists():
        return AssemblyExportResult(
            success=False,
            output_dir=str(out_dir),
            manufacturer=manufacturer,
            error=f"PCB file not found: {pcb_path}",
        )

    if pcb.suffix != ".kicad_pcb":
        warnings.append(f"Unusual PCB file extension: {pcb.suffix} (expected .kicad_pcb)")

    # Validate schematic file
    if not schematic.exists():
        return AssemblyExportResult(
            success=False,
            output_dir=str(out_dir),
            manufacturer=manufacturer,
            error=f"Schematic file not found: {schematic_path}",
            warnings=warnings,
        )

    if schematic.suffix != ".kicad_sch":
        warnings.append(
            f"Unusual schematic file extension: {schematic.suffix} (expected .kicad_sch)"
        )

    # Validate manufacturer
    manufacturer_lower = manufacturer.lower()
    if manufacturer_lower not in ASSEMBLY_MANUFACTURERS:
        return AssemblyExportResult(
            success=False,
            output_dir=str(out_dir),
            manufacturer=manufacturer,
            error=f"Unknown manufacturer: {manufacturer}. "
            f"Supported: {', '.join(ASSEMBLY_MANUFACTURERS)}",
            warnings=warnings,
        )

    try:
        # Create assembly package
        pkg = AssemblyPackage.create(
            pcb=pcb,
            schematic=schematic,
            manufacturer=manufacturer_lower,
            output_dir=out_dir,
        )

        # Export all files
        result = pkg.export(out_dir)

        # Convert to MCP result types
        gerbers_result = None
        bom_result = None
        pnp_result = None

        # Process Gerber results
        if result.gerber_path:
            gerber_files: list[GerberFile] = []
            gerber_dir = (
                result.gerber_path if result.gerber_path.is_dir() else result.gerber_path.parent
            )

            if gerber_dir.exists():
                for file_path in gerber_dir.iterdir():
                    if file_path.is_file():
                        layer = _extract_layer_from_filename(file_path.name)
                        file_type = _determine_file_type(file_path.name, layer)
                        gerber_files.append(
                            GerberFile(
                                filename=file_path.name,
                                layer=layer,
                                file_type=file_type,
                                size_bytes=file_path.stat().st_size,
                            )
                        )

            copper_layers = [f for f in gerber_files if f.file_type == "copper"]
            gerbers_result = GerberExportResult(
                success=True,
                output_dir=str(gerber_dir),
                files=gerber_files,
                layer_count=len(copper_layers),
            )

        # Process BOM results
        if result.bom_path and result.bom_path.exists():
            # Count components in BOM file
            bom_content = result.bom_path.read_text()
            lines = [line for line in bom_content.strip().split("\n") if line.strip()]
            # Subtract header row
            component_count = max(0, len(lines) - 1) if lines else 0

            # Count unique parts (assuming grouped BOM)
            unique_parts = component_count

            # Count missing LCSC parts (if JLCPCB format)
            missing_lcsc = 0
            if manufacturer_lower == "jlcpcb":
                for line in lines[1:]:  # Skip header
                    parts = line.split(",")
                    if len(parts) >= 4 and not parts[3].strip().strip('"'):
                        missing_lcsc += 1

            if missing_lcsc > 0:
                warnings.append(f"{missing_lcsc} parts missing LCSC part numbers")

            bom_result = BOMExportResult(
                output_path=str(result.bom_path),
                component_count=component_count,
                unique_parts=unique_parts,
                missing_lcsc=missing_lcsc,
            )

        # Process PnP results
        if result.pnp_path and result.pnp_path.exists():
            pnp_content = result.pnp_path.read_text()
            lines = [line for line in pnp_content.strip().split("\n") if line.strip()]
            # Subtract header row
            component_count = max(0, len(lines) - 1) if lines else 0

            # Determine which layers have components
            layers: list[str] = []
            for line in lines[1:]:
                line_lower = line.lower()
                if "top" in line_lower and "top" not in layers:
                    layers.append("top")
                if "bottom" in line_lower and "bottom" not in layers:
                    layers.append("bottom")

            pnp_result = PnPExportResult(
                output_path=str(result.pnp_path),
                component_count=component_count,
                layers=layers,
                rotation_corrections=0,  # Would need manufacturer-specific tracking
            )

        # Propagate errors from assembly package
        for error in result.errors:
            warnings.append(error)

        # Create combined zip file for upload
        zip_file_path = _create_assembly_zip(out_dir, manufacturer_lower, result)

        return AssemblyExportResult(
            success=result.success,
            output_dir=str(out_dir),
            manufacturer=manufacturer_lower,
            gerbers=gerbers_result,
            bom=bom_result,
            pnp=pnp_result,
            zip_file=str(zip_file_path) if zip_file_path else None,
            warnings=warnings,
            cost_estimate=None,  # Cost estimation could be added as future enhancement
        )

    except Exception as e:
        logger.exception("Assembly export failed")
        return AssemblyExportResult(
            success=False,
            output_dir=str(out_dir),
            manufacturer=manufacturer,
            error=str(e),
            warnings=warnings,
        )


def _create_assembly_zip(
    output_dir: Path,
    manufacturer: str,
    result,
) -> Path | None:
    """Create a combined zip file with all assembly files."""
    try:
        # Get project name from PCB filename
        project_name = "assembly"
        if result.bom_path:
            project_name = result.bom_path.stem.replace(f"_bom_{manufacturer}", "").replace(
                f"-bom-{manufacturer}", ""
            )

        zip_name = f"{project_name}-{manufacturer}-assembly.zip"
        zip_path = output_dir / zip_name

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Add BOM
            if result.bom_path and result.bom_path.exists():
                zf.write(result.bom_path, result.bom_path.name)

            # Add PnP/CPL
            if result.pnp_path and result.pnp_path.exists():
                zf.write(result.pnp_path, result.pnp_path.name)

            # Add Gerbers (either directory contents or zip)
            if result.gerber_path:
                if result.gerber_path.is_dir():
                    for file_path in result.gerber_path.iterdir():
                        if file_path.is_file():
                            zf.write(file_path, f"gerbers/{file_path.name}")
                elif result.gerber_path.is_file():
                    # Gerber path is already a zip, include it
                    zf.write(result.gerber_path, result.gerber_path.name)

        return zip_path

    except Exception as e:
        logger.warning(f"Failed to create assembly zip: {e}")
        return None

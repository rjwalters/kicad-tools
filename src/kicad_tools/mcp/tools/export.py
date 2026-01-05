"""
Export tools for MCP server.

Provides tools for exporting PCB manufacturing files (Gerbers, drill files).
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_tools.export.gerber import (
    MANUFACTURER_PRESETS,
    GerberConfig,
    GerberExporter,
)
from kicad_tools.mcp.types import (
    GerberExportResult,
    GerberFile,
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

"""
Export tools for MCP server.

Provides tools for exporting PCB manufacturing files (Gerbers, drill files, BOM, PnP).
"""

from __future__ import annotations

import dataclasses
import logging
import time
import zipfile
from pathlib import Path

from kicad_tools.export.bom_formats import (
    BOMExportConfig,
)
from kicad_tools.export.bom_formats import (
    export_bom as export_bom_formatted,
)
from kicad_tools.export.gerber import (
    MANUFACTURER_PRESETS,
    GerberConfig,
    GerberExporter,
)
from kicad_tools.mcp.types import (
    AssemblyExportResult,
    BOMExportResult,
    BOMGenerationResult,
    BOMItemResult,
    GerberExportResult,
    GerberFile,
    PnPExportResult,
    get_file_type,
)
from kicad_tools.schema.bom import extract_bom

logger = logging.getLogger(__name__)


# =============================================================================
# Assembly Validation
# =============================================================================


def _sibling_pcb_for_schematic(sch_file: Path) -> Path | None:
    """The board next to a schematic, preferring the routed one like ``kct export``."""
    for name in (f"{sch_file.stem}_routed.kicad_pcb", f"{sch_file.stem}.kicad_pcb"):
        candidate = sch_file.with_name(name)
        if candidate.is_file():
            return candidate
    return None


def validate_assembly_bom(
    schematic_path: str,
    quantity: int = 1,
    pcb_path: str | None = None,
) -> dict:
    """
    Validate a BOM for JLCPCB assembly.

    Checks all components against the LCSC/JLCPCB parts library and categorizes
    them by tier (Basic/Extended), stock status, and availability.  When a PCB
    is supplied or found next to the schematic, also runs ``kct export``'s
    ``bom_lcsc_values`` preflight check (Issue #7260): every assigned LCSC
    part's value and package against the BOM row and the PCB footprint.  A
    ``FAIL`` forces ``summary.assembly_ready`` false; a ``WARN`` (part not in
    the local cache or catalog, so not checked) does not.

    Args:
        schematic_path: Path to .kicad_sch file
        quantity: Number of boards (multiplies component quantities)
        pcb_path: Path to the .kicad_pcb (default: ``<stem>_routed.kicad_pcb``
            or ``<stem>.kicad_pcb`` next to the schematic)

    Returns:
        Dictionary with validation results including:
        - summary: Statistics on parts by tier and status
        - items: Detailed validation results for each BOM item
        - assembly_ready: Whether all parts are available
        - bom_lcsc_values: ``{status, message, details}`` when a PCB resolved

    Example:
        >>> result = validate_assembly_bom("/path/to/board.kicad_sch")
        >>> if result["summary"]["assembly_ready"]:
        ...     print("All parts available for assembly!")
        >>> else:
        ...     print(f"Missing/OOS: {result['summary']['out_of_stock']}")
    """
    from pathlib import Path

    sch_file = Path(schematic_path)

    # Validate schematic path
    if not sch_file.exists():
        return {
            "success": False,
            "error": f"Schematic file not found: {schematic_path}",
        }

    if sch_file.suffix != ".kicad_sch":
        pass  # Allow but could warn

    try:
        from kicad_tools.assembly.validation import AssemblyValidator

        bom = extract_bom(str(sch_file), hierarchical=True)

        with AssemblyValidator() as validator:
            result = validator.validate_bom(bom, quantity)

        # Return as dict with success flag
        output = result.to_dict()
        output["success"] = True
        # An empty (or all-DNP) BOM validated nothing; the library's
        # vacuous ``assembly_ready=True`` must not reach the agent as a
        # clean verdict (issue #6913).
        if not result.items:
            output["summary"]["assembly_ready"] = False
            output["warnings"] = [
                "No BOM items were validated (empty or all-DNP BOM); "
                "assembly readiness could not be determined."
            ]

        # Issue #7260: the value/package match ``kct export`` gates on.
        pcb_file = Path(pcb_path) if pcb_path else _sibling_pcb_for_schematic(sch_file)
        if pcb_file is not None and pcb_file.exists():
            from kicad_tools.export.preflight import PreflightChecker

            lcsc_values = PreflightChecker(
                pcb_path=pcb_file, schematic_path=sch_file
            ).check_bom_lcsc_values()
            output["bom_lcsc_values"] = {
                "status": lcsc_values.status,
                "message": lcsc_values.message,
                "details": lcsc_values.details,
            }
            if lcsc_values.status == "FAIL":
                output["summary"]["assembly_ready"] = False
        elif pcb_path:
            output.setdefault("warnings", []).append(
                f"PCB file not found: {pcb_path}; bom_lcsc_values was not checked."
            )
        return output

    except ImportError as e:
        return {
            "success": False,
            "error": f"Assembly validation requires 'requests' library: {e}",
        }
    except Exception as e:
        logger.exception("Assembly validation failed")
        return {
            "success": False,
            "error": str(e),
        }


def export_manufacturer_ids() -> list[str]:
    """Every id ``kct export --mfr`` accepts: registry ids/aliases plus ``generic``.

    Sourced from the manufacturer registry (Issue #7276) so the MCP tools can
    never drift from the CLI -- tier ids such as ``jlcpcb-tier1`` included.
    """
    from kicad_tools.manufacturers import get_all_manufacturer_names

    return [*get_all_manufacturer_names(), "generic"]


# Supported manufacturers (computed once, like ``kct export``'s ``--mfr`` choices)
SUPPORTED_MANUFACTURERS = export_manufacturer_ids()


def _gerber_files_in(directory: Path, since: float | None = None) -> list[GerberFile]:
    """List Gerber files in *directory*, looking inside any ``.zip`` archives.

    Loose files are listed as-is.  For every ``*.zip`` the archive's own entry
    is kept and its member names are added too, so a zipped export still
    counts its copper layers (issue #7115).  ``since`` restricts the listing
    to files (and archives) modified at or after that timestamp (issue #6913).
    """
    found: list[GerberFile] = []
    for file_path in sorted(directory.iterdir()):
        if not file_path.is_file():
            continue
        if since is not None and file_path.stat().st_mtime < since:
            continue
        layer = _extract_layer_from_filename(file_path.name)
        found.append(
            GerberFile(
                filename=file_path.name,
                layer=layer,
                file_type=_determine_file_type(file_path.name, layer),
                size_bytes=file_path.stat().st_size,
            )
        )
        if file_path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(file_path) as zf:
                    members = [i for i in zf.infolist() if not i.is_dir()]
            except (zipfile.BadZipFile, OSError):
                continue
            for info in members:
                name = Path(info.filename).name
                if any(f.filename == name for f in found):
                    continue  # also present loose; don't double count
                member_layer = _extract_layer_from_filename(name)
                found.append(
                    GerberFile(
                        filename=name,
                        layer=member_layer,
                        file_type=_determine_file_type(name, member_layer),
                        size_bytes=info.file_size,
                    )
                )
    return found


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
        # Copy the preset: assigning to the shared preset object would leak
        # this call's overrides into every later export in the process.
        # Capability tiers (``jlcpcb-tier1``) use their parent fab's preset.
        from kicad_tools.manufacturers import get_fab_family

        preset_key = get_fab_family(manufacturer_lower)
        base = (
            MANUFACTURER_PRESETS[preset_key].config
            if preset_key in MANUFACTURER_PRESETS
            else GerberConfig()
        )
        config = dataclasses.replace(base, generate_drill=include_drill, create_zip=zip_output)

        # Export
        export_started = time.time()
        result_path = exporter.export(config, out_dir)

        # Collect file information
        files: list[GerberFile] = []
        out_dir.mkdir(parents=True, exist_ok=True)

        files.extend(_gerber_files_in(out_dir, since=export_started - 2))

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

        if layer_count == 0:
            return GerberExportResult(
                success=False,
                output_dir=str(out_dir),
                zip_file=zip_file,
                files=files,
                error="Export produced no copper-layer Gerber files",
                warnings=warnings,
            )

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
            # User drawing layers carry V-score lines on panels (Issue #6156)
            ("User_Comments", "Cmts.User"),
            ("Cmts_User", "Cmts.User"),
            ("User_Drawings", "Dwgs.User"),
            ("Dwgs_User", "Dwgs.User"),
            ("User_Eco1", "Eco1.User"),
            ("Eco1_User", "Eco1.User"),
            ("User_Eco2", "Eco2.User"),
            ("Eco2_User", "Eco2.User"),
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


# Supported assembly manufacturers: the same ids as ``kct export --mfr`` (#7276)
ASSEMBLY_MANUFACTURERS = SUPPORTED_MANUFACTURERS


def export_assembly(
    pcb_path: str,
    schematic_path: str,
    output_dir: str,
    manufacturer: str | None = None,
    strict_preflight: bool = True,
) -> AssemblyExportResult:
    """
    Generate complete assembly package for manufacturing.

    Runs the same pipeline as ``kct export`` (Issue #7260):
    :class:`~kicad_tools.export.manufacturing.ManufacturingPackage` with the
    configuration built by
    :func:`~kicad_tools.cli.export_cmd.build_manufacturing_config` -- the DRC
    short/open safety floor, every preflight check, the unbuildable-BOM block
    and (by default here) strict preflight.  A blocked export writes no BOM,
    CPL, Gerbers or upload zip.  The report PDF and KiCad project zip are not
    generated (MCP never produced them).

    Side effect (as ``kct export``): sibling ``<board>.kicad_pro`` /
    ``<board>.kicad_dru`` holding the manufacturer's DRC constraints are
    written next to the source PCB (#5746).

    Args:
        pcb_path: Path to .kicad_pcb file
        schematic_path: Path to .kicad_sch file
        output_dir: Directory for output files
        manufacturer: Target manufacturer or tier id -- any id ``kct export
            --mfr`` accepts (e.g. ``"jlcpcb"``, ``"jlcpcb-tier1"``, ``"pcbway"``,
            ``"generic"``).  Omitted (``None``): resolved like ``kct check``
            (``fab_profile.json`` > ``project.kct`` ``target_fab`` > ``jlcpcb``)
            and reported in the result's ``manufacturer`` / ``manufacturer_source``.
        strict_preflight: Any preflight FAIL blocks the export (default).
            ``False`` matches ``kct export`` without ``--strict-preflight``:
            FAILs become warnings -- except the unbuildable-BOM block and the
            DRC safety floor, which always refuse.

    Returns:
        AssemblyExportResult with paths to all generated files, component counts,
        the preflight results, and any warnings.

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

    # Resolve an omitted manufacturer from the board's declared fab (#7276)
    manufacturer_source = "cli"
    if manufacturer is None:
        from kicad_tools.manufacturers.resolve import _resolve_effective_check_mfr

        resolved = _resolve_effective_check_mfr(None, pcb)
        manufacturer, manufacturer_source = resolved.mfr, resolved.source

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
        from kicad_tools.cli.export_cmd import build_manufacturing_config
        from kicad_tools.export.manufacturing import ManufacturingPackage
        from kicad_tools.manufacturers import get_fab_family

        config = build_manufacturing_config(
            output_dir=out_dir,
            strict_preflight=strict_preflight,
            include_report=False,
            include_project_zip=False,
        )
        result = ManufacturingPackage(
            pcb_path=pcb,
            schematic_path=schematic,
            manufacturer=manufacturer_lower,
            config=config,
        ).export(out_dir)

        preflight = [
            {"name": pr.name, "status": pr.status, "message": pr.message, "details": pr.details}
            for pr in result.preflight_results
        ]
        warnings.extend(result.warnings)

        gerbers_result = None
        bom_result = None
        pnp_result = None
        assembly = result.assembly_result

        if assembly is not None and assembly.gerber_path:
            gerber_files: list[GerberFile] = []
            gerber_dir = (
                assembly.gerber_path
                if assembly.gerber_path.is_dir()
                else assembly.gerber_path.parent
            )

            if gerber_dir.exists():
                gerber_files.extend(_gerber_files_in(gerber_dir))

            copper_layers = [f for f in gerber_files if f.file_type == "copper"]
            gerbers_result = GerberExportResult(
                success=bool(copper_layers),
                output_dir=str(gerber_dir),
                files=gerber_files,
                layer_count=len(copper_layers),
                error=None if copper_layers else "Export produced no copper-layer Gerber files",
            )

        # BOM / CPL facts come from the generator (Issue #7260), never from
        # re-parsing the CSV text.
        if assembly is not None and assembly.bom_path and assembly.bom_path.exists():
            missing_lcsc = (
                len(assembly.bom_missing_lcsc)
                if get_fab_family(manufacturer_lower) == "jlcpcb"
                else 0
            )
            if missing_lcsc > 0:
                warnings.append(f"{missing_lcsc} parts missing LCSC part numbers")

            bom_result = BOMExportResult(
                output_path=str(assembly.bom_path),
                component_count=assembly.bom_component_count,
                unique_parts=assembly.bom_unique_parts,
                missing_lcsc=missing_lcsc,
            )

        if assembly is not None and assembly.pnp_path and assembly.pnp_path.exists():
            pnp_result = PnPExportResult(
                output_path=str(assembly.pnp_path),
                component_count=assembly.pnp_placement_count,
                layers=list(assembly.pnp_layers),
                rotation_corrections=assembly.rotation_corrections_applied,
            )

        if not result.success:
            # Refused (DRC safety floor, unbuildable BOM, strict preflight) or
            # failed: never hand back an upload-ready zip.
            return AssemblyExportResult(
                success=False,
                output_dir=str(out_dir),
                manufacturer=manufacturer_lower,
                manufacturer_source=manufacturer_source,
                gerbers=gerbers_result,
                bom=bom_result,
                pnp=pnp_result,
                zip_file=None,
                preflight=preflight,
                warnings=warnings,
                error=_with_mcp_mfr_hint("; ".join(result.errors)),
            )

        # Create combined zip file for upload
        zip_file_path = _create_assembly_zip(out_dir, get_fab_family(manufacturer_lower), assembly)

        return AssemblyExportResult(
            success=True,
            output_dir=str(out_dir),
            manufacturer=manufacturer_lower,
            manufacturer_source=manufacturer_source,
            gerbers=gerbers_result,
            bom=bom_result,
            pnp=pnp_result,
            zip_file=str(zip_file_path) if zip_file_path else None,
            preflight=preflight,
            warnings=warnings,
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


def _with_mcp_mfr_hint(error: str) -> str:
    """Tell an MCP caller how to act on a ``--mfr`` remedy (#7276).

    Refusals such as the report-provenance check say "export with --mfr X",
    which is a CLI flag; the MCP equivalent is the ``manufacturer`` parameter.
    """
    if "--mfr" in error:
        return f"{error} (over MCP, pass the id as the `manufacturer` parameter)"
    return error


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


# =============================================================================
# Standalone BOM Export
# =============================================================================

# Supported BOM formats
SUPPORTED_BOM_FORMATS = ["csv", "json", "jlcpcb", "pcbway", "seeed"]

# Supported grouping strategies
SUPPORTED_GROUP_BY = ["value", "footprint", "value+footprint", "mpn", "none"]


def export_bom(
    schematic_path: str,
    output_path: str | None = None,
    format: str = "csv",
    group_by: str = "value+footprint",
    include_dnp: bool = False,
) -> BOMGenerationResult:
    """
    Export Bill of Materials from a schematic file.

    Generates a BOM from the schematic, with support for different manufacturer
    formats (JLCPCB, PCBWay, Seeed) and grouping strategies. Automatically
    extracts LCSC part numbers from component fields.

    Args:
        schematic_path: Path to .kicad_sch file
        output_path: Output file path (None = return data only, no file written)
        format: Output format - "csv", "json", "jlcpcb", "pcbway", "seeed"
        group_by: Grouping strategy - "value", "footprint", "value+footprint", "mpn", "none"
        include_dnp: Include Do Not Place components in the output

    Returns:
        BOMGenerationResult with BOM data, statistics, and file path if written.

    Example:
        >>> result = export_bom(
        ...     "/path/to/board.kicad_sch",
        ...     format="jlcpcb",
        ... )
        >>> if result.success:
        ...     print(f"Generated BOM with {result.unique_parts} unique parts")
        ...     print(f"Total components: {result.total_parts}")
        ...     if result.missing_lcsc:
        ...         print(f"Missing LCSC: {', '.join(result.missing_lcsc)}")
    """
    sch_file = Path(schematic_path)
    warnings: list[str] = []

    # Validate schematic path
    if not sch_file.exists():
        return BOMGenerationResult(
            success=False,
            error=f"Schematic file not found: {schematic_path}",
        )

    if sch_file.suffix != ".kicad_sch":
        warnings.append(f"Unusual file extension: {sch_file.suffix} (expected .kicad_sch)")

    # Validate format
    format_lower = format.lower()
    if format_lower not in SUPPORTED_BOM_FORMATS:
        return BOMGenerationResult(
            success=False,
            error=f"Unknown format: {format}. Supported: {', '.join(SUPPORTED_BOM_FORMATS)}",
            warnings=warnings,
        )

    # Validate group_by
    group_by_lower = group_by.lower()
    if group_by_lower not in SUPPORTED_GROUP_BY:
        return BOMGenerationResult(
            success=False,
            error=f"Unknown group_by: {group_by}. Supported: {', '.join(SUPPORTED_GROUP_BY)}",
            warnings=warnings,
        )

    try:
        # Extract BOM from schematic (handles hierarchical schematics automatically)
        bom = extract_bom(str(sch_file), hierarchical=True)

        # Filter BOM based on include_dnp setting
        filtered_bom = bom.filter(include_dnp=include_dnp)

        # Filter out items without valid references (library definitions, not real components)
        from kicad_tools.schema.bom import BOM

        valid_items = [item for item in filtered_bom.items if item.reference.strip()]
        filtered_bom = BOM(items=valid_items, source=filtered_bom.source)

        # Group items
        if group_by_lower == "none":
            # No grouping - each item is its own group
            groups = []
            for item in filtered_bom.items:
                from kicad_tools.schema.bom import BOMGroup

                group = BOMGroup(value=item.value, footprint=item.footprint)
                group.items.append(item)
                groups.append(group)
        else:
            groups = filtered_bom.grouped(by=group_by_lower)

        # Build result items and track missing LCSC
        items: list[BOMItemResult] = []
        missing_lcsc: list[str] = []
        total_parts = 0

        for group in groups:
            item_result = BOMItemResult(
                reference=group.references,
                value=group.value,
                footprint=group.footprint,
                quantity=group.quantity,
                lcsc_part=group.lcsc or None,
                description=group.description or None,
                manufacturer=group.items[0].manufacturer if group.items else None,
                mpn=group.mpn or None,
            )
            items.append(item_result)
            total_parts += group.quantity

            # Track parts missing LCSC numbers
            if not group.lcsc:
                missing_lcsc.append(group.references)

        # Write output file if requested
        output_file_path: str | None = None
        if output_path:
            out_path = Path(output_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)

            if format_lower == "json":
                # JSON output
                import json

                json_data = {
                    "schematic": str(sch_file),
                    "total_parts": total_parts,
                    "unique_parts": len(groups),
                    "items": [item.to_dict() for item in items],
                }
                out_path.write_text(json.dumps(json_data, indent=2))
            else:
                # CSV-based formats (csv, jlcpcb, pcbway, seeed)
                manufacturer = (
                    format_lower if format_lower in ["jlcpcb", "pcbway", "seeed"] else "generic"
                )

                config = BOMExportConfig(
                    include_dnp=include_dnp,
                    group_by_value=(group_by_lower != "none"),
                )

                csv_content = export_bom_formatted(filtered_bom.items, manufacturer, config)
                out_path.write_text(csv_content)

            output_file_path = str(out_path)

        return BOMGenerationResult(
            success=True,
            total_parts=total_parts,
            unique_parts=len(groups),
            output_path=output_file_path,
            missing_lcsc=missing_lcsc,
            items=items,
            format=format_lower,
            warnings=warnings,
        )

    except Exception as e:
        logger.exception("BOM export failed")
        return BOMGenerationResult(
            success=False,
            error=str(e),
            warnings=warnings,
        )

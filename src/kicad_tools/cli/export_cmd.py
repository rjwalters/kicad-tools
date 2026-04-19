"""
Manufacturing export command for kicad-tools CLI.

Usage:
    kct export board.kicad_pcb --mfr jlcpcb -o manufacturing/
    kct export board.kicad_pcb --mfr jlcpcb --dry-run
    kct export board.kicad_pcb --mfr jlcpcb --no-report
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _find_pcb_for_export(directory: Path) -> Path | None:
    """Find a .kicad_pcb file in the given directory for export.

    Searches recursively and prefers routed files (*_routed.kicad_pcb)
    since those are the manufacturing-ready artifacts. Falls back to the
    primary (non-backup) PCB file if no routed version exists.

    Args:
        directory: Directory to search

    Returns:
        Path to PCB file if found, None otherwise
    """
    pcb_files = list(directory.glob("**/*.kicad_pcb"))
    # Filter out backup files
    pcb_files = [f for f in pcb_files if not f.name.endswith("-bak.kicad_pcb")]

    if not pcb_files:
        return None

    # Prefer routed files (manufacturing-ready)
    routed = [f for f in pcb_files if f.name.endswith("_routed.kicad_pcb")]
    if routed:
        return routed[0]

    # Fall back to non-routed PCB files
    return pcb_files[0]


def main(argv: list[str] | None = None) -> int:
    """Entry point for the kct export command."""
    parser = argparse.ArgumentParser(
        prog="kct export",
        description="Generate a complete manufacturing package.",
    )
    parser.add_argument(
        "pcb",
        help="Path to .kicad_pcb file or directory containing one",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        default="jlcpcb",
        choices=["jlcpcb", "pcbway", "oshpark", "generic"],
        help="Target manufacturer (default: jlcpcb)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output directory (default: <pcb-dir>/manufacturing/)",
    )
    parser.add_argument(
        "--sch",
        default=None,
        help="Path to .kicad_sch file (auto-detected by default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be generated without writing files",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Skip report generation",
    )
    parser.add_argument(
        "--no-gerbers",
        action="store_true",
        help="Skip Gerber export",
    )
    parser.add_argument(
        "--no-bom",
        action="store_true",
        help="Skip BOM generation",
    )
    parser.add_argument(
        "--no-cpl",
        action="store_true",
        help="Skip CPL/pick-and-place generation",
    )
    parser.add_argument(
        "--no-project-zip",
        action="store_true",
        help="Skip KiCad project ZIP creation",
    )
    parser.add_argument(
        "--auto-lcsc",
        action="store_true",
        default=True,
        help="Auto-match LCSC part numbers for JLCPCB BOMs (default: enabled)",
    )
    parser.add_argument(
        "--no-auto-lcsc",
        action="store_true",
        help="Disable LCSC auto-matching",
    )
    parser.add_argument(
        "--no-spec",
        action="store_true",
        help="Disable BOM enrichment from .kct project spec",
    )
    parser.add_argument(
        "--no-merge-lcsc",
        action="store_true",
        help="Disable merging LCSC part numbers from an existing BOM CSV",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip all pre-flight validation checks",
    )
    parser.add_argument(
        "--strict-preflight",
        action="store_true",
        help="Block export when preflight checks fail (for CI; default: export proceeds with warnings)",
    )
    parser.add_argument(
        "--skip-drc",
        action="store_true",
        help="Skip DRC check in pre-flight validation",
    )
    parser.add_argument(
        "--skip-erc",
        action="store_true",
        help="Skip ERC check in pre-flight validation",
    )
    parser.add_argument(
        "--drc-report",
        default=None,
        help="Path to pre-existing DRC report file",
    )
    parser.add_argument(
        "--erc-report",
        default=None,
        help="Path to pre-existing ERC report file",
    )
    parser.add_argument(
        "--bom-source",
        default="schematic",
        choices=["schematic", "pcb", "auto"],
        help=(
            "Source for BOM data: 'schematic' (default) extracts from .kicad_sch; "
            "'pcb' extracts from PCB footprints (no schematic needed); "
            "'auto' uses schematic but falls back to PCB on reference mismatch"
        ),
    )
    parser.add_argument(
        "--include-tht",
        action="store_true",
        help="Include through-hole components in CPL (they are excluded by default for JLCPCB)",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        default="text",
        choices=["text", "json"],
        help="Output format (default: text)",
    )

    args = parser.parse_args(argv)
    return run_export(args)


def run_export(args: argparse.Namespace) -> int:
    """Execute the export command with parsed arguments."""
    import json

    from kicad_tools.export.manufacturing import (
        ManufacturingConfig,
        ManufacturingPackage,
    )
    from kicad_tools.export.preflight import PreflightConfig

    input_path = Path(args.pcb).resolve()

    if not input_path.exists():
        print(f"Error: Path not found: {input_path}", file=sys.stderr)
        return 1

    if input_path.is_dir():
        # Auto-discover PCB file in directory (consistent with kct build/check)
        pcb_path = _find_pcb_for_export(input_path)
        if pcb_path is None:
            print(
                f"Error: No .kicad_pcb file found in directory: {input_path}",
                file=sys.stderr,
            )
            print(
                "Hint: Specify a .kicad_pcb file directly, or ensure the directory contains one.",
                file=sys.stderr,
            )
            return 1
    elif input_path.suffix != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {input_path.name}", file=sys.stderr)
        print("Hint: Provide a .kicad_pcb file or a directory containing one.", file=sys.stderr)
        return 1
    else:
        pcb_path = input_path

    # Determine output directory
    output_dir = Path(args.output) if args.output else pcb_path.parent / "manufacturing"

    # Build preflight configuration
    preflight_cfg = PreflightConfig(
        skip_all=getattr(args, "skip_preflight", False),
        skip_drc=getattr(args, "skip_drc", False),
        skip_erc=getattr(args, "skip_erc", False),
        drc_report_path=getattr(args, "drc_report", None),
        erc_report_path=getattr(args, "erc_report", None),
    )

    # Build PnP configuration when --include-tht is specified
    pnp_config = None
    if getattr(args, "include_tht", False):
        from kicad_tools.export.pnp import PnPExportConfig

        pnp_config = PnPExportConfig(exclude_tht=False)

    # Build configuration
    auto_lcsc = args.auto_lcsc and not args.no_auto_lcsc
    config = ManufacturingConfig(
        output_dir=output_dir,
        include_bom=not args.no_bom,
        include_pnp=not args.no_cpl,
        include_gerbers=not args.no_gerbers,
        include_report=not args.no_report,
        include_project_zip=not args.no_project_zip,
        auto_lcsc=auto_lcsc,
        no_spec=getattr(args, "no_spec", False),
        merge_lcsc=not getattr(args, "no_merge_lcsc", False),
        bom_source=getattr(args, "bom_source", "schematic"),
        preflight=preflight_cfg,
        strict_preflight=getattr(args, "strict_preflight", False),
        pnp_config=pnp_config,
    )

    pkg = ManufacturingPackage(
        pcb_path=pcb_path,
        schematic_path=args.sch,
        manufacturer=args.mfr,
        config=config,
    )

    output_format = getattr(args, "output_format", "text")

    # Dry run
    if args.dry_run:
        result = pkg.export(output_dir, dry_run=True)
        if output_format == "json":
            print(json.dumps({"dry_run": True, "files": [f.name for f in result.all_files]}))
        else:
            print("Dry run -- the following files would be generated:")
            print(f"  Output directory: {result.output_dir}")
            for f in result.all_files:
                print(f"  - {f.name}")
        return 0

    # Normal run with progress output
    quiet = getattr(args, "global_quiet", False)

    if not quiet and output_format == "text":
        print(f"Generating manufacturing package for {args.mfr}...")
        print(f"  PCB: {pcb_path}")
        print(f"  Output: {output_dir}")
        print()

    result = pkg.export(output_dir)

    # Display preflight results
    if result.preflight_results and output_format == "text" and not quiet:
        print("Pre-flight checks:")
        for pr in result.preflight_results:
            status_tag = f"[{pr.status}]"
            print(f"  {status_tag:6s} {pr.name}: {pr.message}")
            if pr.details and pr.status != "OK":
                print(f"         {pr.details}")
        print()

    # Display preflight warnings (non-blocking failures) in text mode
    if result.warnings and output_format == "text" and not quiet:
        print(f"Preflight warnings ({len(result.warnings)}):")
        for warn in result.warnings:
            print(f"  - {warn}")
        print()

    # JSON output mode
    if output_format == "json":
        json_result: dict = {
            "success": result.success,
            "output_dir": str(result.output_dir),
            "files": [str(f) for f in result.all_files],
            "errors": result.errors,
        }
        if result.warnings:
            json_result["warnings"] = result.warnings
        if result.preflight_results:
            json_result["preflight"] = [pr.to_dict() for pr in result.preflight_results]
        print(json.dumps(json_result, indent=2))
        return 0 if result.success else 1

    if not quiet:
        if result.assembly_result:
            if result.assembly_result.bom_path:
                print(f"  [ok] BOM: {result.assembly_result.bom_path.name}")
            if result.assembly_result.pnp_path:
                print(f"  [ok] CPL: {result.assembly_result.pnp_path.name}")
            if result.assembly_result.gerber_path:
                print(f"  [ok] Gerbers: {result.assembly_result.gerber_path.name}")
            # Report spec overlay results
            if result.assembly_result.spec_overlay:
                overlay = result.assembly_result.spec_overlay
                print()
                for line in overlay.summary_lines():
                    print(f"  {line}")
            # Report LCSC enrichment results
            if result.assembly_result.lcsc_enrichment:
                enrichment = result.assembly_result.lcsc_enrichment
                print()
                for line in enrichment.summary_lines():
                    print(f"  {line}")
        if result.report_path:
            print(f"  [ok] Report: {result.report_path.name}")
        if result.project_zip_path:
            print(f"  [ok] Project ZIP: {result.project_zip_path.name}")
        if result.manifest_path:
            print(f"  [ok] Manifest: {result.manifest_path.name}")

    if result.errors:
        print(f"\n{len(result.errors)} error(s) during export:", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    if not quiet:
        n_files = len(result.all_files)
        print(f"\nDone -- {n_files} file(s) written to {output_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

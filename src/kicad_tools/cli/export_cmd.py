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


def main(argv: list[str] | None = None) -> int:
    """Entry point for the kct export command."""
    parser = argparse.ArgumentParser(
        prog="kct export",
        description="Generate a complete manufacturing package.",
    )
    parser.add_argument(
        "pcb",
        help="Path to .kicad_pcb file",
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

    args = parser.parse_args(argv)
    return run_export(args)


def run_export(args: argparse.Namespace) -> int:
    """Execute the export command with parsed arguments."""
    from kicad_tools.export.manufacturing import (
        ManufacturingConfig,
        ManufacturingPackage,
    )

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1

    # Determine output directory
    output_dir = Path(args.output) if args.output else pcb_path.parent / "manufacturing"

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
    )

    pkg = ManufacturingPackage(
        pcb_path=pcb_path,
        schematic_path=args.sch,
        manufacturer=args.mfr,
        config=config,
    )

    # Dry run
    if args.dry_run:
        result = pkg.export(output_dir, dry_run=True)
        print("Dry run -- the following files would be generated:")
        print(f"  Output directory: {result.output_dir}")
        for f in result.all_files:
            print(f"  - {f.name}")
        return 0

    # Normal run with progress output
    quiet = getattr(args, "global_quiet", False)

    if not quiet:
        print(f"Generating manufacturing package for {args.mfr}...")
        print(f"  PCB: {pcb_path}")
        print(f"  Output: {output_dir}")
        print()

    result = pkg.export(output_dir)

    if not quiet:
        if result.assembly_result:
            if result.assembly_result.bom_path:
                print(f"  [ok] BOM: {result.assembly_result.bom_path.name}")
            if result.assembly_result.pnp_path:
                print(f"  [ok] CPL: {result.assembly_result.pnp_path.name}")
            if result.assembly_result.gerber_path:
                print(f"  [ok] Gerbers: {result.assembly_result.gerber_path.name}")
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

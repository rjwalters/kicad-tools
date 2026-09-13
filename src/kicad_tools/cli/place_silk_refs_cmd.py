#!/usr/bin/env python3
"""Place readable silkscreen reference designators (issue #5030).

Moves (and, optionally, rotates) *visible* reference designator text just
far enough to clear real mask apertures, other silk/text, and the board
edge -- while preserving visibility, text height, and stroke width, and
without moving footprints/copper or touching net bindings.

Usage:
    kct place-silk-refs board.kicad_pcb [options]

Examples:
    # Preview the move plan without touching the file
    kct place-silk-refs board.kicad_pcb --dry-run

    # Apply, using JLCPCB mask-clearance defaults
    kct place-silk-refs board.kicad_pcb --mfr jlcpcb

    # Tighten the silk-to-obstacle clearance and allow 90-degree rotation
    kct place-silk-refs board.kicad_pcb --clearance 0.15 --allow-rotate

    # Apply, then run an independent native DRC check on the silk rules
    kct place-silk-refs board.kicad_pcb --verify-drc
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.manufacturers import get_all_manufacturer_names
from kicad_tools.manufacturers.base import load_design_rules_from_yaml
from kicad_tools.silkscreen.place_refs import (
    DEFAULT_CLEARANCE_MM,
    DEFAULT_MAX_OFFSET_MM,
    DEFAULT_STEP_MM,
    SILK_EDGE_CLEARANCE_MM,
    PlaceSilkRefsResult,
    SilkRefPlacer,
)


def _get_mask_clearance(mfr: str | None, layers: int, copper: float) -> float:
    """Resolve the pad/via solder-mask clearance used to size apertures."""
    if mfr:
        try:
            rules_dict = load_design_rules_from_yaml(mfr)
            key = f"{layers}layer_{int(copper)}oz"
            if key in rules_dict:
                return rules_dict[key].min_solder_mask_clearance_mm
            key = f"{layers}layer_1oz"
            if key in rules_dict:
                return rules_dict[key].min_solder_mask_clearance_mm
            first = next(iter(rules_dict.values()))
            return first.min_solder_mask_clearance_mm
        except FileNotFoundError:
            print(
                f"Warning: No configuration found for manufacturer '{mfr}'",
                file=sys.stderr,
            )
    return 0.05


def _print_json(
    result: PlaceSilkRefsResult,
    dry_run: bool,
    drc_summary: dict | None,
    render_path: Path | None = None,
) -> None:
    data = {
        "clearance_mm": result.clearance_mm,
        "dry_run": dry_run,
        "total_moved": result.total_moved,
        "total_unchanged": len(result.unchanged),
        "total_unplaceable": len(result.unplaceable),
        "total_under_component_fallback": len(result.under_component_fallback),
        "placements": [p.to_dict() for p in result.placements],
    }
    if drc_summary is not None:
        data["drc_verification"] = drc_summary
    if render_path is not None:
        data["render_artifact"] = str(render_path)
    print(json.dumps(data, indent=2))


def _print_summary(result: PlaceSilkRefsResult, dry_run: bool) -> None:
    action = "Would move" if dry_run else "Moved"
    parts = [f"{action} {result.total_moved} reference(s)"]
    if result.unplaceable:
        parts.append(f"{len(result.unplaceable)} unplaceable")
    if result.under_component_fallback:
        parts.append(f"{len(result.under_component_fallback)} under-component fallback")
    print(f"{'; '.join(parts)} (clearance: {result.clearance_mm:.2f}mm)")


def _print_text(result: PlaceSilkRefsResult, dry_run: bool) -> None:
    if not result.placements:
        print("No visible reference designators found.")
        return

    action = "Would move" if dry_run else "Moved"
    if result.moved:
        print(f"{action} {result.total_moved} reference designator(s):")
        for p in sorted(result.moved, key=lambda p: p.footprint_ref):
            ox, oy = p.old_position
            nx, ny = p.new_position
            rot = (
                f", {p.old_rotation:.0f}deg -> {p.new_rotation:.0f}deg"
                if p.new_rotation != p.old_rotation
                else ""
            )
            print(f"  {p.footprint_ref}: ({ox:.3f}, {oy:.3f}) -> ({nx:.3f}, {ny:.3f}){rot}")
    elif not result.unplaceable and not result.under_component_fallback:
        print("No collisions found -- every visible reference is already clear.")
    else:
        print("No references could be moved to a clear location.")

    if result.under_component_fallback:
        print(
            f"\n{len(result.under_component_fallback)} reference(s) fell back under their own component body:"
        )
        for p in sorted(result.under_component_fallback, key=lambda p: p.footprint_ref):
            print(f"  {p.footprint_ref}: {p.reason}")

    if result.unplaceable:
        print(f"\n{len(result.unplaceable)} reference(s) could not be placed cleanly:")
        for p in sorted(result.unplaceable, key=lambda p: p.footprint_ref):
            print(f"  {p.footprint_ref}: {p.reason}")


def _print_results(
    result: PlaceSilkRefsResult,
    output_format: str,
    dry_run: bool,
    drc_summary: dict | None,
    render_path: Path | None = None,
) -> None:
    if output_format == "json":
        _print_json(result, dry_run, drc_summary, render_path)
    elif output_format == "summary":
        _print_summary(result, dry_run)
    else:
        _print_text(result, dry_run)
    if output_format != "json" and drc_summary is not None:
        if not drc_summary.get("available"):
            print(f"Native DRC: {drc_summary.get('message', 'unavailable')}")
        elif drc_summary.get("error"):
            print(f"Native DRC error: {drc_summary['error']}")
        else:
            print(f"Native DRC: {drc_summary['silk_violations']} silk violation(s)")


def _run_verify_drc(pcb_path: Path) -> dict:
    """Run an independent native ``kicad-cli pcb drc`` pass and summarize silk findings."""
    from kicad_tools.cli.runner import find_kicad_cli, run_drc
    from kicad_tools.drc.report import DRCReport

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        return {
            "available": False,
            "message": "kicad-cli not found; skipped native DRC verification",
        }

    try:
        drc_result = run_drc(pcb_path, None)
    except Exception as e:
        return {"available": True, "error": f"DRC run failed: {e}"}
    try:
        if not drc_result.success or drc_result.return_code != 0 or not drc_result.output_path:
            return {"available": True, "error": drc_result.stderr or "DRC run failed"}
        raw_report = json.loads(drc_result.output_path.read_text())
        if not isinstance(raw_report, dict) or not isinstance(raw_report.get("violations"), list):
            return {
                "available": True,
                "error": "Invalid native DRC report: missing violations array",
            }
        report = DRCReport.load(drc_result.output_path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        return {"available": True, "error": f"Invalid native DRC report: {e}"}
    finally:
        if drc_result.output_path:
            drc_result.output_path.unlink(missing_ok=True)

    silk_types = {"silk_over_copper", "silk_overlap", "silk_edge_clearance", "silkscreen"}
    silk_violations = [v for v in report.violations if any(t in v.type_str for t in silk_types)]
    return {
        "available": True,
        "total_violations": report.violation_count,
        "silk_violations": len(silk_violations),
    }


def main(argv: list[str] | None = None) -> int:
    """Main entry point for place-silk-refs command."""
    parser = argparse.ArgumentParser(
        description="Move readable silkscreen reference designators to clear collisions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "--mfr",
        choices=get_all_manufacturer_names(),
        default=None,
        help="Manufacturer to source solder-mask clearance from (default: built-in 0.05mm)",
    )
    parser.add_argument("--layers", type=int, default=2, help="Number of PCB layers (default: 2)")
    parser.add_argument(
        "--copper", type=float, default=1.0, help="Outer copper weight in oz (default: 1.0)"
    )
    parser.add_argument(
        "--clearance",
        type=float,
        default=DEFAULT_CLEARANCE_MM,
        help=f"Required silk-to-pad/silk-to-silk clearance in mm (default: {DEFAULT_CLEARANCE_MM})",
    )
    parser.add_argument(
        "--edge-clearance",
        type=float,
        default=SILK_EDGE_CLEARANCE_MM,
        help=f"Required silk-to-board-edge clearance in mm (default: {SILK_EDGE_CLEARANCE_MM})",
    )
    parser.add_argument(
        "--max-offset",
        type=float,
        default=DEFAULT_MAX_OFFSET_MM,
        help=f"Maximum search distance from the component body in mm (default: {DEFAULT_MAX_OFFSET_MM})",
    )
    parser.add_argument(
        "--step",
        type=float,
        default=DEFAULT_STEP_MM,
        help=f"Positive search ring spacing in mm; at most 4096 rings (default: {DEFAULT_STEP_MM})",
    )
    parser.add_argument(
        "--allow-rotate",
        action="store_true",
        help="Also try a 90-degree rotated orientation when the original does not fit",
    )
    parser.add_argument("-o", "--output", help="Output file path (default: overwrite input)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview the move plan without modifying files"
    )
    parser.add_argument(
        "--verify-drc",
        action="store_true",
        help="After applying, run native DRC; fail on silk findings or unavailable/failed verification",
    )
    parser.add_argument(
        "--render",
        metavar="SVG_PATH",
        help=(
            "Write a rendered review artifact (SVG) showing old/new reference "
            "positions, pad apertures, and courtyards -- DRC passing alone does "
            "not prove the placement is readable."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output (for scripting)"
    )

    args = parser.parse_args(argv)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1
    if pcb_path.suffix.lower() != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {pcb_path.suffix}", file=sys.stderr)
        return 1

    mask_clearance = _get_mask_clearance(args.mfr, args.layers, args.copper)

    try:
        placer = SilkRefPlacer(pcb_path)
    except Exception as e:
        print(f"Error parsing PCB file: {e}", file=sys.stderr)
        return 1

    try:
        result = placer.plan(
            clearance_mm=args.clearance,
            edge_clearance_mm=args.edge_clearance,
            mask_clearance_mm=mask_clearance,
            max_offset_mm=args.max_offset,
            step_mm=args.step,
            allow_rotate=args.allow_rotate,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    drc_summary: dict | None = None
    output_path = Path(args.output) if args.output else pcb_path

    if not args.dry_run:
        applied = placer.apply(result)
        if applied > 0 or args.output:
            try:
                placer.save(output_path)
            except Exception as e:
                print(f"Error saving PCB file: {e}", file=sys.stderr)
                return 1
        if args.verify_drc:
            drc_summary = _run_verify_drc(output_path)

    render_path: Path | None = None
    if args.render:
        render_path = placer.render_svg(result, Path(args.render))

    if not args.quiet:
        _print_results(
            result,
            output_format=args.format,
            dry_run=args.dry_run,
            drc_summary=drc_summary,
            render_path=render_path,
        )
        if not args.dry_run and result.total_moved > 0 and args.format == "text":
            print(f"\nSaved to: {output_path}")
        if render_path is not None and args.format == "text":
            print(f"Review artifact: {render_path}")

    if drc_summary is not None and (
        not drc_summary.get("available")
        or drc_summary.get("error")
        or drc_summary.get("silk_violations", 0) > 0
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Fix silkscreen line widths and text heights to meet manufacturer specifications.

Usage:
    kct fix-silkscreen board.kicad_pcb [options]

Examples:
    # Fix silkscreen lines and text to meet JLCPCB minimums (default)
    kct fix-silkscreen board.kicad_pcb --mfr jlcpcb

    # Specify minimum width directly
    kct fix-silkscreen board.kicad_pcb --min-width 0.15

    # Specify minimum text height directly
    kct fix-silkscreen board.kicad_pcb --min-height 1.0

    # Trim silk lines that come too close to pads (opt-in)
    kct fix-silkscreen board.kicad_pcb --clip-to-pads

    # Preview changes without applying
    kct fix-silkscreen board.kicad_pcb --dry-run

    # Output to a different file
    kct fix-silkscreen board.kicad_pcb -o fixed_board.kicad_pcb
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from kicad_tools.drc.repair_silkscreen import (
    PadClearanceRepairResult,
    SilkscreenRepairer,
    SilkscreenRepairResult,
    TextHeightRepairResult,
)
from kicad_tools.manufacturers import get_all_manufacturer_names
from kicad_tools.manufacturers.base import load_design_rules_from_yaml


def _get_min_width(
    mfr: str | None,
    layers: int,
    copper: float,
    explicit_width: float | None,
) -> float:
    """Resolve the target minimum silkscreen width.

    If *explicit_width* is given it takes precedence.  Otherwise the value is
    loaded from the manufacturer YAML profile.
    """
    if explicit_width is not None:
        return explicit_width

    if mfr:
        try:
            rules_dict = load_design_rules_from_yaml(mfr)
            key = f"{layers}layer_{int(copper)}oz"
            if key in rules_dict:
                return rules_dict[key].min_silkscreen_width_mm
            # Try without copper weight
            key = f"{layers}layer_1oz"
            if key in rules_dict:
                return rules_dict[key].min_silkscreen_width_mm
            # Fall back to first available
            first = next(iter(rules_dict.values()))
            return first.min_silkscreen_width_mm
        except FileNotFoundError:
            print(
                f"Warning: No configuration found for manufacturer '{mfr}'",
                file=sys.stderr,
            )

    # Sensible default matching JLCPCB minimum
    return 0.15


def _get_min_height(
    mfr: str | None,
    layers: int,
    copper: float,
    explicit_height: float | None,
) -> float:
    """Resolve the target minimum silkscreen text height.

    If *explicit_height* is given it takes precedence.  Otherwise the value is
    loaded from the manufacturer YAML profile.
    """
    if explicit_height is not None:
        return explicit_height

    if mfr:
        try:
            rules_dict = load_design_rules_from_yaml(mfr)
            key = f"{layers}layer_{int(copper)}oz"
            if key in rules_dict:
                return rules_dict[key].min_silkscreen_height_mm
            # Try without copper weight
            key = f"{layers}layer_1oz"
            if key in rules_dict:
                return rules_dict[key].min_silkscreen_height_mm
            # Fall back to first available
            first = next(iter(rules_dict.values()))
            return first.min_silkscreen_height_mm
        except FileNotFoundError:
            print(
                f"Warning: No configuration found for manufacturer '{mfr}'",
                file=sys.stderr,
            )

    # Sensible default matching JLCPCB minimum
    return 1.0


def _get_min_pad_clearance(mfr: str | None, layers: int, copper: float) -> float | None:
    """Resolve the manufacturer's minimum silk-to-pad clearance (mm), if defined."""
    if not mfr:
        return None
    try:
        rules_dict = load_design_rules_from_yaml(mfr)
    except FileNotFoundError:
        print(f"Warning: No configuration found for manufacturer '{mfr}'", file=sys.stderr)
        return None
    for key in (f"{layers}layer_{int(copper)}oz", f"{layers}layer_1oz"):
        if key in rules_dict:
            return rules_dict[key].min_silk_to_pad_clearance_mm
    return next(iter(rules_dict.values())).min_silk_to_pad_clearance_mm


def _print_results(
    line_result: SilkscreenRepairResult,
    text_result: TextHeightRepairResult,
    output_format: str,
    dry_run: bool,
    mfr: str | None,
    clip_result: PadClearanceRepairResult | None = None,
) -> None:
    """Print repair results in the requested format."""
    if output_format == "json":
        _print_json(line_result, text_result, dry_run, mfr, clip_result)
    elif output_format == "summary":
        _print_summary(line_result, text_result, dry_run, mfr, clip_result)
    else:
        _print_text(line_result, text_result, dry_run, mfr, clip_result)


def _clip_dicts(clip_result: PadClearanceRepairResult) -> dict:
    return {
        "min_pad_clearance_mm": clip_result.min_clearance_mm,
        "total_pad_clearance_fixed": clip_result.total_fixed,
        "pad_clearance_fixes": [
            {
                "footprint_ref": f.footprint_ref,
                "element_type": f.element_type,
                "layer": f.layer,
                "action": f.action,
                "old_length_mm": round(f.old_length, 6),
                "new_length_mm": round(f.new_length, 6),
            }
            for f in clip_result.fixes
        ],
        "pad_clearance_skipped": [
            {
                "footprint_ref": k.footprint_ref,
                "element_type": k.element_type,
                "layer": k.layer,
                "reason": k.reason,
            }
            for k in clip_result.skipped
        ],
    }


def _print_json(
    line_result: SilkscreenRepairResult,
    text_result: TextHeightRepairResult,
    dry_run: bool,
    mfr: str | None,
    clip_result: PadClearanceRepairResult | None = None,
) -> None:
    data = {
        "min_width_mm": line_result.min_width_mm,
        "min_height_mm": text_result.min_height_mm,
        "manufacturer": mfr,
        "dry_run": dry_run,
        "total_line_width_fixed": line_result.total_fixed,
        "total_text_height_fixed": text_result.total_fixed,
        "total_fixed": line_result.total_fixed
        + text_result.total_fixed
        + (clip_result.total_fixed if clip_result else 0),
        "line_width_fixes": [
            {
                "element_type": f.element_type,
                "layer": f.layer,
                "footprint_ref": f.footprint_ref,
                "old_width_mm": f.old_width,
                "new_width_mm": f.new_width,
            }
            for f in line_result.fixes
        ],
        "text_height_fixes": [
            {
                "element_type": f.element_type,
                "layer": f.layer,
                "footprint_ref": f.footprint_ref,
                "old_height_mm": f.old_height,
                "new_height_mm": f.new_height,
                "old_width_mm": f.old_width,
                "new_width_mm": f.new_width,
            }
            for f in text_result.fixes
        ],
    }
    if clip_result is not None:
        data.update(_clip_dicts(clip_result))
    print(json.dumps(data, indent=2))


def _print_summary(
    line_result: SilkscreenRepairResult,
    text_result: TextHeightRepairResult,
    dry_run: bool,
    mfr: str | None,
    clip_result: PadClearanceRepairResult | None = None,
) -> None:
    action = "Would fix" if dry_run else "Fixed"
    source = f" to {mfr.upper()} minimum" if mfr else ""
    total = line_result.total_fixed + text_result.total_fixed
    if clip_result is not None:
        total += clip_result.total_fixed
    parts = []
    if line_result.total_fixed > 0:
        parts.append(
            f"{line_result.total_fixed} line width violation(s) (min: {line_result.min_width_mm}mm)"
        )
    if text_result.total_fixed > 0:
        parts.append(
            f"{text_result.total_fixed} text height violation(s)"
            f" (min: {text_result.min_height_mm}mm)"
        )
    if clip_result is not None and clip_result.total_fixed > 0:
        parts.append(
            f"{clip_result.total_fixed} pad clearance violation(s)"
            f" (min: {clip_result.min_clearance_mm}mm)"
        )
    if not parts:
        parts.append("0 silkscreen violation(s)")
    print(f"{action} {total} silkscreen violation(s){source}: {'; '.join(parts)}")


def _print_text(
    line_result: SilkscreenRepairResult,
    text_result: TextHeightRepairResult,
    dry_run: bool,
    mfr: str | None,
    clip_result: PadClearanceRepairResult | None = None,
) -> None:
    has_line_fixes = line_result.total_fixed > 0
    has_text_fixes = text_result.total_fixed > 0
    has_clip = clip_result is not None and (
        clip_result.total_fixed > 0 or len(clip_result.skipped) > 0
    )

    if not has_line_fixes and not has_text_fixes and not has_clip:
        print("No silkscreen violations found.")
        return

    source = f" to {mfr.upper()} minimum" if mfr else ""

    # Line width fixes
    if has_line_fixes:
        action = "Would widen" if dry_run else "Widened"
        print(
            f"{action} {line_result.total_fixed} silkscreen line(s){source}"
            f" (min: {line_result.min_width_mm}mm):"
        )
        by_fp: Counter[str] = Counter()
        width_examples: dict[str, tuple[float, float]] = {}
        for fix in line_result.fixes:
            key = fix.footprint_ref or "(board-level)"
            by_fp[key] += 1
            if key not in width_examples:
                width_examples[key] = (fix.old_width, fix.new_width)
        for fp_ref, count in by_fp.most_common():
            old_w, new_w = width_examples[fp_ref]
            print(f"  {fp_ref}: {count} line(s) widened ({old_w:.2f}mm -> {new_w:.2f}mm)")

    # Text height fixes
    if has_text_fixes:
        action = "Would scale" if dry_run else "Scaled"
        print(
            f"{action} {text_result.total_fixed} silkscreen text element(s){source}"
            f" (min height: {text_result.min_height_mm}mm):"
        )
        by_fp_text: Counter[str] = Counter()
        height_examples: dict[str, tuple[float, float]] = {}
        for fix in text_result.fixes:
            key = fix.footprint_ref or "(board-level)"
            by_fp_text[key] += 1
            if key not in height_examples:
                height_examples[key] = (fix.old_height, fix.new_height)
        for fp_ref, count in by_fp_text.most_common():
            old_h, new_h = height_examples[fp_ref]
            print(f"  {fp_ref}: {count} text element(s) scaled ({old_h:.2f}mm -> {new_h:.2f}mm)")

    if clip_result is not None and has_clip:
        print(
            f"{'Would clip' if dry_run else 'Clipped'} {clip_result.total_fixed} silkscreen "
            f"line(s) to {clip_result.min_clearance_mm}mm pad clearance:"
        )
        for cfix in clip_result.fixes:
            print(
                f"  {cfix.footprint_ref}: {cfix.element_type} on {cfix.layer} {cfix.action}"
                f" ({cfix.old_length:.3f}mm -> {cfix.new_length:.3f}mm)"
            )
        for cskip in clip_result.skipped:
            print(
                f"  {cskip.footprint_ref}: {cskip.element_type} on {cskip.layer} skipped"
                f" ({cskip.reason})"
            )


def main(argv: list[str] | None = None) -> int:
    """Main entry point for fix-silkscreen command."""
    parser = argparse.ArgumentParser(
        description="Fix silkscreen line widths and text heights to meet manufacturer specifications",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Fix silkscreen lines and text to meet JLCPCB minimums
    kct fix-silkscreen board.kicad_pcb --mfr jlcpcb

    # Specify minimum width directly
    kct fix-silkscreen board.kicad_pcb --min-width 0.15

    # Specify minimum text height directly
    kct fix-silkscreen board.kicad_pcb --min-height 1.0

    # Preview changes without applying
    kct fix-silkscreen board.kicad_pcb --dry-run
        """,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "--mfr",
        choices=get_all_manufacturer_names(),
        default="jlcpcb",
        help="Manufacturer to use for design rules (default: jlcpcb)",
    )
    parser.add_argument(
        "--layers",
        type=int,
        default=2,
        help="Number of PCB layers (default: 2)",
    )
    parser.add_argument(
        "--copper",
        type=float,
        default=1.0,
        help="Outer copper weight in oz (default: 1.0)",
    )
    parser.add_argument(
        "--min-width",
        type=float,
        help="Minimum silkscreen line width in mm (overrides manufacturer rules)",
    )
    parser.add_argument(
        "--min-height",
        type=float,
        help="Minimum silkscreen text height in mm (overrides manufacturer rules)",
    )
    parser.add_argument(
        "--clip-to-pads",
        action="store_true",
        help=(
            "Also trim or remove footprint silk lines closer to a pad than the "
            "manufacturer's silk-to-pad clearance (pin-1 markers are never touched)"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )

    args = parser.parse_args(argv)

    # Validate input file
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix.lower() != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {pcb_path.suffix}", file=sys.stderr)
        return 1

    # Resolve minimums
    min_width = _get_min_width(args.mfr, args.layers, args.copper, args.min_width)
    min_height = _get_min_height(args.mfr, args.layers, args.copper, args.min_height)

    # Parse and repair
    try:
        repairer = SilkscreenRepairer(pcb_path)
    except Exception as e:
        print(f"Error parsing PCB file: {e}", file=sys.stderr)
        return 1

    line_result = repairer.repair_line_widths(min_width, dry_run=args.dry_run)
    text_result = repairer.repair_text_heights(min_height, dry_run=args.dry_run)

    clip_result: PadClearanceRepairResult | None = None
    if args.clip_to_pads:
        min_clearance = _get_min_pad_clearance(args.mfr, args.layers, args.copper)
        if min_clearance is None:
            print(
                f"Error: --clip-to-pads needs a silk-to-pad clearance, but "
                f"'{args.mfr}' defines none",
                file=sys.stderr,
            )
            return 1
        try:
            clip_result = repairer.repair_pad_clearance(min_clearance, dry_run=args.dry_run)
        except ImportError as e:
            print(f"Error: --clip-to-pads requires shapely: {e}", file=sys.stderr)
            return 1

    total_fixed = (
        line_result.total_fixed
        + text_result.total_fixed
        + (clip_result.total_fixed if clip_result else 0)
    )

    # Print results
    if not args.quiet:
        _print_results(
            line_result,
            text_result,
            output_format=args.format,
            dry_run=args.dry_run,
            mfr=args.mfr,
            clip_result=clip_result,
        )

    # Save if not dry run and there were fixes
    if total_fixed > 0 and not args.dry_run:
        output_path = Path(args.output) if args.output else pcb_path
        try:
            repairer.save(output_path)
            if not args.quiet and args.format == "text":
                print(f"\nSaved to: {output_path}")
        except Exception as e:
            print(f"Error saving PCB file: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

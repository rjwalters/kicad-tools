"""
Pure Python DRC check command for KiCad PCBs.

Runs design rule checks against manufacturer specifications without
requiring kicad-cli to be installed. Suitable for CI/CD pipelines.

Usage:
    kct check board.kicad_pcb                      # Run all checks
    kct check board.kicad_pcb --mfr jlcpcb         # With manufacturer rules
    kct check board.kicad_pcb --format json        # JSON output for CI
    kct check board.kicad_pcb --only clearance     # Run specific checks
    kct check board.kicad_pcb --skip silkscreen    # Exclude checks

Exit Codes:
    0 - No errors (warnings may be present without --strict)
    1 - Command failure (file not found, parse error, etc.)
    2 - Errors found, or warnings found with --strict

Difference from `kct drc`:
    - kct drc: Uses kicad-cli to run DRC (requires KiCad)
    - kct check: Pure Python DRC (no external dependencies)
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.manufacturers import get_manufacturer_ids
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker, DRCResults, DRCViolation

# Available check categories


def _find_pcb_file(directory: Path) -> Path | None:
    """Find a .kicad_pcb file in the given directory.

    Searches recursively and filters out routed/backup files to find
    the primary unrouted PCB file.

    Args:
        directory: Directory to search

    Returns:
        Path to PCB file if found, None otherwise
    """
    pcb_files = list(directory.glob("**/*.kicad_pcb"))
    # Filter out routed and backup files
    pcb_files = [
        f
        for f in pcb_files
        if not f.name.endswith("_routed.kicad_pcb") and not f.name.endswith("-bak.kicad_pcb")
    ]
    if pcb_files:
        return pcb_files[0]
    return None


CHECK_CATEGORIES = [
    "clearance",
    "connectivity",
    "diffpair_clearance_intra",
    "diffpair_length_skew",
    "diffpair_routing_continuity",
    "dimensions",
    "edge",
    "impedance",
    "match_group_length_skew",
    "netlist",
    "pad_grid",
    "placement",
    "silkscreen",
    "single_pad_net",
    "solder_mask",
    "via_in_pad",
    "zones",
]


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct check command."""
    parser = argparse.ArgumentParser(
        prog="kct check",
        description="Pure Python DRC for PCBs (no kicad-cli required)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "pcb",
        help="Path to .kicad_pcb file or directory containing one",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--errors-only",
        action="store_true",
        help="Show only errors, not warnings",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error code 2 on warnings",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        choices=get_manufacturer_ids(),
        default="jlcpcb",
        help="Target manufacturer for design rules (default: jlcpcb)",
    )
    parser.add_argument(
        "--layers",
        "-l",
        type=int,
        default=None,
        help="Number of copper layers (auto-detected from board if not specified)",
    )
    parser.add_argument(
        "--copper",
        "-c",
        type=float,
        default=1.0,
        help="Copper weight in oz (default: 1.0)",
    )
    parser.add_argument(
        "--only",
        dest="only_checks",
        help=f"Run only specific checks (comma-separated: {', '.join(CHECK_CATEGORIES)})",
    )
    parser.add_argument(
        "--skip",
        dest="skip_checks",
        help=f"Skip specific checks (comma-separated: {', '.join(CHECK_CATEGORIES)})",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Write JSON report to file (implies --format json for file output)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed violation information",
    )
    parser.add_argument(
        "--suppress-library",
        action="store_true",
        help="Suppress silkscreen warnings from standard KiCad library footprints",
    )
    parser.add_argument(
        "--net-class-map",
        dest="net_class_map",
        default=None,
        help=(
            "Path to a JSON sidecar mapping net names to NetClassRouting "
            "fields (see kicad_tools.router.rules.NetClassRouting.to_dict). "
            "When supplied, enables the diff-pair routing_continuity and "
            "length_skew rules to fire on routed boards; without it those "
            "rules degrade to no-ops (Issue #2684)."
        ),
    )
    # Issue #3061: auto-derive the pad_grid tolerance from each board's
    # pad-offset histogram by default for the CLI.  Users can opt back into
    # the fixed-0.05mm behaviour with --pad-grid-strict, or pin a custom
    # value with --pad-grid-tolerance.
    pad_grid_group = parser.add_mutually_exclusive_group()
    pad_grid_group.add_argument(
        "--pad-grid-strict",
        action="store_true",
        help=(
            "Use the fixed 0.05mm pad_grid tolerance (PR #3057 default) "
            "instead of auto-deriving per-board from the pad-offset "
            "histogram (issue #3061).  Default: auto-derive."
        ),
    )
    pad_grid_group.add_argument(
        "--pad-grid-tolerance",
        type=float,
        default=None,
        metavar="MM",
        help=(
            "Override the pad_grid L2 tolerance with an explicit value "
            "in mm (e.g. ``--pad-grid-tolerance 0.02``).  Disables "
            "auto-derivation."
        ),
    )

    args = parser.parse_args(argv)

    # Parse and validate filter options
    only_set: set[str] | None = None
    skip_set: set[str] = set()

    if args.only_checks:
        only_set = set()
        for cat in args.only_checks.split(","):
            cat = cat.strip().lower()
            if cat not in CHECK_CATEGORIES:
                print(f"Error: Unknown check category: {cat!r}", file=sys.stderr)
                print(f"Available: {', '.join(CHECK_CATEGORIES)}", file=sys.stderr)
                return 1
            only_set.add(cat)

    if args.skip_checks:
        for cat in args.skip_checks.split(","):
            cat = cat.strip().lower()
            if cat not in CHECK_CATEGORIES:
                print(f"Error: Unknown check category: {cat!r}", file=sys.stderr)
                print(f"Available: {', '.join(CHECK_CATEGORIES)}", file=sys.stderr)
                return 1
            skip_set.add(cat)

    # Load PCB - resolve to absolute path for reliable file access
    # Handles both file paths and directory paths (like kct build)
    input_path = Path(args.pcb).resolve()

    if not input_path.exists():
        print(f"Error: Path not found: {input_path}", file=sys.stderr)
        return 1

    if input_path.is_dir():
        # Auto-discover PCB file in directory (consistent with kct build)
        pcb_path = _find_pcb_file(input_path)
        if pcb_path is None:
            print(f"Error: No .kicad_pcb file found in directory: {input_path}", file=sys.stderr)
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

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Auto-detect layer count from PCB if not explicitly provided
    if args.layers is not None:
        layers = args.layers
    else:
        detected = len(pcb.copper_layers)
        layers = detected if detected > 0 else 2

    # Load optional net-class-map sidecar (Issue #2684).  When supplied,
    # the diff-pair routing-continuity and length-skew rules can re-derive
    # engagement / skew state from the routed PCB and fire.  When omitted,
    # the rules degrade to no-ops (AC #3: graceful-degradation contract).
    net_class_map = None
    if args.net_class_map is not None:
        from kicad_tools.router.rules import net_class_map_from_dict

        ncm_path = Path(args.net_class_map).resolve()
        if not ncm_path.exists():
            print(f"Error: net-class-map file not found: {ncm_path}", file=sys.stderr)
            return 1
        try:
            ncm_data = json.loads(ncm_path.read_text())
        except json.JSONDecodeError as e:
            print(f"Error parsing net-class-map JSON: {e}", file=sys.stderr)
            return 1
        try:
            net_class_map = net_class_map_from_dict(ncm_data)
        except (TypeError, ValueError) as e:
            print(f"Error: invalid net-class-map structure: {e}", file=sys.stderr)
            return 1

    # Create checker with manufacturer rules
    try:
        checker = DRCChecker(
            pcb,
            manufacturer=args.mfr,
            layers=layers,
            copper_oz=args.copper,
            suppress_library=args.suppress_library,
            net_class_map=net_class_map,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Resolve pad_grid tolerance policy (issue #3061).
    # Precedence: explicit value > strict mode > auto-derive (CLI default).
    if args.pad_grid_tolerance is not None:
        pad_grid_threshold: float | None = args.pad_grid_tolerance
        pad_grid_auto_derive = False
    elif args.pad_grid_strict:
        pad_grid_threshold = None  # Falls through to DEFAULT_PAD_GRID_TOLERANCE_MM
        pad_grid_auto_derive = False
    else:
        pad_grid_threshold = None
        pad_grid_auto_derive = True

    # Run selected checks
    results = run_selected_checks(
        checker,
        only_set,
        skip_set,
        pad_grid_threshold=pad_grid_threshold,
        pad_grid_auto_derive=pad_grid_auto_derive,
    )

    # Apply errors-only filter
    violations = list(results.violations)
    if args.errors_only:
        violations = [v for v in violations if v.is_error]

    # Output results
    if args.format == "json":
        output_json(violations, results, pcb_path, args.mfr, layers)
    elif args.format == "summary":
        output_summary(violations, results, pcb_path)
    else:
        output_table(violations, results, pcb_path, args.mfr, layers, args.verbose)

    # Write JSON report to file if --output specified
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_report(violations, results, pcb_path, args.mfr, layers, output_path)

    # Determine exit code
    # Exit 2 = check ran successfully but found issues (errors, or warnings+strict)
    # Exit 1 = reserved for tool-level failures (file not found, parse error) above
    # Exit 0 = no errors (warnings may be present without --strict; infos
    #   never affect exit code -- they are advisory by definition).
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)

    if error_count > 0 or (warning_count > 0 and args.strict):
        return 2
    return 0


def run_selected_checks(
    checker: DRCChecker,
    only_set: set[str] | None,
    skip_set: set[str],
    pad_grid_threshold: float | None = None,
    pad_grid_auto_derive: bool = True,
) -> DRCResults:
    """Run the selected DRC checks based on filters.

    Args:
        checker: The DRC checker pre-loaded with the PCB and rules.
        only_set: Optional whitelist of check category names.
        skip_set: Set of check category names to skip.
        pad_grid_threshold: Explicit pad_grid L2 tolerance in mm, or
            ``None`` to use the threshold-resolution policy below.
            Issue #3061.
        pad_grid_auto_derive: When ``True`` and ``pad_grid_threshold``
            is ``None``, the pad_grid check derives the threshold from
            the board's pad-offset histogram (issue #3061).  Defaults
            to ``True`` for the CLI; ``False`` preserves the PR #3057
            fixed-0.05mm behaviour.
    """
    results = DRCResults()

    # Build the pad_grid invocation as a thunk so the map below can
    # remain uniform (every value is a zero-arg callable).
    def _pad_grid_check() -> DRCResults:
        return checker.check_pad_grid_alignment(
            threshold=pad_grid_threshold,
            auto_derive_threshold=pad_grid_auto_derive,
        )

    # Map of category to check method.  This dict MUST stay a superset
    # of the methods invoked by ``DRCChecker.check_all`` (i.e., every
    # name in ``DRCChecker.CHECK_ALL_METHODS`` must be referenced as a
    # value here).  The regression test in
    # ``tests/test_check_cmd_coverage.py`` enforces the invariant for
    # Issue #3046.
    check_methods = {
        "clearance": checker.check_clearances,
        "connectivity": checker.check_connectivity,
        "diffpair_clearance_intra": checker.check_diffpair_clearance_intra,
        "diffpair_length_skew": checker.check_diffpair_length_skew,
        "diffpair_routing_continuity": checker.check_diffpair_routing_continuity,
        "dimensions": checker.check_dimensions,
        "edge": checker.check_edge_clearances,
        "impedance": checker.check_impedance,
        "match_group_length_skew": checker.check_match_group_length_skew,
        "netlist": checker.check_netlist,
        "pad_grid": _pad_grid_check,
        "placement": checker.check_footprint_placement,
        "silkscreen": checker.check_silkscreen,
        "single_pad_net": checker.check_single_pad_nets,
        "solder_mask": checker.check_solder_mask_pads,
        "via_in_pad": checker.check_via_in_pad,
        "zones": checker.check_zones,
    }

    for category, method in check_methods.items():
        # Skip if --only specified and this category not in it
        if only_set is not None and category not in only_set:
            continue

        # Skip if this category is in --skip
        if category in skip_set:
            continue

        # Run the check
        category_results = method()
        results.merge(category_results)

    return results


def output_table(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
    mfr: str,
    layers: int,
    verbose: bool = False,
) -> None:
    """Output violations as a formatted table."""
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)
    info_count = sum(1 for v in violations if v.is_info)

    print(f"\n{'=' * 60}")
    print("PURE PYTHON DRC CHECK")
    print(f"{'=' * 60}")
    print(f"File: {pcb_path.name}")
    print(f"Manufacturer: {mfr.upper()}")
    print(f"Layers: {layers}")
    print(f"Rules checked: {results.rules_checked}")

    print("\nResults:")
    print(f"  Errors:     {error_count}")
    print(f"  Warnings:   {warning_count}")
    if info_count > 0:
        print(f"  Infos:      {info_count}")
    if results.suppressed_count > 0:
        print(f"  Suppressed: {results.suppressed_count} (standard library footprints)")

    if not violations:
        print(f"\n{'=' * 60}")
        print("DRC PASSED - No violations found")
        return

    # Group by rule_id summary
    by_rule: dict[str, dict[str, int]] = {}
    for v in violations:
        if v.rule_id not in by_rule:
            by_rule[v.rule_id] = {"errors": 0, "warnings": 0, "infos": 0}
        if v.is_error:
            by_rule[v.rule_id]["errors"] += 1
        elif v.is_info:
            by_rule[v.rule_id]["infos"] += 1
        else:
            by_rule[v.rule_id]["warnings"] += 1

    print(f"\n{'-' * 60}")
    print("BY RULE:")
    for rule_id, counts in sorted(
        by_rule.items(),
        key=lambda x: -(x[1]["errors"] + x[1]["warnings"] + x[1]["infos"]),
    ):
        parts = []
        if counts["errors"]:
            parts.append(f"{counts['errors']} error{'s' if counts['errors'] != 1 else ''}")
        if counts["warnings"]:
            parts.append(f"{counts['warnings']} warning{'s' if counts['warnings'] != 1 else ''}")
        if counts["infos"]:
            parts.append(f"{counts['infos']} info{'s' if counts['infos'] != 1 else ''}")
        print(f"  {rule_id}: {', '.join(parts)}")

    # Detailed output
    errors = [v for v in violations if v.is_error]
    warnings = [v for v in violations if v.is_warning]
    infos = [v for v in violations if v.is_info]

    if errors:
        print(f"\n{'-' * 60}")
        print("ERRORS (must fix):")
        for v in errors:
            _print_violation(v, verbose)

    if warnings:
        print(f"\n{'-' * 60}")
        print("WARNINGS (review recommended):")
        display_warnings = warnings if verbose else warnings[:10]
        for v in display_warnings:
            _print_violation(v, verbose)
        if len(warnings) > 10 and not verbose:
            print(f"\n  ... and {len(warnings) - 10} more warnings (use --verbose)")

    if infos:
        print(f"\n{'-' * 60}")
        print("INFOS (advisory only):")
        display_infos = infos if verbose else infos[:10]
        for v in display_infos:
            _print_violation(v, verbose)
        if len(infos) > 10 and not verbose:
            print(f"\n  ... and {len(infos) - 10} more infos (use --verbose)")

    print(f"\n{'=' * 60}")
    if errors:
        print("DRC FAILED - Fix errors before manufacturing")
    elif warnings:
        print("DRC WARNING - Review warnings")
    else:
        print("DRC PASSED - Advisory infos only")


def _print_violation(v: DRCViolation, verbose: bool, indent: str = "  ") -> None:
    """Print a single violation."""
    if v.is_error:
        symbol = "X"
    elif v.is_info:
        symbol = "i"
    else:
        symbol = "!"
    print(f"\n{indent}[{symbol}] {v.rule_id}")
    print(f"{indent}    {v.message}")

    if verbose:
        if v.location:
            print(f"{indent}    -> ({v.location[0]:.2f}, {v.location[1]:.2f}) mm")
        if v.layer:
            print(f"{indent}    Layer: {v.layer}")
        if v.actual_value is not None and v.required_value is not None:
            print(f"{indent}    Actual: {v.actual_value:.3f}mm, Required: {v.required_value:.3f}mm")
        if v.items:
            print(f"{indent}    Items: {', '.join(v.items)}")
        if v.nets:
            net_labels = [n if n else "<no net>" for n in v.nets]
            print(f"{indent}    Nets: {', '.join(net_labels)}")


def output_json(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
    mfr: str,
    layers: int,
) -> None:
    """Output violations as JSON."""
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)
    info_count = sum(1 for v in violations if v.is_info)

    summary_data: dict = {
        "errors": error_count,
        "warnings": warning_count,
        "infos": info_count,
        "rules_checked": results.rules_checked,
        # Issue #2660 / Epic #2556 Phase 4N: per-rule check counter.
        # The single ``rules_checked`` integer cannot tell a CI consumer
        # WHICH rules ran -- only the aggregate.  Without this map, a
        # diff-pair CI gate cannot distinguish "rule X ran and reported
        # 0 violations" from "rule X did not run at all" (e.g., the rule
        # short-circuited because no engaged pairs were detected, which
        # would be a silent regression in detection).  Always emitted
        # (even when empty) so downstream consumers can rely on the
        # field being present.
        "rules_checked_by_rule": dict(results.rules_checked_by_rule),
        "passed": error_count == 0,
    }
    if results.suppressed_count > 0:
        summary_data["suppressed"] = results.suppressed_count

    data = {
        "file": str(pcb_path),
        "manufacturer": mfr,
        "layers": layers,
        "summary": summary_data,
        "violations": [v.to_dict() for v in violations],
    }
    print(json.dumps(data, indent=2))


def write_json_report(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
    mfr: str,
    layers: int,
    output_path: Path,
) -> None:
    """Write DRC results as a JSON report file."""
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)
    info_count = sum(1 for v in violations if v.is_info)

    summary_data: dict = {
        "errors": error_count,
        "warnings": warning_count,
        "infos": info_count,
        "rules_checked": results.rules_checked,
        # See ``output_json`` for the rationale on emitting this field
        # alongside the aggregate ``rules_checked`` integer.  Issue
        # #2660 / Epic #2556 Phase 4N.
        "rules_checked_by_rule": dict(results.rules_checked_by_rule),
        "passed": error_count == 0,
    }
    if results.suppressed_count > 0:
        summary_data["suppressed"] = results.suppressed_count

    data = {
        "file": str(pcb_path),
        "manufacturer": mfr,
        "layers": layers,
        "summary": summary_data,
        "violations": [v.to_dict() for v in violations],
    }
    output_path.write_text(json.dumps(data, indent=2) + "\n")


def output_summary(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
) -> None:
    """Output violation summary by rule."""
    if not violations:
        msg = f"  {results.rules_checked} rules checked, no violations found."
        if results.suppressed_count > 0:
            msg += (
                f"\n  ({results.suppressed_count} silkscreen warnings suppressed"
                f" -- standard library footprints)"
            )
        print(f"DRC PASSED: {pcb_path.name}")
        print(msg)
        return

    print(f"DRC Summary: {pcb_path.name}")
    print("=" * 50)

    # Group by rule_id
    by_rule: dict[str, dict[str, int]] = {}
    for v in violations:
        key = v.rule_id
        if key not in by_rule:
            by_rule[key] = {"errors": 0, "warnings": 0, "infos": 0}
        if v.is_error:
            by_rule[key]["errors"] += 1
        elif v.is_info:
            by_rule[key]["infos"] += 1
        else:
            by_rule[key]["warnings"] += 1

    print(f"{'Rule ID':<30} {'Errors':<8} {'Warnings':<10} {'Infos':<8}")
    print("-" * 60)

    for rule_id, counts in sorted(by_rule.items()):
        print(f"{rule_id:<30} {counts['errors']:<8} {counts['warnings']:<10} {counts['infos']:<8}")

    print("-" * 60)
    total_errors = sum(c["errors"] for c in by_rule.values())
    total_warnings = sum(c["warnings"] for c in by_rule.values())
    total_infos = sum(c["infos"] for c in by_rule.values())
    print(f"{'TOTAL':<30} {total_errors:<8} {total_warnings:<10} {total_infos:<8}")


if __name__ == "__main__":
    sys.exit(main())

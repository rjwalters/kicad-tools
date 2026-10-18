"""
DRC (Design Rules Check) validation for KiCad PCBs.

Runs KiCad DRC on PCB files or parses existing DRC reports.
Optionally checks against manufacturer design rules.

Usage:
    kicad-drc design.kicad_pcb              # Run DRC on PCB
    kicad-drc design-drc.json               # Parse existing report
    kicad-drc design.kicad_pcb --mfr jlcpcb # Check manufacturer rules
    kicad-drc --rules --mfr seeed           # Show manufacturer rules (deprecated: kct mfr rules)
    kicad-drc --compare                     # Compare manufacturers (deprecated: kct mfr compare)

Exit Codes:
    0 - No errors (warnings may be present)
    1 - Errors found (including error-severity schematic parity findings)
        or command failure
    2 - Warnings found (only with --strict; includes parity warnings), or
        schematic parity NOT RUN (unless --allow-incomplete).

    --mfr mode uses the same codes (Issue #7170).  Its errors are the
    findings that breach the manufacturer's limits plus every error-severity
    finding kct could not judge against them ("not checked"); a finding that
    meets the fab's limit (a board rule stricter than the fab) is cleared.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..drc import (
    DRCReport,
    DRCViolation,
    ViolationCategory,
    ViolationType,
    check_manufacturer_rules,
    generate_fix_suggestions,
)
from ..manufacturers import compare_design_rules, get_all_manufacturer_names, get_profile
from .runner import find_kicad_cli, run_drc

if TYPE_CHECKING:
    from ..drc.checker import ManufacturerCheck
    from ..drc.parity import SchematicParityStatus
    from ..drc.waivers import WaiverApplication
    from ..validate.rules.waivers import Waivers

# Default manufacturer for compatibility hints
DEFAULT_HINT_MANUFACTURER = "jlcpcb"


def _warn_deprecated_flag(flag: str, replacement: str) -> None:
    """Print a one-line stderr pointer to the ``kct mfr`` equivalent (Issue #6898)."""
    print(
        f"[DEPRECATED] kicad-drc {flag} is superseded by `{replacement}`; "
        "the flag still works but is not available on `kct drc`.",
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the ``kicad-drc`` argument parser.

    Factored out of :func:`main` so the parity test can compare it against the
    unified ``kct drc`` subparser (Issue #6898).
    """
    parser = argparse.ArgumentParser(
        prog="kicad-drc",
        description="Run DRC on PCBs or parse DRC reports",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="PCB (.kicad_pcb) to check or DRC report (.json/.rpt) to parse",
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
        help="Exit with error code on warnings",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help=(
            "Exit 0 instead of 2 when schematic parity did NOT RUN (no "
            "schematic resolved, several candidates, one kicad-cli cannot "
            "netlist, or a report that does not record whether parity ran). "
            "Same name and meaning as kct check --allow-incomplete (Issue #7077)"
        ),
    )
    parser.add_argument(
        "--ignore-unconnected",
        action="store_true",
        help=(
            "Do not count unconnected-item (missing connection) findings toward "
            "the verdict and exit code -- the old clearance-only semantics. By "
            "default an unrouted board fails DRC (Issue #7096)"
        ),
    )
    parser.add_argument(
        "--schematic",
        type=Path,
        default=None,
        help=(
            "Schematic for the parity check (default: resolved from the board "
            "-- project.kct, then <base>.kicad_sch with _routed/_optimized/... "
            "stripped).  kicad-cli only reads <pcb-stem>.kicad_sch, so kct "
            "runs it on a temporary copy of the board folder with this "
            "schematic under the board's name (Issue #7147).  Ignored for "
            ".json/.rpt report input"
        ),
    )
    parser.add_argument(
        "--type",
        "-t",
        dest="filter_type",
        help="Filter by violation type (partial match)",
    )
    parser.add_argument(
        "--net",
        help="Filter by net name",
    )
    parser.add_argument(
        "--category",
        "-c",
        choices=[c.value for c in ViolationCategory],
        help="Filter by root-cause category (placement, routing, manufacturing, connectivity, cosmetic)",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        choices=get_all_manufacturer_names(),
        help="Target manufacturer for rules check",
    )
    parser.add_argument(
        "--layers",
        "-l",
        type=int,
        default=2,
        help="Number of copper layers (default: 2)",
    )
    parser.add_argument(
        "--rules",
        action="store_true",
        help="Print manufacturer design rules and exit",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Compare design rules across manufacturers",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed violation information",
    )
    parser.add_argument(
        "--suggest",
        "-s",
        action="store_true",
        help="Show fix suggestions for each violation",
    )
    parser.add_argument(
        "--filter-config",
        type=Path,
        help="TOML file with [[drc.filters]] rules to suppress or reclassify violations",
    )
    parser.add_argument(
        "--waivers",
        dest="waivers",
        default=None,
        help=(
            "Path to a .kct_waivers.json sidecar (schema version 2, the same "
            "file kct check --waivers reads) waiving cross-gate DRC findings "
            "by matching the violation's KiCad type against 'rule' and its "
            "normalized component-ref set against 'items' (and optional "
            "'nets'). Matched violations report as WAIVED and are excluded "
            "from the error gate. Auto-discovered next to the input when this "
            "flag is omitted (Issue #4691). Waived findings keep severity "
            "'error' in JSON so the kct audit manufacturing gate stays "
            "blocking by default; --mfr mode does not apply waivers."
        ),
    )
    parser.add_argument(
        "--keep-report",
        action="store_true",
        help="Keep the DRC report file after running",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Save DRC report to this path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kicad-drc command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mfr:
        args.mfr = get_profile(args.mfr).id

    # Mode: Compare manufacturers
    if args.compare:
        _warn_deprecated_flag("--compare", "kct mfr compare")
        print_comparison(args.layers)
        return 0

    # Mode: Show manufacturer rules
    if args.rules:
        _warn_deprecated_flag("--rules", "kct mfr rules")
        mfr = args.mfr or "jlcpcb"
        print_manufacturer_rules(mfr, args.layers)
        return 0

    # Require input for other modes
    if not args.input:
        parser.print_help()
        print("\nError: input file required", file=sys.stderr)
        print("\nTo see design rules, run with --rules --mfr <manufacturer>", file=sys.stderr)
        print("To compare manufacturers, run with --compare", file=sys.stderr)
        return 1

    input_path = Path(args.input)

    # Load the optional .kct_waivers.json sidecar (Issue #4691) BEFORE running
    # kicad-cli, so a bad explicit path fails fast.  Same load/degrade contract
    # as `kct check --waivers`: an explicit --waivers path always wins and a
    # missing/malformed explicit file is a hard error, while an auto-discovered
    # sidecar that fails to parse degrades gracefully (warn + zero waivers).
    # Discovery is anchored at the input path -- the board for .kicad_pcb
    # input, the report's own directory for .json/.rpt input.
    waivers, waiver_rc = _load_waivers(args.waivers, input_path)
    if waiver_rc != 0:
        return waiver_rc

    # Determine if input is PCB or report.  For a PCB, run_drc_on_pcb records
    # whether kicad-cli actually ran schematic parity on the report (Issue
    # #7077); a loaded .json/.rpt report leaves it unknown.
    if input_path.suffix == ".kicad_pcb":
        # Issue #7147: an explicit --schematic must exist.
        if args.schematic is not None and not args.schematic.is_file():
            print(f"Error: schematic not found: {args.schematic}", file=sys.stderr)
            return 1
        # Run DRC on PCB
        report = run_drc_on_pcb(input_path, args.output, args.keep_report, schematic=args.schematic)
        if report is None:
            return 1
    elif input_path.suffix in (".json", ".rpt"):
        if args.schematic is not None:
            print(
                "[INFO] --schematic is ignored for report input (it only applies "
                "when kct drc runs kicad-cli on a .kicad_pcb).",
                file=sys.stderr,
            )
        # Parse existing report
        try:
            report = DRCReport.load(input_path)
        except FileNotFoundError:
            print(f"Error: File not found: {input_path}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error loading report: {e}", file=sys.stderr)
            return 1
    else:
        print(f"Error: Unsupported file type: {input_path.suffix}", file=sys.stderr)
        print("Expected .kicad_pcb (PCB) or .json/.rpt (report)", file=sys.stderr)
        return 1

    # Apply violation filters from config file
    filter_ignored_count = 0
    if args.filter_config:
        try:
            from ..validate.filters import load_filters_from_toml

            drc_filters, _erc_filters = load_filters_from_toml(str(args.filter_config))
            if drc_filters:
                from ..validate.filters import FilterEngine

                engine = FilterEngine(drc_filters)
                filter_result = engine.apply(report.violations)
                report = DRCReport(
                    source_file=report.source_file,
                    created_at=report.created_at,
                    pcb_name=report.pcb_name,
                    violations=filter_result.kept,
                    footprint_errors=report.footprint_errors,
                    # Issue #7096: keep connectivity items so filtering cannot
                    # turn an unrouted board into a pass.
                    unconnected_items=report.unconnected_items,
                    # Issue #7077: parity findings and the run signal are not
                    # geometric violations; carry them through untouched.
                    schematic_parity=report.schematic_parity,
                    schematic_parity_ran=report.schematic_parity_ran,
                    schematic_parity_reason=report.schematic_parity_reason,
                    schematic_parity_schematic=report.schematic_parity_schematic,
                )
                filter_ignored_count = filter_result.ignored_count
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error loading filter config: {e}", file=sys.stderr)
            return 1

    # Manufacturer check mode.  Waivers are deliberately NOT applied here
    # (Issue #4691, v1 scope): --mfr answers "can this fab build it?", which a
    # design-intent waiver does not change.  Say so rather than silently
    # ignoring a supplied sidecar.
    if args.mfr:
        if waivers is not None and waivers.entries:
            print(
                "[INFO] --mfr manufacturer compatibility check does not apply "
                "waivers; run without --mfr for the waived DRC gate.",
                file=sys.stderr,
            )
        # Issue #7170: --mfr shares the plain path's exit gate
        # (_drc_exit_code), including schematic parity, --strict and
        # --allow-incomplete.  This replaces #7077's separate 0/1 rule.
        # Issue #7096: error-severity unconnected items fold into it too.
        return output_manufacturer_check(
            report,
            args.mfr,
            args.layers,
            args.verbose,
            fmt=args.format,
            strict=args.strict,
            allow_incomplete=args.allow_incomplete,
            ignore_unconnected=args.ignore_unconnected,
        )

    # Apply waivers to the parsed report (Issue #4691).  Runs before the
    # --errors-only / --type / --net / --category filters and before the exit
    # gate, so a waived finding is excluded from the errors list everywhere.
    waiver_result = None
    if waivers is not None and waivers.entries:
        from ..drc.waivers import apply_waivers_to_report

        waiver_result = apply_waivers_to_report(report, waivers)

    # Apply filters
    violations = list(report.violations)

    # Issue #7096: connectivity items (KiCad's separate ``unconnected_items``
    # array) are listed and gate the verdict by their own severity, like any
    # other finding, so an unrouted board cannot read as DRC PASSED.  Every
    # severity (and waived entries) is folded in so a JSON report reads the
    # same as the text ``.rpt``, whose flat list already holds them;
    # ``connectivity_items()`` also returns those, so skip them here.
    if not args.ignore_unconnected:
        listed = {id(v) for v in violations}
        violations.extend(v for v in report.connectivity_items() if id(v) not in listed)

    # Issue #7230: view filters change what is shown, never the verdict.  The
    # exit gate counts this snapshot (waived findings excluded, as always).
    gate_violations = list(violations)

    if args.errors_only:
        violations = [v for v in violations if v.is_error]

    if args.filter_type:
        filter_lower = args.filter_type.lower()
        violations = [
            v
            for v in violations
            if filter_lower in v.type_str.lower() or filter_lower in v.message.lower()
        ]

    if args.net:
        violations = [v for v in violations if args.net in v.nets]

    if args.category:
        cat = ViolationCategory(args.category)
        violations = [v for v in violations if v.category == cat]

    from ._gate import hidden_by_view_filter

    hidden = hidden_by_view_filter([v for v in gate_violations if not v.is_waived], violations)

    # Generate suggestions if requested
    if args.suggest:
        for v in violations:
            suggestion = generate_fix_suggestions(v)
            if suggestion:
                v.suggestions = [suggestion]

    # Output
    if args.format == "json":
        output_json(
            violations,
            report,
            show_suggestions=args.suggest,
            filtered_count=filter_ignored_count,
            waiver_result=waiver_result,
            hidden=hidden,
        )
    elif args.format == "summary":
        output_summary(
            violations,
            report,
            filtered_count=filter_ignored_count,
            waiver_result=waiver_result,
            hidden=hidden,
        )
    else:
        output_table(
            violations,
            report,
            args.verbose,
            show_suggestions=args.suggest,
            layers=args.layers,
            filtered_count=filter_ignored_count,
            waiver_result=waiver_result,
            hidden=hidden,
        )

    # Exit code.  ``is_error`` already excludes waived findings (Issue #4691),
    # so the gate is "nonzero iff UNWAIVED errors remain".  Waived findings are
    # likewise kept out of the warning count so --strict never trips on them.
    error_count = sum(1 for v in gate_violations if v.is_error)
    waived_count = sum(1 for v in gate_violations if v.is_waived)
    warning_count = len(gate_violations) - error_count - waived_count

    return _drc_exit_code(
        error_count,
        warning_count,
        report.schematic_parity_status,
        strict=args.strict,
        allow_incomplete=args.allow_incomplete,
    )


def _drc_exit_code(
    error_count: int,
    warning_count: int,
    parity: "SchematicParityStatus",
    *,
    strict: bool,
    allow_incomplete: bool,
) -> int:
    """The one ``kct drc`` exit gate, shared by plain and ``--mfr`` mode.

    ``error_count`` / ``warning_count`` are the caller's own findings; the
    schematic parity findings are folded in here, by their own severity
    (Issue #7077), so neither path can forget them.  Error-severity unconnected
    items (Issue #7096) are already in ``error_count``: the plain path folds
    them into its ``violations`` list and ``--mfr`` adds them to its gate
    counts, unless ``--ignore-unconnected``.

    Returns 1 when any error remains, 2 for warnings under ``strict`` or for
    schematic parity NOT RUN without ``allow_incomplete`` (a check that did
    not run never reads as a pass, matching ``kct check``), else 0.
    Issue #7170 moved ``--mfr`` onto this gate.
    """
    error_count += parity.error_count
    warning_count += parity.warning_count

    if error_count > 0:
        return 1
    if warning_count > 0 and strict:
        return 2
    if not parity.checked and not allow_incomplete:
        return 2
    return 0


def _error_connectivity_items(report: DRCReport) -> list[DRCViolation]:
    """Error-severity (unwaived) connectivity entries of ``report`` (Issue #7096)."""
    return [v for v in report.connectivity_items() if v.is_error]


def _load_waivers(explicit: str | None, input_path: Path) -> tuple["Waivers | None", int]:
    """Resolve the ``.kct_waivers.json`` sidecar for this run (Issue #4691).

    Mirrors ``kct check --waivers`` exactly: an explicit path always wins and a
    missing or malformed explicit file is a hard error; an auto-discovered
    sidecar that fails to parse degrades to zero waivers with a warning.

    Issue #4765: for ``.json``/``.rpt`` report input the discovery anchor is
    the report's own directory, so the board directory one level up (the
    canonical ``boards/NN/`` home of ``.kct_waivers.json`` when the input is
    ``boards/NN/output/drc.json``) is probed as a last resort.

    Args:
        explicit: The ``--waivers`` value, or None to auto-discover.
        input_path: The PCB or report path used as the discovery anchor.

    Returns:
        ``(waivers, exit_code)``; a nonzero exit code means the caller must
        return it immediately.
    """
    from ..validate.rules.waivers import (
        discover_waivers_sidecar,
        load_waivers,
        shadowed_waivers_sidecars,
    )

    if explicit is not None:
        path: Path | None = Path(explicit).resolve()
    else:
        path = discover_waivers_sidecar(input_path)
        if path is not None:
            for shadowed in shadowed_waivers_sidecars(input_path, path):
                print(
                    f"WARNING: waivers sidecar {shadowed} is NOT applied: {path} takes"
                    " precedence and only one sidecar is loaded.",
                    file=sys.stderr,
                )
        if path is None and input_path.suffix in (".json", ".rpt"):
            # Issue #4765: for REPORT input the anchor is the report, not the
            # board -- and ``discover_waivers_sidecar`` probes ``<dir>/``,
            # ``<dir>/output/`` and ``<dir>/../output/``.  For the canonical
            # ``boards/NN/output/drc.json`` layout those collapse to
            # ``boards/NN/output/`` (twice) and ``boards/NN/output/output/``,
            # so the board directory -- exactly where the sidecar lives for
            # the ``.kicad_pcb`` path -- is never probed.  Add that one
            # anchor here rather than widening the shared discovery helper,
            # which ``kct check --waivers`` also uses with correct
            # ``.kicad_pcb`` anchoring.
            board_dir_candidate = input_path.parent.parent / ".kct_waivers.json"
            if board_dir_candidate.is_file():
                path = board_dir_candidate

    if path is None:
        return None, 0

    if not path.exists():
        print(f"Error: waivers file not found: {path}", file=sys.stderr)
        return None, 1

    try:
        waivers = load_waivers(path)
    except ValueError as e:
        if explicit is not None:
            print(f"Error: {e}", file=sys.stderr)
            return None, 1
        print(
            f"WARNING: ignoring malformed waivers sidecar {path}: {e}",
            file=sys.stderr,
        )
        return None, 0

    if explicit is None:
        print(f"[INFO] auto-loaded waivers sidecar: {path}", file=sys.stderr)
    return waivers, 0


def _print_unused_waivers(waiver_result: "WaiverApplication | None") -> None:
    """Print the non-gating stale-waiver advisory block, if any."""
    if waiver_result is None or not waiver_result.unused:
        return
    print(f"\n{'-' * 60}")
    print("UNUSED WAIVERS (advisory, non-blocking):")
    for message in waiver_result.unused_messages:
        print(f"\n  [i] {message}")


def _print_parity_status(parity: "SchematicParityStatus", *, issues: bool = False) -> None:
    """Print the one ``Schematic parity: ...`` status line (Issue #7077).

    The line itself comes from the shared
    :meth:`~kicad_tools.drc.parity.SchematicParityStatus.status_line`, so
    table, summary, ``--mfr`` and ``kct drc-summary`` cannot drift apart.
    With ``issues=True`` a FAILED status also lists its findings.
    """
    print(f"\n{parity.status_line()}")
    if issues and parity.issues:
        print(f"\n{'-' * 60}")
        for line in parity.issue_lines():
            print(line)


def _hidden_text(hidden: dict[str, int]) -> str:
    """``N hidden: E error(s), W warning(s)`` for a view-filter disclosure."""
    total = hidden["errors"] + hidden["warnings"]
    return f"{total} hidden: {hidden['errors']} error(s), {hidden['warnings']} warning(s)"


def _verdict_line(
    has_errors: bool,
    has_warnings: bool,
    parity: "SchematicParityStatus",
    passed_text: str,
) -> str:
    """Final table verdict.  NOT RUN parity never prints ``DRC PASSED``."""
    if has_errors:
        return "DRC FAILED - Fix errors before manufacturing"
    if not parity.checked:
        suffix = "; review warnings" if has_warnings else ""
        return f"DRC INCOMPLETE - Schematic parity NOT RUN{suffix}"
    if has_warnings:
        return "DRC WARNING - Review warnings"
    return f"DRC PASSED - {passed_text}"


def run_drc_on_pcb(
    pcb_path: Path,
    output_path: Path | None,
    keep_report: bool,
    schematic: Path | None = None,
) -> DRCReport | None:
    """Run DRC on a PCB and return the parsed report.

    The report's ``schematic_parity_ran`` is set from kicad-cli's output:
    ``False`` when it said it could not fetch the schematic netlist, so the
    ``--schematic-parity`` checks it was asked for never ran (Issue #7077).
    ``schematic`` overrides the resolved parity schematic; the report records
    the schematic used and the cause of a NOT RUN (Issue #7147).
    """
    if not pcb_path.exists():
        print(f"Error: PCB not found: {pcb_path}", file=sys.stderr)
        return None

    # Check for kicad-cli
    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("Error: kicad-cli not found", file=sys.stderr)
        print("Install KiCad 8 from: https://www.kicad.org/download/", file=sys.stderr)
        print("\nmacOS: brew install --cask kicad", file=sys.stderr)
        return None

    print(f"Running DRC on: {pcb_path.name}", file=sys.stderr)

    result = run_drc(pcb_path, output_path, kicad_cli=kicad_cli, schematic=schematic)

    if not result.success:
        print(f"Error running DRC: {result.stderr}", file=sys.stderr)
        return None

    # Parse the report
    try:
        report = DRCReport.load(result.output_path)
    except Exception as e:
        print(f"Error parsing DRC report: {e}", file=sys.stderr)
        return None

    # Cleanup temporary file unless keeping
    if not keep_report and output_path is None and result.output_path:
        result.output_path.unlink(missing_ok=True)

    report.schematic_parity_ran = not result.parity_skipped
    report.schematic_parity_reason = result.parity_skip_reason
    if result.parity_schematic is not None:
        report.schematic_parity_schematic = str(result.parity_schematic)
    return report


def output_table(
    violations: list[DRCViolation],
    report: DRCReport,
    verbose: bool = False,
    show_suggestions: bool = False,
    layers: int = 2,
    filtered_count: int = 0,
    waiver_result: "WaiverApplication | None" = None,
    hidden: dict[str, int] | None = None,
) -> None:
    """Output violations as a formatted table.

    The summary block carries one ``Schematic parity:`` status line; parity
    findings get their own section and count toward the verdict by their
    severity, and a NOT RUN parity makes the verdict INCOMPLETE rather than
    PASSED (Issue #7077).
    """
    parity = report.schematic_parity_status
    error_count = sum(1 for v in violations if v.is_error)
    # Issue #4691/#4696: waived findings get their own bucket -- they are never
    # folded into the warning count.
    waived_count = sum(1 for v in violations if v.is_waived)
    warning_count = len(violations) - error_count - waived_count

    print(f"\n{'=' * 60}")
    print("DRC VALIDATION SUMMARY")
    print(f"{'=' * 60}")

    if report.source_file:
        print(f"File: {Path(report.source_file).name}")
    if report.pcb_name:
        print(f"PCB: {report.pcb_name}")

    print("\nResults:")
    print(f"  Errors:     {error_count}")
    print(f"  Warnings:   {warning_count}")
    if waived_count > 0:
        print(f"  Waived:     {waived_count}")
    if filtered_count > 0:
        print(f"  Filtered:   {filtered_count} violations filtered")
    if hidden:
        print(f"  Hidden:     {_hidden_text(hidden)} (view filter; still counted in the verdict)")
    hidden_errors = hidden["errors"] if hidden else 0
    hidden_warnings = hidden["warnings"] if hidden else 0
    _print_parity_status(parity)

    if not violations:
        if parity.issues:
            print(f"\n{'-' * 60}")
            for line in parity.issue_lines():
                print(line)
        print(f"\n{'=' * 60}")
        print(
            _verdict_line(
                parity.error_count > 0 or hidden_errors > 0,
                parity.warning_count > 0 or hidden_warnings > 0,
                parity,
                "No violations found",
            )
            + (" (hidden by view filter)" if hidden else "")
        )
        _print_unused_waivers(waiver_result)
        return

    # Group by type summary
    by_type: dict = {}
    for v in violations:
        if v.type_str not in by_type:
            by_type[v.type_str] = {"errors": 0, "warnings": 0, "waived": 0}
        by_type[v.type_str][_bucket(v)] += 1

    print(f"\n{'-' * 60}")
    print("BY TYPE:")
    for vtype, counts in sorted(by_type.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {vtype}: {_format_counts(counts)}")

    # Group by category summary
    by_category: dict = {}
    for v in violations:
        cat = v.category.value
        if cat not in by_category:
            by_category[cat] = {"errors": 0, "warnings": 0, "waived": 0}
        by_category[cat][_bucket(v)] += 1

    print(f"\n{'-' * 60}")
    print("BY CATEGORY:")
    for cat, counts in sorted(by_category.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {cat}: {_format_counts(counts)}")

    # Detailed output
    errors = [v for v in violations if v.is_error]
    warnings = [v for v in violations if not v.is_error and not v.is_waived]
    waived = [v for v in violations if v.is_waived]

    if errors:
        print(f"\n{'-' * 60}")
        print("ERRORS (must fix):")
        for v in errors:
            _print_single(v, verbose, show_suggestions)

    if warnings:
        print(f"\n{'-' * 60}")
        print("WARNINGS (review recommended):")
        display_warnings = warnings if verbose else warnings[:10]
        for v in display_warnings:
            _print_single(v, verbose, show_suggestions)
        if len(warnings) > 10 and not verbose:
            print(f"\n  ... and {len(warnings) - 10} more warnings (use --verbose)")

    # Issue #4691: waived findings stay visible in their own section so a
    # reviewer can audit each documented exception, but never gate.
    if waived:
        print(f"\n{'-' * 60}")
        print("WAIVED (documented exceptions, non-blocking):")
        for v in waived:
            _print_single(v, verbose, show_suggestions)
            print(f"      Waiver reason: {v.waiver_reason}")
            if v.waiver_issue:
                print(f"      Waiver issue: {v.waiver_issue}")

    if parity.issues:
        print(f"\n{'-' * 60}")
        for line in parity.issue_lines():
            print(line)

    _print_unused_waivers(waiver_result)

    print(f"\n{'=' * 60}")
    print(
        _verdict_line(
            bool(errors) or parity.error_count > 0 or hidden_errors > 0,
            bool(warnings) or parity.warning_count > 0 or hidden_warnings > 0,
            parity,
            "No unwaived violations",
        )
        + (" (hidden by view filter)" if hidden else "")
    )
    if errors:
        # Show manufacturer compatibility hint if violations might pass mfr rules
        hint = _get_manufacturer_compatibility_hint(violations, layers=layers)
        if hint:
            print(hint)


def _bucket(v: DRCViolation) -> str:
    """Return the count bucket a violation belongs to (waived is its own)."""
    if v.is_waived:
        return "waived"
    return "errors" if v.is_error else "warnings"


def _format_counts(counts: dict) -> str:
    """Render a {errors, warnings, waived} bucket dict, omitting empty buckets."""
    parts = []
    if counts.get("errors"):
        parts.append(f"{counts['errors']} error{'s' if counts['errors'] != 1 else ''}")
    if counts.get("warnings"):
        parts.append(f"{counts['warnings']} warning{'s' if counts['warnings'] != 1 else ''}")
    if counts.get("waived"):
        parts.append(f"{counts['waived']} waived")
    return ", ".join(parts)


def _print_single(
    v: DRCViolation,
    verbose: bool,
    show_suggestions: bool = False,
    indent: str = "  ",
) -> None:
    """Print a single violation."""
    symbol = "~" if v.is_waived else ("X" if v.is_error else "!")
    print(f"\n{indent}[{symbol}] {v.type_str}")
    print(f"{indent}    {v.message}")

    # Show measurement delta if available
    if v.required_value_mm is not None and v.actual_value_mm is not None:
        delta = v.required_value_mm - v.actual_value_mm
        print(
            f"{indent}    Required: {v.required_value_mm:.3f}mm, "
            f"Actual: {v.actual_value_mm:.3f}mm (need +{delta:.3f}mm)"
        )

    if verbose:
        if v.locations:
            for loc in v.locations:
                pos_str = f"({loc.x_mm:.2f}, {loc.y_mm:.2f})" if loc.x_mm or loc.y_mm else ""
                layer = f"[{loc.layer}]" if loc.layer else ""
                print(f"{indent}    -> {pos_str} {layer}")
        if v.nets:
            print(f"{indent}    Nets: {', '.join(v.nets)}")
        if v.suggestions:
            print(f"{indent}    Suggestions:")
            for suggestion in v.suggestions[:3]:  # Show top 3 suggestions
                print(f"{indent}      - {suggestion}")

    # Show fix suggestions
    if show_suggestions and v.suggestions:
        print(f"{indent}    FIX: {v.suggestions[0].description}")
        if verbose and v.suggestions[0].alternatives:
            for alt in v.suggestions[0].alternatives[:2]:
                print(f"{indent}         or: {alt.description}")


def output_json(
    violations: list[DRCViolation],
    report: DRCReport,
    show_suggestions: bool = False,
    filtered_count: int = 0,
    waiver_result: "WaiverApplication | None" = None,
    hidden: dict[str, int] | None = None,
) -> None:
    """Output violations as JSON.

    Issue #7077: a top-level ``schematic_parity`` object (``status``,
    ``checked``, ``reason``, ``error_count``, ``warning_count``, ``issues``)
    plus ``parity_errors``/``parity_warnings`` in ``summary``.
    ``schematic_parity_checked`` is a convenience alias derived from the
    status (``false`` for NOT RUN, including report input that does not
    record whether parity ran).
    """
    parity = report.schematic_parity_status
    violations_data = []
    for v in violations:
        v_dict = v.to_dict()
        # Only include suggestions if requested and available
        if not show_suggestions and "suggestions" in v_dict:
            del v_dict["suggestions"]
        violations_data.append(v_dict)

    # Issue #4691/#4696: waived findings are counted in their own bucket and
    # never as warnings.  Each waived violation keeps its underlying
    # "severity" and reports "status": "waived" (see DRCViolation.to_dict).
    waived_count = sum(1 for v in violations if v.is_waived)
    summary: dict = {
        "errors": sum(1 for v in violations if v.is_error),
        "warnings": sum(1 for v in violations if not v.is_error and not v.is_waived),
    }
    # Issue #7096: explicit count of missing-connection findings in ``violations``.
    summary["unconnected"] = sum(1 for v in violations if v.type == ViolationType.UNCONNECTED_ITEMS)
    summary["parity_errors"] = parity.error_count
    summary["parity_warnings"] = parity.warning_count
    if waived_count > 0:
        summary["waived"] = waived_count
    if filtered_count > 0:
        summary["filtered"] = filtered_count
    if hidden:
        summary["hidden_by_view_filter"] = hidden

    data: dict[str, Any] = {
        "source": report.source_file,
        "pcb_name": report.pcb_name,
        "summary": summary,
        "violations": violations_data,
        "schematic_parity": parity.to_dict(),
        "schematic_parity_checked": parity.checked,
    }
    if waiver_result is not None and waiver_result.unused:
        data["unused_waivers"] = waiver_result.to_dict_list()
    print(json.dumps(data, indent=2))


def output_summary(
    violations: list[DRCViolation],
    report: DRCReport,
    filtered_count: int = 0,
    waiver_result: "WaiverApplication | None" = None,
    hidden: dict[str, int] | None = None,
) -> None:
    """Output violation summary by type, plus the parity status line."""
    parity = report.schematic_parity_status
    if not violations:
        if filtered_count > 0:
            print(f"No DRC violations found ({filtered_count} violations filtered).")
        else:
            print("No DRC violations found.")
        if hidden:
            print(f"Hidden by view filter: {_hidden_text(hidden)} (still counted)")
        _print_parity_status(parity, issues=True)
        _print_unused_waivers(waiver_result)
        return

    print(f"DRC Summary: {report.source_file}")
    print("=" * 50)

    # Group by type.  Issue #4691/#4696: waived findings get a dedicated
    # column -- folding them into "Warnings" is exactly the defect #4696
    # reports against ``kct check --format summary``.
    by_type: dict = {}
    for v in violations:
        key = v.type_str
        if key not in by_type:
            by_type[key] = {"errors": 0, "warnings": 0, "waived": 0}
        by_type[key][_bucket(v)] += 1

    any_waived = any(c["waived"] for c in by_type.values())
    waived_header = f" {'Waived':<8}" if any_waived else ""
    print(f"{'Type':<25} {'Errors':<8} {'Warnings':<8}{waived_header}")
    print("-" * 50)

    for type_name, counts in sorted(by_type.items()):
        waived_col = f" {counts['waived']:<8}" if any_waived else ""
        print(f"{type_name:<25} {counts['errors']:<8} {counts['warnings']:<8}{waived_col}")

    print("-" * 50)
    total_errors = sum(c["errors"] for c in by_type.values())
    total_warnings = sum(c["warnings"] for c in by_type.values())
    total_waived = sum(c["waived"] for c in by_type.values())
    waived_total_col = f" {total_waived:<8}" if any_waived else ""
    print(f"{'TOTAL':<25} {total_errors:<8} {total_warnings:<8}{waived_total_col}")
    if hidden:
        print(f"Hidden by view filter: {_hidden_text(hidden)} (still counted)")

    _print_parity_status(parity, issues=True)
    _print_unused_waivers(waiver_result)


def _bucket_manufacturer_checks(
    checks: list["ManufacturerCheck"],
) -> tuple[list["ManufacturerCheck"], list["ManufacturerCheck"], list["ManufacturerCheck"]]:
    """Split checks into (compatible, incompatible, not checked).  Issue #7170."""
    compatible = [c for c in checks if c.is_compatible]
    not_checked = [c for c in checks if c.is_not_checked]
    incompatible = [c for c in checks if not c.is_compatible and not c.is_not_checked]
    return compatible, incompatible, not_checked


def _manufacturer_gate_counts(checks: list["ManufacturerCheck"]) -> tuple[int, int]:
    """``(error_count, warning_count)`` of ``--mfr`` checks for :func:`_drc_exit_code`.

    Issue #7170: a fab-limit breach (FAIL) is blocking whatever kicad-cli's
    severity was; a finding kct could not judge keeps kicad-cli's severity;
    a WARNING result (no measured value) is a warning; a PASS (the fab's
    limit is met) clears the kicad-cli finding.
    """
    from ..drc.checker import CheckResult

    errors = 0
    warnings = 0
    for check in checks:
        if check.result == CheckResult.FAIL:
            errors += 1
        elif check.result == CheckResult.UNKNOWN:
            if check.violation.is_error:
                errors += 1
            else:
                warnings += 1
        elif check.result == CheckResult.WARNING:
            warnings += 1
    return errors, warnings


def _count_by_type(checks: list["ManufacturerCheck"]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for check in checks:
        key = check.violation.type_str or check.violation.type.value
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def output_manufacturer_check(
    report: DRCReport,
    mfr: str,
    layers: int,
    verbose: bool,
    *,
    fmt: str = "table",
    strict: bool = False,
    allow_incomplete: bool = False,
    ignore_unconnected: bool = False,
) -> int:
    """Check DRC violations against manufacturer limits.

    Every violation gets exactly one verdict (Issue #7170): compatible (the
    fab's limit is met), incompatible (it is breached), or *not checked*
    (kct has no fab-limit check for that type; kicad-cli's severity stands).
    ``fmt == "json"`` prints a JSON document instead of the table; every other
    format prints the table.  The exit code comes from :func:`_drc_exit_code`,
    the same gate plain ``kct drc`` uses, so schematic parity, ``--strict``
    and ``--allow-incomplete`` behave identically in both modes.

    Issue #7096: error-severity unconnected items fail this check too (unless
    ``ignore_unconnected``) -- no fab can build an unrouted board.  KiCad JSON
    reports keep them in a separate collection that
    :func:`check_manufacturer_rules` never sees, so they are added to the gate
    counts here; text ``.rpt`` entries already sit in ``report.violations``
    (and so in ``checks``) and are not counted twice.
    """
    profile = get_profile(mfr)
    parity = report.schematic_parity_status

    checks = check_manufacturer_rules(report, mfr, layers=layers)
    compatible, incompatible, not_checked = _bucket_manufacturer_checks(checks)
    nc_errors = sum(1 for c in not_checked if c.violation.is_error)
    nc_warnings = len(not_checked) - nc_errors
    error_count, warning_count = _manufacturer_gate_counts(checks)
    # Issue #7096: every error-severity connectivity item, for display; only
    # the ones not already judged in ``checks`` add to the gate.
    unconnected = [] if ignore_unconnected else _error_connectivity_items(report)
    in_checks = {id(c.violation) for c in checks}
    unconnected_extra = [v for v in unconnected if id(v) not in in_checks]
    error_count += len(unconnected_extra)
    exit_code = _drc_exit_code(
        error_count,
        warning_count,
        parity,
        strict=strict,
        allow_incomplete=allow_incomplete,
    )

    if fmt == "json":
        data: dict[str, Any] = {
            "manufacturer": profile.id,
            "layers": layers,
            "source": report.source_file,
            "summary": {
                "total": len(checks),
                "checked": len(compatible) + len(incompatible),
                "compatible": len(compatible),
                "incompatible": len(incompatible),
                "not_checked": len(not_checked),
                "not_checked_errors": nc_errors,
                "not_checked_warnings": nc_warnings,
                "not_checked_by_type": _count_by_type(not_checked),
                "unconnected": len(unconnected),
                "parity_errors": parity.error_count,
                "parity_warnings": parity.warning_count,
            },
            # Issue #7096: error-severity missing connections (unrouted nets).
            # Text ``.rpt`` entries also appear in ``checks`` (as connection
            # failures); the exit gate counts each one once.
            "unconnected_items": [v.to_dict() for v in unconnected],
            "checks": [
                {
                    "type_str": c.violation.type_str or c.violation.type.value,
                    "severity": c.violation.severity.value,
                    "result": c.result.value,
                    "rule_name": c.rule_name,
                    "message": c.message,
                    "violation_message": c.violation.message,
                    "manufacturer_limit": c.manufacturer_limit,
                    "actual_value": c.actual_value,
                }
                for c in checks
            ],
            "schematic_parity": parity.to_dict(),
            "schematic_parity_checked": parity.checked,
            "exit_code": exit_code,
        }
        print(json.dumps(data, indent=2))
        return exit_code

    print(f"\n{'=' * 60}")
    print(f"MANUFACTURER COMPATIBILITY: {profile.name}")
    print(f"{'=' * 60}")
    print(f"Layer count: {layers}")
    print(f"Website: {profile.website}")

    if incompatible:
        print(f"\n{'-' * 60}")
        print(f"INCOMPATIBLE VIOLATIONS ({len(incompatible)}):")
        for check in incompatible:
            print(f"\n  [X] {check.violation.type_str}")
            print(f"      {check.violation.message}")
            if check.actual_value is not None and check.manufacturer_limit is not None:
                print(
                    f"      Actual: {check.actual_value:.3f}mm, Limit: {check.manufacturer_limit:.3f}mm"
                )

    if not_checked:
        print(f"\n{'-' * 60}")
        print(
            f"NOT CHECKED against {profile.name} limits: {len(not_checked)} "
            f"(errors {nc_errors}, warnings {nc_warnings})"
        )
        by_type = ", ".join(f"{t} {n}" for t, n in _count_by_type(not_checked).items())
        print(f"  by type: {by_type}")
        for check in not_checked:
            marker = "[!]" if check.violation.is_error else "[?]"
            print(f"\n  {marker} {check.violation.type_str} ({check.violation.severity.value})")
            print(f"      {check.violation.message}")

    if not incompatible:
        print(f"\n{'-' * 60}")
        if compatible:
            suffix = f" ({len(not_checked)} not checked, listed above)" if not_checked else ""
            print(f"All {len(compatible)} checked violations are compatible{suffix}.")
        elif not_checked:
            print(f"No violation could be checked against {profile.name} limits.")
        elif not unconnected_extra:
            print("No DRC violations to check.")

    # Issue #7096: text-report entries are already judged (and listed) as
    # INCOMPATIBLE connection failures above.
    if unconnected_extra:
        print(f"\n{'-' * 60}")
        print(f"UNCONNECTED ITEMS ({len(unconnected_extra)}): board is not fully routed")
        for v in unconnected_extra:
            print(f"  [X] {v.message}")

    print(f"\n{'=' * 60}")
    print(
        f"Compatible: {len(compatible)}, Incompatible: {len(incompatible)}, "
        f"Not checked: {len(not_checked)}"
    )
    if unconnected:
        print(f"Unconnected: {len(unconnected)}")

    if profile.supports_assembly():
        print("Assembly: Supported")
        if profile.parts_library:
            print(f"Parts Library: {profile.parts_library.name}")
    else:
        print("Assembly: Not available")

    _print_parity_status(parity)

    return exit_code


def print_manufacturer_rules(mfr: str, layers: int) -> None:
    """Print manufacturing rules for a manufacturer."""
    profile = get_profile(mfr)
    rules = profile.get_design_rules(layers)

    print(f"\n{'=' * 60}")
    print(f"{profile.name.upper()} {layers}-LAYER PCB CAPABILITIES")
    print(f"{'=' * 60}")

    print("\nMinimum Values:")
    print(
        f"  Trace width:      {rules.min_trace_width_mm:.4f} mm ({rules.min_trace_width_mil:.1f} mil)"
    )
    print(
        f"  Trace spacing:    {rules.min_clearance_mm:.4f} mm ({rules.min_clearance_mil:.1f} mil)"
    )
    print(f"  Via drill:        {rules.min_via_drill_mm} mm")
    print(f"  Via diameter:     {rules.min_via_diameter_mm} mm")
    print(f"  Annular ring:     {rules.min_annular_ring_mm} mm")
    print(f"  Copper-to-edge:   {rules.min_copper_to_edge_mm} mm")

    print("\nSilkscreen:")
    print(f"  Min line width:   {rules.min_silkscreen_width_mm} mm")
    print(f"  Min text height:  {rules.min_silkscreen_height_mm} mm")

    print("\nBoard Specifications:")
    print(f"  Thickness:        {rules.board_thickness_mm} mm")
    print(f"  Outer copper:     {rules.outer_copper_oz} oz")
    if rules.inner_copper_oz > 0:
        print(f"  Inner copper:     {rules.inner_copper_oz} oz")

    print(f"\nWebsite: {profile.website}")

    if profile.supports_assembly():
        print("\nAssembly: Supported")
        if profile.parts_library:
            print(f"Parts Library: {profile.parts_library.name}")
            if profile.parts_library.catalog_url:
                print(f"Catalog: {profile.parts_library.catalog_url}")
    else:
        print("\nAssembly: Not available (PCB only)")

    print(f"\n{'=' * 60}")


def _get_manufacturer_compatibility_hint(
    violations: list[DRCViolation],
    layers: int = 2,
) -> str | None:
    """Check if DRC violations would pass common manufacturer validation.

    Returns a helpful hint message if violations pass manufacturer rules,
    helping users understand the disconnect between DRC and manufacturability.
    """
    # Only check if there are errors that might be manufacturer-specific
    errors = [v for v in violations if v.is_error]
    if not errors:
        return None

    # Create a temporary report to check manufacturer compatibility
    temp_report = DRCReport(
        source_file="",
        created_at=None,
        pcb_name="",
        violations=errors,
    )

    try:
        checks = check_manufacturer_rules(
            temp_report,
            DEFAULT_HINT_MANUFACTURER,
            layers=layers,
        )
    except Exception:
        # If manufacturer check fails, don't show hint
        return None

    if not checks:
        return None

    # Issue #7170: an error kct could not judge against the fab's limits must
    # never be hinted at as "may pass".
    if any(c.is_not_checked for c in checks):
        return None

    # Count how many violations pass manufacturer validation
    incompatible = [c for c in checks if not c.is_compatible]
    compatible = len(checks) - len(incompatible)

    # If all checked violations pass manufacturer rules, show hint
    if len(incompatible) == 0 and compatible > 0:
        profile = get_profile(DEFAULT_HINT_MANUFACTURER)
        return (
            f"\nNote: These {compatible} violation(s) may pass {profile.name} manufacturing rules.\n"
            f"      Your board's internal rules are stricter than the manufacturer's minimums.\n"
            f"      Run with --mfr {DEFAULT_HINT_MANUFACTURER} to check compatibility:\n"
            f"      kicad-drc <file> --mfr {DEFAULT_HINT_MANUFACTURER} -l {layers}"
        )

    # If some but not all pass, still suggest checking
    if compatible > 0 and len(incompatible) < len(checks):
        profile = get_profile(DEFAULT_HINT_MANUFACTURER)
        return (
            f"\nNote: {compatible} of {len(checks)} violations may pass {profile.name} rules.\n"
            f"      Run with --mfr {DEFAULT_HINT_MANUFACTURER} to see manufacturer compatibility."
        )

    return None


def print_comparison(layers: int) -> None:
    """Print comparison of design rules across manufacturers."""
    rules_by_mfr = compare_design_rules(layers=layers)

    print(f"\n{'=' * 70}")
    print(f"MANUFACTURER COMPARISON - {layers}-LAYER PCB")
    print(f"{'=' * 70}")

    # Header
    mfrs = list(rules_by_mfr.keys())
    header = f"{'Constraint':<25}"
    for mfr in mfrs:
        header += f"{mfr.upper():>12}"
    print(header)
    print("-" * 70)

    # Trace width
    row = f"{'Trace width (mil)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_trace_width_mil:>12.1f}"
    print(row)

    # Clearance
    row = f"{'Clearance (mil)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_clearance_mil:>12.1f}"
    print(row)

    # Via drill
    row = f"{'Via drill (mm)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_via_drill_mm:>12.2f}"
    print(row)

    # Via diameter
    row = f"{'Via diameter (mm)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_via_diameter_mm:>12.2f}"
    print(row)

    # Copper to edge
    row = f"{'Copper-to-edge (mm)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_copper_to_edge_mm:>12.2f}"
    print(row)

    print("-" * 70)

    # Assembly support
    row = f"{'Assembly':<25}"
    for mfr in mfrs:
        profile = get_profile(mfr)
        row += f"{'Yes':>12}" if profile.supports_assembly() else f"{'No':>12}"
    print(row)

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    sys.exit(main())

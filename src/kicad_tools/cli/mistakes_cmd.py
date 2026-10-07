"""
CLI command for detecting common PCB design mistakes.

Provides command-line access to the mistake detection module:

    kct detect-mistakes board.kicad_pcb
    kct detect-mistakes board.kicad_pcb --format json
    kct detect-mistakes board.kicad_pcb --category bypass_capacitor
    kct detect-mistakes board.kicad_pcb --severity warning
    kct detect-mistakes board.kicad_pcb --format sarif     # SARIF 2.1.0 for CI
    kct detect-mistakes board.kicad_pcb --waive KEY --waive-reason R --waive-reviewer ME

Usage:
    kicad-tools detect-mistakes <pcb_file>          # Detect all mistakes
    kicad-tools detect-mistakes <pcb_file> --json   # Output as JSON
    kicad-tools detect-mistakes --list-categories   # List check categories

Exit Codes:
    0 - No errors found (warnings may be present)
    1 - Errors found or command failure
    2 - Warnings found (only with --strict)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kicad_tools.explain.mistakes import MistakeCategory

if TYPE_CHECKING:
    from kicad_tools.explain.mistake_waivers import MistakeWaiverResult


def main(argv: list[str] | None = None) -> int:
    """Main entry point for detect-mistakes command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools detect-mistakes",
        description="Detect common PCB design mistakes with educational explanations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Main argument - PCB file
    parser.add_argument(
        "pcb_file",
        nargs="?",
        help="Path to .kicad_pcb file to analyze",
    )

    # Category filter
    parser.add_argument(
        "--category",
        "-c",
        choices=[cat.value for cat in MistakeCategory],
        help="Only check specific category",
    )

    # Severity filter
    parser.add_argument(
        "--severity",
        "-s",
        choices=["error", "warning", "info"],
        help="Only show issues of this severity or higher",
    )

    # Output format
    parser.add_argument(
        "--format",
        "-f",
        choices=["table", "json", "tree", "summary", "sarif"],
        default="table",
        help=(
            "Output format (default: table). 'sarif' writes a SARIF 2.1.0 log "
            "fingerprinted by finding key and evidence hash (Issue #6006)"
        ),
    )

    # Issue #6006: evidence-bound waivers (same keyed v3 entries and sidecar
    # as ``kct check``; see kicad_tools.explain.mistake_waivers).
    parser.add_argument(
        "--waive",
        action="append",
        default=None,
        metavar="KEY",
        help=(
            "Record an evidence-bound waiver for every current finding with this "
            "stable key (the 'key' field in --format json; repeatable), bound to "
            "its current evidence_hash, then report with it applied. Requires "
            "--waive-reason and --waive-reviewer. Written to --waivers PATH, else "
            "the discovered sidecar, else <board>.kct-waivers.json next to the board."
        ),
    )
    parser.add_argument("--waive-reason", dest="waive_reason", default=None)
    parser.add_argument("--waive-reviewer", dest="waive_reviewer", default=None)
    parser.add_argument("--waive-issue", dest="waive_issue", default=None)
    parser.add_argument(
        "--waivers",
        default=None,
        help=(
            "Path to a waivers sidecar (schema version 3). Default: auto-discover "
            "<board>.kct-waivers.json, then .kct_waivers.json, next to the board. "
            "Only 'mistake.*' entries apply here; kct check applies the rest."
        ),
    )

    # Strict mode
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error code on warnings",
    )

    # List categories
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="List available check categories and exit",
    )

    # Verbose output
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed information",
    )

    args = parser.parse_args(argv)

    # Handle list categories mode
    if args.list_categories:
        return _list_categories()

    # Require PCB file for other modes
    if not args.pcb_file:
        parser.print_help(sys.stderr if args.format in ("json", "sarif") else None)
        print("\nError: pcb_file required", file=sys.stderr)
        return 1
    if args.waive and not (
        (args.waive_reason or "").strip() and (args.waive_reviewer or "").strip()
    ):
        parser.error("--waive requires --waive-reason and --waive-reviewer")

    # Load and analyze PCB
    return _analyze_pcb(args)


def _list_categories() -> int:
    """List all available check categories."""
    from kicad_tools.explain.mistakes import get_default_checks

    print("Available Mistake Categories:")
    print("=" * 50)

    # Get checks and group by category
    checks = get_default_checks()
    by_category: dict[MistakeCategory, list] = {}
    for check in checks:
        cat = check.category
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(check)

    for cat in MistakeCategory:
        checks_in_cat = by_category.get(cat, [])
        check_count = len(checks_in_cat)

        # Get description from docstring
        desc = _category_description(cat)

        print(f"\n{cat.value}")
        print(f"  {desc}")
        print(f"  Checks: {check_count}")
        for check in checks_in_cat:
            check_name = type(check).__name__
            print(f"    - {check_name}")

    print()
    print(f"Total categories: {len(MistakeCategory)}")
    print(f"Total checks: {len(checks)}")

    return 0


def _category_description(cat: MistakeCategory) -> str:
    """Get human-readable description for a category."""
    descriptions = {
        MistakeCategory.BYPASS_CAP: "Bypass capacitor placement issues",
        MistakeCategory.CRYSTAL: "Crystal oscillator layout problems",
        MistakeCategory.DIFFERENTIAL_PAIR: "Differential pair routing issues",
        MistakeCategory.POWER_TRACE: "Power trace width problems",
        MistakeCategory.THERMAL: "Thermal management issues",
        MistakeCategory.EMI: "EMI and shielding concerns",
        MistakeCategory.DECOUPLING: "Decoupling capacitor issues",
        MistakeCategory.GROUNDING: "Grounding and return path issues",
        MistakeCategory.VIA: "Via placement problems",
        MistakeCategory.MANUFACTURABILITY: "Manufacturing-related issues",
        MistakeCategory.CONNECTIVITY: "Pull-up / series-resistor connectivity issues",
        MistakeCategory.BOM_HEALTH: "BOM part-number health issues",
    }
    return descriptions.get(cat, "General PCB design issues")


def _analyze_pcb(args) -> int:
    """Load PCB and detect mistakes."""
    from kicad_tools.explain.mistakes import (
        MistakeCategory,
        MistakeDetector,
    )
    from kicad_tools.schema.pcb import PCB

    pcb_path = Path(args.pcb_file)

    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got {pcb_path.suffix}", file=sys.stderr)
        return 1

    # Load PCB
    try:
        print(f"Analyzing: {pcb_path.name}", file=sys.stderr)
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Detect mistakes. Always ask for coverage (issue #4899) so a check
    # that could not run (CheckIncomplete) is reported explicitly instead
    # of silently looking like a clean pass -- mirrors the #4011
    # vacuity-guard discipline used for `kct check`'s `lvs` sub-check.
    detector = MistakeDetector()
    if args.category:
        cat = MistakeCategory(args.category)
        mistakes, coverage = detector.detect_by_category_with_coverage(pcb, cat)
    else:
        mistakes, coverage = detector.detect_with_coverage(pcb)

    # Issue #6006: stable keys + evidence hashes, then evidence-bound waivers.
    waiver_outcome, code = _apply_waivers(args, pcb, pcb_path, mistakes, coverage)
    if code is not None:
        return code

    # Filter by severity if specified
    if args.severity:
        severity_order = {"error": 0, "warning": 1, "info": 2}
        min_severity = severity_order[args.severity]
        mistakes = [m for m in mistakes if severity_order.get(m.severity, 99) <= min_severity]

    advisories = waiver_outcome.advisories
    active = [m for m in mistakes if not m.waived]
    waived = [m for m in mistakes if m.waived]

    # Output results
    if args.format == "json":
        _output_json(mistakes, coverage, advisories)
    elif args.format == "sarif":
        from kicad_tools.explain.mistake_waivers import board_origin

        _output_sarif(mistakes, coverage, advisories, pcb_path, board_origin(pcb))
    elif args.format == "tree":
        _output_tree(active)
        _print_waiver_notes(waived, advisories)
    elif args.format == "summary":
        _output_summary(active)
        _print_waiver_notes(waived, advisories)
    else:
        _output_table(active, args.verbose)
        _print_waiver_notes(waived, advisories)

    incomplete = [c for c in coverage if c.status == "incomplete"]
    if incomplete and args.format not in ("json", "sarif"):
        print("\n" + "-" * 60)
        print(f"INCOMPLETE COVERAGE: {len(incomplete)} check(s) could not run:")
        for c in incomplete:
            print(f"  - {c.check_name} ({c.category.value}): {c.reason}")

    # Determine exit code.  Waived findings never count; a stale waiver
    # leaves its finding active and adds a waiver_stale warning.
    error_count = sum(1 for m in active if m.severity == "error")
    warning_count = sum(1 for m in active if m.severity == "warning") + sum(
        1 for a in advisories if a.severity == "warning"
    )

    if error_count > 0:
        return 1
    elif (warning_count > 0 or incomplete) and args.strict:
        return 2
    return 0


def _output_table(mistakes: list, verbose: bool = False) -> None:
    """Output mistakes as formatted table."""
    if not mistakes:
        print("\n" + "=" * 60)
        print("NO DESIGN MISTAKES DETECTED")
        print("=" * 60)
        print("Your PCB passed all checks!")
        return

    error_count = sum(1 for m in mistakes if m.severity == "error")
    warning_count = sum(1 for m in mistakes if m.severity == "warning")
    info_count = sum(1 for m in mistakes if m.severity == "info")

    print("\n" + "=" * 60)
    print("PCB DESIGN MISTAKE ANALYSIS")
    print("=" * 60)

    print("\nSummary:")
    print(f"  Errors:   {error_count}")
    print(f"  Warnings: {warning_count}")
    print(f"  Info:     {info_count}")

    # Group by category
    by_category: dict = {}
    for m in mistakes:
        cat = m.category.value
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(m)

    print("\n" + "-" * 60)
    print("BY CATEGORY:")
    for cat, cat_mistakes in sorted(by_category.items()):
        print(f"  {cat}: {len(cat_mistakes)} issue(s)")

    # Show detailed mistakes
    errors = [m for m in mistakes if m.severity == "error"]
    warnings = [m for m in mistakes if m.severity == "warning"]
    infos = [m for m in mistakes if m.severity == "info"]

    if errors:
        print("\n" + "-" * 60)
        print("ERRORS (must fix):")
        for m in errors:
            _print_mistake(m, verbose)

    if warnings:
        print("\n" + "-" * 60)
        print("WARNINGS (should review):")
        display_warnings = warnings if verbose else warnings[:10]
        for m in display_warnings:
            _print_mistake(m, verbose)
        if len(warnings) > 10 and not verbose:
            print(f"\n  ... and {len(warnings) - 10} more warnings (use --verbose)")

    if infos and verbose:
        print("\n" + "-" * 60)
        print("INFO (suggestions):")
        for m in infos:
            _print_mistake(m, verbose)

    print("\n" + "=" * 60)
    if errors:
        print("FIX ERRORS BEFORE MANUFACTURING")
    elif warnings:
        print("REVIEW WARNINGS FOR BEST RESULTS")
    else:
        print("DESIGN LOOKS GOOD!")


def _print_mistake(m, verbose: bool = False) -> None:
    """Print a single mistake."""
    symbol = {"error": "X", "warning": "!", "info": "i"}.get(m.severity, "?")
    print(f"\n  [{symbol}] {m.title}")
    print(f"      Components: {', '.join(m.components)}")

    if m.location:
        print(f"      Location: ({m.location[0]:.2f}, {m.location[1]:.2f}) mm")
    if m.stale_waiver_hash:
        print(
            f"      Waiver: STALE (reviewed as {m.stale_waiver_hash}, now {m.evidence_hash});"
            f" key {m.key}"
        )
    elif verbose and m.key:
        print(f"      Key: {m.key}")

    if verbose:
        print(f"      Problem: {m.explanation}")
        print(f"      Fix: {m.fix_suggestion}")
        if m.learn_more_url:
            print(f"      Learn more: {m.learn_more_url}")


def _apply_waivers(
    args: argparse.Namespace, pcb: Any, pcb_path: Path, mistakes: list, coverage: list
) -> tuple[MistakeWaiverResult, int | None]:
    """Annotate evidence, record ``--waive`` entries and apply waivers (Issue #6006).

    Returns ``(outcome, exit_code)``; ``exit_code`` is ``None`` to continue.
    """
    from kicad_tools.explain.mistake_waivers import (
        MistakeWaiverResult,
        annotate_mistakes,
        apply_mistake_waivers,
    )
    from kicad_tools.validate.rules.waivers import (
        discover_waivers_sidecar,
        load_waivers,
        shadowed_waivers_sidecars,
        write_keyed_waivers,
    )

    adapters = annotate_mistakes(mistakes, pcb)

    # Same load / degrade contract as ``kct check --waivers``: an explicit
    # path must exist and parse; a discovered sidecar that fails to parse
    # degrades to a warning.
    waivers = None
    explicit = args.waivers is not None
    path: Path | None = Path(args.waivers).resolve() if explicit else None
    if path is None:
        path = discover_waivers_sidecar(pcb_path)
        if path is not None:
            for shadowed in shadowed_waivers_sidecars(pcb_path, path):
                print(
                    f"WARNING: waivers sidecar {shadowed} is NOT applied: {path} takes precedence.",
                    file=sys.stderr,
                )
    if path is not None:
        if explicit and not path.is_file() and not args.waive:
            # (Issue #6054: with --waive, a missing --waivers PATH is created.)
            print(f"Error: waivers file not found: {path}", file=sys.stderr)
            return MistakeWaiverResult(), 1
        if path.is_file():
            try:
                waivers = load_waivers(path)
            except ValueError as e:
                if explicit:
                    print(f"Error: invalid waivers file {path}: {e}", file=sys.stderr)
                    return MistakeWaiverResult(), 1
                print(f"WARNING: ignoring malformed waivers sidecar {path}: {e}", file=sys.stderr)

    if args.waive:
        wanted = list(dict.fromkeys(args.waive))
        targets = [v for v in adapters if v.key in wanted]
        missing = [k for k in wanted if not any(v.key == k for v in targets)]
        if missing:
            print(
                "Error: --waive names no current finding for key(s): "
                + ", ".join(repr(k) for k in missing)
                + ". Copy the 'key' field from `kct detect-mistakes --format json`.",
                file=sys.stderr,
            )
            return MistakeWaiverResult(), 1
        waive_path = (
            path if path is not None else pcb_path.parent / f"{pcb_path.stem}.kct-waivers.json"
        )
        try:
            written = write_keyed_waivers(
                waive_path,
                targets,
                reason=args.waive_reason,
                reviewer=args.waive_reviewer,
                issue=args.waive_issue,
            )
            waivers = load_waivers(waive_path)
        except (OSError, ValueError) as e:
            print(f"Error: cannot record waiver(s) in {waive_path}: {e}", file=sys.stderr)
            return MistakeWaiverResult(), 1
        print(
            f"[INFO] recorded {written} evidence-bound waiver(s) in {waive_path}",
            file=sys.stderr,
        )

    if waivers is None:
        return MistakeWaiverResult(), None
    # Only the checks that ran: a --category run must not report every other
    # category's waivers as unused.
    ran = {c.rule_id for c in coverage if c.status == "ran" and c.rule_id}
    return apply_mistake_waivers(mistakes, adapters, waivers.for_mistakes(ran)), None


def _summary(mistakes: list, advisories: list) -> dict:
    active = [m for m in mistakes if not m.waived]
    return {
        "errors": sum(1 for m in active if m.severity == "error"),
        "warnings": sum(1 for m in active if m.severity == "warning"),
        "info": sum(1 for m in active if m.severity == "info"),
        # Issue #6006: waived findings are listed but never counted above.
        "waived": sum(1 for m in mistakes if m.waived),
        "stale_waivers": sum(1 for m in mistakes if m.stale_waiver_hash),
        "waiver_advisories": len(advisories),
    }


def _output_json(
    mistakes: list, coverage: list | None = None, advisories: list | None = None
) -> None:
    """Output mistakes (and coverage, issue #4899) as JSON."""
    coverage = coverage or []
    advisories = advisories or []
    data = {
        "summary": _summary(mistakes, advisories),
        "mistakes": [m.to_dict() for m in mistakes],
        # Issue #6006: waiver_stale / waiver_unused advisories (about the
        # waivers sidecar, not the board).
        "waiver_findings": [a.to_dict() for a in advisories],
        "coverage": [c.to_dict() for c in coverage],
        "coverage_complete": all(c.status == "ran" for c in coverage),
    }
    print(json.dumps(data, indent=2))


def _mistake_finding(m, origin: tuple[float, float] = (0.0, 0.0)) -> dict:
    """A mistake in the finding-dict shape the SARIF builder reads.

    The location is converted to sheet (KiCad file) coordinates, the frame
    ``kct check`` findings and their SARIF use.
    """
    from kicad_tools.explain.mistake_waivers import sheet_location, split_components

    items, nets, layer = split_components(m.components, frozenset())
    data: dict[str, Any] = m.to_dict()
    key = m.key or ""
    parts = key.split("|")
    if len(parts) == 4:
        # The key already split components into items / nets / layer.
        items = tuple(p for p in parts[1].split(",") if p)
        nets = tuple(p for p in parts[2].split(",") if p)
        layer = parts[3] or None
    message = m.title
    if m.explanation:
        message = f"{m.title}: {m.explanation.strip()}"
    data.update(
        {
            "rule_id": m.rule_id or f"mistake.{m.category.value}",
            "message": message,
            "items": list(items),
            "nets": list(nets),
            "layer": layer,
            "location": list(loc) if (loc := sheet_location(m, origin)) else None,
        }
    )
    return data


def _output_sarif(
    mistakes: list,
    coverage: list,
    advisories: list,
    pcb_path: Path,
    origin: tuple[float, float] = (0.0, 0.0),
) -> None:
    """Output mistakes as a SARIF 2.1.0 log (Issue #6006)."""
    from kicad_tools.validate.sarif import sarif_log

    findings = [_mistake_finding(m, origin) for m in mistakes] + [a.to_dict() for a in advisories]
    descriptions: dict[str, str] = {}
    for m in mistakes:
        descriptions.setdefault(m.rule_id or f"mistake.{m.category.value}", m.title)
    summary = _summary(mistakes, advisories)
    summary["coverage_complete"] = all(c.status == "ran" for c in coverage)
    summary["incomplete_checks"] = [c.check_name for c in coverage if c.status == "incomplete"]
    log = sarif_log(
        findings,
        tool_name="kct detect-mistakes",
        artifact=pcb_path,
        rule_descriptions=descriptions,
        run_properties={"summary": summary},
    )
    print(json.dumps(log, indent=2))


def _print_waiver_notes(waived: list, advisories: list) -> None:
    """Human view of waived findings and waiver advisories (Issue #6006)."""
    if waived:
        print("\n" + "-" * 60)
        print(f"WAIVED ({len(waived)}, non-blocking):")
        for m in waived:
            print(f"  [W] {m.title} ({', '.join(m.components)})")
            if m.waiver_reason:
                print(f"      Waiver reason: {m.waiver_reason}")
            if m.waiver_issue:
                print(f"      Waiver issue: {m.waiver_issue}")
    if advisories:
        print("\n" + "-" * 60)
        print("WAIVER ADVISORIES:")
        for a in advisories:
            print(f"  [{a.severity}] {a.rule_id}: {a.message}")


def _output_tree(mistakes: list) -> None:
    """Output mistakes in tree format."""
    if not mistakes:
        print("No design mistakes detected.")
        return

    for m in mistakes:
        print(m.format_tree())
        print()


def _output_summary(mistakes: list) -> None:
    """Output summary only."""
    error_count = sum(1 for m in mistakes if m.severity == "error")
    warning_count = sum(1 for m in mistakes if m.severity == "warning")
    info_count = sum(1 for m in mistakes if m.severity == "info")

    print(f"Errors: {error_count}, Warnings: {warning_count}, Info: {info_count}")

    if not mistakes:
        print("No design mistakes detected!")
    else:
        # Group by category
        by_category: dict = {}
        for m in mistakes:
            cat = m.category.value
            by_category[cat] = by_category.get(cat, 0) + 1

        for cat, count in sorted(by_category.items()):
            print(f"  {cat}: {count}")


if __name__ == "__main__":
    sys.exit(main())

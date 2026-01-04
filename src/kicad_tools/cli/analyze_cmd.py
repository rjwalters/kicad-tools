"""Analyze command for PCB analysis tools.

Provides analysis subcommands:
- `analyze congestion`: Routing congestion analysis
- `analyze trace-lengths`: Trace length analysis for timing-critical nets

Usage:
    kicad-tools analyze congestion board.kicad_pcb
    kicad-tools analyze congestion board.kicad_pcb --format json
    kicad-tools analyze trace-lengths board.kicad_pcb
    kicad-tools analyze trace-lengths board.kicad_pcb --net CLK
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from kicad_tools.analysis import CongestionAnalyzer, Severity, TraceLengthAnalyzer
from kicad_tools.schema.pcb import PCB


def main(argv: list[str] | None = None) -> int:
    """Main entry point for analyze command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools analyze",
        description="PCB analysis tools",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Analysis type")

    # Congestion subcommand
    congestion_parser = subparsers.add_parser(
        "congestion",
        help="Analyze routing congestion",
        description="Identify congested areas and suggest solutions",
    )
    congestion_parser.add_argument(
        "pcb",
        help="PCB file to analyze (.kicad_pcb)",
    )
    congestion_parser.add_argument(
        "--format",
        "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    congestion_parser.add_argument(
        "--grid-size",
        type=float,
        default=2.0,
        help="Grid cell size in mm (default: 2.0)",
    )
    congestion_parser.add_argument(
        "--min-severity",
        choices=["low", "medium", "high", "critical"],
        default="low",
        help="Minimum severity to report (default: low)",
    )
    congestion_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress informational output",
    )

    # Trace-lengths subcommand
    trace_parser = subparsers.add_parser(
        "trace-lengths",
        help="Analyze trace lengths for timing-critical nets",
        description="Calculate trace lengths, identify differential pairs, and check skew",
    )
    trace_parser.add_argument(
        "pcb",
        help="PCB file to analyze (.kicad_pcb)",
    )
    trace_parser.add_argument(
        "--format",
        "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    trace_parser.add_argument(
        "--net",
        "-n",
        action="append",
        dest="nets",
        help="Specific net(s) to analyze (can be used multiple times)",
    )
    trace_parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="Analyze all nets, not just timing-critical ones",
    )
    trace_parser.add_argument(
        "--diff-pairs",
        "-d",
        action="store_true",
        default=True,
        help="Include differential pair analysis (default: True)",
    )
    trace_parser.add_argument(
        "--no-diff-pairs",
        action="store_false",
        dest="diff_pairs",
        help="Disable differential pair analysis",
    )
    trace_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress informational output",
    )

    if argv is None:
        argv = sys.argv[1:]

    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help()
        return 1

    if args.subcommand == "congestion":
        return _run_congestion_analysis(args)

    if args.subcommand == "trace-lengths":
        return _run_trace_lengths_analysis(args)

    return 0


def _run_congestion_analysis(args: argparse.Namespace) -> int:
    """Run congestion analysis on a PCB."""
    pcb_path = Path(args.pcb)

    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {pcb_path.suffix}", file=sys.stderr)
        return 1

    # Load PCB
    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Run analysis
    analyzer = CongestionAnalyzer(grid_size=args.grid_size)
    reports = analyzer.analyze(pcb)

    # Filter by minimum severity
    severity_order = {
        "low": 0,
        "medium": 1,
        "high": 2,
        "critical": 3,
    }
    min_severity = severity_order[args.min_severity]
    reports = [r for r in reports if severity_order[r.severity.value] >= min_severity]

    # Output results
    if args.format == "json":
        _output_json(reports)
    else:
        _output_text(reports, pcb_path.name, quiet=args.quiet)

    # Return non-zero if critical issues found
    has_critical = any(r.severity == Severity.CRITICAL for r in reports)
    has_high = any(r.severity == Severity.HIGH for r in reports)

    if has_critical:
        return 2
    elif has_high:
        return 1
    return 0


def _output_json(reports: list) -> None:
    """Output reports as JSON."""
    output = {
        "reports": [r.to_dict() for r in reports],
        "summary": {
            "total": len(reports),
            "critical": sum(1 for r in reports if r.severity == Severity.CRITICAL),
            "high": sum(1 for r in reports if r.severity == Severity.HIGH),
            "medium": sum(1 for r in reports if r.severity == Severity.MEDIUM),
            "low": sum(1 for r in reports if r.severity == Severity.LOW),
        },
    }
    print(json.dumps(output, indent=2))


def _output_text(reports: list, filename: str, quiet: bool = False) -> None:
    """Output reports as formatted text."""
    console = Console()

    if not reports:
        if not quiet:
            console.print(f"[green]No congestion issues found in {filename}[/green]")
        return

    if not quiet:
        console.print(f"\n[bold]Congestion Analysis: {filename}[/bold]\n")

    # Summary table
    summary_table = Table(title="Summary", show_header=False)
    summary_table.add_column("Metric", style="dim")
    summary_table.add_column("Value")

    critical = sum(1 for r in reports if r.severity == Severity.CRITICAL)
    high = sum(1 for r in reports if r.severity == Severity.HIGH)
    medium = sum(1 for r in reports if r.severity == Severity.MEDIUM)
    low = sum(1 for r in reports if r.severity == Severity.LOW)

    summary_table.add_row("Total areas", str(len(reports)))
    if critical > 0:
        summary_table.add_row("Critical", f"[red]{critical}[/red]")
    if high > 0:
        summary_table.add_row("High", f"[yellow]{high}[/yellow]")
    if medium > 0:
        summary_table.add_row("Medium", f"[blue]{medium}[/blue]")
    if low > 0:
        summary_table.add_row("Low", str(low))

    console.print(summary_table)
    console.print()

    # Detail for each report
    for i, report in enumerate(reports, 1):
        _print_report(console, report, i)


def _print_report(console: Console, report, index: int) -> None:
    """Print a single congestion report."""
    # Severity color
    severity_colors = {
        Severity.CRITICAL: "red",
        Severity.HIGH: "yellow",
        Severity.MEDIUM: "blue",
        Severity.LOW: "dim",
    }
    color = severity_colors[report.severity]

    # Header
    x, y = report.center
    console.print(
        f"[{color}][bold]{report.severity.value.upper()}[/bold][/{color}]: "
        f"Area around ({x:.1f}, {y:.1f})"
    )

    # Metrics
    console.print(f"  Track density: {report.track_density:.2f} mm/mm²")
    console.print(f"  Vias: {report.via_count}")
    if report.unrouted_connections > 0:
        console.print(f"  Unrouted: {report.unrouted_connections} connection(s)")

    # Components
    if report.components:
        comp_str = ", ".join(report.components[:5])
        if len(report.components) > 5:
            comp_str += f" (+{len(report.components) - 5} more)"
        console.print(f"  Components: {comp_str}")

    # Nets
    if report.nets:
        net_str = ", ".join(report.nets[:5])
        if len(report.nets) > 5:
            net_str += f" (+{len(report.nets) - 5} more)"
        console.print(f"  Nets: {net_str}")

    # Suggestions
    if report.suggestions:
        console.print("  [dim]Suggestions:[/dim]")
        for suggestion in report.suggestions:
            console.print(f"    • {suggestion}")

    console.print()


# ============================================================================
# Trace Length Analysis
# ============================================================================


def _run_trace_lengths_analysis(args: argparse.Namespace) -> int:
    """Run trace length analysis on a PCB."""
    pcb_path = Path(args.pcb)

    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {pcb_path.suffix}", file=sys.stderr)
        return 1

    # Load PCB
    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Run analysis
    analyzer = TraceLengthAnalyzer()

    if args.nets:
        # Analyze specific nets
        reports = [analyzer.analyze_net(pcb, net) for net in args.nets]
    elif args.all:
        # Analyze all nets
        reports = []
        for net in pcb.nets.values():
            if net.name:  # Skip unnamed nets
                report = analyzer.analyze_net(pcb, net.name)
                if report.total_length_mm > 0:  # Skip unrouted nets
                    reports.append(report)
        reports.sort(key=lambda r: r.net_name)
    else:
        # Analyze timing-critical nets (default)
        reports = analyzer.analyze_all_critical(pcb, include_diff_pairs=args.diff_pairs)

    # Find differential pairs for summary
    diff_pairs = []
    if args.diff_pairs and not args.nets:
        diff_pairs = analyzer.find_differential_pairs(pcb)

    # Output results
    if args.format == "json":
        _output_trace_lengths_json(reports, diff_pairs)
    else:
        _output_trace_lengths_text(reports, diff_pairs, pcb_path.name, quiet=args.quiet)

    return 0


def _output_trace_lengths_json(reports: list, diff_pairs: list[tuple[str, str]]) -> None:
    """Output trace length reports as JSON."""
    output = {
        "nets": [r.to_dict() for r in reports],
        "differential_pairs": [{"positive": p, "negative": n} for p, n in diff_pairs],
        "summary": {
            "total_nets": len(reports),
            "differential_pairs": len(diff_pairs),
            "total_length_mm": round(sum(r.total_length_mm for r in reports), 3),
        },
    }
    print(json.dumps(output, indent=2))


def _output_trace_lengths_text(
    reports: list,
    diff_pairs: list[tuple[str, str]],
    filename: str,
    quiet: bool = False,
) -> None:
    """Output trace length reports as formatted text."""
    console = Console()

    if not reports:
        if not quiet:
            console.print(f"[dim]No timing-critical nets found in {filename}[/dim]")
        return

    if not quiet:
        console.print(f"\n[bold]Trace Length Report: {filename}[/bold]\n")

    # Main trace length table
    table = Table(title="Trace Lengths")
    table.add_column("Net", style="cyan")
    table.add_column("Length", justify="right")
    table.add_column("Vias", justify="right")
    table.add_column("Layers", style="dim")
    table.add_column("Target", justify="right", style="dim")
    table.add_column("Delta", justify="right")

    for report in reports:
        # Format length
        length_str = f"{report.total_length_mm:.2f}mm"

        # Format layers
        layers_str = ", ".join(sorted(report.layers_used)) if report.layers_used else "-"

        # Format target and delta
        target_str = "-"
        delta_str = "-"

        if report.target_length_mm is not None:
            target_str = f"{report.target_length_mm:.2f}mm"
            delta = report.length_delta_mm or 0
            if report.within_tolerance:
                delta_str = f"[green]{delta:+.2f}mm ✓[/green]"
            else:
                delta_str = f"[red]{delta:+.2f}mm ✗[/red]"

        table.add_row(
            report.net_name,
            length_str,
            str(report.via_count),
            layers_str,
            target_str,
            delta_str,
        )

    console.print(table)

    # Differential pairs table
    pair_reports = [r for r in reports if r.pair_net]
    if pair_reports:
        console.print()

        # Group pairs (only show each pair once)
        shown_pairs: set[tuple[str, str]] = set()
        pair_table = Table(title="Differential Pairs")
        pair_table.add_column("Pair", style="cyan")
        pair_table.add_column("P Length", justify="right")
        pair_table.add_column("N Length", justify="right")
        pair_table.add_column("Skew", justify="right")
        pair_table.add_column("Status")

        for report in pair_reports:
            pair_key = tuple(sorted([report.net_name, report.pair_net or ""]))
            if pair_key in shown_pairs:
                continue
            shown_pairs.add(pair_key)

            # Find the partner report
            partner = next((r for r in reports if r.net_name == report.pair_net), None)
            if not partner:
                continue

            # Determine which is P and which is N
            if report.net_name < (report.pair_net or ""):
                p_report, n_report = report, partner
            else:
                p_report, n_report = partner, report

            pair_name = f"{p_report.net_name} / {n_report.net_name}"
            skew = report.skew_mm or 0

            # Default skew tolerance of 2mm for USB (typical)
            if skew < 2.0:
                skew_str = f"[green]{skew:.2f}mm ✓[/green]"
            elif skew < 5.0:
                skew_str = f"[yellow]{skew:.2f}mm[/yellow]"
            else:
                skew_str = f"[red]{skew:.2f}mm ✗[/red]"

            pair_table.add_row(
                pair_name,
                f"{p_report.total_length_mm:.2f}mm",
                f"{n_report.total_length_mm:.2f}mm",
                skew_str,
                "Matched" if skew < 2.0 else "Check skew",
            )

        console.print(pair_table)

    # Summary
    console.print()
    total_length = sum(r.total_length_mm for r in reports)
    console.print(f"[dim]Total: {len(reports)} nets, {total_length:.2f}mm total trace length[/dim]")


if __name__ == "__main__":
    sys.exit(main())

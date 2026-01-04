"""Analyze command for PCB analysis tools.

Provides the `analyze congestion` subcommand for routing congestion analysis.

Usage:
    kicad-tools analyze congestion board.kicad_pcb
    kicad-tools analyze congestion board.kicad_pcb --format json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from kicad_tools.analysis import CongestionAnalyzer, Severity
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

    if argv is None:
        argv = sys.argv[1:]

    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help()
        return 1

    if args.subcommand == "congestion":
        return _run_congestion_analysis(args)

    return 0


def _run_congestion_analysis(args: argparse.Namespace) -> int:
    """Run congestion analysis on a PCB."""
    pcb_path = Path(args.pcb)

    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if not pcb_path.suffix == ".kicad_pcb":
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
    reports = [
        r for r in reports
        if severity_order[r.severity.value] >= min_severity
    ]

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


if __name__ == "__main__":
    sys.exit(main())

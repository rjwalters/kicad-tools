"""Analyze command for PCB analysis tools.

Provides analysis subcommands:
- `analyze congestion`: Routing congestion analysis
- `analyze trace-lengths`: Trace length analysis for timing-critical nets
- `analyze signal-integrity`: Signal integrity analysis (crosstalk and impedance)
- `analyze thermal`: Thermal analysis and hotspot detection

Usage:
    kicad-tools analyze congestion board.kicad_pcb
    kicad-tools analyze congestion board.kicad_pcb --format json
    kicad-tools analyze trace-lengths board.kicad_pcb
    kicad-tools analyze trace-lengths board.kicad_pcb --net CLK
    kicad-tools analyze signal-integrity board.kicad_pcb
    kicad-tools analyze signal-integrity board.kicad_pcb --format json
    kicad-tools analyze thermal board.kicad_pcb
    kicad-tools analyze thermal board.kicad_pcb --format json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from kicad_tools.analysis import (
    CongestionAnalyzer,
    RiskLevel,
    Severity,
    SignalIntegrityAnalyzer,
    ThermalAnalyzer,
    ThermalSeverity,
    TraceLengthAnalyzer,
)
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

    # Signal-integrity subcommand
    si_parser = subparsers.add_parser(
        "signal-integrity",
        help="Analyze signal integrity (crosstalk and impedance)",
        description="Identify crosstalk risks and impedance discontinuities",
    )
    si_parser.add_argument(
        "pcb",
        help="PCB file to analyze (.kicad_pcb)",
    )
    si_parser.add_argument(
        "--format",
        "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    si_parser.add_argument(
        "--min-risk",
        choices=["low", "medium", "high"],
        default="medium",
        help="Minimum risk level to report for crosstalk (default: medium)",
    )
    si_parser.add_argument(
        "--crosstalk-only",
        action="store_true",
        help="Only analyze crosstalk, skip impedance analysis",
    )
    si_parser.add_argument(
        "--impedance-only",
        action="store_true",
        help="Only analyze impedance, skip crosstalk analysis",
    )
    si_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress informational output",
    )

    # Thermal subcommand
    thermal_parser = subparsers.add_parser(
        "thermal",
        help="Analyze thermal characteristics and hotspots",
        description="Identify heat sources, estimate power dissipation, and suggest thermal improvements",
    )
    thermal_parser.add_argument(
        "pcb",
        help="PCB file to analyze (.kicad_pcb)",
    )
    thermal_parser.add_argument(
        "--format",
        "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    thermal_parser.add_argument(
        "--cluster-radius",
        type=float,
        default=10.0,
        help="Radius for clustering heat sources in mm (default: 10.0)",
    )
    thermal_parser.add_argument(
        "--min-power",
        type=float,
        default=0.05,
        help="Minimum power threshold in Watts (default: 0.05)",
    )
    thermal_parser.add_argument(
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

    if args.subcommand == "signal-integrity":
        return _run_signal_integrity_analysis(args)

    if args.subcommand == "thermal":
        return _run_thermal_analysis(args)

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


# ============================================================================
# Signal Integrity Analysis
# ============================================================================


def _run_signal_integrity_analysis(args: argparse.Namespace) -> int:
    """Run signal integrity analysis on a PCB."""
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
    analyzer = SignalIntegrityAnalyzer()

    crosstalk_risks = []
    impedance_discs = []

    if not args.impedance_only:
        crosstalk_risks = analyzer.analyze_crosstalk(pcb)

        # Filter by minimum risk level
        risk_order = {"low": 0, "medium": 1, "high": 2}
        min_risk = risk_order[args.min_risk]
        crosstalk_risks = [r for r in crosstalk_risks if risk_order[r.risk_level.value] >= min_risk]

    if not args.crosstalk_only:
        impedance_discs = analyzer.analyze_impedance(pcb)

    # Output results
    if args.format == "json":
        _output_signal_integrity_json(crosstalk_risks, impedance_discs)
    else:
        _output_signal_integrity_text(
            crosstalk_risks, impedance_discs, pcb_path.name, quiet=args.quiet
        )

    # Return non-zero if high-risk issues found
    has_high_crosstalk = any(r.risk_level == RiskLevel.HIGH for r in crosstalk_risks)
    has_severe_mismatch = any(d.mismatch_percent >= 25 for d in impedance_discs)

    if has_high_crosstalk or has_severe_mismatch:
        return 1
    return 0


def _output_signal_integrity_json(crosstalk_risks: list, impedance_discs: list) -> None:
    """Output signal integrity reports as JSON."""
    output = {
        "crosstalk_risks": [r.to_dict() for r in crosstalk_risks],
        "impedance_discontinuities": [d.to_dict() for d in impedance_discs],
        "summary": {
            "crosstalk": {
                "total": len(crosstalk_risks),
                "high": sum(1 for r in crosstalk_risks if r.risk_level == RiskLevel.HIGH),
                "medium": sum(1 for r in crosstalk_risks if r.risk_level == RiskLevel.MEDIUM),
                "low": sum(1 for r in crosstalk_risks if r.risk_level == RiskLevel.LOW),
            },
            "impedance": {
                "total": len(impedance_discs),
                "width_changes": sum(1 for d in impedance_discs if d.cause == "width_change"),
                "vias": sum(1 for d in impedance_discs if d.cause == "via"),
            },
        },
    }
    print(json.dumps(output, indent=2))


def _output_signal_integrity_text(
    crosstalk_risks: list,
    impedance_discs: list,
    filename: str,
    quiet: bool = False,
) -> None:
    """Output signal integrity reports as formatted text."""
    console = Console()

    if not crosstalk_risks and not impedance_discs:
        if not quiet:
            console.print(f"[green]No signal integrity issues found in {filename}[/green]")
        return

    if not quiet:
        console.print(f"\n[bold]Signal Integrity Analysis: {filename}[/bold]\n")

    # Crosstalk risks table
    if crosstalk_risks:
        _print_crosstalk_section(console, crosstalk_risks)

    # Impedance discontinuities table
    if impedance_discs:
        if crosstalk_risks:
            console.print()  # Spacing between sections
        _print_impedance_section(console, impedance_discs)

    # Summary
    console.print()
    console.print("[dim]Summary:[/dim]")
    if crosstalk_risks:
        high = sum(1 for r in crosstalk_risks if r.risk_level == RiskLevel.HIGH)
        medium = sum(1 for r in crosstalk_risks if r.risk_level == RiskLevel.MEDIUM)
        console.print(f"  Crosstalk: {len(crosstalk_risks)} risks ({high} high, {medium} medium)")
    if impedance_discs:
        severe = sum(1 for d in impedance_discs if d.mismatch_percent >= 25)
        console.print(f"  Impedance: {len(impedance_discs)} discontinuities ({severe} severe)")


def _print_crosstalk_section(console: Console, risks: list) -> None:
    """Print crosstalk risks section."""
    console.print("[bold]Crosstalk Risks:[/bold]\n")

    for risk in risks:
        # Color based on risk level
        colors = {
            RiskLevel.HIGH: "red",
            RiskLevel.MEDIUM: "yellow",
            RiskLevel.LOW: "dim",
        }
        color = colors[risk.risk_level]

        console.print(
            f"  [{color}][bold]{risk.risk_level.value.upper()}[/bold][/{color}]: "
            f"{risk.aggressor_net} ↔ {risk.victim_net}"
        )
        console.print(
            f"    Parallel: {risk.parallel_length_mm:.1f}mm at "
            f"{risk.spacing_mm:.2f}mm spacing on {risk.layer}"
        )
        console.print(f"    Coupling: {risk.coupling_coefficient:.2f}")
        if risk.suggestion:
            console.print(f"    → {risk.suggestion}")
        console.print()


def _print_impedance_section(console: Console, discontinuities: list) -> None:
    """Print impedance discontinuities section."""
    console.print("[bold]Impedance Discontinuities:[/bold]\n")

    for disc in discontinuities:
        # Color based on severity
        if disc.mismatch_percent >= 25:
            color = "red"
            severity = "SEVERE"
        elif disc.mismatch_percent >= 15:
            color = "yellow"
            severity = "WARNING"
        else:
            color = "dim"
            severity = "MINOR"

        x, y = disc.position
        console.print(
            f"  [{color}][bold]{severity}[/bold][/{color}]: {disc.net} at ({x:.1f}, {y:.1f})"
        )
        console.print(
            f"    {disc.impedance_before:.0f}Ω → {disc.impedance_after:.0f}Ω "
            f"({disc.mismatch_percent:+.0f}% mismatch)"
        )
        console.print(f"    Cause: {disc.cause.replace('_', ' ')}")
        console.print(f"    → {disc.suggestion}")
        console.print()


# ============================================================================
# Thermal Analysis
# ============================================================================


def _run_thermal_analysis(args: argparse.Namespace) -> int:
    """Run thermal analysis on a PCB."""
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
    analyzer = ThermalAnalyzer(
        cluster_radius=args.cluster_radius,
        min_power_w=args.min_power,
    )
    hotspots = analyzer.analyze(pcb)

    # Output results
    if args.format == "json":
        _output_thermal_json(hotspots)
    else:
        _output_thermal_text(hotspots, pcb_path.name, quiet=args.quiet)

    # Return non-zero if critical/hot issues found
    has_critical = any(h.severity == ThermalSeverity.CRITICAL for h in hotspots)
    has_hot = any(h.severity == ThermalSeverity.HOT for h in hotspots)

    if has_critical:
        return 2
    elif has_hot:
        return 1
    return 0


def _output_thermal_json(hotspots: list) -> None:
    """Output thermal hotspots as JSON."""
    output = {
        "hotspots": [h.to_dict() for h in hotspots],
        "summary": {
            "total": len(hotspots),
            "critical": sum(1 for h in hotspots if h.severity == ThermalSeverity.CRITICAL),
            "hot": sum(1 for h in hotspots if h.severity == ThermalSeverity.HOT),
            "warm": sum(1 for h in hotspots if h.severity == ThermalSeverity.WARM),
            "ok": sum(1 for h in hotspots if h.severity == ThermalSeverity.OK),
            "total_power_w": round(sum(h.total_power_w for h in hotspots), 3),
        },
    }
    print(json.dumps(output, indent=2))


def _output_thermal_text(hotspots: list, filename: str, quiet: bool = False) -> None:
    """Output thermal hotspots as formatted text."""
    console = Console()

    if not hotspots:
        if not quiet:
            console.print(f"[green]No thermal concerns found in {filename}[/green]")
        return

    if not quiet:
        console.print(f"\n[bold]Thermal Analysis: {filename}[/bold]\n")

    # Summary table
    summary_table = Table(title="Summary", show_header=False)
    summary_table.add_column("Metric", style="dim")
    summary_table.add_column("Value")

    critical = sum(1 for h in hotspots if h.severity == ThermalSeverity.CRITICAL)
    hot = sum(1 for h in hotspots if h.severity == ThermalSeverity.HOT)
    warm = sum(1 for h in hotspots if h.severity == ThermalSeverity.WARM)
    ok = sum(1 for h in hotspots if h.severity == ThermalSeverity.OK)
    total_power = sum(h.total_power_w for h in hotspots)

    summary_table.add_row("Total hotspots", str(len(hotspots)))
    summary_table.add_row("Total power", f"{total_power:.2f}W")
    if critical > 0:
        summary_table.add_row("Critical", f"[red]{critical}[/red]")
    if hot > 0:
        summary_table.add_row("Hot", f"[yellow]{hot}[/yellow]")
    if warm > 0:
        summary_table.add_row("Warm", f"[blue]{warm}[/blue]")
    if ok > 0:
        summary_table.add_row("OK", str(ok))

    console.print(summary_table)
    console.print()

    # Detail for each hotspot
    for i, hotspot in enumerate(hotspots, 1):
        _print_thermal_hotspot(console, hotspot, i)


def _print_thermal_hotspot(console: Console, hotspot, index: int) -> None:
    """Print a single thermal hotspot."""
    # Severity color
    severity_colors = {
        ThermalSeverity.CRITICAL: "red",
        ThermalSeverity.HOT: "yellow",
        ThermalSeverity.WARM: "blue",
        ThermalSeverity.OK: "green",
    }
    color = severity_colors[hotspot.severity]

    # Header
    x, y = hotspot.position
    console.print(
        f"[{color}][bold]{hotspot.severity.value.upper()}[/bold][/{color}]: "
        f"Hotspot at ({x:.1f}, {y:.1f})"
    )

    # Power and temperature
    console.print(f"  Total power: {hotspot.total_power_w:.2f}W")
    console.print(f"  Est. temp rise: +{hotspot.max_temp_rise_c:.0f}°C")

    # Heat sources
    if hotspot.sources:
        sources_str = ", ".join(f"{s.reference} ({s.power_w:.2f}W)" for s in hotspot.sources[:3])
        if len(hotspot.sources) > 3:
            sources_str += f" (+{len(hotspot.sources) - 3} more)"
        console.print(f"  Sources: {sources_str}")

    # Thermal relief
    console.print(f"  Copper area: {hotspot.copper_area_mm2:.0f}mm²")
    console.print(f"  Vias: {hotspot.via_count} (thermal: {hotspot.thermal_vias})")

    # Suggestions
    if hotspot.suggestions:
        console.print("  [dim]Suggestions:[/dim]")
        for suggestion in hotspot.suggestions:
            console.print(f"    • {suggestion}")

    console.print()


if __name__ == "__main__":
    sys.exit(main())

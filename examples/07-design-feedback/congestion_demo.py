#!/usr/bin/env python3
"""
Routing Congestion Analysis Demo

Demonstrates how kicad-tools v0.7.0 identifies routing congestion hotspots
and provides actionable suggestions for improving routability.
"""

from pathlib import Path

from kicad_tools.analysis.congestion import (
    CongestionAnalyzer,
    Severity,
    analyze_congestion,
)
from kicad_tools.schema.pcb import PCB


def main():
    """Run the congestion analysis demo."""
    # Load the example PCB
    pcb_path = Path(__file__).parent / "fixtures" / "dense_board.kicad_pcb"

    if not pcb_path.exists():
        print("Note: No fixture file found. Using synthetic example.")
        print("Copy a KiCad PCB file to fixtures/dense_board.kicad_pcb to analyze.")
        print()
        show_synthetic_example()
        return

    print(f"Loading PCB: {pcb_path.name}")
    pcb = PCB.load(str(pcb_path))

    # Analyze congestion with 2mm grid
    print("Analyzing routing congestion...")
    print()

    result = analyze_congestion(pcb, grid_size=2.0)

    # Display results
    print("=" * 60)
    print("Congestion Analysis Results")
    print("=" * 60)
    print()

    print(f"Grid size: {result.grid_size}mm")
    print(f"Total cells analyzed: {result.total_cells}")
    print(f"Congested cells: {result.congested_cells}")
    print()

    if not result.hotspots:
        print("No congestion hotspots detected. Design looks good!")
        return

    # Group by severity
    by_severity: dict[Severity, list] = {}
    for hotspot in result.hotspots:
        if hotspot.severity not in by_severity:
            by_severity[hotspot.severity] = []
        by_severity[hotspot.severity].append(hotspot)

    # Display hotspots by severity (highest first)
    for severity in [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]:
        if severity not in by_severity:
            continue

        hotspots = by_severity[severity]
        icon = {"CRITICAL": "!!!", "HIGH": "!!", "MEDIUM": "!", "LOW": "."}.get(
            severity.name, "?"
        )

        print(f"{icon} {severity.name} Severity ({len(hotspots)} areas)")
        print("-" * 40)

        for hotspot in hotspots[:3]:  # Show top 3 per category
            print(f"  Location: ({hotspot.x:.1f}, {hotspot.y:.1f})mm")
            print(f"  Density: {hotspot.track_density:.1f} mm/mm²")
            print(f"  Via count: {hotspot.via_count}")
            print(f"  Suggestion: {hotspot.suggestion}")
            print()

        if len(hotspots) > 3:
            print(f"  ... and {len(hotspots) - 3} more")
            print()

    # Summary
    print("=" * 60)
    print("Recommendations")
    print("=" * 60)
    print()

    if Severity.CRITICAL in by_severity:
        print("CRITICAL areas require immediate attention:")
        print("  - Consider moving components to reduce density")
        print("  - Use inner layers for routing if available")
        print("  - Increase board size if possible")
        print()

    if Severity.HIGH in by_severity:
        print("HIGH severity areas may cause routing failures:")
        print("  - Review component placement around hotspots")
        print("  - Consider via-in-pad or micro-vias")
        print("  - Route critical nets first in these areas")
        print()

    print("Run with different grid sizes to get more/less detail:")
    print("  kct analyze congestion board.kicad_pcb --grid-size 1.0  # Fine")
    print("  kct analyze congestion board.kicad_pcb --grid-size 5.0  # Coarse")


def show_synthetic_example():
    """Show example output without a real PCB file."""
    print("=" * 60)
    print("Example Congestion Analysis Output")
    print("=" * 60)
    print()
    print("Grid size: 2.0mm")
    print("Total cells analyzed: 450")
    print("Congested cells: 12")
    print()
    print("!!! CRITICAL Severity (2 areas)")
    print("-" * 40)
    print("  Location: (45.0, 32.0)mm")
    print("  Density: 8.5 mm/mm²")
    print("  Via count: 14")
    print("  Suggestion: Move U1 bypass caps 2mm outward to reduce density")
    print()
    print("  Location: (52.0, 34.0)mm")
    print("  Density: 7.2 mm/mm²")
    print("  Via count: 11")
    print("  Suggestion: Route USB signals on inner layer to free surface")
    print()
    print("!! HIGH Severity (3 areas)")
    print("-" * 40)
    print("  Location: (30.0, 50.0)mm")
    print("  Density: 5.1 mm/mm²")
    print("  Via count: 8")
    print("  Suggestion: Consider via-in-pad for BGA fanout")
    print()


if __name__ == "__main__":
    main()

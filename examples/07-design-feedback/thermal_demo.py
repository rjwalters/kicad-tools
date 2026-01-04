#!/usr/bin/env python3
"""
Thermal Analysis Demo

Demonstrates how kicad-tools v0.7.0 identifies thermal issues and provides
guidance on heat management for PCB designs.
"""

from pathlib import Path

from kicad_tools.analysis.thermal import (
    ThermalAnalyzer,
    analyze_thermal,
)
from kicad_tools.schema.pcb import PCB


def main():
    """Run the thermal analysis demo."""
    # Load the example PCB
    pcb_path = Path(__file__).parent / "fixtures" / "power_board.kicad_pcb"

    if not pcb_path.exists():
        print("Note: No fixture file found. Using synthetic example.")
        print("Copy a KiCad PCB file to fixtures/power_board.kicad_pcb to analyze.")
        print()
        show_synthetic_example()
        return

    print(f"Loading PCB: {pcb_path.name}")
    pcb = PCB.load(str(pcb_path))

    # Analyze thermal characteristics
    print("Analyzing thermal characteristics...")
    print()

    result = analyze_thermal(pcb)

    # Display results
    print("=" * 60)
    print("Thermal Analysis Results")
    print("=" * 60)
    print()

    if not result.heat_sources:
        print("No significant heat sources detected.")
        return

    print(f"Heat sources found: {len(result.heat_sources)}")
    print(f"Total estimated power: {result.total_power_mw:.0f} mW")
    print()

    # Display heat sources
    print("Heat Sources")
    print("-" * 40)

    for source in sorted(result.heat_sources, key=lambda x: -x.power_mw):
        temp_indicator = ""
        if source.estimated_temp_rise > 50:
            temp_indicator = " [CRITICAL]"
        elif source.estimated_temp_rise > 30:
            temp_indicator = " [WARNING]"

        print(f"  {source.reference}: {source.component_type}")
        print(f"    Power: {source.power_mw:.0f} mW")
        print(f"    Package: {source.package}")
        print(f"    Thermal resistance: {source.thermal_resistance:.1f} °C/W")
        print(f"    Est. temp rise: {source.estimated_temp_rise:.1f}°C{temp_indicator}")
        print()

    # Heat source clusters
    if result.clusters:
        print("Heat Source Clusters (nearby sources)")
        print("-" * 40)

        for cluster in result.clusters:
            print(f"  Cluster at ({cluster.center_x:.1f}, {cluster.center_y:.1f})mm")
            print(f"    Components: {', '.join(cluster.members)}")
            print(f"    Combined power: {cluster.total_power_mw:.0f} mW")
            print(f"    Recommendation: {cluster.recommendation}")
            print()

    # Thermal relief analysis
    if result.thermal_vias:
        print("Thermal Via Analysis")
        print("-" * 40)
        print(f"  Thermal vias detected: {len(result.thermal_vias)}")
        print(f"  Estimated effectiveness: {result.via_effectiveness:.0f}%")
        print()

    # Copper area analysis
    if result.copper_areas:
        print("Copper Pour Analysis")
        print("-" * 40)
        for area in result.copper_areas:
            print(f"  {area.net}: {area.area_mm2:.1f} mm² on {area.layer}")
            print(f"    Heat spreading effectiveness: {area.effectiveness:.0f}%")
        print()

    # Recommendations
    print("=" * 60)
    print("Recommendations")
    print("=" * 60)
    print()

    critical = [s for s in result.heat_sources if s.estimated_temp_rise > 50]
    warning = [s for s in result.heat_sources if 30 < s.estimated_temp_rise <= 50]

    if critical:
        print("CRITICAL - Components likely to overheat:")
        for source in critical:
            print(f"  - {source.reference}: Add heatsink or thermal vias")
        print()

    if warning:
        print("WARNING - Components running warm:")
        for source in warning:
            print(f"  - {source.reference}: Ensure adequate copper pour")
        print()

    if not critical and not warning:
        print("Design looks thermally sound.")
        print("Consider adding thermal vias under power components for margin.")

    print()
    print("Run via CLI:")
    print("  kct analyze thermal board.kicad_pcb")
    print("  kct analyze thermal board.kicad_pcb --min-power 100  # Only >100mW sources")


def show_synthetic_example():
    """Show example output without a real PCB file."""
    print("=" * 60)
    print("Example Thermal Analysis Output")
    print("=" * 60)
    print()
    print("Heat sources found: 4")
    print("Total estimated power: 2850 mW")
    print()
    print("Heat Sources")
    print("-" * 40)
    print("  U1: Voltage Regulator")
    print("    Power: 1500 mW")
    print("    Package: TO-252 (DPAK)")
    print("    Thermal resistance: 40.0 °C/W")
    print("    Est. temp rise: 60.0°C [CRITICAL]")
    print()
    print("  U2: MOSFET Driver")
    print("    Power: 800 mW")
    print("    Package: SOIC-8")
    print("    Thermal resistance: 120.0 °C/W")
    print("    Est. temp rise: 35.0°C [WARNING]")
    print()
    print("  R5: Power Resistor")
    print("    Power: 350 mW")
    print("    Package: 2512")
    print("    Thermal resistance: 50.0 °C/W")
    print("    Est. temp rise: 17.5°C")
    print()
    print("Heat Source Clusters (nearby sources)")
    print("-" * 40)
    print("  Cluster at (25.0, 30.0)mm")
    print("    Components: U1, U2")
    print("    Combined power: 2300 mW")
    print("    Recommendation: Separate U1 and U2 by at least 10mm")
    print()
    print("=" * 60)
    print("Recommendations")
    print("=" * 60)
    print()
    print("CRITICAL - Components likely to overheat:")
    print("  - U1: Add heatsink or thermal vias")
    print()
    print("WARNING - Components running warm:")
    print("  - U2: Ensure adequate copper pour")
    print()


if __name__ == "__main__":
    main()

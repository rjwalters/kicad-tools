#!/usr/bin/env python3
"""
Thermal Awareness Demo

Demonstrates how kicad-tools v0.6.0 classifies components by thermal behavior
and generates constraints to keep heat sources away from sensitive components.
"""

from pathlib import Path

from kicad_tools.optim import (
    classify_thermal_properties,
    detect_thermal_constraints,
    get_thermal_summary,
)
from kicad_tools.schema.pcb import PCB


def main():
    """Run the thermal awareness demo."""
    # Load the example PCB
    pcb_path = Path(__file__).parent / "fixtures" / "mcu_board.kicad_pcb"
    print(f"Loading PCB: {pcb_path.name}")
    pcb = PCB.load(str(pcb_path))

    print(f"Found {len(pcb.footprints)} components")
    print()

    # Classify thermal properties
    print("=" * 60)
    print("Thermal Classification")
    print("=" * 60)
    print()

    thermal_props = classify_thermal_properties(pcb)

    # Get summary
    summary = get_thermal_summary(thermal_props)

    # Display heat sources
    if summary["heat_sources"]:
        print("HEAT SOURCES (LDOs, regulators, power components)")
        print("-" * 50)
        for ref in sorted(summary["heat_sources"]):
            props = thermal_props[ref]
            print(
                f"  {ref:8s} "
                f"power={props.power_dissipation_w:.1f}W "
                f"max_temp={props.max_temp_c}C "
                f"{'[thermal relief needed]' if props.needs_thermal_relief else ''}"
            )
        print()

    # Display heat sensitive components
    if summary["heat_sensitive"]:
        print("HEAT SENSITIVE (crystals, voltage refs, temp sensors)")
        print("-" * 50)
        for ref in sorted(summary["heat_sensitive"]):
            props = thermal_props[ref]
            print(
                f"  {ref:8s} sensitivity={props.thermal_sensitivity} max_temp={props.max_temp_c}C"
            )
        print()

    # Detect thermal constraints
    print("=" * 60)
    print("Thermal Placement Constraints")
    print("=" * 60)
    print()

    constraints = detect_thermal_constraints(pcb, thermal_props)

    if not constraints:
        print("No thermal constraints detected.")
        print("(Constraints require both heat sources and heat-sensitive components)")
        return

    # Group by type
    separations = [c for c in constraints if c.constraint_type == "min_separation"]
    edge_prefs = [c for c in constraints if c.constraint_type == "edge_preference"]
    thermal_zones = [c for c in constraints if c.constraint_type == "thermal_zone"]

    if separations:
        print("MINIMUM SEPARATION (keep heat sources away from sensitive)")
        print("-" * 50)
        for c in separations:
            print(
                f"  Keep {c.parameters['heat_source']:8s} at least "
                f"{c.parameters['min_distance_mm']}mm from {c.parameters['sensitive']}"
            )
        print()

    if edge_prefs:
        print("EDGE PREFERENCE (heat sources near board edge for dissipation)")
        print("-" * 50)
        for c in edge_prefs:
            print(
                f"  {c.parameters['component']:8s} should be within "
                f"{c.parameters['edge_distance_max_mm']}mm of board edge"
            )
        print()

    if thermal_zones:
        print("THERMAL ZONES (group power components together)")
        print("-" * 50)
        for c in thermal_zones:
            print(f"  Zone type: {c.parameters['zone_type']}")
            print(f"  Components: {', '.join(c.parameters['components'])}")
        print()

    # Summary
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print()
    print(f"Heat sources: {len(summary['heat_sources'])}")
    print(f"Heat sensitive: {len(summary['heat_sensitive'])}")
    print(f"Neutral components: {len(summary['neutral'])}")
    print()
    print("Constraints generated:")
    print(f"  - Separation constraints: {len(separations)}")
    print(f"  - Edge preferences: {len(edge_prefs)}")
    print(f"  - Thermal zones: {len(thermal_zones)}")
    print()
    print("Use thermal awareness during placement optimization with:")
    print("  optimizer = PlacementOptimizer.from_pcb(pcb)")
    print("  # Thermal constraints applied automatically based on classification")
    print()
    print("Or via CLI:")
    print("  kicad-tools placement optimize board.kicad_pcb --thermal")


if __name__ == "__main__":
    main()

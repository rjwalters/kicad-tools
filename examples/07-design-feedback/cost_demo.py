#!/usr/bin/env python3
"""
Manufacturing Cost Estimation Demo

Demonstrates how kicad-tools v0.7.0 estimates manufacturing costs and checks
part availability from LCSC.
"""

from pathlib import Path

from kicad_tools.cost import (
    estimate_manufacturing_cost,
    check_availability,
    suggest_alternatives,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.schema.schematic import Schematic


def main():
    """Run the cost estimation demo."""
    # Load example files
    pcb_path = Path(__file__).parent / "fixtures" / "project.kicad_pcb"
    sch_path = Path(__file__).parent / "fixtures" / "project.kicad_sch"

    if not pcb_path.exists() or not sch_path.exists():
        print("Note: No fixture files found. Using synthetic example.")
        print("Copy KiCad project files to fixtures/ to analyze actual costs.")
        print()
        show_synthetic_example()
        return

    print(f"Loading project...")
    print(f"  PCB: {pcb_path.name}")
    print(f"  Schematic: {sch_path.name}")
    print()

    pcb = PCB.load(str(pcb_path))
    sch = Schematic.load(str(sch_path))

    # Estimate manufacturing costs
    print("=" * 60)
    print("Manufacturing Cost Estimate")
    print("=" * 60)
    print()

    for qty in [5, 10, 50, 100]:
        result = estimate_manufacturing_cost(
            pcb, sch, quantity=qty, manufacturer="jlcpcb"
        )

        print(f"Quantity: {qty} boards")
        print("-" * 40)
        print(f"  PCB fabrication:  ${result.pcb_cost:.2f}")
        print(f"  Components:       ${result.component_cost:.2f}")
        print(f"  Assembly:         ${result.assembly_cost:.2f}")
        print(f"  Shipping (est):   ${result.shipping_estimate:.2f}")
        print(f"  ---")
        print(f"  Total:            ${result.total:.2f}")
        print(f"  Per board:        ${result.per_board:.2f}")
        print()

    # Check part availability
    print("=" * 60)
    print("Part Availability (LCSC)")
    print("=" * 60)
    print()

    availability = check_availability(sch)

    available = [p for p in availability.parts if p.status == "AVAILABLE"]
    low_stock = [p for p in availability.parts if p.status == "LOW_STOCK"]
    unavailable = [p for p in availability.parts if p.status in ("OUT_OF_STOCK", "DISCONTINUED")]
    not_found = [p for p in availability.parts if p.status == "NOT_FOUND"]

    print(f"Available:     {len(available)} parts")
    print(f"Low stock:     {len(low_stock)} parts")
    print(f"Unavailable:   {len(unavailable)} parts")
    print(f"Not in LCSC:   {len(not_found)} parts")
    print()

    if low_stock:
        print("Low Stock Parts:")
        for part in low_stock[:5]:
            print(f"  {part.reference}: {part.value} - {part.stock} left")
        if len(low_stock) > 5:
            print(f"  ... and {len(low_stock) - 5} more")
        print()

    if unavailable:
        print("Unavailable Parts:")
        for part in unavailable:
            print(f"  {part.reference}: {part.value} - {part.status}")
        print()

    # Suggest alternatives for unavailable parts
    if unavailable or low_stock:
        print("=" * 60)
        print("Alternative Part Suggestions")
        print("=" * 60)
        print()

        alternatives = suggest_alternatives(sch, prefer_basic=True)

        for suggestion in alternatives.suggestions[:5]:
            print(f"{suggestion.reference}: {suggestion.original_value}")
            print(f"  Issue: {suggestion.reason}")
            print(f"  Alternatives:")
            for alt in suggestion.alternatives[:3]:
                savings = ""
                if alt.price_diff < 0:
                    savings = f" (saves ${-alt.price_diff:.3f}/pc)"
                print(f"    - {alt.part_number}: {alt.value}{savings}")
                print(f"      Stock: {alt.stock}, Lead time: {alt.lead_time}")
            print()

    # Summary
    print("=" * 60)
    print("Cost Optimization Tips")
    print("=" * 60)
    print()
    print("1. Use JLCPCB 'basic' parts where possible (-30% assembly fee)")
    print("2. Order in quantities of 10+ for significant price breaks")
    print("3. Green solder mask is cheapest; other colors add $5-15")
    print("4. 2-layer boards are much cheaper than 4-layer")
    print()
    print("Run via CLI:")
    print("  kct estimate cost board.kicad_pcb --bom schematic.kicad_sch")
    print("  kct parts availability schematic.kicad_sch")
    print("  kct suggest alternatives schematic.kicad_sch --basic")


def show_synthetic_example():
    """Show example output without real project files."""
    print("=" * 60)
    print("Example Manufacturing Cost Estimate")
    print("=" * 60)
    print()
    print("Quantity: 10 boards")
    print("-" * 40)
    print("  PCB fabrication:  $4.50")
    print("  Components:       $23.40")
    print("  Assembly:         $12.00")
    print("  Shipping (est):   $18.00")
    print("  ---")
    print("  Total:            $57.90")
    print("  Per board:        $5.79")
    print()
    print("Quantity: 100 boards")
    print("-" * 40)
    print("  PCB fabrication:  $18.00")
    print("  Components:       $186.00")
    print("  Assembly:         $45.00")
    print("  Shipping (est):   $35.00")
    print("  ---")
    print("  Total:            $284.00")
    print("  Per board:        $2.84")
    print()
    print("=" * 60)
    print("Part Availability (LCSC)")
    print("=" * 60)
    print()
    print("Available:     42 parts")
    print("Low stock:     3 parts")
    print("Unavailable:   1 parts")
    print("Not in LCSC:   2 parts")
    print()
    print("Low Stock Parts:")
    print("  C5: 100nF 0402 - 245 left")
    print("  U3: STM32G031 - 89 left")
    print("  R12: 4.7k 0402 - 156 left")
    print()
    print("Unavailable Parts:")
    print("  U1: MAX17048 - OUT_OF_STOCK")
    print()
    print("=" * 60)
    print("Alternative Part Suggestions")
    print("=" * 60)
    print()
    print("U1: MAX17048")
    print("  Issue: Out of stock")
    print("  Alternatives:")
    print("    - BQ27441: Fuel gauge IC (saves $0.50/pc)")
    print("      Stock: 5420, Lead time: 3 days")
    print("    - LC709203F: Battery monitor")
    print("      Stock: 1200, Lead time: 5 days")
    print()


if __name__ == "__main__":
    main()

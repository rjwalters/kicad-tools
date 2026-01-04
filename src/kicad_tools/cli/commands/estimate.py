"""Estimate command handlers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

__all__ = ["run_estimate_command"]


def run_estimate_command(args) -> int:
    """Handle estimate subcommands."""
    if not args.estimate_command:
        print("Usage: kicad-tools estimate <command> [options]")
        print("Commands: cost")
        return 1

    if args.estimate_command == "cost":
        return _run_cost_command(args)

    return 1


def _run_cost_command(args) -> int:
    """Estimate manufacturing costs for a PCB."""
    from kicad_tools.cost import ManufacturingCostEstimator
    from kicad_tools.schema.bom import extract_bom
    from kicad_tools.schema.pcb import PCB

    # Load PCB
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1

    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Load BOM if provided
    bom = None
    if args.bom:
        bom_path = Path(args.bom)
        if not bom_path.exists():
            print(f"Error: BOM file not found: {bom_path}", file=sys.stderr)
            return 1

        if bom_path.suffix == ".kicad_sch":
            try:
                bom = extract_bom(str(bom_path))
            except Exception as e:
                print(f"Error loading schematic: {e}", file=sys.stderr)
                return 1
        else:
            print(f"Warning: CSV BOM import not yet implemented, using PCB only")

    # Create estimator
    estimator = ManufacturingCostEstimator(manufacturer=args.mfr)

    # Estimate costs
    estimate = estimator.estimate(
        pcb=pcb,
        bom=bom,
        quantity=args.quantity,
        surface_finish=args.finish,
        solder_mask_color=args.color,
        board_thickness_mm=args.thickness,
    )

    # Output
    if args.estimate_format == "json":
        print(json.dumps(estimate.to_dict(), indent=2))
    else:
        _print_text_estimate(estimate, verbose=args.verbose)

    return 0


def _print_text_estimate(estimate, verbose: bool = False) -> None:
    """Print cost estimate in human-readable format."""
    print(
        f"\nManufacturing Cost Estimate ({estimate.manufacturer.upper()}, qty: {estimate.quantity}):\n"
    )

    # PCB costs
    print(f"  PCB Fabrication:      ${estimate.pcb_cost_per_unit:.2f}/unit")
    if verbose:
        pcb = estimate.pcb
        print(
            f"    Board area:         ${pcb.area_cost / estimate.quantity:.2f} ({pcb.width_mm:.0f}x{pcb.height_mm:.0f}mm)"
        )
        if pcb.layer_cost > 0:
            print(
                f"    {pcb.layer_count} layers:           ${pcb.layer_cost / estimate.quantity:.2f}"
            )
        if pcb.finish_cost > 0:
            print(
                f"    {pcb.surface_finish.upper()} finish:     ${pcb.finish_cost / estimate.quantity:.2f}"
            )
        if pcb.color_cost > 0:
            print(f"    {pcb.solder_mask_color} mask:     ${pcb.color_cost:.2f}")
    print()

    # Component costs
    if estimate.components:
        print(f"  Components:           ${estimate.component_cost_per_unit:.2f}/unit")
        if verbose:
            # Show top 5 most expensive components
            sorted_comps = sorted(estimate.components, key=lambda c: c.extended_cost, reverse=True)
            for comp in sorted_comps[:5]:
                stock_str = "in stock" if comp.in_stock else "out of stock"
                print(
                    f"    {comp.reference} ({comp.value}):  ${comp.extended_cost:.2f} ({stock_str})"
                )
            if len(sorted_comps) > 5:
                other_cost = sum(c.extended_cost for c in sorted_comps[5:])
                print(f"    Other ({len(sorted_comps) - 5}):      ${other_cost:.2f}")
        print()

    # Assembly costs
    print(f"  Assembly:             ${estimate.assembly_cost_per_unit:.2f}/unit")
    if verbose:
        asm = estimate.assembly
        print(f"    SMT ({asm.smt_parts} parts):     ${asm.smt_cost / estimate.quantity:.2f}")
        if asm.through_hole_parts > 0:
            print(
                f"    Through-hole ({asm.through_hole_parts}): ${asm.through_hole_cost / estimate.quantity:.2f}"
            )
        if asm.bga_parts > 0:
            print(f"    BGA ({asm.bga_parts}):          ${asm.bga_cost / estimate.quantity:.2f}")
        print(f"    Setup/stencil:      ${asm.setup_cost / estimate.quantity:.2f}")
    print()

    # Total
    print(f"  {'=' * 40}")
    print(
        f"  TOTAL:                ${estimate.total_per_unit:.2f}/unit (${estimate.total_for_quantity:.2f} for {estimate.quantity} units)"
    )
    print()

    # Cost drivers
    if estimate.cost_drivers:
        print("  Cost Drivers:")
        for driver in estimate.cost_drivers:
            print(f"    - {driver}")
        print()

    # Optimization suggestions
    if estimate.optimization_suggestions:
        print("  Optimization Suggestions:")
        for suggestion in estimate.optimization_suggestions:
            print(f"    - {suggestion}")
        print()

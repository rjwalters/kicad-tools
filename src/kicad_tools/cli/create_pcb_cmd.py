"""
CLI command for creating a PCB from a schematic file.

Uses the workflow.PCBFromSchematic module to extract netlist data,
create a blank PCB, place footprints, and assign nets.

Example:
    kicad-tools create-pcb design.kicad_sch -o board.kicad_pcb --layers 4
    kicad-tools create-pcb design.kicad_sch --width 160 --height 100
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Main entry point for create-pcb command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools create-pcb",
        description="Create a PCB from a KiCad schematic file",
    )
    parser.add_argument(
        "schematic",
        help="Path to .kicad_sch schematic file",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output .kicad_pcb file path (default: <schematic-stem>.kicad_pcb)",
    )
    parser.add_argument(
        "--width",
        type=float,
        default=100.0,
        help="Board width in mm (default: 100.0)",
    )
    parser.add_argument(
        "--height",
        type=float,
        default=100.0,
        help="Board height in mm (default: 100.0)",
    )
    parser.add_argument(
        "--layers",
        type=int,
        choices=[2, 4],
        default=2,
        help="Number of copper layers (default: 2)",
    )
    parser.add_argument(
        "--title",
        default="",
        help="Board title for title block (default: schematic filename)",
    )
    parser.add_argument(
        "--revision",
        default="1.0",
        help="Board revision (default: 1.0)",
    )
    parser.add_argument(
        "--company",
        default="",
        help="Company name for title block",
    )
    parser.add_argument(
        "--no-place",
        action="store_true",
        help="Skip automatic component placement",
    )
    parser.add_argument(
        "--spacing",
        type=float,
        default=15.0,
        help="Spacing between auto-placed components in mm (default: 15.0)",
    )
    parser.add_argument(
        "--columns",
        type=int,
        default=None,
        help="Number of columns for auto-placement grid (default: auto-calculated from board width)",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=3.0,
        help="Inset from board edges for auto-placement in mm (default: 3.0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without saving",
    )

    args = parser.parse_args(argv)
    console = Console()

    schematic_path = Path(args.schematic)
    if not schematic_path.exists():
        console.print(f"[red]Error:[/red] Schematic not found: {schematic_path}")
        return 1

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = schematic_path.parent / f"{schematic_path.stem}.kicad_pcb"

    try:
        from kicad_tools.workflow import PCBFromSchematic

        console.print(f"[blue]Schematic:[/blue] {schematic_path}")
        console.print(
            f"[blue]Board size:[/blue] {args.width} x {args.height} mm, {args.layers} layers"
        )

        workflow = PCBFromSchematic(str(schematic_path))

        # Get components
        components = workflow.get_components()
        console.print(f"[green]Found {len(components)} components[/green]")

        # Create PCB
        workflow.create_pcb(
            width=args.width,
            height=args.height,
            layers=args.layers,
            title=args.title,
            revision=args.revision,
            company=args.company,
        )
        console.print("[green]PCB created[/green]")

        # Place components
        if not args.no_place:
            result = workflow.place_all_components(
                spacing=args.spacing,
                columns=args.columns,
                margin=args.margin,
            )
            console.print(
                f"[green]Placed {result.success_count} components[/green]"
                + (
                    f", [yellow]{result.failure_count} failed[/yellow]"
                    if result.failure_count
                    else ""
                )
            )

            for warning in result.warnings:
                console.print(f"[yellow]Warning: {warning}[/yellow]")

            if result.failed:
                table = Table(title="Failed Placements")
                table.add_column("Reference")
                table.add_column("Reason")
                for ref, reason in result.failed:
                    table.add_row(ref, reason)
                console.print(table)

        # Assign nets
        nets = workflow.assign_nets()
        console.print(f"[green]Assigned {nets.success_count} net connections[/green]")

        if nets.missing_footprints:
            console.print(
                f"[yellow]Missing footprints: {', '.join(nets.missing_footprints)}[/yellow]"
            )

        # Summary
        summary = workflow.summary()
        console.print()
        summary_table = Table(title="PCB Summary")
        summary_table.add_column("Property")
        summary_table.add_column("Value")
        for key, value in summary.items():
            summary_table.add_row(str(key), str(value))
        console.print(summary_table)

        # Save
        if args.dry_run:
            console.print(f"\n[yellow]Dry run:[/yellow] Would save to {output_path}")
        else:
            workflow.save(str(output_path))
            console.print(f"\n[green]Saved:[/green] {output_path}")

        return 0

    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

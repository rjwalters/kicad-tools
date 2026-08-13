"""
CLI command for creating a PCB from a schematic file.

Uses the workflow.PCBFromSchematic module to extract netlist data,
create a blank PCB, place footprints, and assign nets.

Example:
    kicad-tools create-pcb design.kicad_sch -o board.kicad_pcb --layers 4
    kicad-tools create-pcb design.kicad_sch --width 160 --height 100

Machine output (``--format json``, issue #4674): one document describing the
generated board -- ``board`` geometry, ``components_found``, the ``placement``
and ``nets`` results, the workflow ``summary``, and whether the file was
actually written (``saved``, false under ``--dry-run``).  The rich prose is
suppressed in JSON mode so stdout carries exactly one document.  See
``docs/reference/machine-output.md``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .format_options import FORMAT_JSON, add_format_flag, emit_json

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
    add_format_flag(parser)

    args = parser.parse_args(argv)
    console = Console()
    as_json = args.format == FORMAT_JSON

    def say(*print_args, **print_kwargs) -> None:
        """Print rich prose unless JSON mode owns stdout."""
        if not as_json:
            console.print(*print_args, **print_kwargs)

    schematic_path = Path(args.schematic)
    if not schematic_path.exists():
        message = f"Schematic not found: {schematic_path}"
        if as_json:
            emit_json(_error_document(str(schematic_path), message))
            return 1
        console.print(f"[red]Error:[/red] {message}")
        return 1

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = schematic_path.parent / f"{schematic_path.stem}.kicad_pcb"

    try:
        from kicad_tools.workflow import PCBFromSchematic

        say(f"[blue]Schematic:[/blue] {schematic_path}")
        say(f"[blue]Board size:[/blue] {args.width} x {args.height} mm, {args.layers} layers")

        workflow = PCBFromSchematic(str(schematic_path))

        # Get components
        components = workflow.get_components()
        say(f"[green]Found {len(components)} components[/green]")

        # Create PCB
        workflow.create_pcb(
            width=args.width,
            height=args.height,
            layers=args.layers,
            title=args.title,
            revision=args.revision,
            company=args.company,
        )
        say("[green]PCB created[/green]")

        # Place components
        placement: dict = {
            "skipped": bool(args.no_place),
            "placed": 0,
            "failed": [],
            "warnings": [],
        }
        if not args.no_place:
            result = workflow.place_all_components(
                spacing=args.spacing,
                columns=args.columns,
                margin=args.margin,
            )
            placement["placed"] = result.success_count
            placement["failed"] = sorted(
                ({"reference": ref, "reason": reason} for ref, reason in result.failed),
                key=lambda item: (item["reference"], item["reason"]),
            )
            placement["warnings"] = sorted(result.warnings)
            say(
                f"[green]Placed {result.success_count} components[/green]"
                + (
                    f", [yellow]{result.failure_count} failed[/yellow]"
                    if result.failure_count
                    else ""
                )
            )

            for warning in result.warnings:
                say(f"[yellow]Warning: {warning}[/yellow]")

            if result.failed:
                table = Table(title="Failed Placements")
                table.add_column("Reference")
                table.add_column("Reason")
                for ref, reason in result.failed:
                    table.add_row(ref, reason)
                say(table)

        # Assign nets
        nets = workflow.assign_nets()
        say(f"[green]Assigned {nets.success_count} net connections[/green]")

        if nets.missing_footprints:
            say(f"[yellow]Missing footprints: {', '.join(nets.missing_footprints)}[/yellow]")

        # Summary
        summary = workflow.summary()
        say()
        summary_table = Table(title="PCB Summary")
        summary_table.add_column("Property")
        summary_table.add_column("Value")
        for key, value in summary.items():
            summary_table.add_row(str(key), str(value))
        say(summary_table)

        # Save
        if args.dry_run:
            say(f"\n[yellow]Dry run:[/yellow] Would save to {output_path}")
        else:
            workflow.save(str(output_path))
            say(f"\n[green]Saved:[/green] {output_path}")

        if as_json:
            emit_json(
                {
                    "command": "create-pcb",
                    "schematic": str(schematic_path),
                    "output": str(output_path),
                    "board": {
                        "width_mm": args.width,
                        "height_mm": args.height,
                        "layers": args.layers,
                    },
                    "components_found": len(components),
                    "placement": placement,
                    "nets": {
                        "assigned": nets.success_count,
                        "missing_footprints": sorted(nets.missing_footprints),
                    },
                    "summary": {str(key): value for key, value in summary.items()},
                    "dry_run": bool(args.dry_run),
                    "saved": not args.dry_run,
                    "success": True,
                }
            )

        return 0

    except FileNotFoundError as e:
        if as_json:
            emit_json(_error_document(str(schematic_path), str(e)))
            return 1
        console.print(f"[red]Error:[/red] {e}")
        return 1
    except Exception as e:
        if as_json:
            emit_json(_error_document(str(schematic_path), str(e)))
            return 1
        console.print(f"[red]Error:[/red] {e}")
        return 1


def _error_document(schematic: str, message: str) -> dict:
    """The failure document shared by every ``create-pcb`` error path."""
    return {
        "command": "create-pcb",
        "schematic": schematic,
        "error": message,
        "saved": False,
        "success": False,
    }


if __name__ == "__main__":
    sys.exit(main())

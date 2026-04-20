"""
MCP tools for PCB generation workflow.

Provides a tool for creating PCBs from KiCad schematics, wrapping
the workflow.PCBFromSchematic module for use by AI agents.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def create_pcb_from_schematic(
    schematic_path: str,
    output_path: str | None = None,
    width: float = 100.0,
    height: float = 100.0,
    layers: int = 2,
    title: str = "",
    revision: str = "1.0",
    company: str = "",
    auto_place: bool = True,
    placement_spacing: float = 15.0,
    placement_columns: int = 10,
) -> dict:
    """
    Create a PCB from a KiCad schematic file.

    Extracts netlist data from the schematic, creates a blank PCB with the
    specified dimensions, optionally places footprints for all components
    in a grid layout, and assigns nets based on schematic connectivity.

    Args:
        schematic_path: Path to .kicad_sch schematic file
        output_path: Output .kicad_pcb file path. If not provided, uses
                     <schematic-stem>.kicad_pcb in the same directory.
        width: Board width in mm (default: 100.0)
        height: Board height in mm (default: 100.0)
        layers: Number of copper layers, 2 or 4 (default: 2)
        title: Board title for title block (default: schematic filename)
        revision: Board revision (default: "1.0")
        company: Company name for title block
        auto_place: Whether to automatically place components (default: True)
        placement_spacing: Spacing between auto-placed components in mm
        placement_columns: Number of columns for auto-placement grid

    Returns:
        Dictionary with results including:
        - success: Whether the operation succeeded
        - output_path: Path to the saved PCB file
        - component_count: Number of components found
        - placed_count: Number of components placed (if auto_place)
        - failed_count: Number of placement failures (if auto_place)
        - net_count: Number of nets assigned
        - summary: Full workflow summary dict

    Example:
        >>> result = create_pcb_from_schematic(
        ...     "/path/to/project.kicad_sch",
        ...     width=160, height=100, layers=4,
        ... )
        >>> if result["success"]:
        ...     print(f"PCB saved to {result['output_path']}")
    """
    sch_file = Path(schematic_path)

    if not sch_file.exists():
        return {
            "success": False,
            "error": f"Schematic file not found: {schematic_path}",
        }

    if output_path:
        out_file = Path(output_path)
    else:
        out_file = sch_file.parent / f"{sch_file.stem}.kicad_pcb"

    try:
        from kicad_tools.workflow import PCBFromSchematic

        workflow = PCBFromSchematic(str(sch_file))

        # Get components
        components = workflow.get_components()

        # Create PCB
        workflow.create_pcb(
            width=width,
            height=height,
            layers=layers,
            title=title,
            revision=revision,
            company=company,
        )

        result: dict = {
            "success": True,
            "output_path": str(out_file),
            "component_count": len(components),
        }

        # Place components
        if auto_place:
            placement = workflow.place_all_components(
                spacing=placement_spacing,
                columns=placement_columns,
            )
            result["placed_count"] = placement.success_count
            result["failed_count"] = placement.failure_count
            if placement.failed:
                result["failed_placements"] = [
                    {"reference": ref, "reason": reason}
                    for ref, reason in placement.failed
                ]

        # Assign nets
        nets = workflow.assign_nets()
        result["net_count"] = nets.success_count
        if nets.missing_footprints:
            result["missing_footprints"] = nets.missing_footprints

        # Save
        workflow.save(str(out_file))

        # Summary
        result["summary"] = workflow.summary()

        return result

    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("Failed to create PCB from schematic")
        return {"success": False, "error": str(e)}

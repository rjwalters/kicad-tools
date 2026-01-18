#!/usr/bin/env python3
"""
Example: Complete Schematic to PCB Workflow

This example demonstrates the end-to-end workflow from creating a project
to exporting manufacturing files using kicad-tools.

The workflow:
1. Create a new KiCad project
2. Cross-reference schematic and PCB (after manual KiCad update)
3. Place components programmatically
4. Route traces
5. Validate design
6. Export for manufacturing
"""

import sys
from pathlib import Path

# Add parent directory to path for development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kicad_tools import Project
from kicad_tools.pcb import PCBEditor


def main():
    """Run the complete workflow example."""
    print("=" * 60)
    print("Schematic to PCB Workflow Example")
    print("=" * 60)

    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)

    # Step 1: Create a new project
    print("\n--- Step 1: Create Project ---")
    project = Project.create(
        name="workflow_demo",
        directory=output_dir,
        board_width=50.0,
        board_height=40.0,
    )
    print(f"Created project: {project.name}")
    print(f"  Schematic: {project._schematic_path}")
    print(f"  PCB: {project._pcb_path}")

    # Step 2: Demonstrate cross-referencing
    # (In a real workflow, you'd design the schematic first, then use
    # KiCad's "Update PCB from Schematic" to transfer components)
    print("\n--- Step 2: Cross-Reference Check ---")
    result = project.cross_reference()
    print(f"Matched components: {result.matched}")
    print(f"Unplaced symbols: {len(result.unplaced)}")
    print(f"Orphaned footprints: {len(result.orphaned)}")

    if result.is_clean:
        print("Schematic and PCB are synchronized!")
    else:
        print("\nNote: This is expected for a new empty project.")
        print("In a real workflow:")
        print("  1. Design your schematic (add symbols, connect nets)")
        print("  2. Run: kicad-cli sch export netlist design.kicad_sch -o design.net")
        print("  3. Run: kicad-cli pcb update design.kicad_pcb --netlist design.net")
        print("  4. Then use kicad-tools for placement, routing, and export")

    # Step 3: Demonstrate PCBEditor capabilities
    print("\n--- Step 3: PCBEditor Demo ---")
    pcb_path = output_dir / "workflow_demo.kicad_pcb"
    editor = PCBEditor(str(pcb_path))

    print(f"Loaded PCB: {editor.path}")
    print(f"Nets defined: {len(editor.nets)}")
    print(f"Footprints: {len(editor.footprints)}")

    # Demonstrate adding routing structures
    # (These would connect actual footprints in a real design)
    print("\nAdding example traces and via...")

    # Add a track (no real net connection in empty board)
    editor.add_track(
        net_name="",  # Unconnected example
        points=[(10, 10), (20, 10), (20, 20)],
        width=0.25,
        layer="F.Cu",
    )

    # Add a via
    editor.add_via(
        position=(20, 20),
        net_name="",  # Unconnected example
        drill=0.3,
        size=0.6,
    )

    # Add a ground pour
    editor.add_zone(
        net_name="",  # Would be "GND" in real design
        layer="B.Cu",
        boundary=[(5, 5), (45, 5), (45, 35), (5, 35)],
    )

    editor.save()
    print(f"Saved modified PCB: {pcb_path}")

    # Step 4: Demonstrate Project-level operations
    print("\n--- Step 4: Project Operations ---")

    # Reload project to pick up changes
    project = Project.load(output_dir / "workflow_demo.kicad_pro")

    # Generate BOM (empty for new project)
    bom = project.get_bom()
    if bom:
        print(f"BOM items: {len(bom.items)}")
    else:
        print("No BOM (empty schematic)")

    # Step 5: Summary
    print("\n--- Workflow Summary ---")
    print(
        """
This example demonstrated the key workflow steps:

1. Project.create() - Creates .kicad_pro, .kicad_sch, .kicad_pcb files
2. project.cross_reference() - Validates schematic/PCB sync
3. PCBEditor - Add tracks, vias, zones to PCB
4. Project-level operations - BOM, routing, export

For a complete workflow with actual components:
- Design schematic with symbols and connections
- Use KiCad CLI to update PCB from schematic
- Use PlacementSession for intelligent component placement
- Use project.route() for autorouting
- Use project.check_drc() for validation
- Use project.export_gerbers() for manufacturing files

See the full guide: docs/guides/schematic-to-pcb-workflow.md
"""
    )

    print(f"\nOutput files created in: {output_dir}")
    for f in output_dir.iterdir():
        print(f"  {f.name}")


if __name__ == "__main__":
    main()

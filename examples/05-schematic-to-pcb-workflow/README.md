# Example: Schematic to PCB Workflow

This example demonstrates the complete workflow from creating a KiCad project to generating manufacturing outputs using kicad-tools.

## Overview

The schematic-to-PCB workflow involves:

1. **Create Project** - Generate .kicad_pro, .kicad_sch, .kicad_pcb files
2. **Design Schematic** - Add symbols and connect nets
3. **Update PCB** - Transfer netlist to PCB (via KiCad)
4. **Place Components** - Position footprints on the board
5. **Route Traces** - Connect pads per the netlist
6. **Validate** - Check DRC and synchronization
7. **Export** - Generate manufacturing files

## Running the Example

```bash
# From the examples directory
cd examples/05-schematic-to-pcb-workflow
python workflow_example.py

# Or with uv from repo root
uv run python examples/05-schematic-to-pcb-workflow/workflow_example.py
```

## Key Concepts

### Project Class

The `Project` class is the high-level interface for complete workflows:

```python
from kicad_tools import Project

# Create new project
project = Project.create("my_board", directory="./designs/")

# Load existing project
project = Project.load("my_board.kicad_pro")

# Access schematic and PCB
sch = project.schematic
pcb = project.pcb

# Operations
result = project.cross_reference()  # Check sync
project.route(skip_nets=["GND"])    # Autoroute
project.export_gerbers("output/")   # Manufacturing
```

### PCBEditor

For direct PCB manipulation:

```python
from kicad_tools.pcb import PCBEditor

editor = PCBEditor("board.kicad_pcb")
editor.place_component("R1", x=50, y=30)
editor.add_track("VCC", [(50, 30), (60, 30)], width=0.25)
editor.add_via((60, 30), net_name="VCC")
editor.save()
```

### Netlist Transfer

KiCad's "Update PCB from Schematic" is required to transfer components:

```bash
# Export netlist from schematic
kicad-cli sch export netlist design.kicad_sch -o design.net

# Update PCB with netlist
kicad-cli pcb update design.kicad_pcb --netlist design.net
```

## Output

The example creates files in the `output/` directory:

- `workflow_demo.kicad_pro` - Project file
- `workflow_demo.kicad_sch` - Empty schematic
- `workflow_demo.kicad_pcb` - PCB with example traces

## Further Reading

- [Schematic to PCB Workflow Guide](../../docs/guides/schematic-to-pcb-workflow.md)
- [Routing Guide](../../docs/guides/routing.md)
- [Placement Optimization Guide](../../docs/guides/placement-optimization.md)

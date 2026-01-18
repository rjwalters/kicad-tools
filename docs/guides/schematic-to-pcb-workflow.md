# Schematic to PCB Workflow

This guide explains how to go from a KiCad schematic to a manufactured PCB using kicad-tools.

---

## Overview

The workflow from schematic to PCB involves these key steps:

1. **Create or Load Schematic** - Design your circuit
2. **Update PCB from Schematic** - Transfer netlist to PCB (via KiCad)
3. **Place Components** - Position footprints on the board
4. **Route Traces** - Connect the pads per the netlist
5. **Validate** - Check DRC and sync
6. **Export** - Generate manufacturing files

kicad-tools supports all these steps through the `Project` class and specialized modules.

---

## Key Concepts

### The Netlist Connection

KiCad maintains a netlist that defines:
- Which components exist (symbols ↔ footprints)
- How they connect (nets)
- Pin-to-pad mappings

**Important**: kicad-tools does not directly copy components from schematic to PCB. Instead:
1. You design the schematic (with kicad-tools or KiCad GUI)
2. KiCad's "Update PCB from Schematic" command transfers components
3. kicad-tools then helps with placement, routing, validation, and export

### Module Responsibilities

| Module | Purpose |
|--------|---------|
| `kicad_tools.schema` | **Read** existing schematics and PCBs |
| `kicad_tools.schematic` | **Create** schematic content (symbols, blocks) |
| `kicad_tools.pcb` | **Edit** PCB files (placement, routing, zones) |
| `kicad_tools.project` | **Orchestrate** complete workflows |
| `kicad_tools.validate` | **Verify** schematic-PCB synchronization |
| `kicad_tools.export` | **Generate** manufacturing outputs |

---

## Quick Start

### Using the Project Class

The `Project` class is the recommended high-level interface:

```python
from kicad_tools import Project

# Create a new project
project = Project.create("my_board", directory="./projects/")
# Creates: my_board.kicad_pro, my_board.kicad_sch, my_board.kicad_pcb

# Load an existing project
project = Project.load("my_board.kicad_pro")

# Access schematic and PCB
sch = project.schematic   # Parsed Schematic object
pcb = project.pcb         # Parsed PCB object

# Cross-reference to check sync
result = project.cross_reference()
print(f"Matched: {result.matched}")
print(f"Unplaced: {len(result.unplaced)}")
```

---

## Complete Workflow Example

### Step 1: Create a Project

```python
from kicad_tools import Project

# Create new project with 65x56mm board
project = Project.create(
    "led_driver",
    directory="./designs/",
    board_width=65.0,
    board_height=56.0,
)

print(f"Created: {project.name}")
# Creates: led_driver.kicad_pro, led_driver.kicad_sch, led_driver.kicad_pcb
```

### Step 2: Add Schematic Content

For simple designs, use label-based schematic generation:

```python
from kicad_tools.schematic.models import Schematic

# Load the empty schematic
sch = Schematic.load("./designs/led_driver.kicad_sch")

# Add symbols
sch.add_symbol(
    lib_id="Device:R",
    x=100, y=50,
    reference="R1",
    value="330",
    footprint="Resistor_SMD:R_0603_1608Metric",
)

sch.add_symbol(
    lib_id="Device:LED",
    x=100, y=70,
    reference="D1",
    value="LED",
    footprint="LED_SMD:LED_0603_1608Metric",
)

# Add wires and power symbols
# ... (see label-based schematic example)

# Save
sch.save("./designs/led_driver.kicad_sch")
```

For complex designs, use circuit blocks:

```python
from kicad_tools.schematic.blocks import LDOBlock, IndicatorBlock

# Create functional blocks
ldo = LDOBlock(
    input_voltage=12.0,
    output_voltage=3.3,
    part="AMS1117-3.3",
)

led_indicator = IndicatorBlock(
    color="green",
    current_ma=10,
    supply_voltage=3.3,
)
```

### Step 3: Update PCB from Schematic

This step must be done in KiCad (GUI or CLI):

```bash
# Option 1: Open in KiCad and use Tools > Update PCB from Schematic

# Option 2: Use kicad-cli (KiCad 8+)
kicad-cli sch export netlist led_driver.kicad_sch -o led_driver.net
kicad-cli pcb update led_driver.kicad_pcb --netlist led_driver.net
```

Or via Python subprocess:

```python
import subprocess

# Export netlist
subprocess.run([
    "kicad-cli", "sch", "export", "netlist",
    "./designs/led_driver.kicad_sch",
    "-o", "./designs/led_driver.net"
])

# Update PCB
subprocess.run([
    "kicad-cli", "pcb", "update",
    "./designs/led_driver.kicad_pcb",
    "--netlist", "./designs/led_driver.net"
])
```

### Step 4: Verify Synchronization

After updating, verify the schematic and PCB are in sync:

```python
# Reload project after KiCad update
project = Project.load("./designs/led_driver.kicad_pro")

# Check synchronization
sync = project.check_sync()

if sync.in_sync:
    print("Schematic and PCB are synchronized!")
else:
    print(f"Issues found: {sync.error_count} errors, {sync.warning_count} warnings")
    for issue in sync.errors:
        print(f"  {issue.message}")
        print(f"  Fix: {issue.suggestion}")
```

### Step 5: Place Components

Use the placement optimizer or manual placement:

```python
from kicad_tools.optim import PlacementSession
from kicad_tools.schema.pcb import PCB

# Load PCB
pcb = PCB.load("./designs/led_driver.kicad_pcb")

# Create placement session
session = PlacementSession(pcb)

# Query a move before applying
result = session.query_move("R1", 45.0, 32.0)
print(f"Score change: {result.score_delta}")

if result.score_delta < 0:  # Lower is better
    session.apply_move("R1", 45.0, 32.0)

# Place LED near resistor
session.apply_move("D1", 45.0, 38.0)

# Commit and save
session.commit()
pcb.save("./designs/led_driver.kicad_pcb")
```

Or use the PCBEditor for direct manipulation:

```python
from kicad_tools.pcb import PCBEditor

editor = PCBEditor("./designs/led_driver.kicad_pcb")

# Move components
editor.place_component("R1", x=45.0, y=32.0, rotation=0)
editor.place_component("D1", x=45.0, y=38.0, rotation=0)

editor.save()
```

### Step 6: Route Traces

Use the autorouter:

```python
# Route via Project class
result = project.route(skip_nets=["GND", "+3.3V"])
print(f"Routed: {result.routed_nets}/{result.total_nets} nets")
print(f"Vias: {result.total_vias}")

# Or use the router directly
from kicad_tools.router import Autorouter, DesignRules

rules = DesignRules(
    trace_width=0.2,
    clearance=0.15,
    via_drill=0.3,
    via_diameter=0.6,
)

router = Autorouter.from_pcb("./designs/led_driver.kicad_pcb", rules=rules)
result = router.route_all()
router.save("./designs/led_driver.kicad_pcb")
```

### Step 7: Validate Design

Run DRC and manufacturer checks:

```python
# Run KiCad DRC first
subprocess.run([
    "kicad-cli", "pcb", "drc",
    "./designs/led_driver.kicad_pcb",
    "-o", "./designs/led_driver-drc.rpt"
])

# Check against manufacturer rules
checks = project.check_drc(
    manufacturer="jlcpcb",
    layers=2,
    report_path="./designs/led_driver-drc.rpt",
)

for check in checks:
    if not check.is_compatible:
        print(f"FAIL: {check}")
```

### Step 8: Export for Manufacturing

```python
# Export Gerber files
gerbers = project.export_gerbers(
    output_dir="./designs/output/",
    manufacturer="jlcpcb",
)
print(f"Generated {len(gerbers)} Gerber files")

# Export complete assembly package (Gerbers + BOM + PNP)
package = project.export_assembly(
    output_dir="./designs/output/",
    manufacturer="jlcpcb",
)
print(f"Assembly package: {package.output_dir}")
print(f"Files: {package.files}")
```

---

## Module Deep Dive

### PCBEditor vs PCBLayout

**PCBEditor**: For editing existing `.kicad_pcb` files
- Load, modify, and save KiCad PCB files
- Place/move components
- Add tracks, vias, zones
- Best for: Modifying real PCB files

```python
from kicad_tools.pcb import PCBEditor

editor = PCBEditor("board.kicad_pcb")
editor.place_component("U1", x=30, y=25)
editor.add_track("VCC", [(30, 25), (40, 25), (40, 35)], width=0.3)
editor.add_via((40, 35), net_name="VCC")
editor.create_ground_pour(layer="B.Cu")
editor.save()
```

**PCBLayout**: For block-based programmatic layouts
- Container for placing PCB blocks
- Manages inter-block routing
- Best for: Building layouts from reusable blocks

```python
from kicad_tools.pcb import PCBLayout
from kicad_tools.pcb.blocks import LDOBlock, MCUBlock

layout = PCBLayout("power_system")

# Add blocks
layout.add_block(LDOBlock(part="AMS1117-3.3"), name="ldo")
layout.add_block(MCUBlock(part="STM32F103"), name="mcu")

# Position blocks
layout.blocks["ldo"].place(10, 10)
layout.blocks["mcu"].place(30, 10)

# Route between blocks
layout.route("ldo", "VOUT", "mcu", "VDD")

# Export placements
placements = layout.export_placements()
```

### Schematic Reading vs Writing

**Reading** (kicad_tools.schema):
```python
from kicad_tools.schema import Schematic

sch = Schematic.load("design.kicad_sch")
for sym in sch.symbols:
    print(f"{sym.reference}: {sym.value}")
```

**Writing** (kicad_tools.schematic):
```python
from kicad_tools.schematic import generate_symbol_sexp, SymbolDef, PinDef

# Generate custom symbols
symbol = SymbolDef(
    name="MY_IC",
    pins=[
        PinDef(name="VCC", number="1", type="power_in"),
        PinDef(name="GND", number="2", type="power_in"),
        PinDef(name="OUT", number="3", type="output"),
    ],
)
sexp = generate_symbol_sexp(symbol)
```

---

## Cross-Reference API

Validate schematic-PCB consistency:

```python
from kicad_tools import Project

project = Project.load("my_board.kicad_pro")

# Simple cross-reference
result = project.cross_reference()

print(f"Matched components: {result.matched}")

# Symbols without footprints on PCB
for sym in result.unplaced:
    print(f"Unplaced: {sym.reference} ({sym.value})")
    print(f"  Expected footprint: {sym.footprint_name}")

# Footprints without symbols in schematic
for fp in result.orphaned:
    print(f"Orphaned: {fp.reference} at {fp.position}")

# Value/footprint mismatches
for mismatch in result.mismatched:
    print(f"Mismatch: {mismatch.reference}")
    print(f"  Schematic: {mismatch.schematic_value}")
    print(f"  PCB: {mismatch.pcb_value}")
```

For detailed netlist validation:

```python
from kicad_tools.validate import NetlistValidator

validator = NetlistValidator(
    schematic="design.kicad_sch",
    pcb="design.kicad_pcb",
)

result = validator.validate()

print(result.summary())
# Output:
# Netlist OUT OF SYNC: 2 errors, 1 warnings
#   Missing on PCB: 2
#   Net mismatches: 1

for issue in result.errors:
    print(f"{issue.severity}: {issue.message}")
    print(f"  Fix: {issue.suggestion}")
```

---

## CLI Commands

Common workflow commands:

```bash
# List symbols in schematic
kct symbols design.kicad_sch

# Generate BOM
kct bom design.kicad_sch --format csv -o bom.csv

# Route PCB
kct route design.kicad_pcb -o routed.kicad_pcb

# Check DRC against manufacturer
kct drc design-drc.rpt --mfr jlcpcb

# Export Gerbers
kct export gerbers design.kicad_pcb -o gerbers/ --mfr jlcpcb

# Export assembly package
kct export assembly design.kicad_pcb -o assembly/ --mfr jlcpcb
```

---

## FAQ

### How do I create a PCB from a schematic?

KiCad's "Update PCB from Schematic" is required. kicad-tools cannot directly transfer components - this is intentional to maintain netlist integrity.

```bash
# After designing schematic:
kicad-cli sch export netlist design.kicad_sch -o design.net
kicad-cli pcb update design.kicad_pcb --netlist design.net
```

### What's the difference between PCBEditor and the schema.PCB class?

- `schema.PCB`: For **reading** and **analyzing** PCB files
- `pcb.PCBEditor`: For **modifying** PCB files (add tracks, move components)

### Can I programmatically place components?

Yes, use `PCBEditor.place_component()` or `PlacementSession`:

```python
# Direct placement
editor = PCBEditor("board.kicad_pcb")
editor.place_component("R1", x=50, y=30)

# With optimization feedback
session = PlacementSession(pcb)
result = session.query_move("R1", 50, 30)
if result.is_valid:
    session.apply_move("R1", 50, 30)
```

### How do I handle power nets?

Skip power nets during routing (they use copper pours):

```python
result = project.route(skip_nets=["GND", "VCC", "+3.3V", "+5V"])

# Add ground pour
editor = PCBEditor("board.kicad_pcb")
editor.create_ground_pour(layer="B.Cu")
editor.save()
```

### How do I validate my design before manufacturing?

```python
# 1. Check schematic-PCB sync
sync = project.check_sync()
assert sync.in_sync, "Schematic and PCB out of sync!"

# 2. Run KiCad DRC
subprocess.run(["kicad-cli", "pcb", "drc", "board.kicad_pcb", "-o", "drc.rpt"])

# 3. Check manufacturer compatibility
checks = project.check_drc(manufacturer="jlcpcb", layers=2)
failures = [c for c in checks if not c.is_compatible]
assert not failures, f"DRC failures: {failures}"
```

---

## See Also

- [Getting Started](../getting-started.md) - Installation and basics
- [Routing Guide](routing.md) - Detailed routing options
- [Placement Optimization](placement-optimization.md) - Intelligent placement
- [Manufacturing Export](manufacturing-export.md) - Gerber and assembly export
- [DRC & Validation](drc-and-validation.md) - Design rule checking

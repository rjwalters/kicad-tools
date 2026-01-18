# Complete KCT Workflow Example

This example demonstrates the **complete end-to-end workflow** from a `.kct` project specification to manufacturing-ready files. It shows how AI agents can use kicad-tools to create manufacturable PCBs without human intervention.

## Overview

The workflow consists of three steps:

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   project.kct   │───▶│  generate_      │───▶│     output/     │
│  (specification)│    │  design.py      │    │  .kicad_sch     │
│                 │    │                 │    │  .kicad_pcb     │
└─────────────────┘    └─────────────────┘    └────────┬────────┘
                                                       │
                       ┌─────────────────┐             │
                       │     export_     │◀────────────┘
                       │  manufacturing  │
                       │      .py        │
                       └────────┬────────┘
                                │
                       ┌────────▼────────┐
                       │    gerbers/     │
                       │    bom.csv      │
                       │    cpl.csv      │
                       └─────────────────┘
```

## Quick Start

### Option 1: Automated Build (Recommended)

```bash
# One command builds everything
kct build .

# Preview what would happen
kct build . --dry-run

# Rebuild from scratch
kct build . --force
```

### Option 2: Manual Steps

```bash
# Step 1: Generate schematic and PCB
python generate_design.py

# Step 2: Export manufacturing files
python export_manufacturing.py
```

### Option 3: Using uv

```bash
# From repository root
uv run python examples/10-complete-kct-workflow/generate_design.py
uv run python examples/10-complete-kct-workflow/export_manufacturing.py
```

## Files

| File | Description |
|------|-------------|
| `project.kct` | Project specification with requirements and decisions |
| `generate_design.py` | Creates schematic, PCB, and routes the board |
| `export_manufacturing.py` | Exports Gerbers, BOM, and pick-and-place files |
| `output/` | Generated KiCad files |
| `output/jlcpcb/` | Manufacturing files ready for upload |

## The Circuit

A simple LED indicator demonstrating the complete workflow:

```
    +5V ─────────┬─────────── J1.1 (Input)
                 │
                [R1]
                330Ω
                 │
                 ▼
               [D1]
                LED
                 │
    GND ─────────┴─────────── J1.2 (Ground)
```

**Components:**
- J1: 2-pin input connector (VIN, GND)
- R1: 330Ω current limiting resistor (0805 SMD)
- D1: LED indicator (0805 SMD)

**Specifications:**
- Input: 5V DC
- LED current: ~10mA
- Board size: 25mm × 20mm
- Layers: 2

## Understanding the .kct File

The `project.kct` file captures everything about the design:

```yaml
# Project metadata
project:
  name: "LED Indicator"
  revision: "A"
  artifacts:
    schematic: "output/led_indicator.kicad_sch"
    pcb: "output/led_indicator.kicad_pcb"

# Design intent - the "why"
intent:
  summary: "A simple LED indicator circuit..."
  interfaces:
    - name: VIN
      type: power_rail
      voltage: "5V"

# Requirements - the constraints
requirements:
  electrical:
    led_current:
      nominal: "10mA"
  manufacturing:
    target_fab: jlcpcb
    min_trace: "0.3mm"

# Design decisions - the reasoning
decisions:
  - topic: "Resistor Value"
    choice: "330 ohm"
    rationale: "R = (5V - 2V) / 10mA = 300Ω..."
```

This specification enables:
- **AI agents** to understand design requirements
- **Build automation** to select correct parameters
- **Traceability** of design decisions
- **Validation** against requirements

## Workflow Details

### Step 1: Design Generation

`generate_design.py` performs:

1. **Load spec** - Reads `project.kct` for requirements
2. **Create schematic** - Adds symbols, wires, and net labels
3. **Create PCB** - Places footprints on a 25mm × 20mm board
4. **Route traces** - Uses autorouter with DRC-compliant settings
5. **Verify design** - Runs DRC to check for errors

```python
from kicad_tools.spec import load_spec
from kicad_tools.schematic.models.schematic import Schematic
from kicad_tools.router import DesignRules, load_pcb_for_routing

# Load project specification
spec = load_spec("project.kct")

# Create schematic programmatically
sch = Schematic(title="LED Indicator")
sch.add_symbol("Device:R", x=100, y=50, ref="R1", value="330")
sch.add_symbol("Device:LED", x=140, y=65, ref="D1", value="LED")
# ... add wires and connections
sch.write("output/led_indicator.kicad_sch")

# Route PCB
rules = DesignRules(
    trace_width=0.3,  # From spec.requirements.manufacturing.min_trace
    trace_clearance=0.2,
)
router, _ = load_pcb_for_routing("output/led_indicator.kicad_pcb", rules=rules)
router.route_all()
```

### Step 2: Manufacturing Export

`export_manufacturing.py` generates:

1. **Gerber files** - Copper layers, masks, silkscreen, drill
2. **BOM** - Component list in JLCPCB format
3. **Pick-and-Place** - Component positions for assembly

```python
from kicad_tools.export import AssemblyPackage

# One-liner: Create complete assembly package
pkg = AssemblyPackage.create(
    pcb="output/led_indicator_routed.kicad_pcb",
    schematic="output/led_indicator.kicad_sch",
    manufacturer="jlcpcb",
)
result = pkg.export("output/jlcpcb/")
print(f"Gerbers: {result.gerber_path}")
print(f"BOM: {result.bom_path}")
print(f"CPL: {result.pnp_path}")
```

## Output Files

After running both scripts:

```
output/
├── led_indicator.kicad_pro      # KiCad project file
├── led_indicator.kicad_sch      # Schematic
├── led_indicator.kicad_pcb      # PCB (unrouted)
├── led_indicator_routed.kicad_pcb  # PCB (routed)
└── jlcpcb/
    ├── quick_export/
    │   ├── gerbers.zip          # Ready for upload
    │   ├── bom.csv              # Bill of materials
    │   └── cpl.csv              # Pick-and-place
    └── individual/
        ├── gerbers/             # Individual Gerber files
        ├── bom.csv
        └── cpl.csv
```

## CLI Commands

The workflow can also be executed via CLI:

```bash
# Build from specification
kct build .

# Or individual steps
kct build . --step schematic
kct build . --step pcb
kct build . --step route
kct build . --step verify

# Export for specific manufacturer
kct export --manufacturer jlcpcb \
    output/led_indicator_routed.kicad_pcb \
    output/led_indicator.kicad_sch \
    -o output/jlcpcb/

# Generate BOM only
kct bom output/led_indicator.kicad_sch --format jlcpcb

# Check design rules
kct check output/led_indicator_routed.kicad_pcb --mfr jlcpcb
```

## For AI Agents

This example demonstrates capabilities useful for AI-driven hardware design:

### Reading Specifications

```python
from kicad_tools.spec import load_spec

spec = load_spec("project.kct")

# Access requirements
trace_width = spec.requirements.manufacturing.min_trace  # "0.3mm"
target_fab = spec.requirements.manufacturing.target_fab   # "jlcpcb"

# Access design decisions
for decision in spec.decisions:
    print(f"{decision.topic}: {decision.choice}")
    print(f"  Rationale: {decision.rationale}")
```

### Programmatic Design

```python
from kicad_tools.schematic.models.schematic import Schematic
from kicad_tools.schema.pcb import PCB

# Create schematic from spec
sch = Schematic(title=spec.project.name)
sch.add_symbol("Device:R", x=100, y=50, ref="R1", value="330")
sch.write("output/design.kicad_sch")

# Load and modify PCB
pcb = PCB.load("output/design.kicad_pcb")
for fp in pcb.footprints:
    print(f"{fp.reference}: {fp.library}")
```

### Manufacturing Validation

```python
from kicad_tools.manufacturers import get_profile, find_compatible_manufacturers

# Check against manufacturer rules
profile = get_profile("jlcpcb")
rules = profile.get_design_rules(layers=2)

# Find compatible manufacturers for design
compatible = find_compatible_manufacturers(
    trace_width_mm=0.3,
    clearance_mm=0.2,
    via_drill_mm=0.3,
    layers=2,
    needs_assembly=True,
)
```

## Ordering

After generating files:

1. Go to [JLCPCB](https://jlcpcb.com)
2. Upload `output/jlcpcb/quick_export/gerbers.zip`
3. Enable "SMT Assembly" if needed
4. Upload `bom.csv` and `cpl.csv`
5. Review quote and place order

## Related Examples

- [01-schematic-analysis](../01-schematic-analysis/) - Schematic parsing basics
- [05-schematic-to-pcb-workflow](../05-schematic-to-pcb-workflow/) - Project class usage
- [09-manufacturing-export](../09-manufacturing-export/) - Detailed export options

## Further Reading

- [API Documentation](../../docs/)
- [CLI Reference](../../README.md#command-line-usage)
- [Project Specification Format](../../docs/spec-format.md)

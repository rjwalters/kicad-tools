# Getting Started with kicad-tools

This guide will help you install kicad-tools and get started with your first KiCad file analysis.

## Prerequisites

- **Python 3.10 or higher** - kicad-tools uses modern Python features
- **KiCad 8+** (optional) - Only needed for ERC/DRC commands that invoke `kicad-cli`

## Installation

### From PyPI (Recommended)

```bash
pip install kicad-tools
```

### From Source (Development)

```bash
git clone https://github.com/rjwalters/kicad-tools.git
cd kicad-tools
pip install -e ".[dev]"
```

### Verify Installation

```bash
# Check CLI is available
kct --version

# Or use the full command name
kicad-tools --version
```

## Your First Analysis

Let's analyze a KiCad schematic file. If you don't have one handy, you can use any `.kicad_sch` file from your projects.

### 1. List Symbols in a Schematic

The `symbols` command lists all component symbols in your schematic:

```bash
kct symbols your_project.kicad_sch
```

Example output:
```
C1: 100nF
C2: 100nF
R1: 10k
R2: 10k
U1: ATmega328P
```

### 2. Get JSON Output for Scripting

Add `--format json` for machine-readable output:

```bash
kct symbols your_project.kicad_sch --format json
```

```json
[
  {"reference": "C1", "value": "100nF", "lib_id": "Device:C"},
  {"reference": "R1", "value": "10k", "lib_id": "Device:R"},
  ...
]
```

### 3. Trace Nets

See which components are connected:

```bash
# List all nets
kct nets your_project.kicad_sch

# Trace a specific net
kct nets your_project.kicad_sch --net VCC
```

### 4. Generate a BOM

Create a bill of materials:

```bash
# Human-readable output
kct bom your_project.kicad_sch

# CSV for spreadsheets
kct bom your_project.kicad_sch --format csv

# Grouped by value (fewer rows, with quantities)
kct bom your_project.kicad_sch --format csv --group
```

## Using the Python API

For more complex analysis, use kicad-tools as a Python library:

```python
from kicad_tools import Schematic

# Load a schematic
sch = Schematic.load("your_project.kicad_sch")

# List all symbols
for symbol in sch.symbols:
    print(f"{symbol.reference}: {symbol.value}")

# Find specific components
caps = sch.symbols.filter(reference__startswith="C")
print(f"Found {len(caps)} capacitors")

# Get a specific component
u1 = sch.symbols.by_reference("U1")
if u1:
    print(f"U1 value: {u1.value}")
    print(f"U1 library: {u1.lib_id}")
```

## Working with PCB Files

kicad-tools also handles `.kicad_pcb` files:

```python
from kicad_tools import PCB

# Load a PCB
pcb = PCB.load("your_project.kicad_pcb")

# List footprints
for fp in pcb.footprints:
    print(f"{fp.reference}: {fp.footprint}")

# Get board statistics
print(f"Layers: {len(pcb.layers)}")
print(f"Nets: {len(pcb.nets)}")
```

## Next Steps

Now that you have kicad-tools installed, explore these guides:

- **[Schematic Analysis](guides/schematic-analysis.md)** - Deep dive into symbols, nets, and BOM generation
- **[DRC & Validation](guides/drc-and-validation.md)** - Validate designs against fabrication limits
- **[Manufacturing Export](guides/manufacturing-export.md)** - Export for JLCPCB and other fabs
- **[Query API](guides/query-api.md)** - Advanced filtering and analysis
- **[Placement Optimization](guides/placement-optimization.md)** - Optimize component placement
- **[Routing](guides/routing.md)** - Autoroute PCBs with the A* router

See the [Documentation Index](index.md) for the complete list.

## Getting Help

```bash
# General help
kct --help

# Command-specific help
kct symbols --help
kct drc --help
```

For issues and feature requests, visit the [GitHub repository](https://github.com/rjwalters/kicad-tools).

# kicad-tools

[![PyPI version](https://badge.fury.io/py/kicad-tools.svg)](https://pypi.org/project/kicad-tools/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Standalone Python tools for parsing and manipulating KiCad schematic and PCB files.

**No running KiCad instance required** - works directly with `.kicad_sch` and `.kicad_pcb` files.

## Installation

```bash
pip install kicad-tools
```

## Quick Start

### Command Line (`kct`)

```bash
# List symbols in a schematic
kct symbols project.kicad_sch
kct symbols project.kicad_sch --format json

# Trace nets
kct nets project.kicad_sch
kct nets project.kicad_sch --net VCC

# Generate bill of materials
kct bom project.kicad_sch
kct bom project.kicad_sch --format csv --group

# Run ERC (requires kicad-cli)
kct erc project.kicad_sch
kct erc project.kicad_sch --strict

# Run DRC with manufacturer rules
kct drc board.kicad_pcb
kct drc board.kicad_pcb --mfr jlcpcb
kct drc --compare  # Compare manufacturer rules
```

### Python API

```python
from kicad_tools import load_schematic, Schematic

# Load and parse a schematic
doc = load_schematic("project.kicad_sch")
sch = Schematic(doc)

# Access symbols
for symbol in sch.symbols:
    print(f"{symbol.reference}: {symbol.value}")

# Access hierarchy
for sheet in sch.sheets:
    print(f"Sheet: {sheet.name}")
```

### PCB Autorouter

```python
from kicad_tools.router import Autorouter, DesignRules

# Configure design rules
rules = DesignRules(
    grid_resolution=0.25,  # mm
    trace_width=0.2,       # mm
    clearance=0.15,        # mm
)

# Create router and add components
router = Autorouter(width=100, height=80, rules=rules)
router.add_component("U1", pads=[...])

# Route all nets
result = router.route_all()
print(f"Routed {result.routed_nets}/{result.total_nets} nets")
```

## CLI Commands

### Unified CLI (`kct` or `kicad-tools`)

| Command | Description |
|---------|-------------|
| `kct symbols <schematic>` | List symbols with filtering |
| `kct nets <schematic>` | Trace and analyze nets |
| `kct bom <schematic>` | Generate bill of materials |
| `kct erc <schematic>` | Run electrical rules check |
| `kct drc <pcb>` | Run design rules check |

All commands support `--format json` for machine-readable output.

### PCB Tools

| Command | Description |
|---------|-------------|
| `kicad-pcb-query summary` | Board overview |
| `kicad-pcb-query footprints` | List footprints |
| `kicad-pcb-query nets` | List all nets |
| `kicad-pcb-query traces` | Trace statistics |
| `kicad-pcb-modify move` | Move component |
| `kicad-pcb-modify rotate` | Rotate component |

### Library Tools

| Command | Description |
|---------|-------------|
| `kicad-lib-symbols` | List symbols in library |

## Modules

| Module | Description |
|--------|-------------|
| `core` | S-expression parsing and file I/O |
| `schema` | Data models (Schematic, PCB, Symbol, Wire, Label) |
| `drc` | Design Rule Check report parsing |
| `erc` | Electrical Rule Check report parsing |
| `manufacturers` | PCB fab profiles (JLCPCB, OSHPark, PCBWay, Seeed) |
| `operations` | Schematic operations (net tracing, symbol replacement) |
| `router` | A* PCB autorouter with pluggable heuristics |

## Features

- **Pure Python parsing** - No KiCad installation needed
- **Round-trip editing** - Parse, modify, and save files preserving formatting
- **Full S-expression support** - Handles all KiCad 8.0+ file formats
- **Schematic analysis** - Symbols, wires, labels, hierarchy traversal
- **PCB analysis** - Footprints, nets, traces, vias, zones
- **Manufacturer rules** - JLCPCB, PCBWay, OSHPark, Seeed design rules
- **PCB autorouter** - A* pathfinding with net class awareness
- **JSON output** - Machine-readable output for automation

## Requirements

- Python 3.10+
- numpy (for router module)
- KiCad 8+ (optional) - for running ERC/DRC via `kicad-cli`

## Development

```bash
# Clone repository
git clone https://github.com/rjwalters/kicad-tools.git
cd kicad-tools

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run linter
ruff check src/

# Format code
ruff format src/
```

## License

MIT

# kicad-tools

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

## Features

- **Pure Python** - No KiCad installation needed for parsing
- **Round-trip editing** - Parse, modify, and save files
- **Full S-expression support** - Handles all KiCad file formats
- **Schematic analysis** - Symbols, wires, labels, hierarchy
- **PCB analysis** - Footprints, nets, traces, vias, zones
- **Manufacturer rules** - JLCPCB, PCBWay, OSHPark, Seeed design rules
- **JSON output** - Machine-readable output for automation

## Requirements

- Python 3.10+
- No external dependencies for parsing
- KiCad 8+ (optional) - for running ERC/DRC via `kicad-cli`

## License

MIT

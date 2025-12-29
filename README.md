# kicad-tools

Standalone Python tools for parsing and manipulating KiCad schematic and PCB files.

**No running KiCad instance required** - works directly with `.kicad_sch` and `.kicad_pcb` files.

## Installation

```bash
pip install kicad-tools
```

## Quick Start

### Command Line

```bash
# Schematic overview
kicad-sch-summary project.kicad_sch

# List all symbols
kicad-sch-symbols project.kicad_sch

# PCB overview
kicad-pcb-query board.kicad_pcb summary

# List footprints
kicad-pcb-query board.kicad_pcb footprints --sorted
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

## CLI Tools

### Schematic Tools

| Command | Description |
|---------|-------------|
| `kicad-sch-summary` | Quick schematic overview |
| `kicad-sch-symbols` | List all symbols |
| `kicad-sch-labels` | List all labels |
| `kicad-sch-wires` | List wires and connections |
| `kicad-sch-hierarchy` | Show sheet hierarchy |
| `kicad-sch-symbol-info` | Detailed symbol info |
| `kicad-sch-pin-positions` | Symbol pin locations |
| `kicad-sch-trace-nets` | Trace net connections |
| `kicad-sch-check-connections` | Check pin connectivity |
| `kicad-sch-find-unconnected` | Find unconnected pins |
| `kicad-sch-validate` | Validate schematic |
| `kicad-sch-replace-symbol` | Replace symbol lib_id |
| `kicad-sch-bom` | Generate bill of materials |
| `kicad-sch-erc` | Run electrical rules check |

### PCB Tools

| Command | Description |
|---------|-------------|
| `kicad-pcb-query summary` | Board overview |
| `kicad-pcb-query footprints` | List footprints |
| `kicad-pcb-query footprint <ref>` | Footprint details |
| `kicad-pcb-query nets` | List all nets |
| `kicad-pcb-query net <name>` | Net details |
| `kicad-pcb-query traces` | Trace statistics |
| `kicad-pcb-query vias` | Via summary |
| `kicad-pcb-query stackup` | Layer stackup |
| `kicad-pcb-modify move` | Move component |
| `kicad-pcb-modify rotate` | Rotate component |
| `kicad-pcb-modify flip` | Flip to opposite layer |
| `kicad-pcb-modify update-value` | Update component value |
| `kicad-pcb-modify rename` | Rename reference |
| `kicad-pcb-modify delete-traces` | Delete net traces |

### Library Tools

| Command | Description |
|---------|-------------|
| `kicad-lib-symbols` | List symbols in library |

## Features

- **Pure Python** - No KiCad installation needed
- **Round-trip editing** - Parse, modify, and save files
- **Full S-expression support** - Handles all KiCad file formats
- **Schematic analysis** - Symbols, wires, labels, hierarchy
- **PCB analysis** - Footprints, nets, traces, vias, zones
- **Modification tools** - Move, rotate, update components

## Requirements

- Python 3.10+
- No external dependencies

## License

MIT

# kicad-tools Documentation

**Tools for AI agents to work with KiCad projects.**

kicad-tools provides standalone Python tools that enable AI agents to parse, analyze, and manipulate KiCad schematic and PCB files programmatically.

---

## Quick Navigation

### Getting Started
- [Installation & Quick Start](getting-started.md) - Install and run your first command
- [Architecture Overview](architecture.md) - Understand how the modules fit together

### Guides
Step-by-step guides for common tasks:

| Guide | Description |
|-------|-------------|
| [Schematic Analysis](guides/schematic-analysis.md) | Parse schematics, query symbols, trace nets |
| [DRC & Validation](guides/drc-and-validation.md) | Run design rule checks with manufacturer rules |
| [Manufacturing Export](guides/manufacturing-export.md) | Generate Gerbers, BOM, pick-and-place files |
| [Query API](guides/query-api.md) | Fluent interface for finding components |
| [Placement Optimization](guides/placement-optimization.md) | Optimize component placement |
| [Routing](guides/routing.md) | Autoroute PCBs with the A* router |

### Reference
Detailed reference documentation:

| Reference | Description |
|-----------|-------------|
| [CLI Commands](reference/cli.md) | Complete command-line reference |
| [Python API](reference/api.md) | Module-by-module API documentation |
| [Manufacturer Rules](reference/manufacturers.md) | JLCPCB, OSHPark, PCBWay design rules |
| [Circuit Blocks](reference/circuit-blocks.md) | Reusable schematic building blocks |

### For Contributors
- [Development Guide](contributing/development.md) - Setup, testing, code style
- [Architecture Deep Dive](contributing/architecture-internals.md) - Internal design decisions

---

## Module Overview

```
kicad_tools/
├── core/          # S-expression parsing, file I/O
├── schema/        # Data models (Schematic, PCB, Symbol)
├── query/         # Fluent query API
├── schematic/     # Schematic operations and circuit blocks
├── pcb/           # PCB operations
├── router/        # A* autorouter with diff pairs
├── optim/         # Placement optimization
├── drc/           # Pure Python design rule checking
├── erc/           # Electrical rule checking
├── manufacturers/ # Manufacturer design rules
├── parts/         # LCSC parts database
├── datasheet/     # PDF parsing, pin extraction
├── export/        # Manufacturing export
├── reasoning/     # LLM-driven PCB layout
└── cli/           # Command-line interface
```

---

## Installation

```bash
pip install kicad-tools
```

Verify installation:
```bash
kct --help
```

---

## Quick Examples

### Command Line
```bash
# List symbols in a schematic
kct symbols project.kicad_sch

# Run DRC with JLCPCB rules
kct drc board.kicad_pcb --mfr jlcpcb

# Generate BOM as CSV
kct bom project.kicad_sch --format csv --group

# Autoroute a PCB
kct route board.kicad_pcb --output routed.kicad_pcb
```

### Python API
```python
from kicad_tools import Schematic, PCB, Project

# Load and query schematic
sch = Schematic.load("project.kicad_sch")
caps = sch.symbols.filter(value="100nF")

# Load and query PCB
pcb = PCB.load("board.kicad_pcb")
smd = pcb.footprints.smd()

# Work with complete project
project = Project.load("project.kicad_pro")
project.export_assembly("output/", manufacturer="jlcpcb")
```

---

## Examples

Complete working examples are in the [`examples/`](https://github.com/rjwalters/kicad-tools/tree/main/examples) directory:

| Example | Description |
|---------|-------------|
| `01-schematic-analysis/` | Parse and analyze schematics |
| `02-bom-generation/` | Generate bills of materials |
| `03-drc-checking/` | Design rule checking |
| `04-autorouter/` | Automatic PCB routing |
| `05-end-to-end/` | Complete design workflow |
| `06-intelligent-placement/` | Smart component placement |
| `agent-integration/` | Integration with AI agents |
| `llm-routing/` | LLM-driven routing decisions |

---

## Version History

See [CHANGELOG.md](https://github.com/rjwalters/kicad-tools/blob/main/CHANGELOG.md) for release notes.

See [ROADMAP.md](https://github.com/rjwalters/kicad-tools/blob/main/ROADMAP.md) for planned features.

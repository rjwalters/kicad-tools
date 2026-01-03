# kicad-tools

[![PyPI version](https://badge.fury.io/py/kicad-tools.svg)](https://pypi.org/project/kicad-tools/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Tools for AI agents to work with KiCad projects.**

This project provides standalone Python tools that enable AI agents (LLMs, autonomous coding assistants, etc.) to parse, analyze, and manipulate KiCad schematic and PCB files programmatically. All tools output machine-readable JSON and require no running KiCad instance.

## Why Agent-Focused?

Traditional EDA tools require GUIs and manual interaction. `kicad-tools` bridges the gap by providing:

- **Structured data access** - Parse KiCad files into clean Python objects
- **Machine-readable output** - All CLI commands support `--format json`
- **Programmatic modification** - Edit schematics and PCBs without a GUI
- **LLM reasoning interface** - Purpose-built module for LLM-driven PCB layout decisions

Whether you're building an AI assistant that reviews PCB designs, automating DRC checks in CI, or experimenting with LLM-driven routing, these tools provide the foundation.

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

### LLM-Driven PCB Layout

The reasoning module enables LLMs to make strategic PCB layout decisions while tools handle geometric execution:

```python
from kicad_tools import PCBReasoningAgent

# Load board
agent = PCBReasoningAgent.from_pcb("board.kicad_pcb")

# Reasoning loop
while not agent.is_complete():
    # Get state as prompt for LLM
    prompt = agent.get_prompt()

    # Call your LLM (OpenAI, Anthropic, local, etc.)
    command = call_llm(prompt)

    # Execute and get feedback
    result, diagnosis = agent.execute(command)

# Save result
agent.save("board_routed.kicad_pcb")
```

CLI usage:
```bash
# Export state for external LLM
kct reason board.kicad_pcb --export-state

# Interactive mode
kct reason board.kicad_pcb --interactive

# Auto-route priority nets
kct reason board.kicad_pcb --auto-route
```

See `examples/llm-routing/` for complete examples.

### Circuit Blocks

Build schematics using reusable, tested circuit blocks:

```python
from kicad_tools.schematic import Schematic
from kicad_tools.schematic.blocks import (
    MCUBlock, CrystalOscillator, LDOBlock, USBConnector,
    DebugHeader, I2CPullups, ResetButton
)

sch = Schematic.create("project.kicad_sch")

# Add an MCU with bypass capacitors
mcu = MCUBlock(sch, x=150, y=100,
    part="STM32F103C8T6",
    bypass_caps=["100nF", "100nF", "100nF", "4.7uF"])

# Add crystal oscillator
xtal = CrystalOscillator(sch, x=100, y=100,
    frequency="8MHz", load_caps="20pF")

# Add power supply
ldo = LDOBlock(sch, x=50, y=100,
    input_voltage=5.0, output_voltage=3.3)

# Add USB connector with ESD protection
usb = USBConnector(sch, x=50, y=150,
    connector_type="type-c", esd_protection=True)

# Add debug header for programming
debug = DebugHeader(sch, x=200, y=100, interface="swd")

# Add I2C pull-ups
i2c = I2CPullups(sch, x=180, y=150, pullup_value="4.7k")

# Add reset button with debounce
reset = ResetButton(sch, x=120, y=50, debounce_cap="100nF")

# Connect via ports
sch.add_wire(ldo.port("VOUT"), mcu.port("VDD"))
sch.add_wire(xtal.port("OUT"), mcu.port("OSC_IN"))

sch.save()
```

Available blocks:
- **MCUBlock**: Microcontroller with bypass capacitors
- **CrystalOscillator**: Crystal/oscillator with load capacitors
- **LDOBlock**: Linear regulator with input/output capacitors
- **USBConnector**: USB-B/Mini/Micro/Type-C with optional ESD protection
- **DebugHeader**: SWD/JTAG/Tag-Connect programming headers
- **I2CPullups**: I2C bus pull-up resistors with optional filtering
- **ResetButton**: Reset switch with debounce capacitor
- **BarrelJackInput/USBPowerInput/BatteryInput**: Power input circuits
- **LEDIndicator**: Status LED with current-limiting resistor
- **DecouplingCaps**: Decoupling capacitor placement

See `examples/05-end-to-end/` for a complete design example.

### Project Workflow

Work with complete KiCad projects using the unified Project class:

```python
from kicad_tools import Project

# Load a KiCad project
project = Project.load("myboard.kicad_pro")

# Cross-reference schematic to PCB
result = project.cross_reference()
print(f"Unplaced components: {result.unplaced}")

# Export manufacturing files
project.export_assembly("output/", manufacturer="jlcpcb")
```

### Progress Callbacks

Monitor long-running operations with progress callbacks:

```python
from kicad_tools import ProgressCallback, ProgressContext
from kicad_tools.router import Autorouter

def on_progress(progress: float, message: str, cancelable: bool) -> bool:
    print(f"{progress*100:.0f}%: {message}")
    return True  # Return False to cancel

# Use with context manager
with ProgressContext(on_progress):
    router = Autorouter(...)
    router.route_all()  # Progress reported automatically

# Or create JSON-formatted callbacks for automation
from kicad_tools import create_json_callback
callback = create_json_callback()
```

### Parametric Footprint Generators

Create KiCad footprints programmatically with IPC-7351 naming:

```python
from kicad_tools.library import create_soic, create_qfp, create_chip

# Generate SOIC-8 footprint
fp = create_soic(pins=8, pitch=1.27)
fp.save("SOIC-8.kicad_mod")

# Generate LQFP-48
fp = create_qfp(pins=48, pitch=0.5, body_size=7.0)
fp.save("LQFP-48.kicad_mod")

# Generate 0402 chip resistor
fp = create_chip("0402", prefix="R")
fp.save("R_0402.kicad_mod")
```

Available generators: `create_soic`, `create_qfp`, `create_qfn`, `create_sot`, `create_chip`, `create_dip`, `create_pin_header`.

### Symbol Library Management

Create and edit KiCad symbol libraries programmatically:

```python
from kicad_tools.schema.library import SymbolLibrary

# Create a new symbol library
lib = SymbolLibrary.create("myproject.kicad_sym")

# Create a symbol with pins
sym = lib.create_symbol("MyPart")
sym.add_pin("1", "VCC", "power_in", (0, 5.08))
sym.add_pin("2", "GND", "power_in", (0, -5.08))
sym.add_pin("3", "IN", "input", (-7.62, 0))
sym.add_pin("4", "OUT", "output", (7.62, 0))

# Save the library
lib.save()

# Load and edit existing library
lib = SymbolLibrary.load("existing.kicad_sym")
```

### Pure Python DRC

Run design rule checks without requiring kicad-cli:

```bash
# Check against manufacturer rules
kct check board.kicad_pcb --mfr jlcpcb --format json

# Check with custom rules
kct check board.kicad_pcb --clearance 0.15 --trace-width 0.2
```

Python API:

```python
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker

pcb = PCB.load("board.kicad_pcb")
checker = DRCChecker(pcb, manufacturer="jlcpcb")
results = checker.check_all()

print(results.summary())
for violation in results:
    print(f"  {violation.rule_id}: {violation.message}")
```

### Placement Optimization

Optimize component placement using physics-based or evolutionary algorithms:

```python
from kicad_tools.optim import PlacementOptimizer, EvolutionaryPlacementOptimizer
from kicad_tools.schema.pcb import PCB

pcb = PCB.load("board.kicad_pcb")

# Physics-based optimization (force-directed)
optimizer = PlacementOptimizer.from_pcb(pcb)
optimizer.run(iterations=1000, dt=0.01)

# Get optimized placements
for comp in optimizer.components:
    print(f"{comp.ref}: ({comp.x:.2f}, {comp.y:.2f}) @ {comp.rotation:.1f}°")

# Evolutionary optimization (genetic algorithm)
evo = EvolutionaryPlacementOptimizer.from_pcb(pcb)
best = evo.optimize(generations=100, population_size=50)

# Hybrid: evolutionary global search + physics refinement
physics_opt = evo.optimize_hybrid(generations=50)
physics_opt.write_to_pcb(pcb)
pcb.save("optimized.kicad_pcb")
```

CLI usage:
```bash
kct placement board.kicad_pcb --optimize --iterations 1000
```

### Trace Optimization

Optimize routed traces for shorter paths and fewer vias:

```python
from kicad_tools.router import TraceOptimizer
from kicad_tools.schema.pcb import PCB

pcb = PCB.load("board.kicad_pcb")
optimizer = TraceOptimizer(pcb)
optimizer.optimize()
pcb.save("optimized.kicad_pcb")
```

CLI usage:
```bash
kct optimize-traces board.kicad_pcb -o optimized.kicad_pcb
```

### Datasheet Tools

Search, download, and parse component datasheets:

```bash
# Search for datasheets
kct datasheet search STM32F103C8T6

# Download a datasheet
kct datasheet download STM32F103C8T6 -o datasheets/

# Convert PDF to markdown
kct datasheet convert datasheet.pdf -o datasheet.md

# Extract pin tables
kct datasheet extract-pins datasheet.pdf

# Extract images and tables
kct datasheet extract-images datasheet.pdf -o images/
kct datasheet extract-tables datasheet.pdf
```

Python API:

```python
from kicad_tools.datasheet import DatasheetManager, DatasheetParser

# Search and download
manager = DatasheetManager()
results = manager.search("STM32F103C8T6")
datasheet = manager.download(results[0])

# Parse PDF
parser = DatasheetParser("STM32F103.pdf")
markdown = parser.to_markdown()

# Extract images and tables
images = parser.extract_images()
tables = parser.extract_tables()
for table in tables:
    print(table.to_markdown())
```

## CLI Commands

### Unified CLI (`kct` or `kicad-tools`)

| Command | Description |
|---------|-------------|
| `kct symbols <schematic>` | List symbols with filtering |
| `kct nets <schematic>` | Trace and analyze nets |
| `kct bom <schematic>` | Generate bill of materials |
| `kct erc <schematic>` | Run electrical rules check |
| `kct drc <pcb>` | Run design rules check (requires kicad-cli) |
| `kct check <pcb>` | Pure Python DRC (no kicad-cli needed) |
| `kct route <pcb>` | Autoroute a PCB |
| `kct reason <pcb>` | LLM-driven PCB layout reasoning |
| `kct placement <pcb>` | Detect and optimize component placement |
| `kct optimize-traces <pcb>` | Optimize routed traces |
| `kct datasheet <subcommand>` | Search, download, parse datasheets |

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
| `schematic.blocks` | Reusable circuit blocks (MCU, LDO, USB, debug headers, etc.) |
| `project` | Unified Project class for schematic+PCB workflows |
| `library` | Footprint generation and symbol library management |
| `drc` | Design Rule Check report parsing (kicad-cli output) |
| `validate` | Pure Python DRC checker (no kicad-cli needed) |
| `erc` | Electrical Rule Check report parsing |
| `manufacturers` | PCB fab profiles (JLCPCB, OSHPark, PCBWay, Seeed) |
| `operations` | Schematic operations (net tracing, symbol replacement) |
| `router` | A* PCB autorouter with trace optimization |
| `optim` | Placement optimization (physics-based, evolutionary) |
| `reasoning` | LLM-driven PCB layout with chain-of-thought reasoning |
| `progress` | Progress callbacks for long-running operations |
| `datasheet` | Datasheet search, download, and PDF parsing |

## Features

- **Pure Python parsing** - No KiCad installation needed
- **Round-trip editing** - Parse, modify, and save files preserving formatting
- **Full S-expression support** - Handles all KiCad 8.0+ file formats
- **Schematic analysis** - Symbols, wires, labels, hierarchy traversal
- **Circuit blocks** - Reusable blocks for MCU, power, USB, debug headers, I2C, reset
- **PCB analysis** - Footprints, nets, traces, vias, zones
- **Manufacturer rules** - JLCPCB, PCBWay, OSHPark, Seeed design rules
- **PCB autorouter** - A* pathfinding with net class awareness
- **Pure Python DRC** - Design rule checking without kicad-cli
- **Placement optimization** - Physics-based and evolutionary algorithms
- **Trace optimization** - Path shortening and via reduction
- **Footprint generation** - Parametric generators for common packages
- **Symbol library creation** - Programmatic symbol creation and editing
- **Datasheet tools** - Search, download, and PDF parsing
- **Progress callbacks** - Monitor and cancel long-running operations
- **JSON output** - Machine-readable output for automation

## Requirements

- Python 3.10+
- numpy (for router module)
- KiCad 8+ (optional) - for running ERC/DRC via `kicad-cli`

## Development

This project uses [uv](https://docs.astral.sh/uv/) for fast, reproducible Python environment management.

### Quick Start

```bash
# Clone repository
git clone https://github.com/rjwalters/kicad-tools.git
cd kicad-tools

# Set up development environment (installs all dev dependencies)
uv sync --extra dev

# Run tests
uv run pytest

# Run linter
uv run ruff check .

# Format code
uv run ruff format .
```

### Available Commands

If you have `pnpm` installed, you can use these convenience scripts:

| Command | Description |
|---------|-------------|
| `pnpm setup` | Set up dev environment (`uv sync --extra dev`) |
| `pnpm test` | Run tests |
| `pnpm test:cov` | Run tests with coverage |
| `pnpm test:benchmark` | Run performance benchmarks |
| `pnpm lint` | Check code with ruff |
| `pnpm lint:fix` | Auto-fix lint issues |
| `pnpm format` | Format code with ruff |
| `pnpm format:check` | Check formatting |
| `pnpm typecheck` | Run mypy type checking |
| `pnpm check:ci` | Run full CI suite (format + lint + tests) |

### Direct uv Commands

```bash
# Run tests with coverage
uv run pytest --cov=kicad_tools --cov-report=term-missing

# Run benchmarks
uv run pytest tests/test_benchmarks.py --benchmark-only

# Type checking
uv run mypy src/

# Full CI check
uv run ruff format . --check && uv run ruff check . && uv run pytest
```

## License

MIT

# kicad-tools

[![PyPI version](https://badge.fury.io/py/kicad-tools.svg)](https://pypi.org/project/kicad-tools/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Tools for AI agents to work with KiCad projects.**

🌐 **Live demo gallery: [kicad-tools.org](https://kicad-tools.org)** — explore example boards built end-to-end by these tools, with 2D/3D renders, routing & manufacturing metrics, downloadable fabrication packages, and an interactive in-browser PCB viewer.

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
# Base install (CPU only)
pip install kicad-tools

# With CUDA GPU acceleration (NVIDIA GPUs on Linux/Windows)
pip install kicad-tools[cuda]

# With Metal GPU acceleration (Apple Silicon Macs)
pip install kicad-tools[metal]

# With native C++ router backend
pip install kicad-tools[native]

# With CMA-ES placement optimization (kct optimize-placement / kct build --optimize-placement)
pip install kicad-tools[placement]

# Everything (all optional dependencies)
pip install kicad-tools[all]
```

> The `placement` extra pulls in `cmaes` (and, transitively, `scipy`). It is
> required by `kct optimize-placement` and the placement step of
> `kct build --optimize-placement`; without it those commands exit with a
> clear message naming the extra. The `dev` and `all` extras already include
> it, so `uv sync --extra dev` is sufficient for development.

To check GPU acceleration status:
```bash
kct calibrate --show-gpu
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

### MCP Server for AI Agents

Enable AI assistants like Claude to interact with KiCad designs via the Model Context Protocol:

```bash
# Install with MCP support
pip install "kicad-tools[mcp]"

# Run the MCP server
kct mcp serve
```

Configure Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "kicad-tools": {
      "command": "python",
      "args": ["-m", "kicad_tools.mcp.server"]
    }
  }
}
```

Available MCP tools:
- **Analysis**: `analyze_board`, `get_drc_violations`, `measure_clearance`
- **Export**: `export_gerbers`, `export_bom`, `export_assembly`
- **Placement**: `placement_analyze`, `placement_suggestions`
- **Sessions**: `start_session`, `query_move`, `apply_move`, `commit`, `rollback`
- **Routing**: `route_net`, `route_net_auto`, `get_unrouted_nets`
- **Optimization**: `optimize_placement`, `evaluate_placement`

See `docs/mcp/` for complete documentation.

### Circuit Blocks

Build schematics using reusable, tested circuit blocks:

```python
from kicad_tools.schematic.models import Schematic
from kicad_tools.schematic.blocks import (
    MCUBlock, CrystalOscillator, LDOBlock, USBConnector,
    DebugHeader, I2CPullups, ResetButton
)

sch = Schematic("My STM32 Design")

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

See `boards/04-stm32-devboard/` for a complete design example.

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

### Parts Lookup (LCSC / JLCPCB)

Look up LCSC part numbers, check BOM availability, and pull pricing/stock:

```python
from kicad_tools.parts import LCSCClient

client = LCSCClient()
part = client.lookup("C2040")
if part:
    print(f"{part.mfr_part}: {part.stock} in stock, best ${part.best_price:.4f}")
```

Part lookups resolve through a tiered chain (each tier falls back to the next):

1. **Local response cache** — previously fetched parts.
2. **Official JLCPCB open-platform API** — only when you supply your own API key
   (see below). Off by default; inert without keys.
3. **Anonymous scrape API** — the public JLCPCB web endpoints (requires the
   `parts` extra: `pip install "kicad-tools[parts]"`). **Note:** as of
   July 2026 JLCPCB's public endpoint returns 404 — treat this tier as
   best-effort/deprecated and rely on tier 2 (BYO key) or tier 4 (offline).
4. **Offline jlcparts catalog** — a locally synced mirror
   (`kct parts sync-catalog`, ~620 MB download, ~5 GB on disk, ~7.1 M
   components), usable fully offline.

#### Using your own JLCPCB API key

kicad-tools can talk to the **official JLCPCB open-platform API** using
credentials you register yourself at the JLCPCB developer portal
(<https://jlcpcb.com/> → developer/open platform). This is strictly opt-in: you
bring your own key, kicad-tools ships only the signed client. **Without keys,
behavior is unchanged** — the official tier is simply skipped.

Set all three environment variables (kicad-tools reads them via `os.environ`;
it does **not** load a `.env` file itself, so use your shell, `direnv`, or a
dotenv runner — see [`.env.example`](.env.example)):

```bash
export JLCPCB_APP_ID="your-app-id"
export JLCPCB_ACCESS_KEY="your-access-key"
export JLCPCB_SECRET_KEY="your-secret-key"
```

All three must be set (and non-empty) to activate the official tier; if any is
missing it stays inert. Once set, `LCSCClient.lookup()` / `lookup_many()` prefer
the official API for part-detail-by-code, falling back down the chain above on
any failure.

Notes and caveats:

- **What it unlocks:** authenticated *part detail lookup by LCSC code* only.
  There is **no confirmed official keyword/MPN search endpoint**, so
  `LCSCClient.search()` always uses the anonymous/offline path even with keys.
- **IP whitelisting:** the developer portal offers an IP-whitelist feature. If
  you enable it, your machine's public IP must be whitelisted or requests are
  rejected (surfaced as a distinct, actionable error).
- **Never commit credentials.** `.env` is gitignored; the secret key is used
  only as HMAC key material and is never transmitted.
- The request-signing scheme is not first-party-documented by JLCPCB; the client
  implements the best-available community-reverse-engineered variant for the
  Parts surface, with the two ambiguous parameters isolated as single
  flip-points in `parts/jlcpcb_api.py` (see that module and issue #4118).

## CLI Commands

### Unified CLI (`kct` or `kicad-tools`)

The commands below are the ones most workflows reach for. The authoritative
full list (every command and subcommand) lives in
[CLI Reference → Commands Overview](docs/reference/cli.md#commands-overview).

**Inspection and validation**

| Command | Description |
|---------|-------------|
| `kct symbols <schematic>` | List symbols with filtering |
| `kct nets <schematic>` | Trace and analyze nets |
| `kct bom <schematic>` | Generate bill of materials |
| `kct erc <schematic>` | Run electrical rules check |
| `kct drc <pcb>` | Run design rules check (requires kicad-cli) |
| `kct check <pcb>` | Pure Python DRC (no kicad-cli needed) |
| `kct creepage <pcb>` | HV surface-path (creepage) audit vs IEC 60664-1 / 62368-1 |
| `kct creepage-export-rules <project>` | Export voltage-domain netclasses + pairwise HV clearance rules so kicad-cli DRC enforces creepage |
| `kct analyze <pcb>` | Signal-integrity, current-sense, and electrical-rating layout lint |
| `kct audit <project>` | Manufacturing readiness audit (ERC, DRC, connectivity, compatibility) |
| `kct impedance <subcommand>` | Transmission line impedance calculations |

**Layout and routing**

| Command | Description |
|---------|-------------|
| `kct route <pcb>` | Autoroute a PCB (`--nets`/`--skip-nets` to select nets, `--complete` to finish one) |
| `kct route-auto <pcb>` | Orchestrator-based multi-strategy autorouting |
| `kct optimize-traces <pcb>` | Optimize routed traces |
| `kct placement <pcb>` | Detect and optimize component placement |
| `kct optimize-placement <pcb>` | CMA-ES/Bayesian global placement optimization |
| `kct zones <subcommand>` | Add copper pour zones (and `hv-keepout` plane voids) |
| `kct stitch <pcb>` | Auto-add stitching vias for plane connections |
| `kct reason <pcb>` | LLM-driven PCB layout reasoning |

**Repair**

| Command | Description |
|---------|-------------|
| `kct fix-drc <pcb>` | Automated DRC violation repair (clearance + drill) |
| `kct fix-erc <schematic>` | Automated ERC violation repair (PWR_FLAG + no-connect) |
| `kct fix-vias <pcb>` | Fix vias to meet manufacturer specifications (incl. off-pad relocation) |
| `kct pipeline <input>` | End-to-end repair pipeline for existing PCBs |

**Parts and manufacturing**

| Command | Description |
|---------|-------------|
| `kct datasheet <subcommand>` | Search, download, parse datasheets |
| `kct parts <subcommand>` | LCSC/JLCPCB part lookup, cache, offline catalog sync |
| `kct export <pcb>` | Manufacturing bundle export (gerbers, BOM, CPL) |
| `kct mfr <subcommand>` | Manufacturer rule profiles (apply-rules, validate) |
| `kct spec <subcommand>` | Project specification (`.kct`) management |
| `kct fleet status` | Routing + manufacturing readiness across all boards |

**Environment**

| Command | Description |
|---------|-------------|
| `kct doctor` | Diagnose kicad-tools installation health (version-record drift) |
| `kct build-native` | Build the C++ router backend for 10-100x faster routing |
| `kct mcp serve` | Start MCP server for AI agent integration |

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
| `mcp` | MCP server for AI agent integration |
| `pcb.layout` | Layout preservation for PCB regeneration |

## What's New (v0.19.0, July 2026)

Recent additions an agent reading these docs cold should know about. Older
entries live in [CHANGELOG.md](CHANGELOG.md).

- **HV-isolation design loop** (v0.19.0) — `kct creepage --voltage-map` derives
  each conductor pair's required creepage from its own `|ΔV|` instead of one
  group working voltage; `kct zones hv-keepout` generates plane voids so inner
  pours clear HV nets; `kct optimize-placement --voltage-map` / `--hv-domains`
  place HV parts with a hard creepage-keepout feasibility term. The
  `/kct:hv-isolation-loop` skill sequences the whole loop.
- **`kct creepage-export-rules`** (since v0.19.0) — export voltage-domain
  netclasses plus pairwise HV clearance `(rule ...)` clauses into the project so
  `kicad-cli pcb drc` enforces creepage too, not just `kct creepage`.
- **`kct check --emit-dru` / `--emit-drc-constraints`** (v0.19.0) — emit
  `.kicad_dru` / `.kicad_pro` sidecars from the checker's already-resolved
  `--mfr` floors, so `kicad-cli pcb drc` and `kct check` reason over identical
  rules by construction.
- **`kct analyze electrical-rating`** (v0.19.0) — deterministic, advisory
  LED-overcurrent and capacitor voltage-derating checks sourced from schematic
  fields; parts missing ratings are skipped, never failed.
- **`kct fix-vias` off-pad relocation** (v0.19.0) — relocate via-in-pad and
  plane-stitch vias off-pad while preserving connectivity, with THT
  hole-to-hole clearance checking and multi-branch relocation.
- **`kct doctor`** (v0.19.0) — diagnose an installation's version-record drift
  (dependency pin, `.kct/install-metadata.json`, CLAUDE.md marker block).
  Advisory by default; `--strict` exits non-zero so it can gate CI.
- **`kct route --complete`** (since v0.19.0) — targeted completion pass: detect
  the still-unconnected links and route only those, treating every other net's
  copper as a fixed obstacle. Implies `--preserve-existing` and never deletes
  copper; skip pour-carried nets with `--complete-exclude-nets`. See
  [CLI Reference → route](docs/reference/cli.md#route).
- **`kct creepage` HV audit + `kct analyze current-sense`** (v0.18.0) — per-pair
  creepage census against IEC 60664-1 / 62368-1 tables (slot-aware, clearance
  and creepage reported as distinct values), and an analog lint for
  sense↔high-current parallel runs, sense-loop area, and Kelvin-tap integrity.
  A below-standard HV pair fails the `kct audit` gate.
- **Real `--nets NET[,NET...]` on `route` / `route-auto`** (v0.18.0) — route
  only the listed nets (inverse of `--skip-nets`); non-listed copper is treated
  as a fixed obstacle.
- **Experimental routing substrates** (v0.17.0) — `--route-engine lattice`
  (adaptive octilinear; 45°-legal copper by construction) and `--route-engine
  mesh` (constrained-Delaunay navmesh), both default **off**. `--route-engine
  grid` remains the default and is unchanged. See
  [Routing Guide](docs/guides/routing.md).
- **`kct net-status --why`** (v0.17.0) — ranked fix recommendations explaining
  why each incomplete net is stuck, with pin-order-verified reversed-bundle
  detection.

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

# Build the C++ router backend (REQUIRED for production routing speed,
# delivers 10-100x A* speedup vs pure-Python; takes ~30s on first build,
# cached thereafter). uv sync does NOT build this — fresh checkouts and
# fresh worktrees need this step explicitly.
uv run kct build-native

# Verify the C++ backend is installed
uv run kct build-native --check
# Expected: "C++ backend: available (version 1.0.0)"

# Run tests
uv run pytest

# Run linter
uv run ruff check .

# Format code
uv run ruff format .
```

> **Routing performance note**: Without the C++ backend, board routing falls
> back to pure-Python A* — typically 5-10x slower per net, which can push
> medium boards (e.g. board 07 in `boards/07-matchgroup-test/`) past the
> default 6-minute CI cap. If `kct route` appears stuck or per-net log lines
> take tens of seconds, run `kct build-native --check` first — a missing
> extension is the most common cause.

#### Fresh worktree checklist

1. **Sync the lock-pinned dev environment first**:

   ```bash
   uv sync --frozen --extra dev
   ```

   A fresh worktree performs no Python env setup, and a stale or drifted
   `.venv/` (e.g. a mypy newer than the `uv.lock` pin) produces spurious
   mypy-baseline noise that looks like real type regressions (issue #4558).
   `--frozen` guarantees the resolved set matches `uv.lock` exactly — the
   same environment CI uses. Loom-created worktrees run this automatically
   via the `.loom/hooks/post-worktree.sh` hook; run it manually for
   hand-made worktrees or if the hook reported a failure.

2. **Build the native extension.** The worktree's `.venv/` does not inherit
   the C++ extension built in the main checkout, and `uv sync` does not
   build it. After `cd` into the worktree, run `uv run kct build-native`
   once before any routing benchmarks.

The build's `nanobind` dependency is composed into the default dev
dependency-group, so a plain `uv sync` keeps it resolved and a later
`uv sync` (e.g. adding `--extra placement`) will not prune it. If you
installed with only a subset of groups, add the `native` extra explicitly
so nanobind stays lockfile-tracked:

```bash
uv sync --extra native            # repo/dev worktree
pip install "kicad-tools[native]" # consumer / installed wheel
```

Avoid a bare `pip install nanobind` — it is not recorded in the resolved
set and a subsequent `uv sync` will uninstall it, breaking the next
`kct build-native` (issue #4412).

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

## Agent Skills (`kct` namespace)

kicad-tools ships its own Claude agent skills under `.claude/commands/kct/` (invoked as `/kct:<name>`), a harness-agnostic namespace kept separate from any installed orchestration framework. Seven skills ship today — `/kct:tapeout` (complete fab-ready export bundle or loud refusal), `/kct:manufacturing-readiness` (sign-off gates), `/kct:hv-isolation-loop` (mains/HV creepage design loop), `/kct:board-recipe-scaffold`, `/kct:layout-journal`, `/kct:ee-review` (advisory EE decision document for analog/placement-blocked boards), and `/kct:help` (introspective guide to the namespace). See [.claude/commands/kct/README.md](.claude/commands/kct/README.md) for the full contracts.

## Related Projects

- **[Zeo](https://github.com/zeodotdev/zeo)** — A KiCad fork with an integrated AI agent sidebar and MCP server. Takes a complementary approach: live editor manipulation via IPC vs. our offline file-based analysis and optimization.
- **[kipy](https://github.com/zeodotdev/zeo-python)** — Python bindings for the KiCad 9.0+ IPC API. Could be used to push kicad-tools optimization results into a running KiCad instance.

## License

MIT

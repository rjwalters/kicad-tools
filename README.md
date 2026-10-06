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
- **Machine-readable output** - Analysis and query commands offer JSON output; check each command’s `--help` for supported formats
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
    trace_width=0.2,  # mm
    trace_clearance=0.15,  # mm
)

# Create router and add components
router = Autorouter(width=100, height=80, rules=rules)
router.add_component("U1", pads=[{"number": "1", "x": 10, "y": 10, "net": 1}])
router.add_component("U2", pads=[{"number": "1", "x": 20, "y": 10, "net": 1}])

# Route all nets
routes = router.route_all(timeout=30)
print(f"Created {len(routes)} routes")
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

Register the server with your agent harness. `kct mcp setup` finds the
absolute `kct` path, merges into the existing config without touching other
servers, and is a no-op when re-run (`--dry-run` previews, `--format json` for
scripts):

```bash
kct mcp setup --client claude-code      # ~/.claude/mcp.json (default)
kct mcp setup --client claude-desktop   # Claude Desktop config
kct mcp setup --client codex            # [mcp_servers.kct] in $CODEX_HOME/config.toml
kct mcp setup --client opencode         # ~/.config/opencode/opencode.json (--project for ./opencode.json)
```

Or configure Claude Desktop by hand (`~/Library/Application Support/Claude/claude_desktop_config.json`):

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
    MCUBlock,
    CrystalOscillator,
    LDOBlock,
    USBConnector,
    DebugHeader,
    I2CPullups,
    ResetButton,
)

sch = Schematic("My STM32 Design")

# Add an MCU with bypass capacitors
mcu = MCUBlock(
    sch, x=150, y=100, part="STM32F103C8T6", bypass_caps=["100nF", "100nF", "100nF", "4.7uF"]
)

# Add crystal oscillator
xtal = CrystalOscillator(sch, x=100, y=100, frequency="8MHz", load_caps="20pF")

# Add power supply
ldo = LDOBlock(sch, x=50, y=100, input_voltage=5.0, output_voltage=3.3)

# Add USB connector with ESD protection
usb = USBConnector(sch, x=50, y=150, connector_type="type-c", esd_protection=True)

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
    print(f"{progress * 100:.0f}%: {message}")
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
| `kct analyze <pcb>` | Signal-integrity, current-sense, electrical-rating, and operating-state component-stress lint |
| `kct audit <project>` | Manufacturing readiness audit (ERC, DRC, connectivity, compatibility) |
| `kct impedance <subcommand>` | Transmission line impedance calculations |

**Layout and routing**

| Command | Description |
|---------|-------------|
| `kct route <pcb>` | Autoroute a PCB (`--nets`/`--skip-nets` to select nets, `--complete` to finish one) |
| `kct route-auto <pcb>` | Orchestrator-based multi-strategy autorouting |
| `kct optimize-traces <pcb>` | Optimize routed traces |
| `kct placement <pcb>` | Detect and optimize component placement |
| `kct optimize-placement <pcb>` | CMA-ES/Bayesian (`--strategy bayesian`, needs `kicad-tools[bayesian]`) global placement optimization |
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

## What's New (v0.21.1, September 2026)

Recent additions an agent reading these docs cold should know about. Older
entries live in [CHANGELOG.md](CHANGELOG.md).

- **Routing, validation and CLI hot-path performance sweep** (v0.21.0) —
  measured removal of overhead across the router, validator, placement and
  CLI: `import kicad_tools` 1.46 s → 0.34 s user CPU, the A\* neighbor-batch
  cost ~3.9× faster with identical routes, the placement C++ path ~64 s →
  0.55 s, board-05 LVS pin resolution ~20× faster. Local measurements; the
  hosted-CI median comparison remains open on #5240.
- **MCP server requires the mcp 2.x SDK** (v0.21.0, breaking for `[mcp]`
  extra users) — `FastMCP` was renamed to `mcp.server.mcpserver.MCPServer`
  and the `fastmcp` pin is now `>=4,<5`. In-process API callers using the
  stdio `MCPServer` dataclass are unaffected.
- **Copper-LVS and `kct net-status` decide on physical copper contact**
  (v0.21.0) — a net owning a zone somewhere no longer suppresses its `open`
  findings, copper contact is decided over a segment's full length, dangling
  tracks are detected by copper-cap contact, and `kct check` gains a
  geometry-based copper-slit detector.
- **Pour bridges terminate on existing same-net copper** (v0.21.0) — pour
  repair reuses an existing same-net via barrel or through-hole pad as the
  bridge terminus instead of adding a new via; board07 pour repair is now
  frame-independent with native-refill stability.
- **Legacy `(module …)` footprints and rotated pads are honored everywhere**
  (v0.21.0) — KiCad-4-era boards are recognized by every router/zones/DRC/LVS/
  panel tree-walk, and non-cardinal pad rotation is applied in every obstacle,
  clearance and thermal consumer so a rotated pad is priced identically
  everywhere.
- **Slimmer sdist and honest failure modes** (v0.21.1) — the sdist drops from
  127 MB to 18 MB (board artifacts and evidence trees excluded; the wheel is
  unchanged), `kct route` on board06 refuses a foreign input PCB with a named
  error instead of emitting misleading `UNREPAIRED` lines, and the
  access-witness sidecar is written even on a wall-clock deadline kill.
- **Search-time HV pairwise clearance in the lattice engine** (v0.20.0) — with
  `--voltage-map`, the lattice router now *avoids* HV↔LV proximity during
  search instead of merely failing the post-route gate; KiCad keepout rule
  areas are honored too, with a per-net-class `spatial_keepouts` sidecar
  filter for declared HV-domain segregation.
- **`kct sch tidy`** (v0.20.0) — headless autoplace of Reference/Value fields
  (bbox-relative, rotation/mirror-aware) with `--refs`, `--threshold`,
  `--dry-run`, and a strict cosmetic-only guarantee (netlist/BOM/ERC provably
  unchanged). Pairs with the new `sch_fields` advisory lint in `kct check`
  (`sch_field_offset` / `sch_field_overlap`).
- **`kct net-status` strict by default** (v0.20.0) — connectivity now uses
  real copper geometry (kicad-cli semantics) instead of endpoint proximity,
  eliminating false opens on poured nets; `--legacy-proximity` restores the
  old model, and `--strict --why` now compose.
- **`kct pcb add-3d-models --refresh`** (v0.20.0) — re-resolve and rewrite
  existing `(model ...)` refs in place through the current tier chain
  (library → variant → substitution → LCSC sidecar); default remains
  byte-identical insert-only.
- **Board-edge keepout auto-resolution** (v0.20.0) — in-process routing via
  `load_pcb_for_routing` now derives the board-edge keepout from the
  manufacturer's `min_edge_clearance` automatically (explicit `0` opts out),
  matching what the CLI already did.
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
- **`kct analyze component-stress`** (v0.20.0) — operating-state MOSFET
  VDS/VGS gate: evaluates `V(D)-V(S)` / `V(G)-V(S)` per state of an explicit
  `--states` manifest against sourced `Vds_max`/`Vgs_max` symbol fields. No
  circuit-state inference — an undeclared state, unresolved D/G/S pin role,
  unbound terminal or uncited rating is reported `UNRESOLVED` (a release
  blocker), never a silent pass, and a creepage waiver can never suppress it.
  Exact MPN and distinct, valid terminal-pin assignments are required;
  conflicting rating or identity fields remain unresolved. The
  census includes identifiable MOSFETs in child sheets; reused sheets, duplicate
  references, and repeated local labels without proven global connections remain
  unresolved. State/net aliases and duplicate JSON/YAML
  keys cannot overwrite operating data. A project-specific coverage checklist
  must be nonempty, and structured states cannot mix `nets:` with shorthand
  potentials. These checks do not infer circuit states or qualify hardware.
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
  (adaptive octilinear; 45°-legal copper by construction) and `--route-engine mesh` (constrained-Delaunay navmesh), both default **off**. `--route-engine grid` remains the default and is unchanged. See
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

3. **If a local `mypy` error names a file outside your diff, `rm -rf .mypy_cache` and re-run before investigating it.** `.mypy_cache/` is
   gitignored, so it survives `git reset --hard`, `git clean -fd`, a rebase,
   and a worktree reuse — bare `mypy` / `pnpm typecheck` can replay an error
   computed against an older tree. CI is always cold (no `actions/cache`), so
   it will not reproduce the phantom. `scripts/ci/check_mypy_baseline.py` is
   already immune (it runs `--no-incremental`); see
   [`docs/contributing/development.md`](docs/contributing/development.md#the-stale-mypy_cache-trap).

4. **Initialize KiCad's global library tables before trusting native ERC/DRC
   results.** `kct check`'s native ERC leg (and the board 03/04 reviewed
   paid-drill gates, which run `kct check` with `--strict`) shell out to
   `kicad-cli`, which reads the host's `fp-lib-table`/`sym-lib-table` under
   `~/.config/kicad/<version>/` (GUI-provisioned on first launch — a fresh
   worktree or CI container has neither). Without them, kicad-cli raises
   ordinary `lib_symbol_issues`/`footprint_link_issues` strict-mode ERC
   warnings that read identically to a genuine footprint/symbol-link defect
   in the schematic (issue #5860). CI always runs this first, so the
   committed `boards/*/output/check-report.json` files are unaffected; run
   it yourself once per host before relying on `kct check` locally:

   ```bash
   uv run python scripts/ci/init_kicad_libraries.py
   ```

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

**Opening a PR from a fork?** Fork PRs run on GitHub-hosted runners rather than
this project's self-hosted one, which makes the `Test` job roughly 2.2x slower
(~50 min, with a 90-minute budget). Nothing is skipped and nothing extra is
required of you — see
[`docs/contributing/development.md`](docs/contributing/development.md#6-what-to-expect-from-ci-on-a-fork-pr)
for the details and the maintainer fallback if a run still cannot finish.

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

kicad-tools ships its own Claude agent skills under `.claude/commands/kct/` (invoked as `/kct:<name>`), a harness-agnostic namespace kept separate from any installed orchestration framework. Seven skills ship today — `/kct:tapeout` (complete fab-ready export bundle or loud refusal), `/kct:manufacturing-readiness` (sign-off gates), `/kct:hv-isolation-loop` (mains/HV creepage design loop), `/kct:board-recipe-scaffold`, `/kct:layout-journal`, `/kct:ee-review` (advisory EE decision document for analog/placement-blocked boards), and `/kct:help` (introspective guide to the namespace). See [.claude/commands/kct/README.md](.claude/commands/kct/README.md) for the full contracts. The skill text is harness-neutral; [docs/agent-surfaces.md](docs/agent-surfaces.md) lists what each skill needs from Claude Code, Codex CLI or opencode.

The skills ship inside the `kicad-tools` wheel (`kicad_tools/agent_skills/kct/`), so a plain `pip install kicad-tools` is enough; no clone of this repository is needed. Install them with `kct skills install`:

```bash
kct skills install                       # ./.claude/commands/kct/*.md (Claude Code, project)
kct skills install --user                # ~/.claude/commands/kct/
kct skills install --target /tmp/x       # /tmp/x/kct/*.md
kct skills install --harness codex       # ./.agents/skills/kct-<name>/SKILL.md
kct skills install --harness opencode    # ./.opencode/commands/kct/*.md, run as /kct/<name>
kct skills install --list                # packaged skills and their install state
kct skills install --check               # exit 1 if the installed copy has drifted
```

Re-running is idempotent. `--prune` removes `kct` skill files the package no longer ships, and `--dry-run` previews. Edit skills in `src/kicad_tools/agent_skills/kct/`; `.claude/commands/kct/` is a synced copy (`uv run python scripts/sync_agent_skills.py --write`).

## Using kicad-tools from Claude Code, Codex and opencode

Each harness needs two setup commands: one registers the MCP server, the other
installs the `kct` skills in that harness's own format. The skill sources are
shared, so every harness gets the same skills.

| Harness | MCP server | Skills | Invoke a skill |
|---|---|---|---|
| Claude Code | `kct mcp setup --client claude-code` | `kct skills install` (`.claude/commands/kct/`) | `/kct:<name>`, e.g. `/kct:tapeout <board-path>` |
| Codex CLI | `kct mcp setup --client codex` | `kct skills install --harness codex` (`.agents/skills/kct-<name>/`) | Codex skills: `$kct-<name>`, e.g. `$kct-tapeout <board-path>` |
| opencode | `kct mcp setup --client opencode` | `kct skills install --harness opencode` once opencode rendering lands (#5951) | `/kct/<name>` |

Add `--user` to `kct skills install` to install for every project. Then start
the agent with the primer:

```bash
kct agent-guide                   # workflow, JSON contract, sign-off rule, pitfalls
kct agent-guide --harness codex   # with skill examples in Codex syntax
kct agent-guide --format json     # {"version", "harness", "sections": [...]}
```

`kct agent-guide` ships in the wheel and reads the same in every harness. Point
your agent at it from `CLAUDE.md` or `AGENTS.md` instead of copying it.

## Related Projects

<!-- BEGIN kct:ecosystem -->

kicad-tools is one piece of a fast-moving KiCad automation ecosystem. This
section is generated from `src/kicad_tools/ecosystem/data/projects.toml` --
run `kct ecosystem where-we-sit` for our invariants and non-goals, `kct
ecosystem show <id>` for any entry below, and see [docs/ecosystem.md](docs/ecosystem.md)
for the full positioning narrative. Where we have evaluated a project in
depth, the linked note carries the method, the measurements and the verdict,
including the cases where the other tool wins.

Licenses are the SPDX id from the upstream `LICENSE` file, and are
load-bearing: an AGPL or unlicensed neighbour is studied at the capability
level only, never copied from. Facts below were last verified on the date each
registry entry records; `.github/workflows/ecosystem-drift.yml` re-checks them
weekly.

### Autorouters

- **[KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools)** (MIT, Python + Rust, 488★) -- **Benchmarked against us.** The closest peer to `kct route`: octilinear multi-layer A\* with via insertion, diff pairs, length matching, BGA/QFN fanout and plane pours, shipped as both a KiCad 9/10 action plugin and a CLI. Benchmarked head-to-head against us on eight demo boards under one shared `kicad-cli` referee: it is faster and completes more nets on the sparse and medium boards, while on the dense boards it emits literal shorts where we instead run out of time. Our evaluation: [docs/research/kicad-routing-tools-comparison.md](docs/research/kicad-routing-tools-comparison.md).
- **[freerouting](https://github.com/freerouting/freerouting)** (GPL-3.0, Java, 2068★) -- the long-standing Specctra DSN/SES autorouter and the baseline most KiCad users try first. Has a headless CLI, but the DSN round-trip is a lossy interchange boundary: it routes a translated view of the board rather than the `.kicad_pcb` itself. We operate on the board file directly, which is what lets LVS and the manufacturing gates run against the same artifact that gets fabricated. PCBWorld's paper reports it as the strongest published rule-based baseline on real boards (Clean Pass 0.80 / 0.78 on D3-A / D3-B). fastroute is a faster Rust port of it with a byte-identical parity mode.
- **[OrthoRoute](https://github.com/bbenchoff/OrthoRoute)** (MIT, Python + CUDA/Metal, 411★) -- GPU-accelerated PathFinder routing, with plugin and headless modes. Its author is explicit that it targets a narrow class of very large, dense, highly regular multilayer backplanes and BGA escape patterns and does poorly on typical boards -- the opposite end of the board-size spectrum from our demo fleet.
- **[DeepPCB KiCad plugin](https://github.com/instadeepai/deeppcb-kicad-plugin)** (Apache-2.0, Python, 66★) -- **Evaluated and not adopted.** An open-source plugin in front of a closed, credit-metered cloud router. The plugin is local; the routing is not. It requires an account and API key, so it cannot participate in an offline or air-gapped CI pipeline. DeepPCB's own published benchmark numbers are recorded in `benchmarks/external/boards.toml` for side-by-side comparison. Our evaluations: [benchmarks/external/README.md](benchmarks/external/README.md), [benchmarks/external/results/report.md](benchmarks/external/results/report.md).
- **[tscircuit autorouter](https://github.com/tscircuit/tscircuit-autorouter)** (MIT, TypeScript, 85★) -- **Benchmarked against us.** Tscircuit's built-in router (`@tscircuit/capacity-autorouter`): a pipeline of hypergraph / successive-approximation solvers rather than net-by-net A\*, fed a `SimpleRouteJson` problem instead of a KiCad file. It ships a public benchmark (`dataset-srj18`, derived from real Arduino and Antmicro KiCad boards) and a bug-report-to-fixture workflow. Partly benchmarked against us (Arduino Nano only): SRJ drops netclass width, clearance, zones and keepouts, so its completion is not comparable. Our evaluation: [docs/research/tscircuit-evaluation.md](docs/research/tscircuit-evaluation.md).

### Design as code (upstream of us)

- **[atopile](https://github.com/atopile/atopile)** (MIT, Python, 3964★) -- **Ideas adopted.** A language and compiler for describing boards as code (`.ato`), with constraint solving, a package registry and parametric part selection via its Faebryk library; it emits a netlist and updates a KiCad layout, but deliberately stops at "place and route in KiCad". That is exactly where we start, which makes the two complementary rather than competing. Our MCP server exists because of this evaluation. Our evaluations: [docs/research/atopile-research-synthesis.md](docs/research/atopile-research-synthesis.md), [docs/research/faebryk-component-library.md](docs/research/faebryk-component-library.md), [docs/research/atopile-constraint-solving.md](docs/research/atopile-constraint-solving.md), [docs/research/atopile-interface-patterns.md](docs/research/atopile-interface-patterns.md), [docs/research/atopile-layout-reuse.md](docs/research/atopile-layout-reuse.md), [docs/research/atopile-lsp-analysis.md](docs/research/atopile-lsp-analysis.md), [docs/research/atopile-mcp-analysis.md](docs/research/atopile-mcp-analysis.md), [docs/research/atopile-package-registry.md](docs/research/atopile-package-registry.md), [docs/research/atopile-error-handling.md](docs/research/atopile-error-handling.md).
- **[SKiDL](https://github.com/devbisme/skidl)** (MIT, Python, 1675★) -- the original Python-as-schematic-capture tool: a program describes the circuit and SKiDL emits netlists, XML BOMs and (for KiCad 6-10) editable schematics, with its own ERC. Another upstream producer of the files we consume.
- **[tscircuit](https://github.com/tscircuit/tscircuit)** (MIT, TypeScript, 2776★) -- **Evaluated and not adopted.** "React for Electronics" -- circuits described in TypeScript/React and compiled to its own Circuit JSON. Its `circuit-json-to-kicad` converter emits `.kicad_sch`, `.kicad_pcb` (with traces, vias and pours) and `.kicad_pro`, and `kicad-to-circuit-json` reads them back, so it is an upstream producer of the files we consume, like atopile and SKiDL. **Gated and not yet usable as a front end**: five representative designs were run through `kct check --mfr`, LVS and `kicad-cli pcb drc --refill-zones`, before and after re-routing with `kct route`, and all five failed. The blocking defect is that its schematic and PCB converters name the same net differently, so no emitted project can pass LVS at all; vias, netclass and silkscreen defaults also sit below every mainstream fab floor. Our parser reads the files without complaint, and `kct route` produced opens where tscircuit's own autorouter produced shorts. Six upstream defects recorded with reproducers; the relation stays upstream/complementary and all six look fixable. Our evaluation: [docs/research/tscircuit-evaluation.md](docs/research/tscircuit-evaluation.md).
- **[component-importer-for-kicad](https://github.com/robertxdx/component-importer-for-kicad)** (MIT, Python, 55★) -- **Evaluated and not adopted.** Evaluated as a vendor-ZIP (SnapEDA / Ultra Librarian) ingestion backend for symbol and footprint import. Verdict was borrow-with-refactor rather than vendor: its headless modules are PyQt6-free and close to the surface we need, but a clean-room re-implementation of roughly 600 LoC plus a `kct ingest-zip` CLI is the smaller change. Not built yet. Our evaluation: [docs/research/component-importer-for-kicad-evaluation.md](docs/research/component-importer-for-kicad-evaluation.md).

### Agent and MCP interfaces

- **[Zeo](https://github.com/zeodotdev/zeo)** (AGPL-3.0, C++, 31★) -- a KiCad fork with an integrated AI agent sidebar and MCP server. Takes a complementary approach: live editor manipulation via IPC vs. our offline file-based analysis and optimization.
- **[Konnect](https://github.com/mixelpixx/Konnect)** (AGPL-3.0, Rust, 841★) -- **Ideas adopted.** A single-binary native KiCad 10 plugin exposing a very large LLM tool surface (217 tools) across schematic, layout, routing, placement, review and manufacturing. AGPL means no code can move between it and this MIT repo in either direction, so our engagement is capability-level only: we audited its connectivity primitives, its tool-economy design and its design-rule handling against our own surface. Five of its six connectivity primitives we already had; the sixth (near-miss auto-snap repair) was a real gap. Our evaluations: [docs/research/konnect-connectivity-audit.md](docs/research/konnect-connectivity-audit.md), [docs/konnect-item1-toolset-economy-audit.md](docs/konnect-item1-toolset-economy-audit.md), [docs/konnect-item8-design-rules-audit.md](docs/konnect-item8-design-rules-audit.md).
- **[copperhead](https://github.com/copperheadhq/copperhead)** (Apache-2.0, TypeScript, 312★) -- **Ideas adopted.** "cursor for circuit boards": a natural-language brief drives direct edits to `.kicad_sch` / `.kicad_pcb`, gated by an LLM-free `check` and wrapped in a git snapshot that auto-rolls-back on failure. It does not route, and verifies with `kicad-cli` ERC/DRC only -- no LVS, no manufacturing cross-gate. Its *process* design (design memory as committed artifacts, a constraints registry, a per-run audit trail) is the part worth borrowing. Note: moved from `chouhanindustries/copperhead` since our note was written. Our evaluation: [docs/research/copperhead-workflow-ideas.md](docs/research/copperhead-workflow-ideas.md).
- **[Seeed-Studio/kicad-mcp-server](https://github.com/Seeed-Studio/kicad-mcp-server)** (no `LICENSE` committed, Python, 135★) -- the closest peer to `kct mcp serve`: an MCP server that reads schematic and PCB files offline (shelling out to `kicad-cli` for validation) to trace pin-level connections and edit designs. Overlaps our read/analyze tools; carries no router, LVS or fab-readiness gates. Its README claims MIT but the repo commits no LICENSE file, so we treat it as all-rights-reserved and copy nothing.

### Fabrication and CI

- **[KiBot](https://github.com/INTI-CMNB/KiBot)** (AGPL-3.0, Python, 747★) -- the mature, scriptable fabrication- and documentation-output generator for KiCad (gerbers, drill, position, BOM, 3D, plots), with a GitHub Action. Deeper than `kct export` on output *variety* and configuration; it assumes a board that is already routed and signed off, which is the part we automate.

### KiCad bindings

- **[kipy](https://gitlab.com/kicad/code/kicad-python)** (MIT, Python, 19★) -- **Evaluated and not adopted.** The official KiCad Python bindings (`kicad-python` on PyPI) for the KiCad IPC API. Requires a **running KiCad instance** with the IPC API enabled and a document open in an editor -- every accessor is an RPC to the live editor; there is no offline file-reading path, so it cannot participate in headless/CI workflows. Our evaluation: [docs/research/kipy-ipc-api-evaluation.md](docs/research/kipy-ipc-api-evaluation.md).

### Benchmarks and evaluation protocols

- **[OmniLayout / OmniRouting](https://omnieda.com)** (no `LICENSE` committed) -- **Ideas adopted.** A published routing benchmark and leaderboard. We adopted its *metric vocabulary and evaluation protocol* -- the sharpest external definition of "routed well" we have found -- and declined the dataset and tooling: no license grant on the data, and the evaluation harness is unpublished. Our evaluation: [docs/research/omnilayout-recon.md](docs/research/omnilayout-recon.md).

### Watching

Recorded in the registry with a re-evaluation trigger, deliberately not featured above yet:

- **[TraceMaker](https://github.com/DingoOz/TraceMaker)** (GPL-3.0, C++20 + CUDA, 42★) -- C++20/CUDA KiCad 9/10 router that reads and writes `.kicad_pcb` directly and is judged by `kicad-cli` DRC. Octilinear-lattice A\* with exact legality checks, negotiated rip-up, an 8-variant deterministic portfolio under a `--work` budget, min-cost-flow escape-corridor reservation, a nogood cache, coupled diff-pair search, and a SQLite bandit that learns which variant to pick across runs; the GPU only accelerates cost fields. On PCBWorld D3 the circuit-skills author reports 0.99 / 1.00 / 0.90 clean on the small / medium / large splits, against about 0.7 / 0.7 / 0.2 for Freerouting 2.2.4 -- third-party, single 60 s runs, not reproduced by us, and on PCBench boards TraceMaker was developed against (its own held-out figure is 73-78% clean). Its "clean" also excludes DRC errors already in the input board, looser than our 0-DRC bar. Days old; copyleft, so ideas only, and run only as an external binary.
- **[fastroute](https://github.com/parisxmas/fastroute)** (GPL-3.0, Rust, 73★) -- Rust port of freerouting: Specctra DSN in, SES out, with a `--parity` mode it claims is byte-identical to Freerouting on its 20 benchmark boards. Its own measurements show about 4-5x less wall time (2102 s vs 510 s over those boards, 420 s with PGO) in 20-300 MB instead of 1-2 GB, and it adds fixes for several Freerouting optimizer bugs, multi-start, length tuning, diff pairs and impedance-derived widths. A pcbnew plugin patches the gaps in KiCad's lossy Specctra export (DRU clearances, keepouts, planes). Like freerouting it routes a translated view rather than the board file. Copyleft, so ideas only.
- **[AutoRoute](https://github.com/pingpongshow/AutoRoute)** (MIT, Rust, 0★) -- Rust negotiated-congestion router with an exact-DRC clearance model, a headless CLI and DSN/SES KiCad integration. Recorded rather than featured: 8 commits and no adoption signal yet, so there is nothing stable to measure against.
- **[Topola](https://codeberg.org/topola/topola)** (MIT, Rust, 33★) -- work-in-progress interactive topological (rubber-band) router and autorouter in Rust, funded through NLnet's NGI0 Entrust fund. Routing topologically before committing geometry is a different shape of search from our grid A\*, and the license is permissive -- but it is pre-release and we have not run it.
- **[circuit-synth](https://github.com/circuit-synth/circuit-synth)** (MIT, Python, 279★) -- Python-defined circuits (`@circuit` functions over KiCad library symbols and footprints) compiled to KiCad projects, built around Claude Code with slash commands for symbol search and design help. Another upstream producer of the files we consume, in the SKiDL mould; no pushes since March 2026, and we have not run its output through our gates.
- **[2040-ArtNode](https://github.com/pixeldestrukt/2040-artnode)** (no `LICENSE` committed, Python, 0★) -- a Codex-assisted RP2354A Art-Net LED node whose KiCad 9 files come from hand-rolled Python S-expression generators, plus a SKiDL attempt. The 4-layer board is placed but carries no traces, and fails `kct check --mfr jlcpcb` with 255 errors, mostly stitching vias dropped onto pads -- a worked example of how hand-rolled generators drift. Not a benchmark candidate: no license and no reference copper.
- **[circuit-skills](https://github.com/punkfab/circuit-skills)** (no `LICENSE` committed, Python, 9★) -- Claude Code skills for code-driven electronics: ngspice/Falstad circuit simulation, a tscircuit-placement -> KiCad IPC (`kipy`) route-and-DRC loop, board/enclosure fit co-design and 3D renders. Its `route_eval` harness now runs five routing backends on any KiCad board (two Freerouting versions, TraceMaker, the tscircuit router, and an experimental `srj-legal` that has TraceMaker re-route what KiCad's DRC rejects in tscircuit output) and keeps the best, with an optional fastroute backend and a PCBWorld D3 harness; it self-reports 0.98 Clean Pass on D3-A for `srj-legal`, unreproduced by us. Overlaps our `/kct:*` skills, but still assumes the tail is hand-finished in KiCad -- the opposite of our fully automated bar. No LICENSE committed, so ideas only; mining tracked in issue #5886.
- **[kicadmium](https://github.com/meawoppl/agent-portal-plugins/tree/main/kicadmium)** (no `LICENSE` committed, Rust, 0★) -- an Agent Portal plugin: a single Rust binary that wraps a user's own KiCad install with a browser workbench and agent skills. Its `kct/` crate is a ~137k-line Rust port of kicad-tools taken from our commit `37665de5`, keeping our command names, flags and JSON keys, and it claims parity on several command families. It adds a 101-rule lint engine whose reviewed exceptions go stale when their evidence changes, and porting surfaced four bugs in our code (#5937-#5940). Useful as a second implementation to diff our output against.
- **[kicad-happy](https://github.com/aklofas/kicad-happy)** (MIT, Python, 1343★) -- agent skills (Claude Code, Codex and others) for design-level review of KiCad projects: power trees computed from feedback dividers, connector-by-connector ESD audits, passive-network checks, SPICE and EMC pre-compliance, plus datasheet and distributor lookups. That is the circuit-level review our `/kct:ee-review` does not yet do; it does not route or gate manufacturing.
- **[KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server)** (MIT, Python + TypeScript, 2592★) -- the most-starred KiCad MCP server: project setup, schematic editing, placement, routing via Freerouting, DRC/ERC, export, custom symbol and footprint generation and JLCPCB parts lookup. It is the MIT predecessor of Konnect, which its authors now say is where new development happens. Not yet compared tool-by-tool with `kct mcp serve`.
- **[KiCad MCP Pro](https://github.com/oaslananka/kicad-mcp-pro)** (MIT, Python, 119★) -- an MCP server for KiCad with staged permission profiles: a default of 24 read-only review tools, a `build` profile for controlled edits, and a `release` profile for human-gated manufacturing export. Those stages map onto our sign-off gates. It is candid that its SI/PI/EMC/thermal tools are first-order estimates rather than sign-off.
- **[KiKit](https://github.com/yaqwsx/KiKit)** (MIT, Python, 2038★) -- the standard KiCad panelization tool, as a library, plugin and CLI: regular and oddly-shaped panels, fab-data export from manufacturer presets, stencils, multi-board projects and presentation pages. Overlaps `kct panel` and `kct export`; we have not compared feature or fab-preset coverage.
- **[kicad-jlcpcb-tools](https://github.com/Bouni/kicad-jlcpcb-tools)** (MIT, Python, 2096★) -- a KiCad plugin that generates JLCPCB Gerbers, drill, BOM and CPL files, and assigns LCSC part numbers to footprints by searching the JLCPCB parts database. A GUI-plugin take on what our JLCPCB export and `/kct:tapeout` BOM checks do headlessly.
- **[jlcparts](https://github.com/yaqwsx/jlcparts)** (MIT, Python, 837★) -- a parametric-search front end over the JLCPCB/LCSC assembly catalogue, regularly rebuilt. A candidate data source for the LCSC part-number and stock checks in `/kct:tapeout`, if its terms and refresh cadence suit us.
- **[PCBWorld](https://github.com/LGAI-Research/PCBWorld)** (BSD-3-Clause, Python, 25★) -- a Gymnasium environment around KiCad's push-and-shove router, scored by KiCad's own DRC (arXiv:2607.05915). It ships synthetic sets D1/D2 and a recipe to rebuild D3: 679 PCBench boards converted to KiCad 9, keeping their human reference copper. Its headline metric, Clean Pass (fully connected and 0 error-level DRC), matches our bar. Published Clean Pass on D3-A / D3-B: Freerouting 0.80 / 0.78, KiCadRoutingTools 0.74 / 0.20. The most direct public leaderboard for `kct route`.
- **[PCBSchemaGen v2](https://github.com/HZou9/PCBSchemaGen_v2)** (MIT, Python, 12★) -- 227 schematic-synthesis tasks (62 hand-written, 165 from public schematics) with a deterministic 5-layer verifier that needs no LLM: ERC invariants, pin-role compatibility, per-IC connection templates, topology motifs and power rules. Tasks are scored as SKiDL designs. A ready-made eval for our schematic generation.
- **[PCB-Bench](https://github.com/digailab/PCB-Bench)** (no `LICENSE` committed, Python, 49★) -- an ICLR 2026 benchmark of LLM knowledge about PCB placement and routing: about 3,700 text questions, about 500 image-and-text questions, and 174 OSHWHub designs to describe from screenshots. It measures what a model knows about layout, not what a tool produces, so it bears on our agent-facing docs rather than on `kct route`. No LICENSE, so ideas only.

<!-- END kct:ecosystem -->

## License

MIT

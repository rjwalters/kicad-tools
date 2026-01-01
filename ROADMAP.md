# kicad-tools Roadmap

**Mission**: Enable AI agents to design PCBs programmatically.

kicad-tools provides the programmatic interface between AI reasoning and KiCad file manipulation. It's not about replacing KiCad—it's about making PCB design automatable by agents.

## Design Philosophy

1. **Agent-First API** - Every operation callable from code with structured I/O
2. **Rich Feedback** - Validation returns actionable information, not just pass/fail
3. **Round-Trip Fidelity** - Edits preserve existing file structure
4. **Minimal Dependencies** - Core operations work without external tools
5. **Hierarchical Abstractions** - Work with circuit blocks, not just primitives

---

## Released Versions

### v0.1.0 - Foundation

- S-expression parser with round-trip editing
- Schematic/PCB/Library parsing
- Unified CLI with JSON output
- Manufacturer design rules (JLCPCB, OSHPark, PCBWay, Seeed)
- ERC/DRC report parsing

### v0.2.0 - Manufacturing Readiness

- LCSC parts database integration
- Assembly package export (gerbers, BOM, pick-and-place)
- Fluent query API (`sch.symbols.filter(value="100nF")`)
- A* autorouter with obstacle awareness
- Trace optimizer with length matching
- Footprint validation and repair

### v0.3.0 - Reasoning & Routing

- **LLM reasoning integration** (`kct reason`)
  - PCB state representation for LLMs
  - Command vocabulary for routing actions
  - Feedback/diagnosis for failed operations
- Differential pair routing with length matching
- Bus routing for grouped signals
- Zone-aware routing with thermal relief
- Interactive REPL mode
- Staircase pattern compression

### v0.4.0 - Library Management & Validation

- **Symbol library tools**: create, edit, save
- **Footprint library tools**: create, edit, save
- **Parametric footprint generators**:
  - SOIC, QFP, QFN, DFN, BGA
  - Chip (0402-1206), SOT variants
  - Through-hole (DIP, SIP, pin headers)
- **Pure Python DRC** (no kicad-cli required)
  - Clearance, dimension, edge, silkscreen checks
  - Manufacturer presets (2/4/6 layer)
- **Datasheet tools**:
  - PDF parsing and pin extraction
  - Symbol generation from datasheets
  - Part import workflow

---

## Current Capabilities

### What Agents Can Do Today

| Workflow Step | API | CLI |
|--------------|-----|-----|
| Parse datasheets, extract pins | `kicad_tools.datasheet` | `kct datasheet` |
| Create symbols from specs | `SymbolLibrary.create_symbol()` | `kct lib` |
| Generate parametric footprints | `library.generators.*` | `kct footprint generate` |
| Create schematics programmatically | `Schematic.add_symbol()`, `.add_wire()` | - |
| Use circuit blocks (LDO, LED, etc.) | `schematic.blocks.*` | - |
| Place components on PCB | `PCBEditor.place_component()` | - |
| Route traces | `Autorouter.route_all()` | `kct route` |
| Route differential pairs | `route_differential_pair()` | `kct route` |
| Optimize traces | `TraceOptimizer` | `kct optimize-traces` |
| Fix placement conflicts | `PlacementFixer` | `kct placement fix` |
| Validate against DRC | `DRCChecker` | `kct check` |
| Export for manufacturing | `AssemblyPackage` | `kct export assembly` |

### Schematic Primitives

```python
from kicad_tools.schematic import Schematic

sch = Schematic.create("project.kicad_sch")

# Add components
u1 = sch.add_symbol("Device:R", x=100, y=50, ref="R1", value="10k")
u2 = sch.add_symbol("Device:C", x=100, y=70, ref="C1", value="100nF")

# Wire them up
sch.add_wire((100, 55), (100, 65))
sch.add_label("VCC", x=100, y=45)

# Power symbols
sch.add_power("GND", x=100, y=80)
sch.add_rail("VCC", start=(50, 30), end=(150, 30))

sch.save()
```

### Circuit Blocks (Higher-Level Abstraction)

```python
from kicad_tools.schematic.blocks import LDOBlock, LEDIndicator, DecouplingCaps

# Create LDO power supply section
ldo = LDOBlock(sch, x=100, y=80,
    input_voltage=5.0,
    output_voltage=3.3,
    input_cap="10uF",
    output_caps=["10uF", "100nF"])

# Add LED indicator
led = LEDIndicator(sch, x=150, y=80, label="PWR")

# Connect blocks via ports
sch.add_wire(ldo.port("VOUT"), led.port("VCC"))
```

### PCB Blocks

```python
from kicad_tools.pcb.blocks import PCBBlock, BlockAssembler

# Define block with internal placement + routing
mcu_block = MCUBlock(
    mcu_footprint="QFP-48",
    bypass_caps=["C1", "C2", "C3", "C4"]
)

# Place on PCB
mcu_block.place(x=100, y=50, rotation=0)

# Get ports for inter-block routing
vdd = mcu_block.port("VDD")
```

---

## Planned Versions

### v0.5.0 - Workflow Polish

**Focus**: Smooth out rough edges in the agent workflow.

**End-to-End Examples**
- [ ] Complete example: datasheet → symbol → schematic → PCB → gerbers
- [ ] Agent integration examples (Claude, GPT)
- [ ] Jupyter notebook tutorials

**Schematic Enhancements**
- [ ] More circuit blocks: MCU, connector, ESD protection, crystal oscillator
- [ ] Auto-layout for schematic symbols (avoid overlaps)
- [ ] Schematic-to-PCB netlist sync validation

**API Refinements**
- [ ] Unified `Project` class for schematic + PCB workflows
- [ ] Better error messages with fix suggestions
- [ ] Progress callbacks for long operations

### v0.6.0 - Manufacturing Independence

**Focus**: Remove kicad-cli dependency for manufacturing outputs.

**Pure Python Exports**
- [ ] Gerber generation (RS-274X)
- [ ] Drill file generation (Excellon)
- [ ] Direct pick-and-place generation
- [ ] SVG/PDF preview generation

**Manufacturing Validation**
- [ ] Gerber preview/verification
- [ ] Basic panelization support

### v0.7.0 - Intelligent Placement

**Focus**: Smarter initial component placement.

**Placement Engine**
- [ ] Functional clustering (group related components)
- [ ] Thermal-aware placement
- [ ] Signal integrity hints (keep high-speed near source)
- [ ] Edge placement for connectors/interfaces

**Placement Constraints**
- [ ] Keep-out zones
- [ ] Component grouping rules
- [ ] Alignment constraints

### v1.0.0 - Production Ready

**Focus**: Stable API, production deployment.

- [ ] API stability guarantees
- [ ] Comprehensive documentation
- [ ] Performance optimization for large boards
- [ ] Container-ready deployment (no external deps)

---

## Non-Goals

These are explicitly **not** planned:

- **Schematic capture GUI** - Use KiCad for interactive design
- **3D rendering** - Use KiCad's 3D viewer
- **SPICE simulation** - Use dedicated simulators
- **Full gerber viewer** - Use gerbv or KiCad
- **Replacing KiCad** - We complement it, not replace it

---

## Architecture

```
kicad_tools/
├── schematic/          # Schematic creation & editing
│   ├── models/         # Schematic data model
│   └── blocks.py       # Reusable circuit blocks
├── pcb/                # PCB creation & editing
│   ├── editor.py       # PCBEditor class
│   └── blocks.py       # PCB block abstractions
├── library/            # Symbol & footprint libraries
│   └── generators/     # Parametric footprint generators
├── router/             # A* autorouter
├── optim/              # Trace & placement optimization
├── validate/           # Pure Python DRC
├── datasheet/          # PDF parsing, pin extraction
├── reasoning/          # LLM integration
├── export/             # Manufacturing outputs
├── manufacturers/      # Design rules per fab
├── parts/              # LCSC parts database
└── cli/                # Command-line interface
```

---

## Agent Workflow Example

```python
from kicad_tools import Project
from kicad_tools.datasheet import DatasheetManager
from kicad_tools.library import SymbolLibrary
from kicad_tools.schematic import Schematic
from kicad_tools.schematic.blocks import LDOBlock, DecouplingCaps
from kicad_tools.router import route_pcb
from kicad_tools.validate import DRCChecker

# 1. Get part info from datasheet
ds = DatasheetManager()
stm32_pins = ds.extract_pins("STM32F103C8T6")

# 2. Create symbol from pins
lib = SymbolLibrary.create("project.kicad_sym")
lib.create_symbol_from_pins("STM32F103C8T6", stm32_pins)
lib.save()

# 3. Create schematic with circuit blocks
sch = Schematic.create("project.kicad_sch")
mcu = sch.add_symbol("project:STM32F103C8T6", x=150, y=100)
ldo = LDOBlock(sch, x=50, y=100, input_voltage=5.0, output_voltage=3.3)
decoupling = DecouplingCaps(sch, symbol=mcu, caps=["100nF", "100nF", "4.7uF"])

# Wire power
sch.add_wire(ldo.port("VOUT"), mcu.pin("VDD"))
sch.save()

# 4. Route PCB
route_pcb("project.kicad_pcb", strategy="negotiated")

# 5. Validate
checker = DRCChecker.from_file("project.kicad_pcb", manufacturer="jlcpcb")
results = checker.check_all()

if results.errors:
    for err in results.errors:
        print(f"DRC Error: {err.message} at {err.location}")
else:
    print("Design passes DRC!")
```

---

## Contributing

1. Maintain round-trip fidelity for all file modifications
2. Add tests for new functionality
3. Support `--json` output in CLI commands
4. Every API should return actionable errors
5. Test with real KiCad files (8.0+)

---

## Release History

| Version | Date | Focus |
|---------|------|-------|
| 0.1.0 | 2025-12-29 | Foundation: parsing, CLI, manufacturer rules |
| 0.2.0 | 2025-12-30 | Manufacturing: LCSC, export, autorouter |
| 0.3.0 | 2025-12-31 | Reasoning: LLM integration, diff pairs, zones |
| 0.4.0 | 2025-12-31 | Libraries: symbol/footprint creation, pure Python DRC, datasheets |

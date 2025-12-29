# kicad-tools Roadmap

Standalone Python tools for parsing and manipulating KiCad schematic and PCB files.

## Completed

### Core Infrastructure
- [x] S-expression parser with round-trip editing
- [x] Schematic parsing (symbols, wires, labels, hierarchy)
- [x] PCB parsing (footprints, nets, traces, vias, zones)
- [x] Symbol library parsing
- [x] BOM extraction
- [x] Manufacturer design rules (JLCPCB, OSHPark, PCBWay, Seeed)

### CLI Tools (Current - Separate Commands)
- [x] `kicad-sch-summary` - Schematic overview
- [x] `kicad-sch-symbols` - List symbols
- [x] `kicad-sch-labels` - List labels
- [x] `kicad-sch-hierarchy` - Show sheet hierarchy
- [x] `kicad-sch-bom` - Generate BOM
- [x] `kicad-sch-validate` - Validate schematic
- [x] `kicad-pcb-query` - PCB queries
- [x] `kicad-pcb-modify` - PCB modifications

---

## Phase 1: Unified CLI (Next)

**Goal**: Single `kicad` command with subcommands for better discoverability and consistency.

### 1.1 CLI Structure
```bash
kicad <category> <command> [options] <file>

# Schematic commands
kicad sch summary project.kicad_sch
kicad sch symbols project.kicad_sch --json
kicad sch labels project.kicad_sch
kicad sch hierarchy project.kicad_sch
kicad sch nets project.kicad_sch

# PCB commands
kicad pcb summary board.kicad_pcb
kicad pcb footprints board.kicad_pcb --filter "U*"
kicad pcb nets board.kicad_pcb --sorted
kicad pcb traces board.kicad_pcb --layer F.Cu

# BOM & Manufacturing
kicad bom project.kicad_sch --format jlcpcb
kicad gerbers board.kicad_pcb --manufacturer jlcpcb

# Validation
kicad validate project.kicad_sch      # ERC
kicad validate board.kicad_pcb        # DRC
kicad validate board.kicad_pcb --rules jlcpcb

# Library
kicad lib symbols library.kicad_sym
kicad lib footprints library.pretty/
```

### 1.2 Consistent Options
All commands support:
- `--json` - Structured JSON output (for agents/scripts)
- `--quiet` - Minimal output
- `--verbose` - Detailed output
- `--help` - Command help

### 1.3 Agent-Friendly Features
- Predictable exit codes (0=success, 1=error, 2=warnings)
- Machine-readable JSON output
- Clear error messages with file:line references
- Single `kicad --help` shows all available commands

---

## Phase 2: Enhanced Python API

### 2.1 Fluent Query API
```python
from kicad_tools import Schematic

sch = Schematic.load("project.kicad_sch")

# Find symbols
dac = sch.symbols.by_reference("U1")
caps = sch.symbols.by_value("100nF")

# Access properties
print(dac.footprint)
print(dac.properties["MPN"])

# Modify
dac.move_to(100, 200)
dac.properties["Value"] = "PCM5122"
sch.save()
```

### 2.2 PCB API
```python
from kicad_tools import PCB

pcb = PCB.load("board.kicad_pcb")

# Query
u1 = pcb.footprints.by_reference("U1")
gnd_net = pcb.nets["GND"]

# Analyze
print(f"GND trace length: {gnd_net.total_length}mm")
print(f"Via count: {len(gnd_net.vias)}")

# Modify
u1.move_to(50, 50)
u1.rotate(90)
pcb.save()
```

### 2.3 Cross-Reference
```python
# Link schematic to PCB
project = Project.load("project.kicad_pro")
sch = project.schematic
pcb = project.pcb

# Find all capacitors and their PCB locations
for cap in sch.symbols.by_prefix("C"):
    fp = pcb.footprints.by_reference(cap.reference)
    print(f"{cap.reference}: {cap.value} at {fp.position}")
```

---

## Phase 3: Validation & Design Rules

### 3.1 Built-in Validation
```python
from kicad_tools.validate import ERCChecker, DRCChecker

# Schematic validation
erc = ERCChecker(schematic)
issues = erc.check_all()
for issue in issues:
    print(f"{issue.severity}: {issue.message} at {issue.location}")

# PCB validation
drc = DRCChecker(pcb, rules="jlcpcb-4layer")
violations = drc.check_all()
```

### 3.2 Manufacturer Profiles
```python
from kicad_tools.manufacturers import JLCPCB, OSHPark

# Check PCB against manufacturer rules
jlcpcb = JLCPCB(layer_count=4, copper_weight="1oz")
violations = jlcpcb.check(pcb)

# Get manufacturing capabilities
print(jlcpcb.min_trace_width)    # 0.127mm
print(jlcpcb.min_via_diameter)   # 0.3mm
```

### 3.3 BOM Validation
```python
from kicad_tools.bom import BOMValidator

validator = BOMValidator(bom)
validator.check_availability("jlcpcb")  # Check JLCPCB parts library
validator.check_alternates()            # Suggest alternates for unavailable parts
```

---

## Phase 4: Manufacturing Automation

### 4.1 Gerber Export
```bash
kicad gerbers board.kicad_pcb --manufacturer jlcpcb --output gerbers/
```

```python
from kicad_tools.export import GerberExporter

exporter = GerberExporter(pcb, manufacturer="jlcpcb")
exporter.export("gerbers/")
exporter.create_zip("board_gerbers.zip")
```

### 4.2 Pick & Place
```bash
kicad pnp board.kicad_pcb --format jlcpcb --output pnp.csv
```

### 4.3 Assembly Package
```bash
kicad package board.kicad_pcb \
  --manufacturer jlcpcb \
  --output assembly/
# Creates: gerbers.zip, bom.csv, pnp.csv
```

---

## Phase 5: Library Management

### 5.1 Symbol Library Tools
```python
from kicad_tools.library import SymbolLibrary

lib = SymbolLibrary.load("project.kicad_sym")

# List symbols
for symbol in lib.symbols:
    print(f"{symbol.name}: {len(symbol.pins)} pins")

# Create new symbol
new_sym = lib.create_symbol("MyPart")
new_sym.add_pin("VCC", 1, "power_in")
new_sym.add_pin("GND", 2, "power_in")
lib.save()
```

### 5.2 Footprint Library Tools
```python
from kicad_tools.library import FootprintLibrary

lib = FootprintLibrary.load("project.pretty")

# Parametric footprint generation
lib.create_soic(pins=8, pitch=1.27, name="SOIC-8")
lib.create_qfp(pins=48, pitch=0.5, name="LQFP-48")
```

---

## Phase 6: Advanced Features

### 6.1 Netlist Operations
```python
from kicad_tools.netlist import Netlist

netlist = Netlist.from_schematic(schematic)

# Analyze connectivity
for net in netlist.power_nets:
    print(f"{net.name}: {len(net.pins)} connections")

# Find issues
floating = netlist.find_floating_pins()
shorts = netlist.find_potential_shorts()
```

### 6.2 Diff & Merge
```bash
kicad diff old.kicad_sch new.kicad_sch --output diff.html
kicad diff old.kicad_pcb new.kicad_pcb --visual
```

### 6.3 AI Integration Hooks
```python
from kicad_tools.hooks import DesignAssistant

assistant = DesignAssistant(schematic)

# Get suggestions
suggestions = assistant.suggest_decoupling()
suggestions = assistant.check_power_integrity()
suggestions = assistant.review_component_selection()
```

---

## Architecture

```
kicad_tools/
├── __init__.py           # Public API
├── core/
│   ├── sexp.py           # S-expression parser
│   └── sexp_file.py      # File I/O
├── schema/
│   ├── schematic.py      # Schematic model
│   ├── symbol.py         # Symbol model
│   ├── pcb.py            # PCB model
│   ├── library.py        # Library models
│   └── ...
├── cli/
│   ├── main.py           # Unified CLI entry point
│   ├── sch.py            # Schematic subcommands
│   ├── pcb.py            # PCB subcommands
│   ├── bom.py            # BOM commands
│   └── ...
├── validate/
│   ├── erc.py            # Electrical rules
│   └── drc.py            # Design rules
├── manufacturers/
│   ├── jlcpcb.py
│   ├── oshpark.py
│   └── rules/            # Design rule files
├── export/
│   ├── gerber.py
│   ├── bom.py
│   └── pnp.py
└── tests/
```

---

## Dependencies

**Core** (no dependencies):
- Python 3.10+
- Standard library only

**Optional**:
- `numpy` - Router module, geometry operations
- `shapely` - Polygon operations (zone pours)
- `requests` - Parts database lookups

---

## Contributing

1. Maintain round-trip fidelity for all file modifications
2. Add tests for new functionality
3. Support `--json` output in all CLI commands
4. Document new features in README.md
5. Test with real KiCad files (8.0+)

---

## Release Plan

| Version | Focus | Target |
|---------|-------|--------|
| 0.1.0 | Initial release, separate CLI tools | Done |
| 0.2.0 | Unified CLI (`kicad` command) | Next |
| 0.3.0 | Enhanced Python API | |
| 0.4.0 | Validation & manufacturer profiles | |
| 0.5.0 | Manufacturing automation | |
| 1.0.0 | Stable API, full documentation | |

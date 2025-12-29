# kicad-tools Roadmap

Standalone Python tools for parsing and manipulating KiCad schematic and PCB files.

## v0.1.0 (Current)

### Core Infrastructure
- [x] S-expression parser with round-trip editing
- [x] Schematic parsing (symbols, wires, labels, hierarchy)
- [x] PCB parsing (footprints, nets, traces, vias, zones)
- [x] Symbol library parsing
- [x] BOM extraction
- [x] Manufacturer design rules (JLCPCB, OSHPark, PCBWay, Seeed)
- [x] ERC/DRC report parsing
- [x] KiCad CLI integration (kicad-cli wrapper)

### Unified CLI (`kct`)
- [x] `kct symbols` - List symbols with filtering
- [x] `kct nets` - Trace and analyze nets
- [x] `kct bom` - Generate bill of materials
- [x] `kct erc` - Run/parse ERC reports
- [x] `kct drc` - Run/parse DRC reports with manufacturer rules
- [x] JSON output for all commands
- [x] Predictable exit codes (0=success, 1=error, 2=warnings)

### Additional Tools
- [x] `kicad-pcb-query` - PCB queries (summary, footprints, nets, traces)
- [x] `kicad-pcb-modify` - PCB modifications (move, rotate, flip)
- [x] `kicad-lib-symbols` - Library symbol listing

---

## Next: Enhanced Python API

### Fluent Query API
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

### PCB API
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

### Cross-Reference
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

## Future: Validation & Design Rules

### Built-in Validation
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

### Manufacturer Profiles
```python
from kicad_tools.manufacturers import JLCPCB, OSHPark

# Check PCB against manufacturer rules
jlcpcb = JLCPCB(layer_count=4, copper_weight="1oz")
violations = jlcpcb.check(pcb)

# Get manufacturing capabilities
print(jlcpcb.min_trace_width)    # 0.127mm
print(jlcpcb.min_via_diameter)   # 0.3mm
```

### BOM Validation
```python
from kicad_tools.bom import BOMValidator

validator = BOMValidator(bom)
validator.check_availability("jlcpcb")  # Check JLCPCB parts library
validator.check_alternates()            # Suggest alternates for unavailable parts
```

---

## Future: Manufacturing Automation

### Gerber Export
```bash
kicad gerbers board.kicad_pcb --manufacturer jlcpcb --output gerbers/
```

```python
from kicad_tools.export import GerberExporter

exporter = GerberExporter(pcb, manufacturer="jlcpcb")
exporter.export("gerbers/")
exporter.create_zip("board_gerbers.zip")
```

### Pick & Place
```bash
kicad pnp board.kicad_pcb --format jlcpcb --output pnp.csv
```

### Assembly Package
```bash
kicad package board.kicad_pcb \
  --manufacturer jlcpcb \
  --output assembly/
# Creates: gerbers.zip, bom.csv, pnp.csv
```

---

## Future: Library Management

### Symbol Library Tools
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

### Footprint Library Tools
```python
from kicad_tools.library import FootprintLibrary

lib = FootprintLibrary.load("project.pretty")

# Parametric footprint generation
lib.create_soic(pins=8, pitch=1.27, name="SOIC-8")
lib.create_qfp(pins=48, pitch=0.5, name="LQFP-48")
```

---

## Future: Advanced Features

### Netlist Operations
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

### Diff & Merge
```bash
kicad diff old.kicad_sch new.kicad_sch --output diff.html
kicad diff old.kicad_pcb new.kicad_pcb --visual
```

### AI Integration Hooks
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

| Version | Focus | Status |
|---------|-------|--------|
| 0.1.0 | Unified CLI (`kct`), core parsing, manufacturer rules | Current |
| 0.2.0 | Enhanced Python API | Next |
| 0.3.0 | Validation & BOM checking | |
| 0.4.0 | Manufacturing automation (gerbers, pick-and-place) | |
| 0.5.0 | Library management | |
| 1.0.0 | Stable API, full documentation | |

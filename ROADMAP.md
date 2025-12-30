# kicad-tools Roadmap

Standalone Python tools for parsing and manipulating KiCad schematic and PCB files.

## v0.1.0 (Released)

### Core Infrastructure
- [x] S-expression parser with round-trip editing
- [x] Schematic parsing (symbols, wires, labels, hierarchy)
- [x] PCB parsing (footprints, nets, traces, vias, zones)
- [x] Symbol library parsing
- [x] BOM extraction
- [x] Manufacturer design rules (JLCPCB, OSHPark, PCBWay, Seeed)
- [x] ERC/DRC report parsing
- [x] KiCad CLI integration (kicad-cli wrapper)

### Unified CLI (`kicad-tools` / `kct`)
- [x] `kct symbols` - List symbols with filtering
- [x] `kct nets` - Trace and analyze nets
- [x] `kct bom` - Generate bill of materials
- [x] `kct erc` - Run/parse ERC reports
- [x] `kct drc` - Run/parse DRC reports with manufacturer rules
- [x] `kct sch` - Schematic tools (summary, hierarchy, labels, validate)
- [x] `kct pcb` - PCB tools (summary, footprints, nets, traces, stackup)
- [x] `kct lib` - Library tools (symbols)
- [x] `kct mfr` - Manufacturer tools (list, info, rules, compare)
- [x] JSON output for all commands
- [x] Predictable exit codes (0=success, 1=error, 2=warnings)

---

## v0.2.0 (Released): Manufacturing Readiness

Focus: Complete the design-to-manufacturing workflow with parts database integration and assembly export.

**Status: Released** - All planned features implemented plus significant bonus capabilities (autorouter, footprint tools, conflict detection).

### LCSC Parts Integration

Connect BOM generation to JLCPCB's LCSC parts database for availability checking and pricing.

```python
from kicad_tools.parts import LCSCClient

# Direct part lookup
client = LCSCClient()
part = client.lookup("C123456")
print(f"Stock: {part.stock}, Price: ${part.price_usd}")

# Search for parts
results = client.search("100nF 0402 X7R")
for part in results[:5]:
    print(f"{part.lcsc}: {part.description} - ${part.price_usd}")
```

```python
from kicad_tools.schema.bom import extract_bom

# BOM with availability checking
bom = extract_bom("project.kicad_sch", hierarchical=True)
availability = bom.check_availability("jlcpcb")

print(f"Available: {availability.available_count}/{availability.total_count}")

for item in availability.unavailable:
    print(f"  {item.reference}: {item.value} ({item.lcsc or 'no LCSC#'})")
    for alt in item.alternates[:3]:
        print(f"    → {alt.lcsc}: {alt.description} ({alt.stock} in stock)")
```

```bash
# CLI commands
kct bom project.kicad_sch --check-lcsc
kct bom project.kicad_sch --check-lcsc --mfr jlcpcb --format csv

kct parts lookup C123456
kct parts search "100nF 0402 X7R" --in-stock
```

### Assembly Package Export

Generate complete manufacturing packages for JLCPCB, Seeed, and other fabs.

```bash
# Full assembly package (gerbers + BOM + pick-and-place)
kct export assembly board.kicad_pcb --mfr jlcpcb --output mfr/
# Creates:
#   mfr/gerbers.zip          - Gerber + drill files
#   mfr/bom_jlcpcb.csv       - BOM in JLCPCB format (LCSC part numbers)
#   mfr/cpl_jlcpcb.csv       - Component placement list

# Individual exports
kct export gerbers board.kicad_pcb --mfr jlcpcb --output gerbers/
kct export pnp board.kicad_pcb --mfr jlcpcb --output cpl.csv
kct export bom project.kicad_sch --format jlcpcb --output bom.csv
```

```python
from kicad_tools.export import AssemblyPackage

# Python API
pkg = AssemblyPackage.create(
    pcb="board.kicad_pcb",
    schematic="project.kicad_sch",
    manufacturer="jlcpcb"
)
pkg.export("manufacturing/")

# Or step by step
from kicad_tools.export import GerberExporter, PickAndPlace, BOMExporter

gerbers = GerberExporter(pcb, manufacturer="jlcpcb")
gerbers.export("gerbers/")
gerbers.create_zip("gerbers.zip")

pnp = PickAndPlace(pcb, manufacturer="jlcpcb")
pnp.export("cpl_jlcpcb.csv")

bom_exp = BOMExporter(bom, manufacturer="jlcpcb")
bom_exp.export("bom_jlcpcb.csv")
```

### Fluent Query API

Improved Python API for common schematic and PCB queries.

```python
from kicad_tools import Schematic, PCB

# Schematic queries
sch = Schematic.load("project.kicad_sch")
u1 = sch.symbols.by_reference("U1")
caps = sch.symbols.filter(value="100nF")
power_symbols = sch.symbols.filter(lib_id__startswith="power:")

# PCB queries
pcb = PCB.load("board.kicad_pcb")
fp = pcb.footprints.by_reference("U1")
gnd = pcb.nets["GND"]
qfp_footprints = pcb.footprints.filter(footprint__contains="QFP")
```

### Cross-Reference API

Link schematics to PCBs for unified project queries.

```python
from kicad_tools import Project

project = Project.load("project.kicad_pro")

# Cross-reference symbols to footprints
for sym in project.schematic.symbols.filter(prefix="C"):
    fp = project.pcb.footprints.by_reference(sym.reference)
    if fp:
        print(f"{sym.reference}: {sym.value} at PCB position {fp.position}")

# Find unplaced components
unplaced = project.find_unplaced_symbols()
for sym in unplaced:
    print(f"Not on PCB: {sym.reference} ({sym.value})")
```

### What's NOT in v0.2.0
- Pure Python DRC (deferred to v0.3.0)
- Symbol/footprint library creation (deferred to v0.4.0)
- Cost estimation and quoting
- Autorouter CLI exposure

---

## v0.3.0: Validation & CI/CD

Focus: Pure Python design rule checking without requiring kicad-cli.

### Pure Python DRC
```python
from kicad_tools.validate import DRCChecker

# Check PCB against manufacturer rules (no kicad-cli needed)
checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4)
results = checker.check_all()

for violation in results.violations:
    print(f"{violation.severity}: {violation.message}")
    print(f"  Location: {violation.location}")
    print(f"  Rule: {violation.rule}")
```

### CI/CD Integration
```bash
# Exit codes for CI pipelines
kct check board.kicad_pcb --mfr jlcpcb --format json
# Returns: 0 = pass, 1 = errors, 2 = warnings only

# Pre-commit hook
kct validate project.kicad_sch --strict
```

### Design Rule Comparison
```python
from kicad_tools.validate import compare_to_rules

# Check if design meets manufacturer requirements
pcb = PCB.load("board.kicad_pcb")
results = compare_to_rules(pcb, manufacturer="jlcpcb")

if results.compatible:
    print("Design is compatible with JLCPCB")
else:
    for issue in results.issues:
        print(f"Incompatible: {issue}")
```

---

## v0.4.0: Library Management

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

## v0.5.0: Advanced Features

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
kct diff old.kicad_sch new.kicad_sch --output diff.html
kct diff old.kicad_pcb new.kicad_pcb --visual
```

### Autorouter CLI
```bash
# Expose the existing Python autorouter via CLI
kct route board.kicad_pcb --strategy adaptive --output routed.kicad_pcb
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
│   ├── bom.py            # BOM extraction
│   ├── library.py        # Library models
│   └── ...
├── cli/
│   ├── __init__.py       # Unified CLI entry point
│   ├── sch_*.py          # Schematic subcommands
│   ├── pcb_*.py          # PCB subcommands
│   ├── generate_bom.py   # BOM commands
│   ├── mfr.py            # Manufacturer commands
│   └── ...
├── parts/                # v0.2.0: Parts database
│   ├── lcsc.py           # LCSC API client
│   ├── cache.py          # Local caching
│   └── models.py         # Part data models
├── export/               # v0.2.0: Manufacturing export
│   ├── gerber.py         # Gerber generation
│   ├── pnp.py            # Pick-and-place files
│   ├── bom_formats.py    # Manufacturer BOM formats
│   └── assembly.py       # Assembly package
├── validate/             # v0.3.0: Validation
│   ├── erc.py            # Electrical rules
│   └── drc.py            # Design rules
├── manufacturers/
│   ├── jlcpcb.py
│   ├── oshpark.py
│   └── rules/            # Design rule files
├── router/               # Autorouter (existing)
│   ├── autorouter.py
│   └── heuristics.py
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
| 0.1.0 | Unified CLI (`kct`), core parsing, manufacturer rules | Released |
| 0.2.0 | Manufacturing readiness: LCSC integration, assembly export, fluent API, autorouter, footprint tools | Released |
| 0.3.0 | Validation & CI/CD: Pure Python DRC, design rule checking | Next |
| 0.4.0 | Library management: Symbol/footprint creation and editing | Planned |
| 0.5.0 | Advanced: Diff/merge, netlist analysis | Planned |
| 1.0.0 | Stable API, full documentation, production ready | Planned |

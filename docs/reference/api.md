# Python API Reference

This document provides an overview of the kicad-tools Python API.

---

## Quick Import

```python
from kicad_tools import (
    # Core
    Schematic, PCB, Project,
    load_schematic, save_schematic,
    load_pcb, save_pcb,

    # Query API
    SymbolQuery, FootprintQuery,

    # Reasoning
    PCBReasoningAgent, PCBState,

    # BOM
    BOM, extract_bom,
)
```

---

## Core Classes

### `Schematic`

Represents a KiCad schematic file.

```python
from kicad_tools import Schematic

# Load from file
sch = Schematic.load("project.kicad_sch")

# Access symbols
for symbol in sch.symbols:
    print(f"{symbol.reference}: {symbol.value}")

# Query API
caps = sch.symbols.filter(value="100nF")
resistors = sch.symbols.filter(reference__startswith="R")

# Save changes
sch.save()
sch.save("modified.kicad_sch")
```

**Properties:**
- `symbols` - `SymbolList` of all symbols
- `sheets` - List of hierarchical sheets
- `nets` - List of nets
- `wires` - List of wire segments
- `labels` - List of labels

---

### `PCB`

Represents a KiCad PCB file.

```python
from kicad_tools import PCB

# Load from file
pcb = PCB.load("board.kicad_pcb")

# Access footprints
for fp in pcb.footprints:
    print(f"{fp.reference}: {fp.footprint}")

# Query API
smd = pcb.footprints.smd()
bottom = pcb.footprints.filter(layer="B.Cu")

# Save changes
pcb.save()
```

**Properties:**
- `footprints` - `FootprintList` of all footprints
- `tracks` - List of traces
- `vias` - List of vias
- `zones` - List of copper zones
- `nets` - List of nets

---

### `Project`

Unified interface for complete KiCad projects.

```python
from kicad_tools import Project

# Load project
project = Project.load("project.kicad_pro")

# Access schematic and PCB
sch = project.schematic
pcb = project.pcb

# Cross-reference
result = project.cross_reference()

# Export for manufacturing
project.export_assembly("output/", manufacturer="jlcpcb")
```

---

## Query API

### `SymbolQuery` / `SymbolList`

Fluent interface for querying symbols.

```python
# Get all symbols
all_symbols = sch.symbols

# Filter by attribute
caps = sch.symbols.filter(value="100nF")
resistors = sch.symbols.filter(reference__startswith="R")
power = sch.symbols.filter(lib_id__contains="power")

# Filter by mounting
smd = sch.symbols.smd()
tht = sch.symbols.tht()

# Chain filters
smd_caps = sch.symbols.filter(value="100nF").smd()

# Get single item
u1 = sch.symbols.by_reference("U1")

# Iterate
for symbol in sch.symbols.filter(value="10k"):
    print(symbol.reference)
```

**Filter operators:**
- `field=value` - Exact match
- `field__startswith=prefix` - Starts with
- `field__endswith=suffix` - Ends with
- `field__contains=substring` - Contains

---

### `FootprintQuery` / `FootprintList`

Same fluent interface for PCB footprints.

```python
# Filter footprints
smd = pcb.footprints.smd()
qfp = pcb.footprints.filter(footprint__contains="QFP")
bottom = pcb.footprints.filter(layer="B.Cu")

# Get by reference
u1_fp = pcb.footprints.by_reference("U1")
```

---

## Router API

### `Autorouter`

A* pathfinding-based PCB autorouter.

```python
from kicad_tools.router import Autorouter, DesignRules

# Configure rules
rules = DesignRules(
    trace_width=0.2,      # mm
    clearance=0.15,       # mm
    via_drill=0.3,        # mm
    via_diameter=0.6,     # mm
)

# Create router
router = Autorouter(
    width=100,    # Board width mm
    height=80,    # Board height mm
    rules=rules,
)

# Add components
router.add_component("U1", pads=[...])
router.add_component("R1", pads=[...])

# Add net
router.add_net("VCC", ["U1.1", "R1.1"])

# Route
result = router.route_all()
print(f"Routed: {result.routed_nets}/{result.total_nets}")
```

---

## Reasoning API

### `PCBReasoningAgent`

LLM-driven PCB layout agent.

```python
from kicad_tools import PCBReasoningAgent

# Load board
agent = PCBReasoningAgent.from_pcb("board.kicad_pcb")

# Reasoning loop
while not agent.is_complete():
    # Get prompt for LLM
    prompt = agent.get_prompt()

    # Call your LLM
    command = your_llm(prompt)

    # Execute and get feedback
    result, diagnosis = agent.execute(command)

# Save result
agent.save("routed.kicad_pcb")
```

**Available commands:**
- `ROUTE <net>` - Route a specific net
- `MOVE <ref> <x> <y>` - Move component
- `VIA <x> <y>` - Place via
- `PRIORITY <net>` - Set net priority
- `COMPLETE` - Mark routing complete

---

## BOM API

### `extract_bom`

Extract bill of materials from schematic.

```python
from kicad_tools import extract_bom

bom = extract_bom("project.kicad_sch")

for item in bom.items:
    print(f"{item.reference}: {item.value} ({item.footprint})")

# Group identical parts
grouped = bom.grouped()
for group in grouped:
    refs = ", ".join(g.reference for g in group)
    print(f"{group[0].value}: {refs}")

# Export
bom.to_csv("bom.csv")
bom.to_json("bom.json")
```

---

## DRC API

### `DRCChecker`

Pure Python design rule checking.

```python
from kicad_tools.drc import DRCChecker
from kicad_tools.manufacturers import JLCPCBRules

# Create checker with manufacturer rules
checker = DRCChecker(rules=JLCPCBRules())

# Run check
violations = checker.check("board.kicad_pcb")

for v in violations:
    print(f"{v.type}: {v.message} at {v.location}")
```

---

## Progress Callbacks

For long-running operations:

```python
from kicad_tools import create_print_callback

# Create callback that prints progress
callback = create_print_callback()

# Use with router
router.route_all(progress=callback)

# Use with export
project.export_assembly("output/", progress=callback)
```

---

## See Also

- [Architecture Overview](../architecture.md) - How modules fit together
- [CLI Reference](cli.md) - Command-line interface
- [Examples](https://github.com/rjwalters/kicad-tools/tree/main/examples)

# Tutorial: Using the Query API for Custom Analysis

This tutorial covers the fluent query API for advanced filtering and analysis of KiCad designs. The API is inspired by Django's ORM, making it intuitive for developers familiar with that pattern.

## Overview

kicad-tools provides a query interface for:
- **Symbols** - Schematic components
- **Footprints** - PCB components

The API supports chained operations like `filter()`, `exclude()`, `order_by()`, and Django-style field lookups like `reference__startswith="C"`.

## Prerequisites

```bash
pip install kicad-tools
```

## Basic Queries

### Direct Access Methods

The simplest way to query elements:

```python
from kicad_tools import Schematic

sch = Schematic.load("project.kicad_sch")

# Get by exact reference
u1 = sch.symbols.by_reference("U1")

# Filter by attribute
caps = sch.symbols.filter(reference__startswith="C")
resistors = sch.symbols.filter(reference__startswith="R")

# Get first match
first_ic = sch.symbols.filter(reference__startswith="U").first()

# Check if any match
has_leds = sch.symbols.filter(reference__startswith="D").exists()
```

### Query Objects

For complex queries, use the query builder:

```python
# Create a query object
query = sch.symbols.query()

# Chain operations
power_ics = (
    query
    .filter(reference__startswith="U")
    .exclude(lib_id__startswith="power:")
    .order_by("reference")
    .all()
)

# Result is a list of SymbolInstance objects
for symbol in power_ics:
    print(f"{symbol.reference}: {symbol.value}")
```

## Field Lookups

The query API supports Django-style field lookups using double underscores:

### Exact Match (default)

```python
# Find all 100nF capacitors
caps = sch.symbols.filter(value="100nF")

# Equivalent to
caps = sch.symbols.filter(value__exact="100nF")
```

### String Operations

```python
# Starts with
resistors = sch.symbols.filter(reference__startswith="R")

# Ends with
nf_caps = sch.symbols.filter(value__endswith="nF")

# Contains
usb_parts = sch.symbols.filter(lib_id__contains="USB")

# Case-insensitive variants
all_caps = sch.symbols.filter(value__icontains="nf")
exact_match = sch.symbols.filter(value__iexact="100NF")
```

### Regex Matching

```python
# Match pattern
high_value_caps = sch.symbols.filter(value__regex=r"\d+[um]F")

# Match resistors in k or M range
big_resistors = sch.symbols.filter(value__regex=r"\d+[kM]")
```

### Numeric Comparisons

```python
# Greater than
rotated = sch.symbols.filter(rotation__gt=0)

# Less than
left_side = sch.symbols.filter(position_x__lt=100)

# Greater than or equal
bottom_half = sch.symbols.filter(position_y__gte=150)

# Less than or equal
top_half = sch.symbols.filter(position_y__lte=150)
```

### In List

```python
# Match any value in list
common_caps = sch.symbols.filter(value__in=["100nF", "10nF", "1uF"])

# Match multiple references
specific = sch.symbols.filter(reference__in=["U1", "U2", "U3"])
```

## Combining Filters

### Multiple Conditions (AND)

```python
# All conditions must match
smd_caps = sch.symbols.filter(
    reference__startswith="C",
    lib_id__contains="SMD",
)

# Chain filters (same effect)
smd_caps = (
    sch.symbols
    .filter(reference__startswith="C")
    .filter(lib_id__contains="SMD")
)
```

### Excluding Results

```python
# Exclude power symbols
active_components = (
    sch.symbols
    .exclude(lib_id__startswith="power:")
    .exclude(lib_id__contains=":PWR_")
)

# Combine filter and exclude
non_power_ics = (
    sch.symbols
    .filter(reference__startswith="U")
    .exclude(lib_id__contains="power:")
)
```

## Sorting Results

```python
# Sort by reference (ascending)
sorted_caps = (
    sch.symbols
    .filter(reference__startswith="C")
    .order_by("reference")
)

# Sort descending
reverse_sorted = (
    sch.symbols
    .filter(reference__startswith="C")
    .order_by("-reference")
)

# Multiple sort keys
by_value_then_ref = (
    sch.symbols
    .filter(reference__startswith="R")
    .order_by("value", "reference")
)
```

## Query Results

### Getting All Results

```python
# Get list of all matches
all_caps = sch.symbols.filter(reference__startswith="C").all()

# Iterate directly (no .all() needed)
for cap in sch.symbols.filter(reference__startswith="C"):
    print(cap.reference)
```

### First and Last

```python
# Get first match (or None)
first = sch.symbols.filter(reference__startswith="U").first()

# Get last match (or None)
last = sch.symbols.filter(reference__startswith="U").last()
```

### Count and Existence

```python
# Count matches
cap_count = sch.symbols.filter(reference__startswith="C").count()

# Check if any exist
has_inductors = sch.symbols.filter(reference__startswith="L").exists()
```

### Slicing

```python
# Get first 5
first_five = sch.symbols.filter(reference__startswith="R")[:5]

# Skip first 10, take next 5
page = sch.symbols.filter(reference__startswith="R")[10:15]
```

## PCB Footprint Queries

The same query API works for PCB footprints:

```python
from kicad_tools import PCB

pcb = PCB.load("board.kicad_pcb")

# Filter footprints
smd_parts = pcb.footprints.filter(layer="F.Cu")
qfp_packages = pcb.footprints.filter(footprint__contains="QFP")

# Shortcuts
all_smd = pcb.footprints.smd()      # Top layer SMD
all_tht = pcb.footprints.tht()      # Through-hole
```

### Position-Based Queries

```python
# Components in a region
top_left = pcb.footprints.filter(
    position_x__lt=50,
    position_y__lt=50,
)

# Components along an edge
edge_connectors = pcb.footprints.filter(
    position_x__gt=95,  # Near right edge
    reference__startswith="J",
)
```

## Complete Examples

### Example 1: BOM Analysis

```python
from collections import Counter
from kicad_tools import Schematic

sch = Schematic.load("project.kicad_sch")

# Count component types
types = Counter()
for symbol in sch.symbols:
    prefix = ''.join(c for c in symbol.reference if c.isalpha())
    types[prefix] += 1

print("Component breakdown:")
for prefix, count in types.most_common():
    print(f"  {prefix}: {count}")

# Find most common capacitor values
cap_values = Counter(
    c.value for c in sch.symbols.filter(reference__startswith="C")
)
print("\nMost common capacitor values:")
for value, count in cap_values.most_common(5):
    print(f"  {value}: {count}")
```

### Example 2: Find Unplaced Components

```python
from kicad_tools import Schematic, PCB

sch = Schematic.load("project.kicad_sch")
pcb = PCB.load("project.kicad_pcb")

# Get references from schematic
sch_refs = {s.reference for s in sch.symbols}

# Get references from PCB
pcb_refs = {f.reference for f in pcb.footprints}

# Find missing from PCB
unplaced = sch_refs - pcb_refs
if unplaced:
    print("Components in schematic but not on PCB:")
    for ref in sorted(unplaced):
        symbol = sch.symbols.by_reference(ref)
        print(f"  {ref}: {symbol.value}")
```

### Example 3: Design Rule Check

```python
from kicad_tools import PCB

pcb = PCB.load("board.kicad_pcb")

# Check for components too close to edge
EDGE_MARGIN = 2.0  # mm

edge_violations = []
for fp in pcb.footprints:
    x, y = fp.position
    if x < EDGE_MARGIN or y < EDGE_MARGIN:
        edge_violations.append(fp)
    # Add checks for max x/y based on board dimensions

if edge_violations:
    print("Components too close to board edge:")
    for fp in edge_violations:
        print(f"  {fp.reference} at ({fp.position[0]:.1f}, {fp.position[1]:.1f})")
```

### Example 4: Generate Custom Report

```python
from kicad_tools import Schematic
import json

sch = Schematic.load("project.kicad_sch")

# Build structured report
report = {
    "file": "project.kicad_sch",
    "statistics": {
        "total_components": len(sch.symbols),
        "ics": sch.symbols.filter(reference__startswith="U").count(),
        "passives": sum([
            sch.symbols.filter(reference__startswith="R").count(),
            sch.symbols.filter(reference__startswith="C").count(),
            sch.symbols.filter(reference__startswith="L").count(),
        ]),
    },
    "ics": [
        {
            "reference": s.reference,
            "value": s.value,
            "library": s.lib_id,
        }
        for s in sch.symbols.filter(reference__startswith="U").order_by("reference")
    ],
    "high_value_resistors": [
        s.reference
        for s in sch.symbols.filter(
            reference__startswith="R",
            value__regex=r"\d+[kM]"
        )
    ],
}

print(json.dumps(report, indent=2))
```

### Example 5: Cross-Reference Schematic and PCB

```python
from kicad_tools import Schematic, PCB

sch = Schematic.load("project.kicad_sch")
pcb = PCB.load("project.kicad_pcb")

print("Cross-Reference Report")
print("=" * 60)

# Match schematic symbols to PCB footprints
for symbol in sch.symbols.order_by("reference"):
    fp = pcb.footprints.by_reference(symbol.reference)

    if fp:
        print(f"{symbol.reference}: {symbol.value}")
        print(f"  Schematic lib: {symbol.lib_id}")
        print(f"  PCB footprint: {fp.footprint}")
        print(f"  Position: ({fp.position[0]:.1f}, {fp.position[1]:.1f})")
        print(f"  Rotation: {fp.rotation}°")
    else:
        print(f"{symbol.reference}: {symbol.value} - NOT ON PCB")
    print()
```

## Available Fields

### Symbol Fields

| Field | Type | Description |
|-------|------|-------------|
| `reference` | str | Component reference (R1, C2, U3) |
| `value` | str | Component value (10k, 100nF) |
| `lib_id` | str | Library identifier (Device:R) |
| `unit` | int | Unit number for multi-unit parts |
| `position` | tuple | (x, y) position in schematic |
| `position_x` | float | X coordinate |
| `position_y` | float | Y coordinate |
| `rotation` | float | Rotation in degrees |

### Footprint Fields

| Field | Type | Description |
|-------|------|-------------|
| `reference` | str | Component reference |
| `footprint` | str | Footprint name |
| `layer` | str | Layer (F.Cu, B.Cu) |
| `position` | tuple | (x, y) position on board |
| `position_x` | float | X coordinate |
| `position_y` | float | Y coordinate |
| `rotation` | float | Rotation in degrees |

## Next Steps

- **[Schematic Analysis](schematic-analysis.md)** - More schematic operations
- **[DRC with Manufacturer Rules](drc-manufacturer-rules.md)** - Design validation
- **[Manufacturing Export](manufacturing-export.md)** - Generate production files

# Tutorial: Analyzing a Schematic

This tutorial covers how to extract information from KiCad schematic files, including symbols, nets, and bill of materials.

## Overview

kicad-tools provides both CLI commands and a Python API for schematic analysis:

| Task | CLI Command | Python Class |
|------|-------------|--------------|
| List symbols | `kct symbols` | `Schematic.symbols` |
| Trace nets | `kct nets` | `Schematic` operations |
| Generate BOM | `kct bom` | `extract_bom()` |

## Prerequisites

Install kicad-tools if you haven't already:

```bash
pip install kicad-tools
```

## Working with Symbols

### CLI: List All Symbols

```bash
kct symbols project.kicad_sch
```

Output:
```
C1: 100nF
C2: 100nF
C3: 10uF
R1: 10k
R2: 4.7k
U1: ATmega328P
U2: LM7805
```

### CLI: Filter by Reference

```bash
# Only capacitors
kct symbols project.kicad_sch --filter "C*"

# Only ICs
kct symbols project.kicad_sch --filter "U*"
```

### CLI: JSON Output

```bash
kct symbols project.kicad_sch --format json
```

```json
[
  {
    "reference": "C1",
    "value": "100nF",
    "lib_id": "Device:C",
    "unit": 1,
    "position": [100.0, 50.0],
    "rotation": 0
  },
  ...
]
```

### Python API: Basic Symbol Access

```python
from kicad_tools import Schematic

# Load schematic
sch = Schematic.load("project.kicad_sch")

# Iterate all symbols
for symbol in sch.symbols:
    print(f"{symbol.reference}: {symbol.value}")

# Count symbols
print(f"Total components: {len(sch.symbols)}")
```

### Python API: Finding Specific Symbols

```python
from kicad_tools import Schematic

sch = Schematic.load("project.kicad_sch")

# Get by exact reference
u1 = sch.symbols.by_reference("U1")
if u1:
    print(f"Found {u1.reference}: {u1.value}")
    print(f"  Library: {u1.lib_id}")
    print(f"  Position: {u1.position}")

# Get multiple by reference pattern
capacitors = sch.symbols.filter(reference__startswith="C")
resistors = sch.symbols.filter(reference__startswith="R")

print(f"Capacitors: {len(capacitors)}")
print(f"Resistors: {len(resistors)}")
```

### Python API: Filter by Value

```python
# Find all 100nF capacitors
caps_100nf = sch.symbols.filter(value="100nF")

# Find all resistors over 10k (using regex)
high_value_r = sch.symbols.filter(
    reference__startswith="R",
    value__regex=r"[1-9]\d+k|[1-9]\d*M"
)

# Find components from a specific library
power_symbols = sch.symbols.filter(lib_id__contains="power:")
```

## Tracing Nets

Nets show how components are electrically connected.

### CLI: List All Nets

```bash
kct nets project.kicad_sch
```

Output:
```
VCC (4 connections)
  U1.VCC
  C1.1
  C2.1
  R1.1

GND (6 connections)
  U1.GND
  C1.2
  C2.2
  C3.2
  R2.2
  J1.3
```

### CLI: Trace a Specific Net

```bash
kct nets project.kicad_sch --net VCC
```

### CLI: JSON Format for Processing

```bash
kct nets project.kicad_sch --format json
```

```json
{
  "VCC": {
    "name": "VCC",
    "connections": [
      {"reference": "U1", "pin": "VCC"},
      {"reference": "C1", "pin": "1"},
      {"reference": "C2", "pin": "1"}
    ]
  },
  ...
}
```

## Generating a Bill of Materials

### CLI: Basic BOM

```bash
kct bom project.kicad_sch
```

Output:
```
Ref     Value       Footprint              Qty
---     -----       ---------              ---
C1      100nF       Capacitor_SMD:0402     1
C2      100nF       Capacitor_SMD:0402     1
C3      10uF        Capacitor_SMD:0805     1
R1      10k         Resistor_SMD:0402      1
R2      4.7k        Resistor_SMD:0402      1
U1      ATmega328P  Package_QFP:TQFP-32    1
```

### CLI: Grouped BOM

Group identical components to reduce row count:

```bash
kct bom project.kicad_sch --group
```

Output:
```
Value       Footprint              Qty   References
-----       ---------              ---   ----------
100nF       Capacitor_SMD:0402     2     C1, C2
10uF        Capacitor_SMD:0805     1     C3
10k         Resistor_SMD:0402      1     R1
4.7k        Resistor_SMD:0402      1     R2
ATmega328P  Package_QFP:TQFP-32    1     U1
```

### CLI: CSV Export

```bash
# For spreadsheet import
kct bom project.kicad_sch --format csv > bom.csv

# Grouped CSV
kct bom project.kicad_sch --format csv --group > bom_grouped.csv
```

### Python API: BOM Generation

```python
from kicad_tools import Schematic, extract_bom

sch = Schematic.load("project.kicad_sch")

# Generate BOM
bom = extract_bom(sch)

# Access BOM items
for item in bom.items:
    print(f"{item.reference}: {item.value} ({item.footprint})")

# Get BOM statistics
print(f"Total unique parts: {len(bom.items)}")
print(f"Total components: {sum(item.quantity for item in bom.items)}")
```

### Python API: Grouped BOM

```python
from kicad_tools import Schematic, extract_bom

sch = Schematic.load("project.kicad_sch")
bom = extract_bom(sch, group=True)

for item in bom.items:
    refs = ", ".join(item.references)
    print(f"{item.value} x{item.quantity}: {refs}")
```

## Hierarchical Schematics

kicad-tools handles multi-sheet schematics automatically.

### Detecting Hierarchy

```python
sch = Schematic.load("top_level.kicad_sch")

# Check for sub-sheets
if sch.sheets:
    print("This is a hierarchical schematic")
    for sheet in sch.sheets:
        print(f"  Sheet: {sheet.name} ({sheet.filename})")
```

### Loading All Sheets

```python
from kicad_tools import Project

# Load the entire project (all sheets)
project = Project.load("project.kicad_pro")

# Access all schematics in the project
for sch_path, sch in project.schematics.items():
    print(f"\n{sch_path}:")
    print(f"  Symbols: {len(sch.symbols)}")
```

## Complete Example: Component Summary

Here's a complete script that analyzes a schematic and produces a summary:

```python
#!/usr/bin/env python3
"""Analyze a KiCad schematic and print a summary."""

from collections import Counter
from kicad_tools import Schematic, extract_bom

def analyze_schematic(path: str):
    """Print detailed analysis of a schematic."""

    sch = Schematic.load(path)
    bom = extract_bom(sch, group=True)

    print(f"Schematic: {path}")
    print("=" * 50)

    # Component counts by type
    type_counts = Counter()
    for symbol in sch.symbols:
        prefix = ''.join(c for c in symbol.reference if c.isalpha())
        type_counts[prefix] += 1

    print("\nComponent Types:")
    for prefix, count in sorted(type_counts.items()):
        name = {
            'R': 'Resistors',
            'C': 'Capacitors',
            'L': 'Inductors',
            'D': 'Diodes',
            'Q': 'Transistors',
            'U': 'ICs',
            'J': 'Connectors',
            'SW': 'Switches',
        }.get(prefix, prefix)
        print(f"  {name}: {count}")

    print(f"\nTotal Components: {len(sch.symbols)}")
    print(f"Unique Parts: {len(bom.items)}")

    # Value distribution
    print("\nCapacitor Values:")
    caps = sch.symbols.filter(reference__startswith="C")
    cap_values = Counter(c.value for c in caps)
    for value, count in cap_values.most_common(5):
        print(f"  {value}: {count}")

    print("\nResistor Values:")
    resistors = sch.symbols.filter(reference__startswith="R")
    res_values = Counter(r.value for r in resistors)
    for value, count in res_values.most_common(5):
        print(f"  {value}: {count}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python analyze.py <schematic.kicad_sch>")
        sys.exit(1)
    analyze_schematic(sys.argv[1])
```

## Next Steps

- **[DRC with Manufacturer Rules](drc-manufacturer-rules.md)** - Validate your PCB design
- **[Manufacturing Export](manufacturing-export.md)** - Export for fabrication
- **[Query API](query-api.md)** - Advanced filtering techniques

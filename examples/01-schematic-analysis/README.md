# Example: Schematic Analysis

This example demonstrates how to load and analyze KiCad schematic files using the kicad-tools Python API.

## Files

- `simple_rc.kicad_sch` - A simple RC circuit schematic (resistor + capacitor)
- `analyze.py` - Python script demonstrating schematic analysis

## Usage

### Python API

```python
from kicad_tools import Schematic

# Load schematic
sch = Schematic.load("simple_rc.kicad_sch")

# Access symbols
for symbol in sch.symbols:
    print(f"{symbol.reference}: {symbol.value} ({symbol.lib_id})")

# Access net labels
for label in sch.labels:
    print(f"Net: {label.text}")

# Summary info
print(f"Symbols: {len(sch.symbols)}")
print(f"Wires: {len(sch.wires)}")
```

### Run the Example

```bash
cd examples/01-schematic-analysis
python analyze.py
```

### CLI Alternative

You can also use the command-line tools:

```bash
# List symbols
kct symbols simple_rc.kicad_sch

# List symbols in JSON format
kct symbols simple_rc.kicad_sch --format json

# Trace nets
kct nets simple_rc.kicad_sch

# Get schematic summary
kct sch summary simple_rc.kicad_sch
```

## Expected Output

```
Loading schematic: simple_rc.kicad_sch
============================================================

Title: Simple RC Circuit
Revision: 1.0
Date: 2024-01-01
Company: Test Corp

=== Symbols ===
Reference    Value           Library ID
------------------------------------------------------------
C1           100nF           Device:C
R1           10k             Device:R

=== Labels (Nets) ===
  VIN
  GND

=== Summary ===
Total symbols: 2
Total wires: 6
Total junctions: 2
Total labels: 2
```

## What You Can Learn

1. **Loading schematics** - Use `Schematic.load()` to parse `.kicad_sch` files
2. **Accessing symbols** - Iterate over `sch.symbols` to get component info
3. **Net discovery** - Use `sch.labels` to find named nets
4. **Title block** - Access project metadata via `sch.title_block`
5. **Hierarchy** - Check `sch.is_hierarchical()` and `sch.sheets` for multi-sheet designs

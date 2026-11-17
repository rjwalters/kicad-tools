# Example: BOM Generation

This example demonstrates how to extract a Bill of Materials (BOM) from KiCad schematic files using the kicad-tools Python API and CLI.

## Files

- `simple_rc.kicad_sch` - A simple RC circuit schematic
- `generate_bom.py` - Python script demonstrating BOM extraction

## Usage

### Python API

```python
from kicad_tools import extract_bom

# Extract BOM from schematic
bom = extract_bom("simple_rc.kicad_sch")

# Individual components
for item in bom.items:
    print(f"{item.reference}: {item.value} ({item.footprint})")

# Grouped by value+footprint
for group in bom.grouped():
    print(f"{group.quantity}x {group.value} - {group.references}")

# Summary info
print(f"Total: {bom.total_components} components, {bom.unique_parts} unique")
```

### Run the Example

```bash
cd examples/02-bom-generation
python generate_bom.py
```

### CLI Alternative

The command-line tool provides quick BOM generation:

```bash
# Table format (default)
kct bom simple_rc.kicad_sch

# CSV format for spreadsheets
kct bom simple_rc.kicad_sch --format csv > bom.csv

# JSON format for automation
kct bom simple_rc.kicad_sch --format json | jq .

# Grouped output
kct bom simple_rc.kicad_sch --group

# Exclude power symbols or test points
kct bom simple_rc.kicad_sch --exclude "TP*" --exclude "PWR*"
```

## Expected Output

```
Extracting BOM from: simple_rc.kicad_sch
======================================================================

=== Individual Components ===
Reference    Value           Footprint                           DNP
----------------------------------------------------------------------
C1           100nF           Capacitor_SMD:C_0603_1608Metric
R1           10k             Resistor_SMD:R_0603_1608Metric

=== Grouped BOM ===
Qty   References      Value           Footprint
----------------------------------------------------------------------
1     C1              100nF           Capacitor_SMD:C_0603_1608Metric
1     R1              10k             Resistor_SMD:R_0603_1608Metric

=== Summary ===
Total components: 2
Unique parts: 2
```

## BOM Features

### Grouping Options

Group components by different criteria:

```python
# Group by value and footprint (default)
groups = bom.grouped("value+footprint")

# Group by value only (ignores package size)
groups = bom.grouped("value")

# Group by manufacturer part number
groups = bom.grouped("mpn")
```

### Filtering

Filter BOM to specific components:

```python
# Include only resistors
resistors = bom.filter(reference_pattern="R*")

# Include DNP components
with_dnp = bom.filter(include_dnp=True)
```

### Component Properties

Access detailed component information:

```python
for item in bom.items:
    print(f"Reference: {item.reference}")
    print(f"Value: {item.value}")
    print(f"Footprint: {item.footprint}")
    print(f"Datasheet: {item.datasheet}")
    print(f"MPN: {item.mpn}")
    print(f"LCSC: {item.lcsc}")
    print(f"DNP: {item.dnp}")
```

## What You Can Learn

1. **BOM extraction** - Use `extract_bom()` to parse component data
2. **Grouping** - Combine identical parts using `bom.grouped()`
3. **Filtering** - Select specific components with `bom.filter()`
4. **Export formats** - Generate CSV, JSON, or custom outputs
5. **Hierarchical support** - Extract from multi-sheet designs

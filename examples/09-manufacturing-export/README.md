# Example: Manufacturing Export

This example demonstrates how to generate complete manufacturing packages for PCB assembly services using the kicad-tools Python API.

## Overview

The kicad-tools library provides a complete workflow from KiCad designs to manufacturer-ready files:

```
KiCad Design → kicad-tools → Manufacturer Package
     ↓                            ↓
  .kicad_pcb             ├─ Gerbers (zip)
  .kicad_sch             ├─ BOM (csv)
                         └─ Pick-and-Place (csv)
```

## Files

- `export_manufacturing.py` - Python script demonstrating all export workflows

## Quick Start

### Minimum Viable JLCPCB Export

```python
from kicad_tools.export import AssemblyPackage

# One-liner: Generate all files for JLCPCB
pkg = AssemblyPackage.create(
    pcb="board.kicad_pcb",
    schematic="board.kicad_sch",
    manufacturer="jlcpcb",
)
result = pkg.export("output/")

print(f"BOM: {result.bom_path}")
print(f"CPL: {result.pnp_path}")
print(f"Gerbers: {result.gerber_path}")
```

### Run the Example

```bash
cd examples/05-manufacturing-export
python export_manufacturing.py

# Or with specific files
python export_manufacturing.py board.kicad_pcb board.kicad_sch
```

## Supported Manufacturers

| Manufacturer | BOM Format | PnP Format | Gerber Preset | Assembly |
|--------------|------------|------------|---------------|----------|
| JLCPCB       | ✅         | ✅         | ✅            | ✅       |
| PCBWay       | ✅         | ✅         | ✅            | ✅       |
| OSH Park     | ❌         | ❌         | ✅            | ❌       |
| Seeed Fusion | ✅         | ❌         | ❌            | ✅       |
| Generic      | ✅         | ✅         | ✅            | N/A      |

## API Reference

### AssemblyPackage (Recommended)

The simplest way to generate manufacturing files:

```python
from kicad_tools.export import AssemblyPackage, AssemblyConfig

# Quick export with defaults
pkg = AssemblyPackage.create(
    pcb="board.kicad_pcb",
    schematic="board.kicad_sch",
    manufacturer="jlcpcb",
)
result = pkg.export("output/")

# Or with custom configuration
config = AssemblyConfig(
    include_gerbers=True,
    include_bom=True,
    include_pnp=True,
    exclude_references=["TP*", "MH*"],  # Exclude test points, mounting holes
)
pkg = AssemblyPackage(
    pcb_path="board.kicad_pcb",
    schematic_path="board.kicad_sch",
    manufacturer="pcbway",
    config=config,
)
result = pkg.export("output/")
```

### Individual Exporters

For fine-grained control over each export:

#### Gerber Export

```python
from kicad_tools.export import GerberExporter, GerberConfig

# Export with manufacturer preset
exporter = GerberExporter("board.kicad_pcb")
exporter.export_for_manufacturer("jlcpcb", "gerbers/")

# Or with custom config
config = GerberConfig(
    include_solderpaste=True,
    merge_pth_npth=False,
    create_zip=True,
)
exporter.export(config, "gerbers/")
```

#### BOM Export

```python
from kicad_tools import extract_bom
from kicad_tools.export import export_bom, BOMExportConfig

# Extract BOM from schematic
bom = extract_bom("board.kicad_sch")

# Export in manufacturer format
config = BOMExportConfig(
    include_dnp=False,
    group_by_value=True,
    include_lcsc=True,
)
csv_content = export_bom(bom.items, manufacturer="jlcpcb", config=config)

with open("bom_jlcpcb.csv", "w") as f:
    f.write(csv_content)
```

#### Pick-and-Place Export

```python
from kicad_tools.schema.pcb import PCB
from kicad_tools.export import export_pnp, PnPExportConfig

# Load PCB
pcb = PCB.load("board.kicad_pcb")

# Configure and export
config = PnPExportConfig(
    use_aux_origin=True,
    include_dnp=False,
)
csv_content = export_pnp(list(pcb.footprints), manufacturer="jlcpcb", config=config)

with open("positions.csv", "w") as f:
    f.write(csv_content)
```

### Manufacturer Profiles

Compare manufacturer capabilities:

```python
from kicad_tools.manufacturers import (
    get_profile,
    list_manufacturers,
    compare_design_rules,
    find_compatible_manufacturers,
)

# Get manufacturer info
jlc = get_profile("jlcpcb")
print(f"Min trace: {jlc.get_design_rules(4).min_trace_width_mm}mm")

# Compare design rules
rules = compare_design_rules(layers=4, copper_oz=1.0)
for mfr_id, rule in rules.items():
    print(f"{mfr_id}: {rule.min_trace_width_mm}mm trace")

# Find manufacturers for your design
compatible = find_compatible_manufacturers(
    trace_width_mm=0.15,
    clearance_mm=0.15,
    via_drill_mm=0.3,
    layers=4,
    needs_assembly=True,
)
```

## LCSC Part Numbers

JLCPCB uses LCSC part numbers for assembly. Here's how they flow into the BOM:

### 1. Add LCSC Field to Symbols

In KiCad's schematic editor:
1. Select a component
2. Add a field named "LCSC"
3. Set the value (e.g., "C123456")

### 2. Or Import Parts Programmatically

```python
from kicad_tools.parts import import_lcsc_part

# Import a part with LCSC number pre-populated
import_lcsc_part("C123456", library="MyParts.kicad_sym")
```

### 3. Access LCSC Numbers in BOM

```python
from kicad_tools import extract_bom

bom = extract_bom("board.kicad_sch")
for item in bom.items:
    print(f"{item.reference}: {item.value} - LCSC: {item.lcsc or 'not set'}")
```

### 4. JLCPCB BOM Format

The JLCPCB BOM formatter automatically includes the LCSC column:

```csv
Comment,Designator,Footprint,LCSC Part #
100nF,C1,C_0603_1608Metric,C123456
10k,R1,R_0603_1608Metric,C654321
```

## CLI Commands

Manufacturing export is also available via the command line:

```bash
# Complete assembly package
kct export --manufacturer jlcpcb board.kicad_pcb board.kicad_sch -o output/

# Gerbers only
kct export gerbers board.kicad_pcb --manufacturer jlcpcb -o gerbers/

# BOM in manufacturer format
kct bom board.kicad_sch --format jlcpcb -o bom.csv

# Compare manufacturer capabilities
kct mfr compare --layers 4

# Check design against manufacturer rules
kct mfr check board.kicad_pcb --manufacturer jlcpcb
```

## Expected Output

```
PCB: boards/01-voltage-divider/output/voltage_divider.kicad_pcb
Schematic: boards/01-voltage-divider/voltage_divider.kicad_sch
Output: examples/05-manufacturing-export/output

======================================================================
EXAMPLE 1: Quick Export with AssemblyPackage
======================================================================

Assembly package created:
Assembly Package: output/jlcpcb
  BOM: bom_jlcpcb.csv
  CPL: cpl_jlcpcb.csv
  Gerbers: gerbers

Generated files ready for upload to JLCPCB!

======================================================================
EXAMPLE 2: Individual Exporters
======================================================================

--- Gerber Export ---
Gerbers exported to: output/gerbers_custom/gerbers.zip

--- BOM Export ---
BOM exported to: output/custom_bom.csv
BOM preview (first 5 lines):
  Comment,Designator,Footprint,LCSC Part #
  10k,R1,Resistor_SMD:R_0603_1608Metric,
  ...

--- Pick-and-Place Export ---
CPL exported to: output/custom_cpl.csv
CPL preview (first 5 lines):
  Designator,Val,Package,Mid X,Mid Y,Rotation,Layer
  R1,10k,Resistor_SMD:R_0603_1608Metric,25.4000mm,12.7000mm,0.0,top
  ...
```

## JLCPCB Upload Checklist

After generating files, upload to JLCPCB:

1. **Gerber files**: Upload the `gerbers.zip` file
2. **BOM file**: Upload `bom_jlcpcb.csv` in the assembly section
3. **CPL file**: Upload `cpl_jlcpcb.csv` for component positions
4. **Verify parts**: Check that LCSC part numbers are recognized
5. **Review placement**: Use the 3D preview to verify component positions

## What You Can Learn

1. **Quick export** - Use `AssemblyPackage` for one-liner manufacturing output
2. **Gerber generation** - Configure layers, formats, and manufacturer presets
3. **BOM formatting** - Export in manufacturer-specific CSV formats
4. **Pick-and-place** - Generate component placement files
5. **Manufacturer comparison** - Compare capabilities and design rules
6. **LCSC integration** - How part numbers flow from schematic to BOM

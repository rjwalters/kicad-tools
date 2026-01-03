# Tutorial: Exporting for Manufacturing (JLCPCB Workflow)

This tutorial walks through exporting your KiCad design for PCB fabrication and assembly at JLCPCB, one of the most popular low-cost PCB manufacturers.

## Overview

To order assembled PCBs from JLCPCB, you need:

1. **Gerber files** - PCB fabrication data (copper layers, soldermask, silkscreen)
2. **Drill files** - Hole locations and sizes
3. **BOM (Bill of Materials)** - List of components with LCSC part numbers
4. **CPL (Component Placement List)** - Pick-and-place coordinates

kicad-tools can generate all of these in JLCPCB's required format.

## Prerequisites

```bash
pip install kicad-tools
```

You'll also need KiCad 8+ installed for Gerber generation via `kicad-cli`.

## Quick Start: One-Command Export

The fastest way to export everything:

```python
from kicad_tools.export import AssemblyPackage

# Create complete manufacturing package
pkg = AssemblyPackage.create(
    pcb="board.kicad_pcb",
    schematic="board.kicad_sch",
    manufacturer="jlcpcb",
)

# Export to output directory
result = pkg.export("output/jlcpcb/")

print(f"Exported to: {result.output_dir}")
print(f"  Gerbers: {result.gerber_zip}")
print(f"  BOM: {result.bom_file}")
print(f"  CPL: {result.cpl_file}")
```

This creates:
```
output/jlcpcb/
├── gerbers.zip          # Upload to JLCPCB for PCB fab
├── bom_jlcpcb.csv       # Upload for assembly BOM
└── cpl_jlcpcb.csv       # Upload for assembly placement
```

## Step-by-Step Export

### Step 1: Generate Gerber Files

```python
from kicad_tools.export import GerberExporter

# Create exporter
exporter = GerberExporter("board.kicad_pcb")

# Export with JLCPCB naming conventions
result = exporter.export_for_manufacturer("jlcpcb", "output/gerbers/")

print(f"Generated {len(result.files)} Gerber files")
for f in result.files:
    print(f"  {f}")
```

JLCPCB expects these files:
```
output/gerbers/
├── board-F_Cu.gtl        # Front copper
├── board-B_Cu.gbl        # Back copper
├── board-F_Mask.gts      # Front soldermask
├── board-B_Mask.gbs      # Back soldermask
├── board-F_SilkS.gto     # Front silkscreen
├── board-B_SilkS.gbo     # Back silkscreen
├── board-Edge_Cuts.gm1   # Board outline
├── board.drl             # Drill file
└── board-NPTH.drl        # Non-plated holes
```

### Step 2: Generate BOM

JLCPCB's BOM format requires LCSC part numbers:

```python
from kicad_tools import Schematic
from kicad_tools.export import export_bom

sch = Schematic.load("board.kicad_sch")

# Export in JLCPCB format
export_bom(
    schematic=sch,
    output_path="output/bom_jlcpcb.csv",
    manufacturer="jlcpcb",
)
```

Output format:
```csv
Comment,Designator,Footprint,LCSC Part Number
100nF,C1,Capacitor_SMD:C_0402,C1525
100nF,C2,Capacitor_SMD:C_0402,C1525
10k,R1,Resistor_SMD:R_0402,C25744
ATmega328P,U1,Package_QFP:TQFP-32,C14877
```

#### Adding LCSC Part Numbers

LCSC part numbers come from your schematic's component properties. In KiCad:

1. Open Symbol Properties
2. Add a field named `LCSC` (or `JLCPCB Part #`)
3. Enter the LCSC part number (e.g., `C1525`)

Or use kicad-tools to lookup parts:

```python
from kicad_tools.parts import lookup_lcsc_part

# Find LCSC part for a component
results = lookup_lcsc_part(
    value="100nF",
    footprint="0402",
    category="capacitor",
)

for part in results[:5]:
    print(f"{part.number}: {part.description} (${part.price})")
```

### Step 3: Generate CPL (Pick-and-Place)

```python
from kicad_tools import PCB
from kicad_tools.export import export_pnp

pcb = PCB.load("board.kicad_pcb")

# Export in JLCPCB format
export_pnp(
    pcb=pcb,
    output_path="output/cpl_jlcpcb.csv",
    manufacturer="jlcpcb",
)
```

Output format:
```csv
Designator,Val,Package,Mid X,Mid Y,Rotation,Layer
C1,100nF,0402,23.45,15.67,90,top
C2,100nF,0402,25.12,15.67,90,top
R1,10k,0402,30.00,20.00,0,top
U1,ATmega328P,TQFP-32,50.00,40.00,0,top
```

## CLI Commands

### Export Gerbers

```bash
# Export with JLCPCB naming
kct export gerbers board.kicad_pcb --mfr jlcpcb -o output/

# Generic export
kct export gerbers board.kicad_pcb -o output/
```

### Export BOM

```bash
# JLCPCB format
kct bom board.kicad_sch --format jlcpcb -o bom_jlcpcb.csv

# Generic CSV
kct bom board.kicad_sch --format csv --group -o bom.csv
```

## Complete JLCPCB Workflow

Here's a complete script for preparing a JLCPCB order:

```python
#!/usr/bin/env python3
"""
Generate complete JLCPCB manufacturing package.

Usage:
    python export_jlcpcb.py board.kicad_pcb board.kicad_sch output/
"""

import sys
from pathlib import Path
from kicad_tools import Schematic, PCB
from kicad_tools.export import (
    AssemblyPackage,
    GerberExporter,
    export_bom,
    export_pnp,
)
from kicad_tools.drc import check_manufacturer_rules

def export_for_jlcpcb(pcb_path: str, sch_path: str, output_dir: str):
    """Generate all files needed for JLCPCB order."""

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    print("JLCPCB Manufacturing Export")
    print("=" * 50)

    # Load files
    print("\n1. Loading design files...")
    pcb = PCB.load(pcb_path)
    sch = Schematic.load(sch_path)
    print(f"   PCB: {len(pcb.footprints)} footprints")
    print(f"   Schematic: {len(sch.symbols)} symbols")

    # Validate against JLCPCB rules
    print("\n2. Checking against JLCPCB design rules...")
    result = check_manufacturer_rules(pcb, "jlcpcb")
    if result.passed:
        print("   ✓ Design passes all JLCPCB rules")
    else:
        print("   ✗ Warnings:")
        for v in result.violations:
            print(f"     - {v}")

    # Generate Gerbers
    print("\n3. Generating Gerber files...")
    gerber_dir = output / "gerbers"
    exporter = GerberExporter(pcb_path)
    gerber_result = exporter.export_for_manufacturer("jlcpcb", str(gerber_dir))
    print(f"   Generated {len(gerber_result.files)} files")

    # Create ZIP for upload
    import shutil
    gerber_zip = output / "gerbers.zip"
    shutil.make_archive(str(output / "gerbers"), 'zip', gerber_dir)
    print(f"   Created: {gerber_zip}")

    # Generate BOM
    print("\n4. Generating BOM...")
    bom_path = output / "bom_jlcpcb.csv"
    export_bom(sch, str(bom_path), manufacturer="jlcpcb")
    print(f"   Created: {bom_path}")

    # Check for missing LCSC numbers
    missing_lcsc = []
    for symbol in sch.symbols:
        if not symbol.get_property("LCSC"):
            missing_lcsc.append(symbol.reference)
    if missing_lcsc:
        print(f"   ⚠ Missing LCSC numbers: {', '.join(missing_lcsc[:5])}")
        if len(missing_lcsc) > 5:
            print(f"     ...and {len(missing_lcsc) - 5} more")

    # Generate CPL
    print("\n5. Generating pick-and-place file...")
    cpl_path = output / "cpl_jlcpcb.csv"
    export_pnp(pcb, str(cpl_path), manufacturer="jlcpcb")
    print(f"   Created: {cpl_path}")

    # Summary
    print("\n" + "=" * 50)
    print("Export Complete!")
    print(f"\nFiles ready for JLCPCB upload:")
    print(f"  1. Gerbers:  {gerber_zip}")
    print(f"  2. BOM:      {bom_path}")
    print(f"  3. CPL:      {cpl_path}")
    print("\nNext steps:")
    print("  1. Go to jlcpcb.com and start a new order")
    print("  2. Upload gerbers.zip for PCB fabrication")
    print("  3. Enable 'SMT Assembly' and upload BOM + CPL")
    print("  4. Review and place order")

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python export_jlcpcb.py <pcb> <schematic> <output_dir>")
        sys.exit(1)
    export_for_jlcpcb(sys.argv[1], sys.argv[2], sys.argv[3])
```

## Other Manufacturers

kicad-tools supports multiple manufacturers:

### PCBWay

```python
pkg = AssemblyPackage.create(
    pcb="board.kicad_pcb",
    schematic="board.kicad_sch",
    manufacturer="pcbway",
)
result = pkg.export("output/pcbway/")
```

### Seeed Fusion

```python
pkg = AssemblyPackage.create(
    pcb="board.kicad_pcb",
    schematic="board.kicad_sch",
    manufacturer="seeed",
)
result = pkg.export("output/seeed/")
```

### OSHPark (PCB only, no assembly)

```python
from kicad_tools.export import GerberExporter

exporter = GerberExporter("board.kicad_pcb")
exporter.export_for_manufacturer("oshpark", "output/oshpark/")
```

## Troubleshooting

### "kicad-cli not found"

Install KiCad 8+ and ensure `kicad-cli` is in your PATH:

```bash
# macOS
export PATH="/Applications/KiCad/KiCad.app/Contents/MacOS:$PATH"

# Linux
export PATH="/usr/bin:$PATH"

# Windows
# Add KiCad bin folder to PATH in System Settings
```

### "Missing LCSC part numbers"

Add LCSC numbers to your schematic symbols:
1. Open KiCad Schematic Editor
2. Select component → Edit Properties
3. Add field: `LCSC` = `C1525` (or appropriate part number)

Find LCSC part numbers at: https://www.lcsc.com

### "CPL rotation is wrong"

JLCPCB may expect different rotation for some packages. You can add rotation corrections:

```python
from kicad_tools.export import export_pnp, PnPExportConfig

config = PnPExportConfig(
    rotation_offsets={
        "SOT-23": 180,      # Rotate SOT-23 by 180°
        "TQFP-32": 90,      # Rotate TQFP-32 by 90°
    }
)

export_pnp(pcb, "cpl.csv", manufacturer="jlcpcb", config=config)
```

## Next Steps

- **[Query API](query-api.md)** - Advanced filtering for design analysis
- **[Schematic Analysis](schematic-analysis.md)** - Deep dive into schematic parsing

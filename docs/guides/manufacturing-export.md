# Tutorial: Exporting for Manufacturing (JLCPCB Workflow)

This tutorial walks through exporting your KiCad design for PCB fabrication and assembly at JLCPCB, one of the most popular low-cost PCB manufacturers.

## Overview

To order assembled PCBs from JLCPCB, you need:

1. **Gerber files** - PCB fabrication data (copper layers, soldermask, silkscreen)
2. **Drill files** - Hole locations and sizes
3. **BOM (Bill of Materials)** - List of components with LCSC part numbers
4. **CPL (Component Placement List)** - Pick-and-place coordinates

kicad-tools can generate all of these in JLCPCB's required format.

The manufacturing package's `kicad_project.zip` includes the selected PCB,
adjacent schematic/project files, and the project-local `sym-lib-table` with
its referenced KiCad symbol libraries. Relative paths and `${KIPRJMOD}/`
paths retain their subdirectories after extraction. Repeated references
to the same file produce one archive member.

Project ZIP creation reports an export error for a malformed table, missing
library, or nonportable table entry (absolute paths, parent traversal,
host environment variables, non-KiCad libraries, or symlinks outside the
project). Move those dependencies under the project and use project-relative
URIs before exporting. Libraries supplied by KiCad's global installation
are not copied; footprint libraries and 3D models are outside this symbol
packaging support.

## Prerequisites

```bash
pip install kicad-tools
```

You'll also need KiCad 8+ installed for Gerber generation via `kicad-cli`.

## Are We Ship-Ready? (`kct fleet status`)

Before generating manufacturing artefacts, ask the repo whether every board
is actually ready to ship. `kct fleet status` walks every per-board
subdirectory under `boards/`, inspects the routed PCB, DRC report, and
manufacturing outputs, and surfaces a one-line verdict per board.

```bash
# Plain summary (default: --boards-dir boards, table output)
kct fleet status

# CI-friendly JSON for downstream tooling
kct fleet status --format json > fleet.json

# Only show what is actually ready to ship right now
kct fleet status --ship-only
```

A board is "ship-ready" when **all** gates are green:

1. Routed PCB exists at the expected glob (`*_routed.kicad_pcb` by default).
2. Net status (the same check `kct net-status` runs) is clean — no
   incomplete or unrouted nets.
3. DRC report exists and has zero errors.
4. Manufacturing artefacts (gerbers, BOM, CPL) are present and not stale
   relative to the routed PCB.

If any gate fails, the table view lists the first blocker; `--format json`
emits the full per-board breakdown. Treat `kct fleet status --ship-only`
returning a non-empty list as "go" for the export pipeline below.

See also: [CLI Reference → fleet status](../reference/cli.md#fleet-status).

---

## Routing Completeness Preflight

The default `kct build` sequence runs a routing-completeness preflight
between `stitch` and `verify` that blocks the build if any nets are
incomplete or unrouted. This closes the gap where `kct build` would emit
gerbers, BOM, and CPL even when nets were unconnected.

On failure the build prints, for example:

```text
preflight-routing: 3/214 nets incomplete
  /SDA: incomplete (2 segments missing)
  /SCL: incomplete (1 segment missing)
  /HALL_B: unrouted
```

…and halts before any manufacturing artefact is written.

Two escape hatches are recognised:

- `--allow-incomplete` — the advertised, CI-greppable opt-out. Use this for
  intentional WIP builds (e.g. preview gerbers for a board still in
  routing). Failure becomes a warning, the build continues.
- `--force` — global build override; parity with `--step sync --force`.
  Also converts the failure into a warning.

```bash
# Default end-to-end build (preflight runs automatically between stitch
# and verify; refuses to ship gerbers for a board with unrouted nets).
# `spec` is positional, not a flag.
kct build boards/05-bldc-motor-controller/project.kct

# Run only the preflight check (read-only, no side-effects).
kct build boards/05-bldc-motor-controller/project.kct \
    --step preflight-routing

# Bypass for an intentional WIP preview.
kct build boards/05-bldc-motor-controller/project.kct --allow-incomplete
```

The check is read-only and uses `NetStatusAnalyzer` in-process (no subprocess
overhead). See [CLI Reference → build](../reference/cli.md#build).

---

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

# Export with the JLCPCB preset (layer set, origin, drill options)
result = exporter.export_for_manufacturer("jlcpcb", "output/gerbers/")

print(f"Generated {len(result.files)} Gerber files")
for f in result.files:
    print(f"  {f}")
```

The zip holds kicad-cli's own file names, with Protel extensions and X2
`%TF.FileFunction` attributes, plus a `.gbrjob` job file:
```
output/gerbers/
├── board-F_Cu.gtl           # Front copper
├── board-B_Cu.gbl           # Back copper
├── board-In1_Cu.g1          # Inner copper (4-layer boards: In1/In2)
├── board-F_Mask.gts         # Front soldermask
├── board-B_Mask.gbs         # Back soldermask
├── board-F_Silkscreen.gto   # Front silkscreen
├── board-B_Silkscreen.gbo   # Back silkscreen
├── board-Edge_Cuts.gm1      # Board outline
├── board-job.gbrjob         # Gerber job file (references every layer)
├── board-PTH.drl            # Plated holes (Excellon)
└── board-NPTH.drl           # Non-plated holes (Excellon)
```

Drill files follow the preset's `merge_pth_npth` choice (Issue #6167).
`jlcpcb`, `pcbway` and `seeed` ship KiCad's default separate
`board-PTH.drl` + `board-NPTH.drl` (the NPTH file is written even when the
board has no non-plated holes). `oshpark` ships one merged `board.drl`,
which OSH Park's KiCad guide asks for. Set `GerberConfig(merge_pth_npth=...)`
to override; `drill_units` (`mm`/`in`), `drill_zeros_format` and
`minimal_header` map onto kicad-cli's `--excellon-*` flags. A drill export
that kicad-cli rejects, or that writes no drill file, raises `ExportError`.

Every manufacturer preset (`jlcpcb`, `pcbway`, `seeed`, `oshpark`) keeps
these names. A preset chooses the layer set, origin and drill options. It
never renames files (Issue #6163). The fab KiCad guides for JLCPCB, PCBWay
and OSH Park take this output as-is, and Seeed's own KiCad tooling exports
the same kicad-cli names. Fab-specific renames such as `.GKO` for the outline
are unnecessary, and they would break the kicad-tools consumers that read
layers from these names.

#### V-score layers (panels)

A V-cut panel's score lines live on a user drawing layer (`Cmts.User`,
`Dwgs.User`, `Eco1.User`, `Eco2.User` or `User.N`), and the exporter adds
that layer to the Gerber set when it finds score lines there (Issues #6156,
#6193). It accepts the following:

- **Lines drawn by `kct panel`.** These are tagged, so they always count.
- **Untagged straight lines that cross the whole outline**, as KiKit draws
  them.
- **Untagged partial and jump scores that complete a separation.** Walk
  the line across the outline. Every stretch of board material on it must
  be covered by score pieces, and every gap must lie off the board: in a
  slot or cutout, or between board outlines. A score that hands off to a
  routed slot running on to the far edge qualifies. So does a seam that
  jumps the space between boards.

Section dividers, fold lines, title-block rules and dashed lines on a single
board do not count. Nor do lines that end on a mounting hole, a cutout or a
connector slot with solid board beyond it, the usual result of snapping a
drafting line to a hole. Two drawings do count: a line broken only around a
hole, and a divider ending on the notch wall of an L-shaped board. Each
crosses all the material on its line, so it carries the same risk as a
solid edge-to-edge line. Only top-level Edge.Cuts graphics count as slot or
cutout geometry; a slot drawn inside a footprint does not.

If a panel's scores do not fit these rules, name the layer yourself. The
layer is then plotted whether or not detection finds it:

```bash
kct export panel.kicad_pcb --vscore-layer User.2      # repeatable
python -m kicad_tools.cli.export_gerbers panel.kicad_pcb --vscore-layer User.2
```

The Python equivalent is `GerberConfig(vscore_layers=["User.2"])`, or
`export_for_manufacturer(..., vscore_layers=[...])` when you use a preset.
`GerberConfig(include_vscore=False)` turns detection off, but it does not
drop layers you named explicitly. If you name a layer the board's layer
table does not define, kicad-cli plots nothing for it, so the export warns
on stderr.

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
# Export with the JLCPCB preset
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

### Via Stitching for Power Planes

`kct stitch` adds via stitching to power-plane nets between layers. When you
pass `--mfr`, the via diameter and drill are resolved from the manufacturer
YAML using the board's actual copper layer count — so stitching stays
consistent with the rules the router and DRC are enforcing. See
[CLI Reference → stitch](../reference/cli.md#stitch) for the full flag list.

```bash
# Auto-detect power nets, JLCPCB tier-1 via geometry
kct stitch board.kicad_pcb --mfr jlcpcb-tier1 --copper 1.0

# Blanket-stitch GND on a 3mm grid
kct stitch board.kicad_pcb --net GND --blanket --spacing 3.0
```

---

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
    shutil.make_archive(str(output / "gerbers"), "zip", gerber_dir)
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
        "SOT-23": 180,  # Rotate SOT-23 by 180°
        "TQFP-32": 90,  # Rotate TQFP-32 by 90°
    }
)

export_pnp(pcb, "cpl.csv", manufacturer="jlcpcb", config=config)
```

## Next Steps

- **[Query API](query-api.md)** - Advanced filtering for design analysis
- **[Schematic Analysis](schematic-analysis.md)** - Deep dive into schematic parsing

## Offline submission preparation

For exact-byte, locally verified Gerber/BOM/CPL handoffs, see
[Offline assembly submission preparation](submission-preparation.md). This Python
API produces a deterministic plan and expected reference matching list without
supplier access, uploads, approval, or orders. A narrow `refresh_inventory`
increment can observe exact-ID stock through a caller-supplied official adapter,
preserving unknown-vs-zero evidence; the full milestone B provenance/freshness
contract remains blocked on #5033/#5034 (PRs #5115/#5090), so #5142 stays open.

## Inventory provenance for parts and BOMs

A catalog match establishes part identity; it does not prove current orderable
stock. Lookup/search JSON, BOM availability, suggestions and enrichment records
carry `inventory` metadata with `source` (`live`, `offline_catalog`, or
`unknown`), the original `observed_at`, snapshot revision/time when known,
local `read_at`, and `from_cache`. Cache reads and reinsertion preserve the
original observation time. Older cache rows without provenance remain unknown.

`stock_verified` requires a live observation no more than 24 hours old. This
is a screening freshness limit, not a supplier reservation or order guarantee;
refresh inventory before ordering. Offline snapshots never qualify as verified
stock, even when their recorded count exceeds the requested quantity.
Availability reports mark such matches unverified/unknown, while suggestions
and BOM enrichment can still use them to identify parts. Export enrichment
reports retain this provenance and warn when stock remains unverified.

The offline dataset does not guarantee a stock-observation timestamp. Missing
ages remain null; file modification time and download time are not substitutes.
Callers with trusted snapshot metadata can provide `snapshot_revision`,
`snapshot_at`, and `observed_at` to `JlcpartsCatalog` explicitly.

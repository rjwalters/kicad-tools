# Tutorial: Running DRC with Manufacturer Rules

This tutorial shows how to run Design Rule Checks (DRC) against specific manufacturer capabilities, ensuring your PCB can be fabricated without issues.

## Overview

Different PCB manufacturers have different capabilities:
- **Minimum trace width** - How thin traces can be
- **Minimum clearance** - Spacing between copper features
- **Minimum via size** - Smallest drill diameter
- **Layer count support** - 2, 4, 6+ layer boards

kicad-tools includes profiles for popular manufacturers and can validate your design against their limits.

## Prerequisites

```bash
pip install kicad-tools
```

For full DRC functionality, you'll also need KiCad 8+ installed (for `kicad-cli`).

## Supported Manufacturers

kicad-tools includes profiles for these PCB fabs:

| Manufacturer | ID | Layers | Features |
|--------------|-----|--------|----------|
| JLCPCB | `jlcpcb` | 2, 4, 6 | Low cost, LCSC parts, assembly service |
| PCBWay | `pcbway` | 2, 4, 6 | Flexible options, global shipping |
| OSHPark | `oshpark` | 2, 4 | US-based, purple boards, per-sq-inch pricing |
| Seeed Fusion | `seeed` | 2, 4, 6 | OPL parts library, assembly service |

## CLI: Basic DRC

Run DRC using KiCad's built-in checker:

```bash
kct drc board.kicad_pcb
```

Output:
```
DRC Report for board.kicad_pcb
==============================
Errors: 0
Warnings: 2

Warnings:
  [W001] Silkscreen clipped by solder mask at (45.2, 32.1)
  [W002] Silkscreen clipped by solder mask at (67.8, 45.3)

Result: PASS (with warnings)
```

## CLI: Check Against Manufacturer Rules

Validate your design against a specific manufacturer's capabilities:

```bash
# Check against 2-layer rules (default for most hobbyist boards)
kct check board.kicad_pcb --mfr jlcpcb --layers 2

# Check against 4-layer rules
kct check board.kicad_pcb --mfr jlcpcb --layers 4

# Check with 2oz copper weight (requires wider traces)
kct check board.kicad_pcb --mfr jlcpcb --layers 2 --copper 2.0
```

Output:
```
============================================================
PURE PYTHON DRC CHECK
============================================================
File: board.kicad_pcb
Manufacturer: JLCPCB
Layers: 2
Rules checked: 12

Results:
  Errors:     0
  Warnings:   2

============================================================
DRC PASSED - No violations found
```

## CLI: Compare Manufacturer Rules

See how different fabs compare:

```bash
# Compare 2-layer rules (most common)
kct mfr compare --layers 2

# Compare 4-layer rules
kct mfr compare --layers 4
```

Output for 2-layer:
```
======================================================================
MANUFACTURER COMPARISON - 2-LAYER 1.0oz
======================================================================
Constraint                      JLCPCB      OSHPARK       PCBWAY       SEEED
----------------------------------------------------------------------
Trace width (mil)                  5.0         6.0          5.0         6.0
Clearance (mil)                    5.0         6.0          5.0         6.0
Via drill (mm)                    0.30        0.25         0.20        0.30
Via diameter (mm)                 0.60        0.51         0.40        0.60
Annular ring (mm)                 0.15        0.13         0.10        0.15
Copper-to-edge (mm)               0.30        0.38         0.25        0.50
----------------------------------------------------------------------
Assembly                           Yes          No          Yes         Yes
Parts Library                     LCSC        None       Global   Seeed OPL

======================================================================
```

Note: 2-layer boards typically have slightly relaxed via constraints compared to 4-layer, but may have stricter trace width requirements when using 2oz copper.

## Python API: Get Manufacturer Rules

```python
from kicad_tools.manufacturers import get_profile, list_manufacturers

# Get JLCPCB profile
jlc = get_profile("jlcpcb")

# Get design rules for 2-layer board (most common for hobbyist projects)
rules_2l = jlc.get_design_rules(layers=2, copper_oz=1.0)

print(f"JLCPCB 2-layer 1oz rules:")
print(f"  Min trace width: {rules_2l.min_trace_width_mm}mm")  # 0.127mm (5 mil)
print(f"  Min clearance: {rules_2l.min_clearance_mm}mm")
print(f"  Min via drill: {rules_2l.min_via_drill_mm}mm")

# Get design rules for 4-layer board
rules_4l = jlc.get_design_rules(layers=4, copper_oz=1.0)

print(f"\nJLCPCB 4-layer rules:")
print(f"  Min trace width: {rules_4l.min_trace_width_mm}mm")  # 0.1016mm (4 mil)
print(f"  Min clearance: {rules_4l.min_clearance_mm}mm")
print(f"  Min via drill: {rules_4l.min_via_drill_mm}mm")

# 2oz copper has wider minimum trace requirements
rules_2oz = jlc.get_design_rules(layers=2, copper_oz=2.0)
print(f"\nJLCPCB 2-layer 2oz rules:")
print(f"  Min trace width: {rules_2oz.min_trace_width_mm}mm")  # 0.2032mm (8 mil)
```

## Python API: Compare All Manufacturers

```python
from kicad_tools.manufacturers import compare_design_rules

# Get rules for all manufacturers
rules = compare_design_rules(layers=4, copper_oz=1.0)

print("Manufacturer Comparison (4-layer):")
print("-" * 50)

for mfr_id, dr in rules.items():
    print(f"\n{mfr_id.upper()}:")
    print(f"  Trace: {dr.min_trace_width_mm}mm")
    print(f"  Clearance: {dr.min_clearance_mm}mm")
    print(f"  Via drill: {dr.min_via_drill_mm}mm")
```

## Python API: Find Compatible Manufacturers

Check which fabs can build your design:

```python
from kicad_tools.manufacturers import find_compatible_manufacturers

# Your design constraints
compatible = find_compatible_manufacturers(
    trace_width_mm=0.15,    # Your minimum trace
    clearance_mm=0.15,      # Your minimum clearance
    via_drill_mm=0.3,       # Your minimum via drill
    layers=4,
    needs_assembly=True,    # Need PCBA service
)

print("Compatible manufacturers for your design:")
for mfr in compatible:
    print(f"  - {mfr.name} ({mfr.website})")
```

## Python API: Parse DRC Reports

If you've already run KiCad's DRC, parse the report file:

```python
from kicad_tools.drc import DRCReport

# Parse a DRC report
report = DRCReport.load("board-drc.rpt")

print(f"DRC Results:")
print(f"  Errors: {report.error_count}")
print(f"  Warnings: {report.warning_count}")

# List all violations
for violation in report.errors:
    print(f"  ERROR [{violation.type}]: {violation.message}")
    if violation.location:
        print(f"    at ({violation.location.x}, {violation.location.y})")

for violation in report.warnings:
    print(f"  WARN [{violation.type}]: {violation.message}")
```

## Python API: Custom Rule Checking

Check specific rules against your design:

```python
from kicad_tools import PCB
from kicad_tools.drc import check_manufacturer_rules

# Load PCB
pcb = PCB.load("board.kicad_pcb")

# Check against JLCPCB rules
result = check_manufacturer_rules(pcb, "jlcpcb")

if result.passed:
    print("Design passes JLCPCB rules!")
else:
    print("Design violates JLCPCB rules:")
    for violation in result.violations:
        print(f"  - {violation}")
```

## Common DRC Issues and Fixes

### Trace Width Too Small

**Error:** `Trace width (0.1mm) below minimum (0.127mm)`

**Fix:** Increase trace width in KiCad:
1. Edit → Board Setup → Design Rules
2. Set minimum track width to manufacturer's limit

### Clearance Too Small

**Error:** `Clearance (0.1mm) below minimum (0.127mm)`

**Fix:** Adjust clearance rules:
1. Edit → Board Setup → Design Rules
2. Set minimum clearance to manufacturer's limit

### Via Drill Too Small

**Error:** `Via drill (0.2mm) below minimum (0.3mm)`

**Fix:** Update via settings:
1. Edit → Board Setup → Design Rules → Predefined Sizes
2. Add via sizes that meet manufacturer limits

### Annular Ring Too Small

**Error:** `Via annular ring (0.1mm) below minimum (0.13mm)`

**Fix:** Via annular ring = (via diameter - drill diameter) / 2

Increase via pad size or use larger drill.

## Complete Example: Pre-Fab Validation

```python
#!/usr/bin/env python3
"""Validate PCB design against manufacturer rules before ordering."""

from kicad_tools import PCB
from kicad_tools.manufacturers import get_profile, find_compatible_manufacturers
from kicad_tools.drc import DRCReport, check_manufacturer_rules

def validate_for_manufacturing(
    pcb_path: str,
    manufacturer: str = "jlcpcb",
    layers: int = 2,
    copper_oz: float = 1.0,
):
    """Run comprehensive manufacturing validation."""

    print(f"Validating: {pcb_path}")
    print(f"Target manufacturer: {manufacturer.upper()}")
    print(f"Configuration: {layers}-layer, {copper_oz}oz copper")
    print("=" * 50)

    # Load PCB
    pcb = PCB.load(pcb_path)

    # Get manufacturer profile with layer-specific rules
    mfr = get_profile(manufacturer)
    rules = mfr.get_design_rules(layers=layers, copper_oz=copper_oz)

    print(f"\n{mfr.name} {layers}-layer {copper_oz}oz Design Rules:")
    print(f"  Min trace: {rules.min_trace_width_mm}mm ({rules.min_trace_width_mil:.1f} mil)")
    print(f"  Min clearance: {rules.min_clearance_mm}mm")
    print(f"  Min via drill: {rules.min_via_drill_mm}mm")

    # Check against rules
    result = check_manufacturer_rules(pcb, manufacturer, layers=layers)

    print(f"\nDesign Check:")
    if result.passed:
        print("  ✓ All checks passed!")
    else:
        print("  ✗ Violations found:")
        for v in result.violations:
            print(f"    - {v}")

    # Find all compatible manufacturers for 2-layer boards
    print(f"\nCompatible Manufacturers ({layers}-layer):")
    compatible = find_compatible_manufacturers(
        trace_width_mm=0.15,
        clearance_mm=0.15,
        via_drill_mm=0.3,
        layers=layers,
        needs_assembly=True,
    )

    for m in compatible:
        marker = "→" if m.id == manufacturer else " "
        print(f"  {marker} {m.name}")

    return result.passed

if __name__ == "__main__":
    import sys
    pcb_file = sys.argv[1] if len(sys.argv) > 1 else "board.kicad_pcb"
    mfr = sys.argv[2] if len(sys.argv) > 2 else "jlcpcb"
    layers = int(sys.argv[3]) if len(sys.argv) > 3 else 2  # Default to 2-layer
    validate_for_manufacturing(pcb_file, mfr, layers=layers)
```

## Next Steps

- **[Manufacturing Export](manufacturing-export.md)** - Generate Gerbers and assembly files
- **[Query API](query-api.md)** - Advanced PCB analysis

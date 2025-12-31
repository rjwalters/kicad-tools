# Example: DRC Checking

This example demonstrates how to parse KiCad DRC (Design Rule Check) reports and validate designs against manufacturer requirements using the kicad-tools Python API.

## Files

- `sample_drc.rpt` - Sample DRC report with various violation types
- `check_drc.py` - Python script demonstrating DRC analysis

## Usage

### Python API

```python
from kicad_tools.drc import DRCReport, check_manufacturer_rules

# Load DRC report (.rpt or .json format)
report = DRCReport.load("design-drc.rpt")

# Summary statistics
print(f"Total violations: {report.violation_count}")
print(f"Errors: {report.error_count}")
print(f"Warnings: {report.warning_count}")

# Access violations
for v in report.errors:
    print(f"[{v.type.value}] {v.message}")

# Check against manufacturer rules
checks = check_manufacturer_rules(report, "jlcpcb", layers=2)
for check in checks:
    print(f"{check.result.value}: {check.message}")
```

### Run the Example

```bash
cd examples/03-drc-checking
python check_drc.py
```

### CLI Alternative

The command-line tool provides quick DRC analysis:

```bash
# Parse and display violations
kct drc sample_drc.rpt

# Show only errors
kct drc sample_drc.rpt --errors-only

# Compare against manufacturer rules
kct drc sample_drc.rpt --mfr jlcpcb

# Output as JSON
kct drc sample_drc.rpt --format json

# Filter by violation type
kct drc sample_drc.rpt --type clearance

# Compare multiple manufacturers
kct mfr compare
```

## Expected Output

```
Loading DRC report: sample_drc.rpt
======================================================================

PCB: test-board.kicad_pcb
Created: 2025-01-15 10:30:00

=== Summary ===
Total violations: 5
  Errors: 4
  Warnings: 1
Footprint errors: 1

=== Violations by Type ===
  clearance: 2
  unconnected_items: 1
  shorting_items: 1
  track_width: 1

=== Errors (must fix) ===

1. [clearance] Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1500 mm)
   @ (100.00mm, 50.00mm)
   - Pad 1 [VCC] of R1 on F.Cu
   - Via [GND] on F.Cu - B.Cu

...

=== Manufacturer Compatibility ===

JLCPCB: ISSUES
  Checked: 5 rules
  Pass: 3, Fail: 2
  Failures:
    - FAIL: Clearance 0.1500mm vs JLCPCB min 0.1000mm
    ...
```

## DRC Report Features

### Parsing Formats

```python
# Text format (.rpt)
report = DRCReport.load("design-drc.rpt")

# JSON format (KiCad 8+)
report = DRCReport.load("design-drc.json")
```

### Filtering Violations

```python
# Get only errors
errors = report.errors

# Get only warnings
warnings = report.warnings

# Filter by type
clearance_issues = report.by_type(ViolationType.CLEARANCE)

# Filter by net
vcc_issues = report.by_net("VCC")

# Find violations near a point
nearby = report.violations_near(x_mm=100, y_mm=50, radius_mm=5)
```

### Manufacturer Comparison

Compare violations against specific manufacturer capabilities:

```python
from kicad_tools.drc import check_manufacturer_rules

# Check against JLCPCB rules
checks = check_manufacturer_rules(report, "jlcpcb", layers=2)

# Supported manufacturers: jlcpcb, oshpark, pcbway, seeed
for check in checks:
    if not check.is_compatible:
        print(f"FAIL: {check.message}")
        print(f"  Manufacturer limit: {check.manufacturer_limit}mm")
        print(f"  Actual value: {check.actual_value}mm")
```

## Violation Types

Common DRC violation types:

| Type | Description |
|------|-------------|
| `clearance` | Copper-to-copper spacing too small |
| `track_width` | Trace narrower than allowed |
| `unconnected_items` | Missing connection between pads |
| `shorting_items` | Unintended short between nets |
| `via_annular_width` | Via pad too small |
| `drill_hole_too_small` | Drill diameter below minimum |
| `copper_edge_clearance` | Copper too close to board edge |

## What You Can Learn

1. **Report parsing** - Load DRC reports in text or JSON format
2. **Violation analysis** - Filter and categorize design issues
3. **Manufacturer rules** - Compare against production capabilities
4. **Location tracking** - Find violations by coordinates
5. **Net filtering** - Identify issues affecting specific signals

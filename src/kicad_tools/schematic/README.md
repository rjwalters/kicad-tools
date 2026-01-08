# Schematic Generation Guide

This module provides programmatic schematic generation for KiCad. Understanding KiCad's wire connectivity semantics is essential for generating schematics that pass ERC (Electrical Rules Check).

## Wire Connectivity Rules

### Fundamental Rule: Wire Endpoint Connectivity

**KiCad establishes electrical connections only where wire endpoints meet.** A wire passing through a point does NOT connect to other wires at that point.

```
WRONG: Wire passes through - NO CONNECTION
═══════════════════════════  (single continuous wire)
        │
        │  (separate wire - NOT connected to horizontal wire!)

CORRECT: Wire endpoints meet - CONNECTED
════════╪═══════════════════  (two wire segments meeting at the intersection)
        │
        │  (vertical wire endpoint meets horizontal wire endpoint)
```

This is the most common source of ERC errors in programmatically generated schematics. Visually, both diagrams look identical, but only the second creates an electrical connection.

### Junction Semantics

Junctions (`add_junction()`) serve **visual purposes only**:

1. **Visual clarity**: Shows that wires are intentionally connected (not just crossing)
2. **Crossing disambiguation**: Distinguishes connected crossings from non-connected overlaps

**Junctions do NOT establish electrical connectivity** - wire endpoints must already meet at that point. Adding a junction to a wire intersection where endpoints don't meet will NOT create a connection.

```python
# WRONG: Junction without endpoint connectivity
sch.add_wire((0, 50), (200, 50))    # One continuous wire
sch.add_wire((100, 0), (100, 100))  # Vertical wire passes through
sch.add_junction(100, 50)           # Junction is visual only - NO CONNECTION!

# CORRECT: Endpoints meet, junction for visual clarity
sch.add_wire((0, 50), (100, 50))    # Segment 1 ends at intersection
sch.add_wire((100, 50), (200, 50))  # Segment 2 starts at intersection
sch.add_wire((100, 0), (100, 50))   # Vertical connects at same point
sch.add_wire((100, 50), (100, 100)) # Continues from intersection
sch.add_junction(100, 50)           # Visual indicator of the connection
```

## Common Patterns

### T-Connection Pattern

To create a T-connection (e.g., component tapping off a power rail):

```python
# Scenario: Vertical wire needs to connect to horizontal rail at x=100

# WRONG: Single rail wire, vertical just touches it
rail_wire = sch.add_wire((0, 50), (200, 50))      # Continuous rail
sch.add_wire((100, 50), (100, 100))               # Vertical - NOT CONNECTED!

# CORRECT: Split rail at connection point
sch.add_wire((0, 50), (100, 50))                  # Rail segment 1 ends at x=100
sch.add_wire((100, 50), (200, 50))                # Rail segment 2 starts at x=100
sch.add_wire((100, 50), (100, 100))               # Vertical meets at x=100
sch.add_junction(100, 50)                         # Visual indicator
```

### Power Rail with Multiple Taps

When multiple components tap off a power rail, the rail must be segmented at each tap point:

```python
# Components at x=50, x=100, x=150 need to tap into rail at y=30
tap_points = [50, 100, 150]
rail_y = 30
rail_start = 25
rail_end = 175

# Create rail segments between tap points
all_x = sorted([rail_start] + tap_points + [rail_end])
for i in range(len(all_x) - 1):
    sch.add_wire((all_x[i], rail_y), (all_x[i + 1], rail_y))

# Add vertical connections and junctions at tap points
for x in tap_points:
    sch.add_wire((x, rail_y), (x, component_y))  # Vertical to component
    sch.add_junction(x, rail_y)                   # Visual indicator
```

### Using the High-Level Helper

The `wire_to_rail` method handles rail connections correctly:

```python
from kicad_tools.schematic import Schematic

sch = Schematic(title="My Design")

# Add a resistor
r1 = sch.add_symbol("Device:R", x=100, y=80, ref="R1", value="10k")

# Connect pin to rail - this handles segmentation internally
sch.wire_to_rail(r1, pin_name="1", rail_y=50, add_junction=True)
```

## Power Symbol and PWR_FLAG Usage

### Power Symbols

Power symbols like `+5V`, `GND`, `+3.3V` are defined as **power input** pins in KiCad's library. This means they *consume* power - they indicate where a net receives its power.

### The PWR_FLAG Problem

When you connect a power symbol to a net but don't have an actual power source driving it, ERC reports:

```
Error: Input Power pin not driven by any Output Power pins
```

This happens because:
- The power symbol declares "this net needs power"
- But nothing declares "I am providing power to this net"

### Solution: PWR_FLAG

`PWR_FLAG` is a special symbol that tells KiCad "this power net is intentionally driven by an external source" (e.g., a connector, battery, or regulator output).

```python
# WRONG: Power symbol with no driver indication
sch.add_power("power:+5V", x=50, y=30)
sch.add_wire((50, 30), (50, 50))
# ERC Error: +5V not driven by Output Power pin

# CORRECT: Add PWR_FLAG where power enters the design
sch.add_power("power:+5V", x=50, y=30)
sch.add_wire((50, 30), (50, 50))
sch.add_pwr_flag(50, 35)  # Indicates external power entry
# ERC passes
```

### When to Use PWR_FLAG

Add `PWR_FLAG` at points where external power enters your schematic:

1. **Power connector pins**: Where a DC jack or USB port provides power
2. **Voltage regulator outputs**: Where a regulator output drives a power net
3. **Battery connections**: Where a battery provides power
4. **Test points**: Where power can be injected during testing

```python
# Example: USB power entry
usb_connector = sch.add_symbol("Connector:USB_C", x=50, y=100, ref="J1")
sch.add_power("power:+5V", x=80, y=60)
sch.add_pwr_flag(80, 65)  # Power enters here from USB
sch.wire_to_rail(usb_connector, "VBUS", rail_y=60)
```

## Complete Example: Voltage Divider

Here's a complete example showing correct wire connectivity:

```python
from kicad_tools.schematic import Schematic

# Create schematic
sch = Schematic(title="Voltage Divider", date="2024-01")

# Add power symbols
sch.add_power("power:+5V", x=100, y=20)
sch.add_pwr_flag(100, 25)  # Power entry point

sch.add_power("power:GND", x=100, y=150)
sch.add_pwr_flag(100, 145)  # Ground reference

# Add resistors
r1 = sch.add_symbol("Device:R", x=100, y=60, ref="R1", value="10k", rotation=0)
r2 = sch.add_symbol("Device:R", x=100, y=110, ref="R2", value="10k", rotation=0)

# Wire +5V to R1 top (endpoints meet)
sch.add_wire((100, 20), (100, 50))   # Power symbol to R1 pin 1

# Wire R1 bottom to R2 top - this is where the output tap is
# Create the connection with proper segmentation for the output tap
r1_bottom = r1.pin_position("2")  # (100, 70)
r2_top = r2.pin_position("1")     # (100, 100)

# Vertical wire from R1 to junction point
sch.add_wire(r1_bottom, (100, 85))
# Vertical wire from junction to R2
sch.add_wire((100, 85), r2_top)
# Horizontal wire for output tap
sch.add_wire((100, 85), (130, 85))
sch.add_junction(100, 85)  # Visual indicator at T-junction

# Add output label
sch.add_label("VOUT", 130, 85)

# Wire R2 bottom to GND
sch.add_wire((100, 120), (100, 150))

# Save
sch.write("voltage_divider.kicad_sch")
```

## Debugging ERC Failures

### Common Errors and Solutions

| ERC Error | Likely Cause | Solution |
|-----------|--------------|----------|
| "Input Power pin not driven" | Missing PWR_FLAG | Add `add_pwr_flag()` at power entry points |
| "Unconnected wire endpoint" | Wire doesn't reach pin | Check coordinates match exactly |
| Visual connection but ERC fail | Wire passes through without endpoint | Segment wires at intersection points |

### Verification Checklist

1. **Wire endpoints**: Every wire intersection must have endpoints meeting, not wires passing through
2. **Power flags**: Every power net driven by external sources needs PWR_FLAG
3. **Grid alignment**: Use `snap=True` (default) to ensure components and wires align
4. **Junction visibility**: Add junctions at T-connections for visual clarity

## API Reference

### Wire and Junction Methods

| Method | Purpose |
|--------|---------|
| `add_wire(p1, p2)` | Add wire between two points |
| `add_junction(x, y)` | Add visual junction indicator |
| `add_rail(y, x_start, x_end)` | Add horizontal power rail |
| `wire_to_rail(symbol, pin, rail_y)` | Connect pin to rail with proper segmentation |

### Power Methods

| Method | Purpose |
|--------|---------|
| `add_power(lib_id, x, y)` | Add power symbol (+5V, GND, etc.) |
| `add_pwr_flag(x, y)` | Mark power entry point |

See the module docstrings for detailed parameter documentation.

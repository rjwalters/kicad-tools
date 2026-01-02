# Example Claude Session: USB-Powered LED Blinker

This document demonstrates a complete interaction between a user and Claude using kicad-tools to design a simple USB-powered LED blinker with an ATtiny85.

## Session Overview

**Goal**: Create a complete PCB design for a USB-powered LED blinker
**Components**:
- ATtiny85 microcontroller
- USB Type-C connector (power only)
- Status LED with current-limiting resistor
- Decoupling capacitors

## Conversation

### User Request

> Create a USB-powered LED blinker circuit using an ATtiny85. I want it to work with USB-C power and have a single status LED that I can control from the microcontroller.

### Claude Response

I'll help you design a USB-powered LED blinker with an ATtiny85. Let me create the schematic and PCB layout step by step.

#### Step 1: Create the Schematic

First, I'll set up the basic structure with power symbols:

```python
# Tool call: add_power_symbol
{"symbol": "VCC", "x": 100.0, "y": 20.0}
# Result: Added VCC power symbol at (100, 20)

# Tool call: add_power_symbol
{"symbol": "GND", "x": 100.0, "y": 140.0}
# Result: Added GND power symbol at (100, 140)
```

Now I'll add the USB-C connector for power input:

```python
# Tool call: add_schematic_symbol
{
    "lib_id": "Connector:USB_C_Receptacle_USB2.0",
    "x": 40.0,
    "y": 80.0,
    "reference": "J1",
    "value": "USB_C"
}
# Result: Added USB-C connector J1 at (40, 80)
```

Add the ATtiny85 microcontroller:

```python
# Tool call: add_schematic_symbol
{
    "lib_id": "MCU_Microchip_ATtiny:ATtiny85-20PU",
    "x": 100.0,
    "y": 80.0,
    "reference": "U1",
    "value": "ATtiny85"
}
# Result: Added ATtiny85 U1 at (100, 80)
```

Add the LED indicator circuit (LED + current limiting resistor):

```python
# Tool call: add_led_indicator
{
    "x": 150.0,
    "y": 80.0,
    "ref_prefix": "D1",
    "label": "STATUS",
    "resistor_value": "330R"
}
# Result: Added LED D1 with resistor R1 (330R) at (150, 80)
# Ports: VCC at (150, 72.5), GND at (150, 95)
```

Add decoupling capacitors for the MCU:

```python
# Tool call: add_decoupling_caps
{
    "x": 85.0,
    "y": 60.0,
    "ref_start": "C1",
    "values": ["10uF", "100nF"]
}
# Result: Added capacitors C1 (10uF) and C2 (100nF) at (85, 60)
```

#### Step 2: Wire the Connections

Connect USB power to VCC rail:

```python
# Tool call: wire_components
{
    "from_ref": "J1",
    "from_pin": "VBUS",
    "to_ref": "VCC",
    "to_pin": "1"
}
# Result: Connected J1:VBUS to VCC
```

Connect USB ground:

```python
# Tool call: wire_components
{
    "from_ref": "J1",
    "from_pin": "GND",
    "to_ref": "GND",
    "to_pin": "1"
}
# Result: Connected J1:GND to GND
```

Connect MCU power pins:

```python
# Tool call: wire_components
{"from_ref": "U1", "from_pin": "VCC", "to_ref": "VCC", "to_pin": "1"}
# Result: Connected U1:VCC to VCC

# Tool call: wire_components
{"from_ref": "U1", "from_pin": "GND", "to_ref": "GND", "to_pin": "1"}
# Result: Connected U1:GND to GND
```

Connect LED to GPIO (using PB0):

```python
# Tool call: wire_components
{
    "from_ref": "U1",
    "from_pin": "PB0",
    "to_ref": "D1",
    "to_pin": "A"
}
# Result: Connected U1:PB0 to D1 (via LED indicator block)
```

Add a net label for the LED signal:

```python
# Tool call: add_net_label
{
    "label": "LED_OUT",
    "x": 130.0,
    "y": 72.5
}
# Result: Added net label "LED_OUT" at (130, 72.5)
```

#### Step 3: Save and Verify Schematic

```python
# Tool call: list_symbols
{}
# Result:
# - J1: USB_C_Receptacle_USB2.0 at (40, 80)
# - U1: ATtiny85 at (100, 80)
# - D1: LED at (150, 80)
# - R1: 330R at (150, 95)
# - C1: 10uF at (85, 60)
# - C2: 100nF at (90, 60)

# Tool call: list_nets
{}
# Result:
# - VCC: J1:VBUS, U1:VCC, C1:1, C2:1
# - GND: J1:GND, U1:GND, C1:2, C2:2, R1:2
# - LED_OUT: U1:PB0, D1:A
```

```python
# Tool call: save_schematic
{"file_path": "led_blinker.kicad_sch"}
# Result: Schematic saved to led_blinker.kicad_sch
```

#### Step 4: PCB Layout

Now let's work on the PCB layout. First, load the PCB:

```python
# Tool call: load_pcb
{"file_path": "led_blinker.kicad_pcb"}
# Result: Loaded PCB with 6 components, 3 nets
# Board size: 30mm x 20mm
```

Place the USB connector at the edge:

```python
# Tool call: place_component
{
    "ref": "J1",
    "x": 5.0,
    "y": 10.0,
    "rotation": 90
}
# Result: Placed J1 at (5, 10) rotated 90°
```

Place the MCU centrally:

```python
# Tool call: place_component
{
    "ref": "U1",
    "x": 15.0,
    "y": 10.0,
    "rotation": 0
}
# Result: Placed U1 at (15, 10)
```

Place decoupling caps near MCU:

```python
# Tool call: place_component
{"ref": "C1", "x": 12.0, "y": 6.0}
# Result: Placed C1 at (12, 6)

# Tool call: place_component
{"ref": "C2", "x": 18.0, "y": 6.0}
# Result: Placed C2 at (18, 6)
```

Place LED at visible location:

```python
# Tool call: place_component
{"ref": "D1", "x": 25.0, "y": 10.0}
# Result: Placed D1 at (25, 10)

# Tool call: place_component
{"ref": "R1", "x": 25.0, "y": 14.0}
# Result: Placed R1 at (25, 14)
```

#### Step 5: Route the PCB

Route power nets first with wider traces:

```python
# Tool call: route_net
{
    "net": "VCC",
    "prefer_layer": "F.Cu",
    "trace_width": 0.5
}
# Result: Routed VCC - 3 connections, length: 28.5mm, 0 vias
```

Route ground and add ground plane:

```python
# Tool call: route_net
{
    "net": "GND",
    "prefer_layer": "B.Cu",
    "trace_width": 0.5
}
# Result: Routed GND - 4 connections, length: 35.2mm, 2 vias

# Tool call: define_zone
{
    "net": "GND",
    "layer": "B.Cu",
    "priority": 0
}
# Result: Created GND zone on B.Cu covering entire board
```

Route signal net:

```python
# Tool call: route_net
{
    "net": "LED_OUT",
    "prefer_layer": "F.Cu",
    "minimize_vias": true
}
# Result: Routed LED_OUT - 1 connection, length: 12.3mm, 0 vias
```

#### Step 6: Validate Design

```python
# Tool call: check_drc
{"manufacturer": "jlcpcb", "layers": 2}
# Result: DRC check passed
# - 0 errors
# - 0 warnings
# All nets routed
# Clearance check: PASS
# Trace width check: PASS
```

#### Step 7: Export for Manufacturing

```python
# Tool call: save_pcb
{"file_path": "led_blinker.kicad_pcb"}
# Result: PCB saved

# Tool call: export_gerbers
{
    "output_dir": "./gerbers",
    "manufacturer": "jlcpcb"
}
# Result: Exported Gerber files to ./gerbers/
# - led_blinker-F_Cu.gbr
# - led_blinker-B_Cu.gbr
# - led_blinker-F_Mask.gbr
# - led_blinker-B_Mask.gbr
# - led_blinker-F_Silkscreen.gbr
# - led_blinker-Edge_Cuts.gbr
# - led_blinker.drl

# Tool call: export_assembly
{
    "output_dir": "./assembly",
    "manufacturer": "jlcpcb"
}
# Result: Exported assembly files:
# - BOM_led_blinker.csv
# - CPL_led_blinker.csv
```

### Summary

I've created a complete USB-powered LED blinker design:

**Schematic**:
- USB-C connector for 5V power input
- ATtiny85 microcontroller
- Status LED on PB0 with 330R current limiting resistor
- 10uF + 100nF decoupling capacitors

**PCB Layout**:
- Board size: 30mm x 20mm (2-layer)
- USB connector at edge for easy access
- Ground plane on bottom layer
- All DRC checks pass (JLCPCB rules)

**Files Generated**:
- `led_blinker.kicad_sch` - Schematic
- `led_blinker.kicad_pcb` - PCB layout
- `./gerbers/` - Manufacturing files
- `./assembly/` - BOM and placement files

The board is ready to order from JLCPCB or similar manufacturers!

## Error Handling Examples

### Routing Failure

During the session, if routing fails:

```python
# Tool call: route_net
{"net": "SDA", "prefer_layer": "F.Cu"}
# Error: Route failed - path blocked by U2 at (45.2, 32.1)
```

Recovery approach:

```python
# Option 1: Try different layer
# Tool call: route_net
{"net": "SDA", "prefer_layer": "B.Cu"}

# Option 2: Move blocking component
# Tool call: place_component
{"ref": "U2", "x": 50.0, "y": 32.0}

# Option 3: Delete conflicting traces and reroute
# Tool call: delete_trace
{"net": "SDA", "delete_all": true}
```

### DRC Violation

```python
# Tool call: check_drc
{"manufacturer": "jlcpcb"}
# Result: 1 error found
# - Clearance violation: VCC trace too close to GND via at (12.5, 8.2)
#   Required: 0.127mm, Actual: 0.095mm

# Fix by rerouting
# Tool call: delete_trace
{"net": "VCC", "near_x": 12.5, "near_y": 8.2, "radius": 2.0}

# Tool call: route_net
{"net": "VCC", "prefer_layer": "F.Cu", "trace_width": 0.5}
```

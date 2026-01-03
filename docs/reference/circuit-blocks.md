# Circuit Blocks Reference

Circuit blocks are reusable, tested subcircuits for building schematics programmatically.

---

## Quick Start

```python
from kicad_tools.schematic import Schematic
from kicad_tools.schematic.blocks import (
    MCUBlock, CrystalOscillator, LDOBlock,
    USBConnector, DebugHeader, I2CPullups
)

# Create schematic
sch = Schematic.create("project.kicad_sch")

# Add blocks
mcu = MCUBlock(sch, x=150, y=100, part="STM32F103C8T6")
xtal = CrystalOscillator(sch, x=100, y=100, frequency="8MHz")
ldo = LDOBlock(sch, x=50, y=100, output_voltage=3.3)

# Connect via ports
sch.add_wire(ldo.port("VOUT"), mcu.port("VDD"))
sch.add_wire(xtal.port("OUT"), mcu.port("OSC_IN"))

sch.save()
```

---

## Available Blocks

### Power Blocks

#### `LDOBlock`

Linear voltage regulator with input/output capacitors.

```python
ldo = LDOBlock(sch, x=50, y=100,
    input_voltage=5.0,      # Input voltage
    output_voltage=3.3,     # Output voltage
    input_cap="10uF",       # Input capacitor
    output_caps=["10uF", "100nF"],  # Output capacitors
    ref="U1"                # Optional reference designator
)
```

**Ports:** `VIN`, `VOUT`, `GND`

---

#### `BarrelJackInput`

DC barrel jack power input with protection.

```python
jack = BarrelJackInput(sch, x=50, y=100,
    voltage=12.0,           # Expected voltage
    polarity_protection=True,
    fuse_value="1A"
)
```

**Ports:** `VIN`, `GND`

---

#### `USBPowerInput`

USB power input with protection.

```python
usb_pwr = USBPowerInput(sch, x=50, y=100,
    connector_type="type-c",  # "micro", "mini", "type-c"
    current_limit="500mA"
)
```

**Ports:** `VBUS`, `GND`

---

#### `BatteryInput`

Battery input with protection.

```python
batt = BatteryInput(sch, x=50, y=100,
    chemistry="lipo",        # "lipo", "liion", "nimh"
    cells=1,
    protection=True          # Over-discharge protection
)
```

**Ports:** `VBAT`, `GND`

---

### MCU Blocks

#### `MCUBlock`

Microcontroller with bypass capacitors.

```python
mcu = MCUBlock(sch, x=150, y=100,
    part="STM32F103C8T6",
    bypass_caps=["100nF", "100nF", "100nF", "4.7uF"],
    ref="U1"
)
```

**Ports:** `VDD`, `GND`, `OSC_IN`, `OSC_OUT`, `NRST`, and all GPIO pins

---

#### `CrystalOscillator`

Crystal with load capacitors.

```python
xtal = CrystalOscillator(sch, x=100, y=100,
    frequency="8MHz",
    load_caps="20pF"
)
```

**Ports:** `IN`, `OUT`, `GND`

---

#### `ResetButton`

Reset switch with debounce capacitor.

```python
reset = ResetButton(sch, x=120, y=50,
    debounce_cap="100nF",
    pullup="10k"
)
```

**Ports:** `RST`, `GND`

---

### Interface Blocks

#### `USBConnector`

USB connector with optional ESD protection.

```python
usb = USBConnector(sch, x=50, y=150,
    connector_type="type-c",  # "usb-b", "mini", "micro", "type-c"
    esd_protection=True,
    data_lines=True           # Include D+/D-
)
```

**Ports:** `VBUS`, `GND`, `D+`, `D-`, `CC1`, `CC2` (for Type-C)

---

#### `DebugHeader`

Programming/debug header.

```python
debug = DebugHeader(sch, x=200, y=100,
    interface="swd",          # "swd", "jtag", "tag-connect"
    connector="2x5"           # Pin header type
)
```

**Ports:** `VCC`, `GND`, `SWDIO`, `SWCLK`, `SWO`, `NRST`

---

#### `I2CPullups`

I2C bus pull-up resistors.

```python
i2c = I2CPullups(sch, x=180, y=150,
    pullup_value="4.7k",
    bus_voltage=3.3
)
```

**Ports:** `VCC`, `GND`, `SDA`, `SCL`

---

### Indicator Blocks

#### `LEDIndicator`

Status LED with current-limiting resistor.

```python
led = LEDIndicator(sch, x=200, y=150,
    color="green",
    forward_voltage=2.2,
    current="10mA"
)
```

**Ports:** `ANODE`, `GND`

---

#### `DecouplingCaps`

Decoupling capacitor array.

```python
decoupling = DecouplingCaps(sch, x=160, y=80,
    values=["100nF", "100nF", "10uF"]
)
```

**Ports:** `VCC`, `GND`

---

## Block Anatomy

All blocks share a common structure:

```python
class MyBlock(CircuitBlock):
    def __init__(self, sch, x, y, **params):
        super().__init__(sch, x, y)

        # Add components
        self.r1 = self.add_resistor("R1", "10k", x, y)
        self.c1 = self.add_capacitor("C1", "100nF", x+20, y)

        # Define ports (connection points)
        self.ports = {
            "IN": self.r1.pin(1),
            "OUT": self.c1.pin(2),
            "GND": (x+10, y+30),
        }

        # Internal wiring
        self.add_wire(self.r1.pin(2), self.c1.pin(1))
```

---

## Connecting Blocks

### Via Ports

```python
# Get port position
ldo_vout = ldo.port("VOUT")
mcu_vdd = mcu.port("VDD")

# Add wire
sch.add_wire(ldo_vout, mcu_vdd)
```

### Via Rails

```python
# Connect all blocks to power rails
sch.add_power_rail("VCC", 3.3)
sch.add_ground_rail()

# Connect blocks to rails
ldo.connect_to_rails(vout="VCC", gnd="GND")
mcu.connect_to_rails(vdd="VCC", gnd="GND")
```

---

## Creating Custom Blocks

```python
from kicad_tools.schematic.blocks import CircuitBlock

class MyFilter(CircuitBlock):
    """RC low-pass filter."""

    def __init__(self, sch, x, y, r_value="10k", c_value="100nF"):
        super().__init__(sch, x, y)

        # Add components
        self.r = self.add_resistor("R", r_value, x, y)
        self.c = self.add_capacitor("C", c_value, x+30, y)

        # Internal wiring
        self.add_wire(self.r.pin(2), self.c.pin(1))
        self.add_wire(self.c.pin(2), (x+30, y+20))  # GND

        # Define ports
        self.ports = {
            "IN": self.r.pin(1),
            "OUT": self.c.pin(1),
            "GND": (x+30, y+20),
        }

# Usage
lpf = MyFilter(sch, x=100, y=100, r_value="4.7k", c_value="1uF")
```

---

## Block Parameters

Common parameters across blocks:

| Parameter | Description |
|-----------|-------------|
| `sch` | Schematic object |
| `x`, `y` | Position in schematic coordinates |
| `ref` | Reference designator prefix |

---

## See Also

- [End-to-End Example](https://github.com/rjwalters/kicad-tools/tree/main/examples/05-end-to-end)
- [Schematic Analysis Guide](../guides/schematic-analysis.md)

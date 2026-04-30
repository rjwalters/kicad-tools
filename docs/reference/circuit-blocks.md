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

Blocks can also be composed algebraically with `&` (series) and `|` (parallel)
operators, deferring placement until `realize()` is called:

```python
from kicad_tools.schematic.blocks import (
    VoltageDividerSense, ADCInputFilterBlock
)

# Compose a voltage sense chain: divider feeds into an anti-aliasing filter
sense_chain = VoltageDividerSense(sch, 0, 0, ratio=11.0) \
            & ADCInputFilterBlock(sch, 0, 0, cutoff_hz=1000)

# Place the composed block into the schematic at the desired position
sense_chain.realize(sch, x=50, y=100)
```

See [Composition Operators](#composition-operators) for full details.

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

## Composition Operators

Circuit blocks support algebraic composition through the `&` (series) and `|`
(parallel) operators. Composition is **lazy** -- blocks are wired logically when
the operator executes, but components are not placed into a schematic until
`realize()` is called. This lets you build complex topologies before committing
to a layout.

```python
from kicad_tools.schematic.blocks import (
    ComposedCircuitBlock,
    VoltageDividerSense,
    ADCInputFilterBlock,
    LDOBlock,
    ESDProtectionBlock,
    FuseBlock,
    match_ports,
)
```

### Series Composition (`&`)

Series composition wires the **output ports** of the left block to the
**input ports** of the right block. Port matching follows a priority cascade:

1. **Exact name match** -- e.g. `VOUT` on the left matches `VOUT` on the right.
2. **Alias match** -- common output-to-input aliases are tried automatically:
   `VOUT` -> `VIN`, `OUT` -> `IN`, `TX` -> `RX`, `DOUT` -> `DIN`,
   `MOSI` -> `MISO`.
3. **Direction + interface type** -- remaining unmatched ports are paired by
   compatible direction (output-to-input) and, when available, matching
   interface category.

The resulting `ComposedCircuitBlock` exposes un-wired input ports of the left
block and un-wired output ports of the right block.

#### Example: Voltage Divider + ADC Filter

A voltage sense divider feeds directly into an anti-aliasing filter. The
divider's `VOUT` port matches the filter's `VIN` port via the alias table
(`VOUT` -> `VIN`), so they are automatically wired together:

```python
sch = Schematic.create("adc_sense.kicad_sch")

divider = VoltageDividerSense(sch, 0, 0, ratio=11.0)
adc_filter = ADCInputFilterBlock(sch, 0, 0, cutoff_hz=1000)

# Series: VOUT of divider wires to VIN (via alias) of the filter
sense_chain = divider & adc_filter

# Exposed ports: VIN (from divider), OUT (from filter), GND (both)
print(list(sense_chain.ports.keys()))

# Place the composed block into the schematic
sense_chain.realize(sch, x=50, y=100)
sch.save()
```

#### Example: Protected Power Chain

Multiple protection stages can be chained with repeated `&`:

```python
sch = Schematic.create("protected_power.kicad_sch")

esd = ESDProtectionBlock(sch, 0, 0)
fuse = FuseBlock(sch, 0, 0, current_rating="500mA")
ldo = LDOBlock(sch, 0, 0, output_voltage="3V3")

# Chained series: ESD -> Fuse -> LDO
#   esd.OUT matches fuse.IN (via OUT->IN alias)
#   fuse.OUT matches ldo.VIN (via OUT->IN alias would not match VIN,
#   but direction + interface matching pairs them)
protected_supply = esd & fuse & ldo

# Exposed ports: IN (from ESD), VOUT (from LDO), GND
protected_supply.realize(sch, x=20, y=80)
sch.save()
```

### Parallel Composition (`|`)

Parallel composition ties **matching input ports** together (shared inputs)
and combines output ports under **disambiguated names**. Output port names
are prefixed with a block label when both blocks expose an identically named
output (e.g. `LDO.VOUT` and `LDO.VOUT` become `LDO_1.VOUT`, `LDO_2.VOUT`).

The block label is derived from the class name with common suffixes like
`Block` and `Circuit` stripped (e.g. `LDOBlock` becomes `LDO`).

#### Example: Redundant Power Paths

Two identical LDOs in parallel share the same `VIN` and `GND` but produce
separate, disambiguated `VOUT` ports:

```python
sch = Schematic.create("redundant_power.kicad_sch")

ldo_a = LDOBlock(sch, 0, 0, output_voltage="3V3")
ldo_b = LDOBlock(sch, 0, 0, output_voltage="3V3")

# Parallel: shared VIN and GND, disambiguated outputs
redundant = ldo_a | ldo_b

# Exposed ports include VIN, GND (shared), LDO.VOUT (from ldo_a),
# and LDO.VOUT (from ldo_b, disambiguated)
redundant.realize(sch, x=50, y=100)
sch.save()
```

### Mixed Composition

The `&` and `|` operators compose naturally. Use parentheses to control
grouping:

```python
sch = Schematic.create("mixed_topology.kicad_sch")

ldo = LDOBlock(sch, 0, 0, output_voltage="3V3")
adc_filter = ADCInputFilterBlock(sch, 0, 0, cutoff_hz=1000)
bypass_filter = ADCInputFilterBlock(sch, 0, 0, cutoff_hz=5000)

# Series then parallel: regulated + filtered path alongside a bypass path
filtered_supply = (ldo & adc_filter) | bypass_filter

filtered_supply.realize(sch, x=30, y=60)
sch.save()
```

### Deferred Realization with `realize()`

`ComposedCircuitBlock.realize(sch, x, y)` recursively places child blocks
and draws wires into the target schematic. Child blocks that are themselves
composed are realized recursively; plain blocks have their position updated.

Series children are laid out left-to-right (offset by
`ComposedCircuitBlock.SERIES_GAP`, default 30 units). Parallel children are
laid out top-to-bottom (offset by `ComposedCircuitBlock.PARALLEL_GAP`, default
40 units).

```python
# Compose without a schematic context
esd = ESDProtectionBlock(sch, 0, 0)
fuse = FuseBlock(sch, 0, 0, current_rating="1A")
chain = esd & fuse

# Later, realize into a schematic at a chosen origin
sch = Schematic.create("deferred.kicad_sch")
chain.realize(sch, x=100, y=50)
sch.save()
```

### Inspecting Warnings

When ports are matched, the `ConnectionValidator` checks for interface type
and protocol mismatches. Any issues are collected in the `warnings` attribute
of the composed block and also logged at `WARNING` level.

```python
from kicad_tools.schematic.blocks import ConnectionWarning

sch = Schematic.create("warnings_demo.kicad_sch")

divider = VoltageDividerSense(sch, 0, 0, ratio=11.0)
adc_filter = ADCInputFilterBlock(sch, 0, 0, cutoff_hz=1000)

composed = divider & adc_filter

# Iterate over any validation warnings
for w in composed.warnings:
    print(f"[{w.severity}] {w.source_port} -> {w.target_port}: {w.message}")

if not composed.warnings:
    print("No interface mismatches detected.")
```

Each `ConnectionWarning` contains:

| Attribute | Description |
|-----------|-------------|
| `severity` | `WarningSeverity` enum (`WARNING`, `ERROR`) |
| `source_port` | Name of the upstream port |
| `target_port` | Name of the downstream port |
| `message` | Human-readable description of the mismatch |

### Port Matching Details

The `match_ports()` function (importable from `kicad_tools.schematic.blocks`)
implements the priority cascade used by both `&` and `|` operators.

**Alias table** (output name on left, expected input name on right):

| Source Port | Target Port |
|-------------|-------------|
| `VOUT` | `VIN` |
| `OUT` | `IN` |
| `TX` | `RX` |
| `DOUT` | `DIN` |
| `MOSI` | `MISO` |

**Direction compatibility matrix:**

| Source Direction | Compatible Target Directions |
|-----------------|------------------------------|
| `output` | `input`, `passive`, `bidirectional` |
| `input` | `output`, `passive`, `bidirectional` |
| `bidirectional` | `input`, `output`, `passive`, `bidirectional` |
| `passive` | `input`, `output`, `passive`, `bidirectional`, `power` |
| `power` | `power`, `passive` |

Each port participates in at most one pairing. Exact name and alias matches
are resolved first; remaining ports fall through to direction + interface
matching.

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
- API: `ComposedCircuitBlock` -- `src/kicad_tools/schematic/blocks/base.py`
- API: `match_ports()` -- `src/kicad_tools/schematic/blocks/validator.py`
- API: `ConnectionWarning`, `ConnectionValidator` -- `src/kicad_tools/schematic/blocks/validator.py`

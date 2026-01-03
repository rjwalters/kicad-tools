# Research: Atopile Interface-Based Design Patterns

**Issue**: #304
**Date**: 2025-01-03
**Status**: Complete

## Executive Summary

This document analyzes atopile/faebryk's interface-based module design patterns to identify improvements for kicad-tools circuit blocks. The key takeaway is that atopile uses a hierarchical **typed interface system** that enables type-checked connections, automatic reference management, and constraint propagation - features that could significantly improve kicad-tools' circuit block architecture.

## 1. Core Concepts in Atopile/Faebryk

### 1.1 Interface Hierarchy

Atopile uses a layered interface hierarchy:

```
ModuleInterface (base)
    ├── Signal
    │   └── ElectricSignal (line + reference power)
    │       └── ElectricLogic (digital signals with push-pull modes)
    ├── Electrical (single electrical connection point)
    ├── ElectricPower (hv + lv Electrical, voltage constraints)
    ├── I2C (scl + sda ElectricLogic, frequency, address)
    ├── SPI (sclk + miso + mosi ElectricLogic)
    ├── USB2_0_IF (differential data + bus power)
    └── Addressor (address lines for I2C/configurable addresses)
```

### 1.2 Key Design Patterns

#### Pattern 1: Typed Interfaces with Nested Components

Interfaces compose other interfaces as typed members:

```python
# From faebryk/library/I2C.py
class I2C(ModuleInterface):
    scl: F.ElectricLogic    # Clock line with reference power
    sda: F.ElectricLogic    # Data line with reference power

    address = L.p_field(within=L.Range(0, 0x7F))
    frequency = L.p_field(units=P.Hz)
```

**Benefit**: Type safety - connecting I2C to SPI would be a type error.

#### Pattern 2: Automatic Reference Management

Interfaces auto-connect their power references:

```python
# From faebryk/library/ElectricSignal.py
@L.rt_field
def single_electric_reference(self):
    return F.has_single_electric_reference_defined(self.reference)

@staticmethod
def connect_all_module_references(node):
    """Auto-connect all power references in a module"""
```

**Benefit**: No manual ground/power wiring between connected interfaces.

#### Pattern 3: Constraint Propagation

Parameters with units that propagate through connections:

```python
# From faebryk/library/ElectricPower.py
voltage = L.p_field(
    units=P.V,
    likely_constrained=True,
    soft_set=L.Range(0 * P.V, 1000 * P.V),
)

# Constraint propagates to all connected ElectricPower interfaces
self.voltage.add(F.is_bus_parameter())
```

**Benefit**: Voltage/current constraints automatically validate across connections.

#### Pattern 4: Connection Operators

Clean connection syntax:

```python
# Direct connection (type-checked)
i2c_bus ~ sensor.i2c

# Via component (bridged connection)
power.hv ~> resistor ~> signal.line

# Shallow connection (connects self, not children)
self.connect_shallow(fused_power)
```

#### Pattern 5: Module Composition

Modules compose interfaces and components:

```python
# From faebryk/library/ResistorVoltageDivider.py
class ResistorVoltageDivider(Module):
    # External interfaces
    power: F.ElectricPower
    output: F.ElectricSignal

    # Internal components
    r_bottom: F.Resistor
    r_top: F.Resistor

    def __preinit__(self):
        # Internal wiring
        self.power.hv.connect_via(
            [self.r_top, self.output.line, self.r_bottom],
            self.power.lv
        )
```

#### Pattern 6: Design Checks

Validation traits that run at solve time:

```python
# From faebryk/library/I2C.py
class requires_unique_addresses(ModuleInterface.TraitT):
    @F.implements_design_check.register_post_solve_check
    def __check_post_solve__(self):
        # Validates no duplicate I2C addresses on bus
        ...
```

### 1.3 Atopile Interface Summary Table

| Interface | Composed Of | Key Parameters | Features |
|-----------|-------------|----------------|----------|
| `Electrical` | - | potential | Base electrical node |
| `ElectricPower` | hv, lv: Electrical | voltage, max_current | Decoupling, surge protection |
| `ElectricSignal` | line: Electrical, reference: ElectricPower | - | Reference management |
| `ElectricLogic` | (extends ElectricSignal) | push_pull mode | Pull-up/down, set high/low |
| `I2C` | scl, sda: ElectricLogic | address, frequency | Address validation, termination |
| `SPI` | sclk, miso, mosi: ElectricLogic | - | Reference management |
| `USB2_0_IF` | d: DifferentialPair, buspower: ElectricPower | - | Voltage constraints |

## 2. Current kicad-tools Architecture

### 2.1 Circuit Block Structure

kicad-tools uses a simpler port-based system:

```python
# From src/kicad_tools/schematic/blocks/base.py
class CircuitBlock:
    def __init__(self, sch, x, y):
        self.ports: dict[str, tuple[float, float]] = {}  # name -> (x, y)
        self.components: dict[str, SymbolInstance] = {}

    def port(self, name: str) -> tuple[float, float]:
        return self.ports[name]
```

### 2.2 Example: I2C Pull-ups

```python
# From src/kicad_tools/schematic/blocks/interface/i2c.py
class I2CPullups(CircuitBlock):
    def __init__(self, sch, x, y, resistor_value="4.7k", ...):
        self.ports = {
            "VCC": r1_pin1,
            "SDA": r1_pin2,
            "SCL": r2_pin2,
            "GND": (gnd_x, gnd_y),
        }
```

### 2.3 Comparison

| Feature | Atopile | kicad-tools |
|---------|---------|-------------|
| Port typing | Strong (interface classes) | None (dict of positions) |
| Connection validation | Type-checked at connect time | Manual verification |
| Reference management | Automatic | Manual wiring |
| Constraint propagation | Yes (voltage, current) | No |
| Design rule checking | Built-in traits | Separate DRC module |
| Schematic placement | Abstract (no coordinates) | Concrete (x, y coords) |

## 3. Recommendations for kicad-tools

### 3.1 Introduce Typed Interfaces (Medium Priority)

Add interface types to enable connection validation:

```python
# Proposed: src/kicad_tools/schematic/interfaces/base.py

class Interface(Protocol):
    """Base interface type for type-checked connections."""
    @property
    def interface_type(self) -> str: ...

class PowerInterface(Interface):
    """Power connection (VCC/GND pair)."""
    vcc: Port
    gnd: Port
    voltage: float | None = None
    max_current: float | None = None

class I2CInterface(Interface):
    """I2C bus connection."""
    sda: Port
    scl: Port
    frequency: int = 100_000  # Hz

class USBDataInterface(Interface):
    """USB data connection (D+/D-)."""
    dp: Port
    dm: Port
```

**Benefits**:
- Catch misconnections at design time
- Self-documenting block interfaces
- Enable future auto-wiring features

### 3.2 Add Interface Compatibility Checking (Medium Priority)

```python
# Proposed addition to CircuitBlock
def connect(self, other: "CircuitBlock",
            self_interface: str,
            other_interface: str) -> None:
    """Connect two blocks via compatible interfaces."""
    self_if = self.get_interface(self_interface)
    other_if = other.get_interface(other_interface)

    if not self_if.is_compatible_with(other_if):
        raise InterfaceError(
            f"Cannot connect {type(self_if).__name__} "
            f"to {type(other_if).__name__}"
        )

    # Auto-wire matching ports
    for port_name in self_if.port_names():
        if port_name in other_if.port_names():
            self.schematic.add_wire(
                self_if.port(port_name),
                other_if.port(port_name)
            )
```

### 3.3 Add Parameter Validation (Low Priority)

```python
# Proposed: Add constraints to interfaces
class PowerInterface(Interface):
    voltage: float | None = None
    max_current: float | None = None

    def validate_connection(self, other: "PowerInterface") -> list[str]:
        errors = []
        if self.voltage and other.voltage:
            if abs(self.voltage - other.voltage) > 0.1:
                errors.append(
                    f"Voltage mismatch: {self.voltage}V vs {other.voltage}V"
                )
        return errors
```

### 3.4 Preserve kicad-tools Strengths

Keep what works well:
- **Concrete placement** - kicad-tools' (x, y) coordinates are essential for schematic generation
- **Factory functions** - `create_i2c_pullups()` pattern is user-friendly
- **Component access** - `self.components` dict provides needed flexibility
- **Rail connection methods** - `connect_to_rails()` pattern is practical

### 3.5 Implementation Roadmap

**Phase 1: Interface Type Definitions**
- Define interface protocols (`PowerInterface`, `I2CInterface`, etc.)
- Add `interfaces` property to `CircuitBlock`
- No breaking changes to existing API

**Phase 2: Compatibility Checking**
- Add optional `connect()` method to `CircuitBlock`
- Implement `is_compatible_with()` for each interface type
- Add warnings for potential misconnections

**Phase 3: Auto-Wiring (Future)**
- Implement automatic port-to-port wiring for compatible interfaces
- Add constraint validation on connection

## 4. Key Insights

### 4.1 What Atopile Does Better
1. **Type safety** - Connection errors caught early
2. **Reference management** - Power references auto-propagate
3. **Constraint system** - Parameters validated across connections
4. **Clean syntax** - `~` and `~>` operators intuitive

### 4.2 What kicad-tools Does Better
1. **Concrete output** - Direct schematic file generation
2. **Practical APIs** - Factory functions for common patterns
3. **Explicit control** - Full control over placement and wiring
4. **Simpler model** - Lower learning curve

### 4.3 Hybrid Approach

The ideal solution borrows atopile's type system while preserving kicad-tools' practical schematic generation:

```python
# Vision: Typed interfaces with concrete placement
class MCUBlock(CircuitBlock):
    # Typed interfaces
    power: PowerInterface
    i2c: I2CInterface
    usb: USBDataInterface

    def __init__(self, sch, x, y):
        super().__init__(sch, x, y)
        # Place components with concrete coordinates
        self.mcu = sch.add_symbol("MCU", x, y, "U1")

        # Define typed interfaces with concrete ports
        self.power = PowerInterface(
            vcc=self.mcu.pin_position("VCC"),
            gnd=self.mcu.pin_position("GND"),
            voltage=3.3
        )

# Usage with type-checked connections
mcu = MCUBlock(sch, 100, 100)
i2c_pullups = I2CPullups(sch, 150, 100)

# Type-checked connection
mcu.connect(i2c_pullups, "i2c", "i2c")  # OK
mcu.connect(usb_block, "i2c", "usb")     # Error: incompatible
```

## 5. Conclusions

Atopile's interface-based design provides valuable patterns for improving circuit block reusability and safety. The key innovations are:

1. **Typed interfaces** that catch connection errors early
2. **Automatic reference management** that reduces manual wiring
3. **Constraint propagation** that validates electrical parameters
4. **Clean connection syntax** that improves readability

For kicad-tools, we recommend incrementally adopting these patterns while preserving the practical schematic generation capabilities that are the tool's core strength.

## References

- Atopile repository: `vendor/atopile/`
- Key files analyzed:
  - `faebryk/core/moduleinterface.py` - Base interface class
  - `faebryk/core/module.py` - Module composition
  - `faebryk/library/ElectricPower.py` - Power interface
  - `faebryk/library/I2C.py` - Communication interface
  - `faebryk/library/ElectricSignal.py` - Signal interface
  - `faebryk/library/ResistorVoltageDivider.py` - Example module

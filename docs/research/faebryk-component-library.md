# Research: Atopile Faebryk Component Library

**Issue**: #307
**Date**: 2026-01-03
**Source**: `vendor/atopile/src/faebryk/library/`

## Executive Summary

The Faebryk component library is a Python-based hardware abstraction system used by atopile. It provides a comprehensive trait-based architecture for representing electronic components, their parameters, and relationships to physical implementations (footprints, symbols, suppliers).

Key findings:
- **Trait System**: Components gain capabilities through composable traits
- **Parametric Selection**: Built-in support for part picking via API queries
- **LCSC Integration**: Deep integration with LCSC/EasyEDA for footprint and symbol retrieval
- **Module Inheritance**: Hierarchical component classes enable reuse

## Library Organization

### Directory Structure

The library is organized as a flat namespace with ~170 modules:

```
faebryk/library/
├── _F.py                    # Auto-generated index (imports all modules)
├── Resistor.py              # Component definitions
├── Capacitor.py
├── LED.py
├── has_*.py                 # Traits (capabilities)
├── is_*.py                  # State traits
├── can_*.py                 # Action traits
├── Electric*.py             # Interface definitions
└── *.ato                    # Atopile language snippets
```

### Module Categories

1. **Base Components** (~40 modules)
   - Basic: `Resistor`, `Capacitor`, `Inductor`, `Diode`, `LED`
   - Transistors: `BJT`, `MOSFET`
   - ICs: `OpAmp`, `Comparator`, `LDO`, `Regulator`
   - Passives: `Crystal`, `Crystal_Oscillator`, `Fuse`
   - Protection: `TVS`, `SurgeProtection`

2. **Interfaces** (~20 modules)
   - Power: `ElectricPower`, `ElectricSignal`
   - Communication: `I2C`, `SPI`, `UART`, `CAN`, `USB_C`
   - Logic: `ElectricLogic`, `DifferentialPair`

3. **Package Types** (~10 modules)
   - `SMDTwoPin`, `DIP`, `QFN`, `SOIC`

4. **Traits** (~100 modules)
   - See "Trait System" section below

## Core Architecture

### Module Base Class

All components inherit from `faebryk.core.module.Module`:

```python
class Module(Node):
    """Base class for all faebryk modules (components, interfaces)."""

    # Specialization graph edges
    specializes = f_field(GraphInterfaceModuleSibling)(is_parent=False)
    specialized = f_field(GraphInterfaceModuleSibling)(is_parent=True)

    def specialize[T: Module](self, special: T, ...) -> T:
        """Specialize this module into a more specific implementation."""

    def get_parameters(self) -> list[Parameter]:
        """Get all parameters defined on this module."""
```

### Parameter System

Parameters are typed fields with units, constraints, and solver support:

```python
class Resistor(Module):
    # Parameters with units
    resistance = L.p_field(units=P.ohm)
    max_power = L.p_field(units=P.W)
    max_voltage = L.p_field(units=P.V)
```

```python
class Capacitor(Module):
    capacitance = L.p_field(
        units=P.F,
        likely_constrained=True,
        soft_set=L.Range(100 * P.pF, 1 * P.F),
        tolerance_guess=10 * P.percent,
    )
```

### Trait System

Traits are composable behaviors added to modules:

#### Trait Categories

| Prefix | Purpose | Examples |
|--------|---------|----------|
| `has_*` | Module properties | `has_footprint`, `has_part_picked`, `has_resistance` |
| `is_*` | State markers | `is_pickable`, `is_atomic_part`, `is_decoupled` |
| `can_*` | Actions/capabilities | `can_bridge`, `can_specialize`, `can_attach_to_footprint` |

#### Key Traits

**Part Selection:**
```python
class is_pickable_by_type(F.is_pickable):
    """Marks module as parametrically selectable via API."""

    class Endpoint(StrEnum):
        RESISTORS = "resistors"
        CAPACITORS = "capacitors"
        INDUCTORS = "inductors"

    def __init__(self, endpoint: Endpoint, params: list[Parameter]):
        self.endpoint = endpoint
        self._params = params
```

**Part Assignment:**
```python
class has_part_picked(Module.TraitT):
    """Records which physical part was selected for this module."""

    @classmethod
    def by_supplier(cls, supplier_id, supplier_partno, manufacturer, partno):
        # Creates PickedPartLCSC for LCSC parts
```

**Footprint Attachment:**
```python
class is_atomic_part(Module.TraitT):
    """Links module to specific manufacturer part with footprint/symbol."""

    def __init__(self, manufacturer, partnumber, footprint, symbol, model=None):
        self._manufacturer = manufacturer
        self._footprint = footprint  # Path to .kicad_mod
        self._symbol = symbol        # Path to .kicad_sym
```

**Designator Prefixes:**
```python
class has_designator_prefix(Module.TraitT):
    class Prefix(StrEnum):
        R = "R"    # Resistor
        C = "C"    # Capacitor
        L = "L"    # Inductor
        U = "U"    # IC
        Q = "Q"    # Transistor
        D = "D"    # Diode
        # ... 50+ standard prefixes
```

## Component Examples

### Resistor

```python
class Resistor(Module):
    unnamed = L.list_field(2, F.Electrical)  # Two pins

    resistance = L.p_field(units=P.ohm)
    max_power = L.p_field(units=P.W)
    max_voltage = L.p_field(units=P.V)

    attach_to_footprint: F.can_attach_to_footprint_symmetrically
    designator_prefix = L.f_field(F.has_designator_prefix)(
        F.has_designator_prefix.Prefix.R
    )

    @L.rt_field
    def pickable(self) -> F.is_pickable_by_type:
        return F.is_pickable_by_type(
            endpoint=F.is_pickable_by_type.Endpoint.RESISTORS,
            params=[self.resistance, self.max_power, self.max_voltage],
        )
```

### LED (extends Diode)

```python
class LED(F.Diode):
    class Color(Enum):
        RED = auto()
        GREEN = auto()
        BLUE = auto()
        # ... more colors

    brightness = L.p_field(units=P.candela)
    max_brightness = L.p_field(units=P.candela)
    color = L.p_field(domain=L.Domains.ENUM(Color))

    def set_intensity(self, intensity):
        self.brightness.alias_is(intensity * self.max_brightness)
```

## LCSC/EasyEDA Integration

### Part Fetching Flow

1. **Query API** via `download_easyeda_info(lcsc_id)`
2. **Parse Response** into `EasyEDAAPIResponse`
3. **Convert to KiCad**:
   - Footprint: `EasyEDAFootprint.from_api()` → `.kicad_mod`
   - Symbol: `EasyEDASymbol.from_api()` → `.kicad_sym`
   - 3D Model: `EasyEDA3DModel` → `.step`
4. **Attach to Module** via `F.KicadFootprint.from_path()`

### Key Classes

```python
class PickedPartLCSC(PickedPart):
    """Represents an LCSC-sourced part."""

    @dataclass
    class Info:
        stock: int
        price: float
        description: str
        basic: bool       # JLCPCB basic part
        preferred: bool   # JLCPCB preferred part

class EasyEDAPart:
    """Composite of footprint, symbol, and 3D model from LCSC."""

    lcsc_id: str
    description: str
    mfn_pn: tuple[str, str]
    footprint: EasyEDAFootprint
    symbol: EasyEDASymbol
    model: EasyEDA3DModel | None
```

### Part Lifecycle

```
LCSC ID (C123456)
    ↓
EasyEDA API → EasyEDAAPIResponse
    ↓
PartLifecycle.ingest_part() → EasyEDAPart
    ↓
Library.ingest_part_from_easyeda() → ato component
    ↓
Module.get_trait(F.can_attach_to_footprint).attach(fp)
```

## Comparison with kicad-tools

### Current kicad-tools Library

| Feature | kicad-tools | Faebryk |
|---------|-------------|---------|
| **Component Model** | Footprint-centric | Module/Trait-centric |
| **Parameters** | Manual | Units-aware, constrained |
| **Part Selection** | Manual LCSC lookup | Parametric API queries |
| **Footprint Generation** | Parametric generators | Template + LCSC fetch |
| **Supplier Integration** | LCSC client for BOM | Deep LCSC/EasyEDA |
| **Symbol Support** | None | Full symbol generation |
| **3D Models** | None | STEP from EasyEDA |

### kicad-tools Strengths

1. **Parametric Footprint Generators**
   - `create_soic()`, `create_qfp()`, `create_chip()`, etc.
   - IPC-7351 compliant naming
   - Detailed pad/courtyard control

2. **LCSC Client**
   - Simple search and lookup
   - BOM availability checking
   - Caching layer

### Faebryk Strengths

1. **Trait System**
   - Composable behaviors
   - Clear ownership of capabilities
   - Extensible architecture

2. **Parametric Selection**
   - Query parts by electrical parameters
   - Automatic part equivalents discovery

3. **Complete CAD Asset Pipeline**
   - Footprint + Symbol + 3D model in one flow
   - Auto-generation from LCSC data

## Potential Improvements for kicad-tools

### 1. Add Trait-like Metadata to Components

Create a metadata system for generated footprints:

```python
@dataclass
class FootprintMetadata:
    manufacturer: str | None = None
    partnumber: str | None = None
    supplier_id: str | None = None
    supplier_partno: str | None = None
    designator_prefix: str = "U"
    datasheet_url: str | None = None
```

### 2. Support Manufacturer/Supplier Linking

Link footprints to specific parts:

```python
class FootprintWithPart(Footprint):
    metadata: FootprintMetadata

    def to_sexp(self):
        # Include properties in KiCad format
        props = f'\t(property "MPN" "{self.metadata.partnumber}")'
```

### 3. Component Discovery/Search API

Extend LCSC client for parametric search:

```python
def search_by_parameters(
    component_type: str,  # "resistor", "capacitor", etc.
    parameters: dict,     # {"resistance": "10kohm", "package": "0402"}
) -> list[Part]:
    """Find parts matching electrical parameters."""
```

### 4. Parametric Component Generators with Metadata

Combine footprint generation with part selection:

```python
def create_resistor(
    resistance: str,
    package: str = "0402",
    tolerance: str = "1%",
) -> tuple[Footprint, PartSelection]:
    """Generate footprint and find matching LCSC parts."""

    fp = create_chip(package, prefix="R")
    parts = search_by_parameters("resistor", {
        "resistance": resistance,
        "package": package,
        "tolerance": tolerance,
    })
    return fp, parts
```

### 5. Component Alternative Suggestions

Add a system for tracking equivalent parts:

```python
class PartAlternatives:
    primary: Part
    alternatives: list[Part]

    @classmethod
    def from_parameters(cls, params: dict) -> "PartAlternatives":
        """Find primary and alternative parts for given parameters."""
```

## Questions for Future Work

1. **Integration Strategy**: Should kicad-tools adopt Faebryk's trait system, or create a simpler metadata approach?

2. **API Dependencies**: Faebryk relies heavily on atopile's API backend. Should kicad-tools maintain independence or integrate?

3. **Symbol Generation**: Is symbol generation needed, or should users rely on manufacturer symbols?

4. **Constraint Solving**: Faebryk has a parameter constraint solver. Is this valuable for kicad-tools?

## Appendix: File References

- **Core Module**: `faebryk/core/module.py`
- **Resistor**: `faebryk/library/Resistor.py`
- **Capacitor**: `faebryk/library/Capacitor.py`
- **LED**: `faebryk/library/LED.py`
- **LCSC Integration**: `faebryk/libs/picker/lcsc.py`
- **Trait Examples**:
  - `faebryk/library/has_part_picked.py`
  - `faebryk/library/is_pickable_by_type.py`
  - `faebryk/library/is_atomic_part.py`
  - `faebryk/library/has_designator_prefix.py`

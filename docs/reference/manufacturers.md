# Manufacturer Design Rules Reference

kicad-tools includes design rules for common PCB manufacturers. Use these with DRC checking to ensure your design is manufacturable.

---

## Supported Manufacturers

| Manufacturer | ID | Layers | Min Feature |
|--------------|-----|--------|-------------|
| JLCPCB | `jlcpcb` | 1-6 | 0.127mm |
| OSHPark | `oshpark` | 2-4 | 0.152mm |
| PCBWay | `pcbway` | 1-14 | 0.102mm |
| Seeed Studio | `seeed` | 2-4 | 0.152mm |

---

## Using Manufacturer Rules

### CLI

```bash
# Run DRC with manufacturer rules
kct drc board.kicad_pcb --mfr jlcpcb

# Compare manufacturers
kct mfr compare jlcpcb oshpark pcbway

# Generate KiCad DRU file
kct mfr dru jlcpcb -o jlcpcb.dru
```

### Python

```python
from kicad_tools.drc import DRCChecker
from kicad_tools.manufacturers import get_rules

# Get manufacturer rules
rules = get_rules("jlcpcb", layers=4)

# Use with DRC
checker = DRCChecker(rules=rules)
violations = checker.check("board.kicad_pcb")
```

---

## JLCPCB

Budget-friendly manufacturer with fast turnaround.

### Standard PCB (1-2 layers)

| Parameter | Value |
|-----------|-------|
| Min trace width | 0.127mm (5mil) |
| Min trace spacing | 0.127mm (5mil) |
| Min drill size | 0.3mm |
| Min annular ring | 0.13mm |
| Min via diameter | 0.45mm |
| Board thickness | 0.4-2.0mm |

### 4-6 Layer PCB

| Parameter | Value |
|-----------|-------|
| Min trace width | 0.09mm (3.5mil) |
| Min trace spacing | 0.09mm (3.5mil) |
| Min drill size | 0.2mm |
| Min annular ring | 0.1mm |
| Via-in-pad | Supported |

### Assembly (SMT)

| Parameter | Value |
|-----------|-------|
| Min component | 0201 |
| Min pitch | 0.4mm |
| Min BGA pitch | 0.35mm |

**Note:** JLCPCB has specific parts library for assembly. Use `kct parts search` to find compatible parts.

---

## OSHPark

High-quality purple PCBs, made in USA.

### 2-Layer

| Parameter | Value |
|-----------|-------|
| Min trace width | 0.152mm (6mil) |
| Min trace spacing | 0.152mm (6mil) |
| Min drill size | 0.254mm (10mil) |
| Min annular ring | 0.127mm (5mil) |
| Board thickness | 1.6mm |
| Finish | ENIG |

### 4-Layer

| Parameter | Value |
|-----------|-------|
| Min trace width | 0.127mm (5mil) |
| Min trace spacing | 0.127mm (5mil) |
| Min drill size | 0.254mm (10mil) |
| Board thickness | 0.8mm or 1.6mm |
| Controlled impedance | Supported |

---

## PCBWay

Flexible manufacturer with many options.

### Standard (1-2 layers)

| Parameter | Value |
|-----------|-------|
| Min trace width | 0.102mm (4mil) |
| Min trace spacing | 0.102mm (4mil) |
| Min drill size | 0.2mm |
| Min annular ring | 0.1mm |
| Board thickness | 0.2-3.2mm |

### Advanced (4+ layers)

| Parameter | Value |
|-----------|-------|
| Min trace width | 0.076mm (3mil) |
| Min trace spacing | 0.076mm (3mil) |
| Blind/buried vias | Supported |
| HDI | Supported |

---

## Seeed Studio (Fusion)

Beginner-friendly with good documentation.

### 2-Layer

| Parameter | Value |
|-----------|-------|
| Min trace width | 0.152mm (6mil) |
| Min trace spacing | 0.152mm (6mil) |
| Min drill size | 0.3mm |
| Min annular ring | 0.15mm |
| Board thickness | 0.6-2.0mm |

---

## Rule Comparison

Use the CLI to compare manufacturers:

```bash
$ kct mfr compare jlcpcb oshpark pcbway seeed

Parameter           JLCPCB    OSHPark   PCBWay    Seeed
─────────────────────────────────────────────────────────
Min trace width     0.127mm   0.152mm   0.102mm   0.152mm
Min spacing         0.127mm   0.152mm   0.102mm   0.152mm
Min drill           0.30mm    0.25mm    0.20mm    0.30mm
Min annular ring    0.13mm    0.13mm    0.10mm    0.15mm
Min via diameter    0.45mm    0.51mm    0.40mm    0.60mm
```

---

## Custom Rules

Create custom design rules:

```python
from kicad_tools.manufacturers import DesignRules

my_rules = DesignRules(
    name="MyFab",
    min_trace_width=0.15,      # mm
    min_trace_spacing=0.15,     # mm
    min_drill=0.25,             # mm
    min_annular_ring=0.125,     # mm
    min_via_diameter=0.5,       # mm
    min_silkscreen_width=0.15,  # mm
    min_silkscreen_clearance=0.1,
)

checker = DRCChecker(rules=my_rules)
```

---

## Generating DRU Files

Export rules as KiCad Design Rules files:

```bash
kct mfr dru jlcpcb -o jlcpcb.dru
```

Then import in KiCad: **Board Setup > Design Rules > Import**

---

## See Also

- [DRC & Validation Guide](../guides/drc-and-validation.md)
- [Manufacturing Export Guide](../guides/manufacturing-export.md)

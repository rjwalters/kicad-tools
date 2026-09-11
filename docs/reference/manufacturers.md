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

### Standard PCB (1-2 layers, 1oz copper)

| Parameter | Value |
|-----------|-------|
| Min trace width | 0.127mm (5mil) |
| Min trace spacing | 0.127mm (5mil) |
| Min drill size | 0.3mm |
| Min annular ring | 0.13mm |
| Min via diameter | 0.45mm |
| Board thickness | 0.4-2.0mm |

### Standard PCB (1-2 layers, 2oz copper)

| Parameter | Value |
|-----------|-------|
| Min trace width | 0.1524mm (6mil) |
| Min trace spacing | 0.1524mm (6mil) |
| Min drill size | 0.3mm |
| Min annular ring | 0.15mm |
| Min via diameter | 0.6mm |
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
    min_trace_width=0.15,  # mm
    min_trace_spacing=0.15,  # mm
    min_drill=0.25,  # mm
    min_annular_ring=0.125,  # mm
    min_via_diameter=0.5,  # mm
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

### Object-specific JLCPCB spacing

The `jlcpcb` and `jlcpcb-tier1` profiles include these rigid-board limits,
verified against the [JLCPCB capabilities table](https://jlcpcb.com/capabilities/pcb-capabilities/)
on 2026-09-10:

| Constraint | Minimum | Applicability |
| --- | --- | --- |
| Pad to silkscreen | 0.15 mm | Same board side, explicit cross-layer `silk_clearance` |
| SMD pad to pad | 0.15 mm | Different nets; never weaker than general copper clearance |
| PTH to track | 0.28 mm | Plated component-hole edge to different-net track copper; 0.35 mm is a recommendation |
| Inner PTH hole to copper | 0.30 mm | Inner copper layers, different nets |

The PTH distances are measured from the drilled hole, not the pad's copper edge.
They do not replace the distinct via-hole spacing limits. These constraints do
not impose a same-net physical clearance on valid copper joins. Other profiles
can leave the optional object-specific fields unset.

Export and `check --emit-dru` place the explicit constraints in the managed fab
rule block while retaining custom rules outside it. The silk rule is not
restricted to a silk layer: it must compare objects across layers. KiCad also
checks pad copper when no mask opening is present. The project-wide silk floor
is unchanged, so the pad-specific limit does not become a silk-to-silk limit.

Python checks cover SMD pad pairs, round/slotted PTH holes (including drill
offsets and rotation), inner filled-zone copper, and modeled silk line/rectangle
strokes against pad copper and mask openings. Text glyph rendering and unsupported
silk primitives still require native KiCad DRC; Python text bounding-box checks
remain advisory. Native DRC and the manufacturer's DFM review are separate
checks. Emitting these rules neither certifies a board nor clears an existing
manufacturing hold, and no board artifacts are regenerated automatically.

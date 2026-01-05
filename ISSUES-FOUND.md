# kicad-tools v0.7.1 - Issues Found During Testing

Testing performed on: Chorus Timing Test HAT Rev A (4-layer Pi HAT)
Date: 2026-01-04
Updated: 2026-01-04 (v0.7.1 testing round 2)

## GitHub Issues Filed

| Issue | Title | Severity |
|-------|-------|----------|
| [#438](https://github.com/rjwalters/kicad-tools/issues/438) | analyze signal-integrity documented but not implemented | Critical |
| [#439](https://github.com/rjwalters/kicad-tools/issues/439) | parts availability throws IndexError | Critical |
| [#440](https://github.com/rjwalters/kicad-tools/issues/440) | Router --grid parameter is ignored | Critical |
| [#441](https://github.com/rjwalters/kicad-tools/issues/441) | net-status doesn't detect via-to-zone connectivity | Critical |
| [#442](https://github.com/rjwalters/kicad-tools/issues/442) | audit command attribute errors | High |
| [#443](https://github.com/rjwalters/kicad-tools/issues/443) | estimate cost shows 0 components | High |
| [#444](https://github.com/rjwalters/kicad-tools/issues/444) | reasoning module issues (PadState, board size, layers) | High |
| [#445](https://github.com/rjwalters/kicad-tools/issues/445) | placement optimize missing --format json | High |
| [#446](https://github.com/rjwalters/kicad-tools/issues/446) | Router treats 4-layer as 2-layer | Medium |
| [#447](https://github.com/rjwalters/kicad-tools/issues/447) | validate --sync confusing CLI syntax | Medium |

---

## Critical Issues

### 1. `analyze signal-integrity` Documented But Not Implemented ([#438](https://github.com/rjwalters/kicad-tools/issues/438))
**Command**: `kct analyze signal-integrity <pcb>`
**Error**: `invalid choice: 'signal-integrity' (choose from 'congestion', 'trace-lengths', 'thermal')`
**Reference**: CHANGELOG.md v0.7.0 lists this command with full feature description
**Impact**: Cannot run signal integrity analysis as documented

### 2. `parts availability` Throws IndexError ([#439](https://github.com/rjwalters/kicad-tools/issues/439))
**Command**: `kct parts availability <schematic> --format json`
**Error**: `IndexError: string index out of range`
**Impact**: Cannot check LCSC stock levels for components

### 3. `--grid` Parameter Ignored in Router ([#440](https://github.com/rjwalters/kicad-tools/issues/440))
**Command**: `kct route <pcb> --grid 0.1`
**Observed**: Grid resolution remains at 0.25mm regardless of `--grid` value
**Warning shown**: "Grid resolution 0.25mm exceeds clearance 0.15mm. This WILL cause DRC violations."
**Impact**: Cannot achieve proper DRC compliance through routing

### 4. `net-status` Doesn't Detect Via-to-Zone Connectivity ([#441](https://github.com/rjwalters/kicad-tools/issues/441))
**Scenario**: After adding 44 stitching vias with `kicad-pcb-stitch`, `net-status` still reports the same 80 unconnected pads
**Expected**: Vias connecting pads to plane nets should be recognized as completing the connection
**Impact**: False negatives in connectivity validation after stitching

## High Priority Issues

### 5. `audit` Missing DesignRules Attributes ([#442](https://github.com/rjwalters/kicad-tools/issues/442))
**Command**: `kct audit <pcb> --mfr jlcpcb --format json`
**Errors**:
- `Compatibility check failed: 'DesignRules' object has no attribute 'max_board_width_mm'`
- `Cost estimation failed: 'ManufacturingCostEstimator' object has no attribute 'estimate_pcb'`
**Impact**: Audit runs but compatibility/cost checks fail

### 6. `estimate cost` Shows 0 Components ([#443](https://github.com/rjwalters/kicad-tools/issues/443))
**Command**: `kct estimate cost <pcb> --format json`
**Observed**: `"total_parts": 0, "unique_parts": 0`
**Expected**: Should read BOM from schematic or detect footprints on PCB
**Impact**: Cost estimation incomplete - only PCB fab cost, not component cost

### 7. Reasoning Module Issues ([#444](https://github.com/rjwalters/kicad-tools/issues/444))
**Commands**: `kct reason <pcb> --export-state`, `kct reason <pcb> --analyze`
**Errors**:
- `AttributeError: 'PadState' object has no attribute 'name'`
- Board size reported as 0.0mm x 0.0mm (actual: 65mm x 56mm)
- Layer count reported as 2 (actual: 4)
**Impact**: Cannot export PCB state for external LLM reasoning

### 8. `placement optimize` Missing --format Flag ([#445](https://github.com/rjwalters/kicad-tools/issues/445))
**Command**: `kct placement optimize <pcb> --cluster --thermal --format json`
**Error**: `unrecognized arguments: --format json`
**Expected**: JSON output for scripting/automation
**Impact**: Cannot use placement optimization in automated pipelines

## Medium Priority Issues

### 9. Autorouter Only Routes F.Cu/B.Cu on 4-Layer Board ([#446](https://github.com/rjwalters/kicad-tools/issues/446))
**Observation**: On a 4-layer board with In1.Cu (GND) and In2.Cu (+3.3V) zones, the router reports "2-Layer" and only uses F.Cu and B.Cu for signal routing
**Suggestion**: Document that inner layers are plane-only, or add `--layers` option to specify routable layers

### 10. Stitch Command Auto-Detection Now Works
**Status**: ✅ FIXED in v0.7.1
**Command**: `kicad-pcb-stitch <pcb> --net GND`
**Observed**: Correctly auto-detects `GND -> In1.Cu` from zone assignment
**Previously**: Defaulted to B.Cu incorrectly

### 11. validate --sync CLI Syntax Confusing ([#447](https://github.com/rjwalters/kicad-tools/issues/447))
**Command**: `kct validate --sync <sch> <pcb>`
**Error**: `unrecognized arguments`
**Correct syntax**: `kct validate --sync -s <sch> -p <pcb>`
**Suggestion**: Accept positional arguments for common case

## Low Priority / Cosmetic

### 12-13. Reason Agent Board Size and Layer Issues
**Covered by**: [#444](https://github.com/rjwalters/kicad-tools/issues/444)
These issues are included in the reasoning module issue above.

---

## Summary

| Severity | Count | Fixed |
|----------|-------|-------|
| Critical | 4 | 0 |
| High | 4 | 0 |
| Medium | 3 | 1 |
| Low | 2 | 0 |
| **Total** | **13** | **1** |

## Features Working Well

These features work correctly and provide valuable output:

- ✅ `kct check <pcb> --mfr jlcpcb` - Pure Python DRC with actionable suggestions
- ✅ `kct net-status <pcb>` - Net connectivity report (except via-to-zone detection)
- ✅ `kct analyze congestion <pcb>` - Routing hotspot detection
- ✅ `kct analyze trace-lengths <pcb>` - Timing-critical net analysis
- ✅ `kct analyze thermal <pcb>` - Heat source clustering and suggestions
- ✅ `kicad-pcb-stitch <pcb> --net GND` - Automatic stitching via placement
- ✅ `kct audit <pcb> --mfr jlcpcb` - Comprehensive audit with action items
- ✅ `kct route <pcb> --strategy negotiated` - Autorouting (with limitations)

## Test Commands Used

```bash
# Audit
kct audit <pcb> --mfr jlcpcb --format json

# Analysis
kct analyze thermal <pcb> --format json
kct analyze congestion <pcb> --format json
kct analyze trace-lengths <pcb> --format json
kct analyze signal-integrity <pcb>  # FAILS - not implemented

# Net Status
kct net-status <pcb> --format json

# DRC
kct check <pcb> --mfr jlcpcb --format json

# Routing
kct route <pcb> --skip-nets "GND,+3.3V" --grid 0.1 --dry-run

# Stitching
kicad-pcb-stitch <pcb> --net GND
kicad-pcb-stitch <pcb> --net "+3.3V"

# Cost
kct estimate cost <pcb> --format json

# Parts
kct parts availability <schematic> --format json

# Placement
kct placement optimize <pcb> --cluster --thermal

# Reasoning
kct reason <pcb> --analyze
kct reason <pcb> --export-state
```

## Test Environment

- macOS Darwin 25.2.0
- Python 3.12
- kicad-tools 0.7.1 (installed via `uv pip install -e`)
- KiCad 9.0 project format
- 4-layer PCB with internal GND and power planes

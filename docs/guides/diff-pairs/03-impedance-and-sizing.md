# 03 — Impedance-Driven Sizing

Set a target impedance and the router derives `(trace_width,
intra_pair_clearance)` from the PCB stackup. From Epic #2556 Phase 3K
(#2655).

## Fields

`NetClassRouting` (`src/kicad_tools/router/rules.py`):

| Field | Line | Use |
|---|---|---|
| `target_diff_impedance` | 486 | Differential impedance in Ω (e.g. 90 for USB 2.0, 100 for USB 3.0 / PCIe / MIPI). |
| `target_single_impedance` | 504 | Single-ended impedance in Ω (50 for clocks; 75 for video/coax). |
| `impedance_tolerance_percent` | 515 | DRC firing threshold, default 10.0 %. |

When `target_diff_impedance` is set, the router calls
`kicad_tools.router.diffpair_impedance.apply_impedance_driven_sizing` to
compute the width/spacing pair from the stackup via
`kicad_tools.physics.CoupledLines`. When `None` (default), the per-class
`trace_width` / `intra_pair_clearance` literals are used unchanged.

```python
from kicad_tools.router.rules import NetClassRouting

usb3 = NetClassRouting(
    name="USB3_SS",
    target_diff_impedance=90.0,
    impedance_tolerance_percent=10.0,
    coupled_routing=True,
)
```

A class may set `target_diff_impedance`, `target_single_impedance`, both, or
neither. When both are set, diff-pair nets consume the diff value; single-
ended nets in the same class consume the single value.

## Stackup awareness

The impedance solver reads the board's stackup (dielectric thickness, εr,
copper thickness). When the actual stackup deviates from the JLCPCB tier-1
calibration baseline (FR-4, εr=4.5, 35 µm Cu) the solver emits a
`StackupMismatchWarning` from
`src/kicad_tools/router/diffpair_impedance.py:76`. The warning is included
in the route result so consumers can flag the discrepancy at CI time.

When the stackup is unknown, the solver falls back to the JLCPCB tier-1
baseline. Boards without an explicit stackup will still route, but the
computed width may not actually achieve the target impedance — set the
stackup explicitly for HSDI work.

## CLI: `kct impedance`

The `impedance` CLI command exposes the underlying solver outside the
router:

| Subcommand | What it does |
|---|---|
| `kct impedance stackup` | Print the board's stackup. |
| `kct impedance width` | Compute width for a target single-ended impedance. |
| `kct impedance diffpair` | Compute width/spacing for a target differential impedance. |
| `kct impedance calculate` | Solve impedance from a given geometry. |
| `kct impedance crosstalk` | Estimate crosstalk for a given geometry. |

Source: `src/kicad_tools/cli/commands/impedance.py`.

## See also

- [02-clearance-and-classes.md](02-clearance-and-classes.md) — when
  `target_diff_impedance` is unset, the literal `intra_pair_clearance` is
  used as-is.
- [06-drc-rules.md](06-drc-rules.md) — the `impedance` DRC rule validates
  routed widths against the target.

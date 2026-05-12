# 01 — Declaring a Match Group

A match group is declared by configuring the **net class**, not by an
imperative `router.add_match_group(...)` call (though that legacy API
is still supported).

Reference board: [`boards/07-matchgroup-test`](../../../boards/07-matchgroup-test/)
exercises all three detection paths end-to-end.

## Three detection paths (priority order)

### 1. Explicit `length_match_group` (authoritative)

The strongest declaration. Set `length_match_group="DDR_DATA_BYTE_0"` on
every net class whose members belong to that group:

```python
from kicad_tools.router.rules import NetClassRouting

ddr_dq = NetClassRouting(
    name="DDR_DQ",
    trace_width=0.15,
    clearance=0.15,
    length_match_group="DDR_DATA_BYTE_0",   # group name
    length_match_tolerance_mm=0.1,           # ±0.1 mm
    length_critical=True,                    # gate for the tuner
)
```

Field defined at `src/kicad_tools/router/rules.py:602`. Multiple net
classes may declare the same group name — their members merge into a
single group (the documented MIPI/HDMI lane-composition pattern; see
guide 03). Overrides suffix inference at detection time
(`match_group_detection.py:172`).

### 2. Legacy `Autorouter.add_match_group(...)` API

Direct Python API kept for backward compatibility. Adds a group to the
underlying `LengthTracker.match_groups` dict at runtime:

```python
router.add_match_group(
    name="DDR_DATA",
    net_ids=[100, 101, 102, 103, 104, 105, 106, 107],
    tolerance=0.5,
)
```

Defined at `src/kicad_tools/router/core.py:7211`. Lower priority than
explicit declarations — a legacy group whose members are already
claimed by an EXPLICIT class is dropped (see `detect_match_groups` at
`match_group_detection.py:172`).

### 3. Suffix inference (opt-in, last resort)

Off by default. Enable by passing `enable_suffix_inference=True` to
`detect_match_groups()`. The detector consults `BUS_GROUP_PATTERNS` at
`match_group_detection.py:110`, which recognises:

- `DQ\d+` → `DDR_DATA`
- `CSI_DAT\d+_[PN]` → `MIPI_CSI_DATA`
- `DSI_DAT\d+_[PN]` → `MIPI_DSI_DATA`
- `TMDS_D\d+_[PN]` → `HDMI_TMDS_DATA`
- `A\d+` → `ADDR_BUS`

Groups with fewer than three members are **refused** (`_MIN_GROUP_SIZE=3`).
This is the same false-positive lesson as USB-CC1/CC2 in
`diffpair_detection.py`: low-confidence patterns (e.g. `A0`/`A1` could
be GPIO not address bus) are not auto-promoted. **Prefer explicit
declarations when in doubt.**

## See also

- [02-reference-selection.md](02-reference-selection.md) — pick which
  trace is the reference (longest / explicit / `clock`).
- [05-protocol-recipes.md](05-protocol-recipes.md) — copy-pasteable
  per-protocol `NetClassRouting` blocks.

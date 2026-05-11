# 01 — Declaring a Differential Pair

A pair is declared by configuring the **net class**, not by an imperative
`router.add_diff_pair(...)` call (no such method exists).

## Three detection paths (priority order)

### 1. Explicit `diffpair_partner` (authoritative)

The strongest declaration. A one-sided declaration (only one of the two nets
has `diffpair_partner` set) is sufficient.

```python
from kicad_tools.router.rules import NetClassRouting

usb_p = NetClassRouting(
    name="USB_DP",
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.075,
    diffpair_partner="USB_DM",   # names the other half
    coupled_routing=True,
)
```

Defined at `src/kicad_tools/router/rules.py:448`. This overrides KiCad group
declarations and suffix inference.

### 2. KiCad schematic DiffPair group

Pairs declared in the schematic via KiCad's DiffPair grouping are honored.
Use this when the schematic is the source of truth and you don't want to
duplicate the pair-name list in Python.

### 3. Suffix inference (fallback)

Net names with matching `_P`/`_N` or `+`/`-` suffixes are detected
automatically by `parse_differential_signal` in
`src/kicad_tools/router/diffpair.py:365`. Examples that auto-detect:

- `USB_D+` / `USB_D-`
- `PCIE_TX_P` / `PCIE_TX_N`
- `MIPI_CLK_P` / `MIPI_CLK_N`

## Single-ended refusal (USB_CC1/CC2 lesson)

USB-C `CC1`/`CC2` and `SBU1`/`SBU2` *look* like a diff pair but are
electrically single-ended orientation/sideband pins. `is_single_ended_refused`
in `src/kicad_tools/router/diffpair.py:240` refuses them at suffix-inference
time, and `should_engage_coupled` in `diffpair.py:296` re-applies the refusal
at the engagement layer — so even an explicit `diffpair_partner="USB_CC2"`
declaration **cannot** force coupled routing on these pins (Issue #2527
curator lesson).

```python
from kicad_tools.router.diffpair import is_single_ended_refused

assert is_single_ended_refused("USB_CC1")    # True — refused
assert is_single_ended_refused("USB_SBU1")   # True — refused
assert not is_single_ended_refused("USB_D+") # False — real diff pair
```

If you genuinely need tight clearance on `CC1`/`CC2` for ESD-trace reasons,
configure `intra_pair_clearance` on the class — they will still be routed as
two independent single-ended nets, not as a coupled pair.

## See also

- [02-clearance-and-classes.md](02-clearance-and-classes.md) — once you have a
  pair, configure its clearances.
- [05-protocol-recipes.md](05-protocol-recipes.md) — copy-pasteable
  `NetClassRouting` blocks for USB / PCIe / MIPI.

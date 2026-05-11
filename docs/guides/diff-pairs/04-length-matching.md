# 04 — Length Matching (Skew Tolerance)

Differential pairs must have matched P and N routed lengths to preserve
signal integrity. Length matching is configured via `skew_tolerance_mm` on
the net class — **not** `length_constraint` (which is for absolute lengths
per Issue #630, not pair skew).

## Field

`NetClassRouting.skew_tolerance_mm` at `src/kicad_tools/router/rules.py:550`
holds the maximum allowed `|L_p - L_n|` for pairs in this class:

```python
from kicad_tools.router.rules import NetClassRouting

usb3 = NetClassRouting(
    name="USB3_SS",
    skew_tolerance_mm=0.1,   # tight: USB 3.0 / PCIe Gen2+
    coupled_routing=True,
)
```

Read it via the accessor, which falls back to the rule's module-level
default when the field is unset:

```python
nc = NetClassRouting(name="USB2", skew_tolerance_mm=None)
assert nc.effective_skew_tolerance(default=0.5) == 0.5

nc2 = NetClassRouting(name="MIPI", skew_tolerance_mm=0.04)
assert nc2.effective_skew_tolerance() == 0.04
```

Defined at `rules.py:604`. Default of `0.5 mm` covers USB 3.0 / PCIe Gen 2+
(~0.5–1 mm), MIPI D-PHY (~1 mm), and DDR4 DQ-strobe (~0.5 mm); set `3.0`
for USB 2.0 HS.

## Measurement: `Autorouter.update_diffpair_skew`

After routing, populate the skew tracker with the detected pairs:

```python
from kicad_tools.router import Autorouter
from kicad_tools.router.diffpair_detection import detect_diff_pairs

router = Autorouter(...)
router.route_all_with_diffpairs(...)
pairs = detect_diff_pairs(...)
tracker = router.update_diffpair_skew(
    detected_pairs=pairs,
    board_thickness_mm=1.6,
    num_copper_layers=4,
)
# tracker exposes (L_p, L_n) and skew per pair
```

`update_diffpair_skew` is at `src/kicad_tools/router/core.py:6926`. The
tracker (`DiffPairLengthTracker`) measures skew unconditionally per pair —
no per-class gate. The `skew_tolerance_mm` field only governs whether the
DRC rule fires (guide 06).

## When serpentine inserts

> **TODO (Phase 3I / Issue #2648)**: serpentine / meander insertion to repair
> skew within `skew_tolerance_mm`. When this lands, this section will
> document:
>
> - When the autorouter automatically inserts serpentines
> - How to disable insertion (per pair, per class)
> - Amplitude / spacing control
>
> Until then, post-route serpentine insertion is a manual `kct` step or a
> KiCad operation. Track #2648 for the autorouter path.

## See also

- [05-protocol-recipes.md](05-protocol-recipes.md) — protocol-tuned
  `skew_tolerance_mm` values.
- [06-drc-rules.md](06-drc-rules.md) — `diffpair_length_skew` validates
  routed skew.

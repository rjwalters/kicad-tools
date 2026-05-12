# 03 — Groups Whose Members Are Diff Pairs (MIPI / HDMI)

MIPI and HDMI lane groups are **groups whose members are themselves
differential pairs**. The Phase 2F symmetric-serpentine path
(`match_group_tuning.tune_match_group_v2`, `router/match_group_tuning.py:214`)
preserves within-pair coupling while equalising lane-to-lane length.

Reference board: [`boards/07-matchgroup-test`](../../../boards/07-matchgroup-test/)
MIPI scenario.

## The naive trap

Treating each net in a MIPI lane group independently — measure each
trace, meander whichever is shorter — destroys diff-pair length skew.
A 0.5 mm bulge on `CSI_DAT0_P` while `CSI_DAT0_N` is left alone yields
0.5 mm of intra-pair skew, an order of magnitude worse than the
~0.05 mm target on MIPI D-PHY.

## What the v2 tuner does

When the tuner sees a group member that is **itself a diff pair**
(detected via the per-net `NetClassRouting.diffpair_partner`), it
inserts the serpentine on **both halves simultaneously** with
identical amplitude, position, and segment count. The two halves stay
coupled; only the lane-to-lane length changes.

```python
from kicad_tools.router.rules import NetClassRouting

mipi_lane_0 = NetClassRouting(
    name="MIPI_CSI_LANE_0",
    intra_pair_clearance=0.075,
    coupled_routing=True,
    diffpair_partner="CSI_DAT0_N",         # within-pair
    length_match_group="MIPI_CSI",         # cross-lane
    length_match_tolerance_mm=0.05,
)

mipi_lane_1 = NetClassRouting(
    name="MIPI_CSI_LANE_1",
    intra_pair_clearance=0.075,
    coupled_routing=True,
    diffpair_partner="CSI_DAT1_N",
    length_match_group="MIPI_CSI",         # same group name as lane 0
    length_match_tolerance_mm=0.05,
)
```

Two separate `NetClassRouting` instances declare `length_match_group="MIPI_CSI"`;
detection merges their members into a single group (see
`match_group_detection.py:318`, `_collect_explicit_match_groups`).

## Worked example: 3-lane MIPI CSI

| Lane | Pair | Routed length | Action |
|---|---|---|---|
| Clock | `CSI_CLK_P/N` | 24.0 mm | reference (longest) |
| Data 0 | `CSI_DAT0_P/N` | 23.5 mm | symmetric +0.5 mm serpentine on both halves |
| Data 1 | `CSI_DAT1_P/N` | 24.0 mm | no perturbation needed |
| Data 2 | `CSI_DAT2_P/N` | 22.9 mm | symmetric +1.1 mm serpentine on both halves |

The post-tune within-pair skew for each lane stays at its pre-tune
value (typically <0.01 mm), while the lane-to-lane skew converges to
within `length_match_tolerance_mm=0.05`.

## Engagement order

The CLI runs `--length-match-diffpairs` **before** `--length-match-groups`
(see `cli/route_cmd.py:5265-5270`). This is intentional: within-pair
serpentines establish the per-pair invariant first, then cross-lane
group tuning operates on pairs whose intra-pair skew is already
within tolerance. Reversing the order would let group tuning corrupt
the pair invariant.

## See also

- [01-declaring-groups.md](01-declaring-groups.md) — the group
  declaration itself (lanes share the group name).
- [04-cascade-safety.md](04-cascade-safety.md) — symmetric serpentine
  insertions count as **two** against the per-member budget (one per
  half), so MIPI groups exhaust the budget faster than single-ended
  groups of the same N.
- [05-protocol-recipes.md](05-protocol-recipes.md) — full
  copy-pasteable MIPI / HDMI blocks.

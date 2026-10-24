# 07 — Protocol Defaults

Set `pair_protocol` on a `NetClassRouting` and leave the limit fields unset.
The router, its pair report and `kct check` then resolve each limit of a pair
the same way, in one place (`resolve_pair_thresholds` in
`src/kicad_tools/validate/diffpair_engagement.py`):

```
class field  >  protocol default  >  generic default (0.5 mm skew)
```

## The table

The protocol defaults are `PROTOCOL_DEFAULTS` in
`src/kicad_tools/router/pair_protocols.py`. Every value carries its source,
or the tag `uncited`.

| `pair_protocol` | Max skew | Uncoupled per terminal | Vias per net | Source |
|---|---|---|---|---|
| `USB2_FS` (12 Mbit/s) | 2.5 mm | 6.35 mm | 4 | Renesas, *Guidelines for Full-Speed USB2.0 Board Design*, Table 2 (skew); TI SPRAAR7J §3.3 and App. A (uncoupled length and vias: high-speed guidance, applied to full speed) |
| `USB2_HS` (480 Mbit/s) | 1.0 mm | 6.35 mm | 4 | Skew: Q16 judgement, rounded down from TI 50 mil / Toradex 1.1 mm; uncoupled length and vias: TI SPRAAR7J App. A and §3.3 |
| `USB3_SS` | 0.127 mm | none | none | 5 mil: TI, Philips/NXP, Renesas; Toradex 0.15 mm |
| `SATA` | 0.127 mm | none | none | as `USB3_SS` |
| `LVDS` | 0.5 mm | none | none | uncited |
| `PCIE_G1` | 0.127 mm | none | none | as `USB3_SS` |
| `PCIE_G2_PLUS` | 0.127 mm | none | none | as `USB3_SS` |
| `MIPI_DPHY` | 0.15 mm | none | none | Toradex <1 ps; AMD Versal ±2 ps incl. package |
| `ETHERNET` | 2.0 mm | none | none | uncited |
| `HDMI_TMDS` | 0.127 mm | none | none | as `USB3_SS` |
| `DDR_DQS_CK` | 0.15 mm | none | none | Q16 judgement: length proxy for ~1 ps (Intel DDR4, NXP LPDDR4) |
| `CUSTOM`, or no protocol | 0.5 mm | none | none | generic default |

The table also carries a gap, a trace width and an impedance per protocol.
None of those has a source yet. `DifferentialPairRules.for_pair_type` and
`DifferentialPairRules.from_protocol` read the same table. The former
`DifferentialPairRules.for_type` shim was removed in Issue #6448.

## Severity follows the source

> **Skew is now a time budget (Issue #7892).** `kct check` judges a pair whose
> protocol it can identify against a derived budget in ps (guide 06), which
> replaces the protocol's millimetre value in the table above. Since #7893 the
> router and every skew tuner tune to that same budget, so the millimetre
> column is no longer a tuning target or a floor of the exported `.kicad_dru`
> skew rule. The severities below apply to the
> millimetre limits that remain: class fields, protocols with no budget, and
> the generic default.

A `diffpair_length_skew` finding is:

- an **error** when the limit is cited (every row but `LVDS`, `ETHERNET`) or explicit
  (`skew_tolerance_mm` set on the class);
- a **warning** when the limit is an `uncited` table value. A number nobody
  has sourced does not block a board;
- an **error** at the generic 0.5 mm default, as it always was.

The router's pair report and `kct check --verbose` print where each limit
came from:

```
USB_D+/USB_D-: continuity threshold 70.0%, skew tolerance 2.500 mm [protocol:USB2_FS (Renesas, ...); error]
```

## Uncoupled length and via count

Since #6443 piece (b) the uncoupled-length allowance and the via count are
checked, by `diffpair_uncoupled_length` (guide 06) in `kct check` and by native
`diff_pair_uncoupled` / `via_count` rules in the routed `.kicad_dru`. The
uncoupled limit is the per-terminal allowance times the number of components on
the net (6.35 mm x 3 = 19.05 mm for board 03's `USB_D+`), unless the class sets
`max_uncoupled_total_mm`.

On `USB2_FS` the per-terminal and via limits come from TI SPRAAR7J, which is
written for a high-speed-capable PHY. Their source strings say "high-speed
guidance, applied to full speed", and a finding quotes the source.

## Stretch target

0.5 mm is reported next to every pair's limit as the figure the skew tuner
aims for. It gates nothing.

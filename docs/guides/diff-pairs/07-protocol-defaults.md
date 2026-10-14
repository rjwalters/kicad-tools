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
| `USB2_FS` (12 Mbit/s) | 2.5 mm | 6.35 mm | 4 | Renesas, *Guidelines for Full-Speed USB2.0 Board Design*, Table 2; TI SPRAAR7J §3.3 and App. A |
| `USB2_HS` (480 Mbit/s) | 1.27 mm | 6.35 mm | 4 | TI SPRAAR7J App. A (50 mil) and §3.3 |
| `USB3_SS` | 0.1 mm | none | none | uncited |
| `LVDS` | 0.5 mm | none | none | uncited |
| `PCIE_G1` | 0.127 mm | none | none | uncited |
| `PCIE_G2_PLUS` | generic | none | none | no value proposed |
| `MIPI_DPHY` | 0.038 mm | none | none | uncited |
| `ETHERNET` | 2.0 mm | none | none | uncited |
| `HDMI_TMDS` | 0.5 mm | none | none | uncited |
| `CUSTOM`, or no protocol | 0.5 mm | none | none | generic default |

The table also carries a gap, a trace width and an impedance per protocol.
None of those has a source yet. `DifferentialPairRules.for_type` is a
deprecated shim over the same table.

## Severity follows the source

A `diffpair_length_skew` finding is:

- an **error** when the limit is cited (the two USB2 rows) or explicit
  (`skew_tolerance_mm` set on the class);
- a **warning** when the limit is an `uncited` table value. A number nobody
  has sourced does not block a board;
- an **error** at the generic 0.5 mm default, as it always was.

The router's pair report and `kct check --verbose` print where each limit
came from:

```
USB_D+/USB_D-: continuity threshold 25.0%, skew tolerance 2.500 mm [protocol:USB2_FS (Renesas, ...); error]
```

## What is not checked yet

The uncoupled-length allowance and the via count are resolved and reported.
The rule that checks them is not built (issue #6443, piece b).

## Stretch target

0.5 mm is reported next to every pair's limit as the figure the skew tuner
aims for. It gates nothing.

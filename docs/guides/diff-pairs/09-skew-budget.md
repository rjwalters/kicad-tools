# 09 — Skew Budgets in Picoseconds

The budget comes from
`src/kicad_tools/physics/skew_budget.py` (Issue #7892, ruling on #7883; the
derivation is in
[`docs/research/diffpair-phase-skew-tolerance.md`](../../research/diffpair-phase-skew-tolerance.md)).

**Skew is checked in time.** Each leg's propagation delay is the sum of its
trimmed track runs times their layer's delay, plus its via barrels times the
via delay. The pair's skew is the difference between the two legs, in ps:

| Copper | Delay (ps/mm) |
|---|---|
| outer layer | `sqrt(eps_eff) / c`, Hammerstad-Jensen microstrip at the class trace width |
| inner layer | `sqrt(eps_r) / c` of the surrounding dielectric (stripline is TEM) |
| via barrel | the slowest inner layer's delay (on a 2-layer board, its densest dielectric), over the same stackup height kicad-cli's length counts |

On the legacy JLCPCB 4-layer stackup F.Cu is about 5.6 ps/mm and the inner
layers 6.9 ps/mm. A pair that is length-matched but runs one leg on an inner
layer therefore carries real skew. 06rf's `USB3_TX1` is 0.015 mm out in
length and 28.8 ps out in time.

**The budget.** `dt_max = dphi_max / (2 pi f_eval)`, with
`dphi_max = 3.62 deg` (-30 dB of mode conversion). `f_eval` is the edge knee
`0.35 / t_r,min` where the protocol publishes an edge floor (USB 2.0 FS/HS,
LVDS), otherwise Nyquist (bit rate / 2). At Nyquist that is UI/50. DDR strobes
and clocks keep the UG583 timing limit of 2 ps instead.

| Protocol (default point = maximum rate) | Rate | Budget |
|---|---|---|
| `USB2_FS` | 12 Mb/s, t_r >= 4 ns | 115.2 ps |
| `USB2_HS` | 480 Mb/s, t_r >= 500 ps | 14.4 ps |
| `USB3_SS` | 5 Gb/s (Gen1; set `data_rate_gbps=10` for Gen2) | 4.0 ps |
| `PCIE_G1` / `PCIE_G2_PLUS` | 2.5 / 8 GT/s | 8.1 / 2.5 ps |
| `MIPI_DPHY` | 2.5 Gb/s | 8.1 ps |
| `HDMI_TMDS` | 6 Gb/s (HDMI 2.0) | 3.4 ps |
| `LVDS` | 400 Mb/s, t_r 0.6 ns | 17.3 ps |
| `DDR_DQS_CK` | timing | 2.0 ps |

**Severity.** A budget at the protocol's maximum rate is a **warning**. Set
`data_rate_gbps` on the net class to derive it at the rate the board actually
runs, which makes it an **error**. A class with a rate but no identifiable
protocol is evaluated at Nyquist. The DDR timing limit is an error when the
protocol is declared (or a rate is) and a warning when it was inferred.

**How the protocol is identified.** The first of these that matches wins:
the class's `pair_protocol`, then the net-class name, then the pair's net
names. Names are split into tokens on non-alphanumerics and matched in a fixed
order: `DQS`/`DDR`, then `MIPI`/`CSI`/`DSI`/`DPHY`, `HDMI`/`TMDS`,
`USB3`/`SSTX`/`SSRX`, `PCIE`, `USB2`/`USB` (taken as high speed), `LVDS`.
Trailing digits are ignored (`LVDS1`). A pair whose protocol and rate give
nothing to derive from (`SATA`, `ETHERNET`, `CUSTOM`, unknown names) keeps the
length check: the table value, or the generic 0.5 mm.

**Length checks that stay.** A derived budget replaces the declared
protocol's millimetre table value. It does not replace:

- a class's own `skew_tolerance_mm`, which is still checked as a length cap
  (error) until #7894 removes those fields from the board generators;
- the generic 0.5 mm, when the protocol was only inferred from a name.

A finding reports the skew in ps, the budget, and the budget in mm at the
slowest layer, and names its source in brackets:

```
Engaged differential pair USB3_TX1+/USB3_TX1- propagation-delay skew 28.81 ps
(length skew 0.015 mm) exceeds budget 4.03 ps = 0.581 mm at the slowest layer
[derived:-30 dB at Nyquist 2.5 GHz (USB3_SS 5 Gb/s, protocol max rate); ...]
-- kct-only: kicad-cli checks length, and this pair's legs run on layers of
different delay
```

```bash
kct check board.kicad_pcb --only diffpair_length_skew
```

Remediate: balance each leg's layer mix, tune the shorter leg, declare the
pair's real `pair_protocol`, or set `data_rate_gbps` to the rate the link
actually runs at (see [guide 04](04-length-matching.md)).

## kicad-cli parity

A black-box probe of pinned kicad-cli 10.0.6 (Issue #7892) found that
`skew (max 4ps)` (and `ns`) parses but enforces nothing on a project without a
KiCad tuning profile; an `mm` rule enforces. The exported `.kicad_dru` rule
is the tightest length form of the pair's limits: the budget at the slowest
layer and any class cap. kicad-cli fails every pair whose legs share a layer
mix and that `kct check` fails. It cannot see layer-dependent delay.

## What the router tunes to (Issue #7893)

The router and every skew tuner (the inline comb, the coupled-candidate
choice, the end-of-recipe and lattice pair tuners, `kct retune-lengths`, the
post-pass pair-skew guard) take their target from the same resolver as
`kct check`: `kicad_tools.router.pair_skew_target.resolve_pair_skew_target`.
The target is the budget in ps (warning or error) **and** the error-level
millimetre caps beside it: a class's own `skew_tolerance_mm` (until #7894)
and, for a protocol only guessed from a name, the generic 0.5 mm. When the
`.kicad_dru` length rule is an error and tighter than those caps, it is a cap
too, so kicad-cli does not fail what the router tuned.

The tuner measures progress in ps. The shorter leg is the one with less
delay, and a bump is sized by its host layer's delay, so a millimetre of
copper on a slow inner layer closes more skew than a millimetre on F.Cu. A
pair matched in length but split across layers (one leg on F.Cu, one on
In1.Cu) is tuned until its delay difference meets the budget, which leaves
the legs different in length on purpose.

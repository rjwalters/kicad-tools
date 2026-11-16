# Detour-ratio fleet measurement (issue #9003)

*Measured 2026-11-16, worktree for issue #9003, `--seed 7`, `--skip-drc`,
`kct route` grid engine (negotiated driver), C++ router backend 1.0.0.*
*Raw per-connection rows: the run's `<output>.detour_ratio.json` sidecars;
the pooled table below was aggregated from them.*

## Method

`kct route --report-detour-ratio` was run over the in-repo fleet boards with
a fixed seed. For every probed connection the report records the drawn copper
length against what the **same search achieves alone on an empty board**
(pads, keepouts and fixed copper only), measured with the shared
`KicadLengthModel` (`copper_order` wired from `route_copper_order`, #7790).
A connection is an offender above `DETOUR_RATIO_THRESHOLD = 1.25`.

## Per-board distribution

| board | probed | matched | no-path | unmatch | median | p90 | p99 | max | offenders | pooled | unattrib. mm |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 00-simple-led | 1 | 1 | 0 | 0 | 0.99 | 0.99 | 0.99 | 0.99 | 0 | 0.993 | 0.0 |
| 01-voltage-divider | 7 | 5 | 0 | 2 | 1.13 | 2.13 | 2.13 | 2.13 | 1 | 1.205 | 0.0 |
| 02-charlieplex-led | 27 | 19 | 0 | 8 | 1.00 | 1.29 | 1.36 | 1.36 | 4 | 1.077 | 160.4 |
| 03-usb-joystick | 46 | 35 | 0 | 11 | 1.00 | 1.36 | 3.71 | 3.71 | 4 | 1.058 | 334.6 |
| 04-stm32-devboard | 11 | 10 | 0 | 1 | 1.05 | 1.16 | 1.22 | 1.22 | 0 | 1.086 | 34.5 |
| 05-bldc-motor-controller | 57 | 42 | 0 | 15 | 1.01 | 1.27 | 1.46 | 1.46 | 7 | 1.101 | 470.6 |
| 06-diffpair-test | 28 | 28 | 0 | 0 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 1.000 | 0.0 |
| synthetic-mains-complete | 7 | 7 | 0 | 0 | 1.00 | 1.12 | 1.12 | 1.12 | 0 | 1.017 | 0.0 |

`unmatch` rows are probe connections the settled route did not reproduce
(partially-routed nets, e.g. board 05's BLOCKED_PATH CPH net); `unattrib.`
is settled copper no probe connection claims (escape-stub hops and similar
infrastructure fragments, e.g. board 03's 0.8 mm hop routes). Neither enters
any ratio.

## Pooled

- n = 147 measured connections; median **1.001**, p90 **1.252**, p99
  **2.128**, max **3.707**.
- Offenders (> 1.25x): **16 / 147 = 10.9%**.
- Ratio histogram: 104 at 1.00-1.05, 27 at 1.05-1.25, 14 at 1.25-2.00,
  2 at >= 2.00.
- Upper-bound avoidable copper if every offender were repaired to its
  alone-cost: **91.7 mm** across the fleet.

Worst rows:

| board | net | drawn mm | alone mm | ratio |
|---|---|---:|---:|---:|
| 03-usb-joystick | HWB | 27.1 | 7.3 | 3.71 |
| 01-voltage-divider | VOUT | 14.1 | 6.6 | 2.13 |
| 03-usb-joystick | BTN1 | 14.8 | 8.5 | 1.74 |
| 03-usb-joystick | USB_CC2 | 23.6 | 14.8 | 1.59 |
| 05-bldc-motor-controller | RESET | 24.5 | 16.8 | 1.46 |
| 05-bldc-motor-controller | CPL | 10.9 | 7.6 | 1.43 |
| 05-bldc-motor-controller | PWM_C | 28.0 | 20.4 | 1.37 |
| 03-usb-joystick | USB_D- | 7.7 | 5.6 | 1.36 |
| 02-charlieplex-led | RESET | 5.4 | 4.0 | 1.36 |
| 02-charlieplex-led | NODE_C | 13.8 | 10.8 | 1.29 |

Offenders concentrate in four boards (01: 1, 02: 4, 03: 4, 05: 7); boards
00/04/06/mains are clean (0 offenders, pooled <= 1.086x).

## Non-perturbation evidence

- Board 01 and board 03 were each routed twice with `--seed 7`, once with
  and once without `--report-detour-ratio`; the written `.kicad_pcb` files
  are **byte-identical** (`cmp`). Board 03 exercises the negotiated driver
  with rip-up activity (its log shows iteration rip-up/re-route rounds).
- The same identity is pinned as a test:
  `tests/test_detour_ratio_report_9003.py` (synthetic board, both the CLI
  negotiated path and the plain `route_all` path, plus a targeted-rip-up
  variant; journal length and search-failure stores asserted equal).

## Boards not measured

- `boards/08-precision-acquisition` has no `.kicad_pcb` (schematic-stage
  only).
- `boards/07-matchgroup-test` and `boards/09-usbc-pd-power` exceeded the
  measurement's wall-clock caps with the probe enabled (900 s / 420 s) --
  the probe roughly doubles search work on these already-heavy boards, and
  both are opt-in-flag territory. They are the known blind spots of this
  measurement.

## Verdict: fat tail -- the repair pass is warranted

The tail is **fat** by the issue's own criterion ("a non-trivial set of
connections at multiples of their alone-cost"): 10.9% of connections sit
above 1.25x, the extreme reaches 3.7x, and offenders concentrate in exactly
the dense boards where an occupied short corridor is the plausible cause
(03, 05) rather than scattering randomly. The bulk of the distribution is
at cost (71% of connections at <= 1.05x), so a *targeted* repair pass -- not
a global re-negotiation -- is the right shape, which is what the issue
already specified.

Follow-up: the bounded post-settle repair pass (identify blockers, lift,
reroute the offender, restore, keep only if `RouteScore.is_better_than`
accepts) is filed separately at #9013 (`loom:triage`), citing this note as
its baseline.

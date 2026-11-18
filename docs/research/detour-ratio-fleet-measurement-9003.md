# Detour-ratio fleet measurement (issue #9003)

*Measured 2026-11-16, worktree for issue #9003, `--seed 7`, `--skip-drc`,
`kct route` grid engine (negotiated driver), C++ router backend 1.0.0.*
*Raw per-connection rows: the run's `<output>.detour_ratio.json` sidecars;
the pooled table below was aggregated from them.*

*Measured at PR #9014 head `c8e4feca6`, before review fixes to the probe's
state restore (`routing_failures` and other leaked state) and its
reporting of partly failed multi-terminal nets. Those fixes change only
what a probe failure leaves behind and how it is reported. No measured
board had a probe-no-path row, but the old code dropped partial failures
silently, so that zero is a lower bound. The numbers were not re-measured
after the fix.*

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
- Scope of that claim: the probe restores router state, and since #9244 its
  wall-clock cost is excluded from the routing budgets too (stage
  `--timeout`, the CLI routing/wall deadlines, installed excursion
  deadlines), so a run a time limit cuts short no longer differs with the
  flag on. Residual: the hard supervisor cap
  (`KCT_ROUTE_INVOCATION_DEADLINE`) still bounds the invocation as a whole.
  The `cmp` runs above finished inside their limits.
- Review of PR #9014 found that the probe at `c8e4feca6` leaked its own
  failure records (`routing_failures`), the failure-analysis cache and
  budget, the Python-fallback memo and stage budget, and lazily built grid
  state into the live run. A board where no probe search fails never
  exercises the failure-path leaks, which is consistent with the `cmp`
  checks passing on 01 and 03. The probe now snapshots and
  restores every attribute of the router, pathfinder, grid and their
  helper objects (`router/probe_state.py`), except the grid's
  occupancy-generation counter, which only moves forward. A router-level test
  (`test_unroutable_net_failure_records_identical`) pins identical failure
  records and copper for a net that cannot route on the empty board.
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

## Verdict: a borderline fat tail -- a targeted repair pass is worth trying

By the issue's criterion ("a non-trivial set of connections at multiples
of their alone-cost") the tail is **fat, but only just**:

- 16 of 147 measured connections (10.9%) sit above 1.25x, and they
  concentrate in the dense boards (03, 05), where an occupied short
  corridor is the plausible cause.
- Only **2 of 147** are at 2x or more (3.7x and 2.1x). Most offenders sit
  between 1.25x and 1.5x.
- If every offender were repaired to its alone-cost, at most **91.7 mm**
  of copper would be saved across the whole fleet. That is an upper bound.
- The bulk of the distribution is at cost: 71% of connections are at or
  below 1.05x.

**Blind spot.** 37 of the 184 probed connections (20%) are `unmatched`:
the settled route did not reproduce them, so they carry no ratio. Tap-in
and Steiner reroutes, which are where avoidable detours could plausibly
hide, would land in that bucket, so the measured tail may understate the
real one. The unmatched rows could equally hold mostly at-cost
connections, so the 10.9% offender rate could move in either direction.
Boards 07 and 09 were not measured at all (below).

So the evidence supports *trying* a targeted repair pass, not a global
re-negotiation, which is the shape the issue already specified. It does
not show that the pass will recover much copper. #9013's planner should
weigh the small upper bound against the cost, and should shrink the
`unmatched` blind spot before relying on these numbers as a baseline.

Follow-up: the bounded post-settle repair pass (identify blockers, lift,
reroute the offender, restore, keep only if `RouteScore.is_better_than`
accepts) is filed separately at #9013 (`loom:triage`), citing this note as
its baseline.

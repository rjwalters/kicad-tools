# Does KRT-style sequential N+1 rip-up help `kct route`? (measured: no — and the rip-up never fired)

Issue #5894 step 3 (Epic #5784, Phase 3). Companion to
[`kct-route-phase-profile.md`](kct-route-phase-profile.md) (step 1, where the
time goes), [`kct-route-net-order-experiment.md`](kct-route-net-order-experiment.md)
(step 2, net ordering — also a "do not adopt as default" verdict), and
[`kicad-routing-tools-comparison.md`](kicad-routing-tools-comparison.md) (the
KRT baseline this epic is chasing).

**Verdict: `--ripup-strategy sequential-n1` is NOT proposed as a default.** It
ships as an opt-in prototype. Against the `negotiated` default on the five
named boards it wins only on board 01, is exactly inert on 00 and 06a, and
regresses both target boards — board 02 from 35/36 to 31/36 connections (and
`kct route` *fails* there, see "The board-02 pour-oracle regression"), board 03
from 103/115 to 69/115 while taking 2.3x longer in the routing loop.

**The more useful finding is that the N+1 rip-up escalation never fired at
all.** On every board the rip-up pass and its own no-rip-up baseline produced
*identical* `(connections, vias)`, and the per-pass counters say why: 0
escalations on 00/01/02/06a (every net in the strategy's universe routed on
its first attempt with zero failed RSMT edges), and on board 03 the two nets
that did fail found **no eligible blocker** — the relaxed frontier search could
not reach their goal even with every routed net's copper unblocked, so there
was nothing for N+1 rip-up to rip. So this experiment measures *sequential
one-pass routing vs. whole-set negotiation*, which negotiation wins. It does
**not** measure N+1 rip-up's value, which remains unanswered (see "Not
measured").

Tool: `scripts/research/ripup_experiment.py`. Implementation under test:
`src/kicad_tools/router/sequential_ripup.py`.

## What was measured

Step 3 as filed asks for a flag-gated KRT-style prototype with four named
pieces, compared against the negotiated default on routing-loop time,
completion and vias. The prototype implements all four:

| Piece | How |
|---|---|
| Frontier-cell blocker attribution | Reuses `NegotiatedRouter.find_blocking_nets_relaxed` (#2274): re-runs the failing net's **own** search with routed copper unblocked, and scores the committed nets sitting on the resulting path. Not a Bresenham straight-line guess (`find_blocking_nets`, the other helper in `negotiated.py`). |
| N+1 escalation with a slot-0 guard | Rip the top blocker, retry; on failure escalate to the top 2, then 3 (`max_escalation`). Only nets this pass has **placed copper for** are eligible — ripping an unplaced net is a no-op that would burn an escalation slot, and the guard bounds how much of the board one failing net can touch. |
| Soft cost on the ripped corridor | Each displaced sibling is rerouted at `present_cost_factor=5.0` instead of the attempt's `1000.0`, so it can reclaim the corridor it just vacated at a premium rather than being forbidden it. |
| Improvement gate that reverts a worse run | After the whole pass, a second cheap **no-rip-up** pass runs and `(connections, vias)` are compared lexicographically; the rip-up pass is kept only if it is at least as good. |

Each escalation attempt is a transaction: it commits only if the failed net
re-routes with zero failed edges **and** every displaced sibling comes back
with at least as many routes as it had, else the whole attempt is rolled back
to the exact pre-attempt `Route` objects (mirroring `targeted_ripup`'s
`_rollback_to_snapshot`).

## Three plumbing/measurement bugs found first

As in step 2, the first fleet run was a broken experiment rather than a null
result. All three are fixed under #5894; the harness now reports each one.

1. **The flag never reached the router on any escape-routed board.** Dispatch
   was wired into `Autorouter.route_all_negotiated` only. `kct route` sends
   every dense / 4-layer board through `route_with_escape` →
   `route_all_two_phase` → `TwoPhaseRouter._detailed_negotiated`, which is a
   **separate** detailed-routing loop that never calls `route_all_negotiated`.
   Board 03's treatment arm logged no prototype banner and produced the same
   251 A\* calls as the control. This is the #5908 shape — the same bug step 2's
   `--order-method` hit twice — so `route_all_two_phase` now carries its own
   dispatch, and `tests/test_router_sequential_ripup.py` pins **both** sites.
2. **The per-net success test was a route count, and it over-counted.** The
   obvious `len(routes) >= len(pads) - 1` proxy is wrong because
   `_route_net_negotiated` prepends intra-IC and block-internal routes to its
   result before any inter-pad A\* runs, so a net with failed edges clears the
   bar. On board 02 it declared **all 10 nets successful** while the graded
   output was 31/36 connections — meaning the escalation could never engage.
   `Autorouter._route_net_negotiated` now accepts an optional
   `failure_callback` (#2425; `None`, i.e. every pre-existing caller, is
   byte-identical) and the prototype's success test is "zero failed RSMT
   edges".
3. **Most of board 03 was excluded from the strategy's net universe.**
   `generate_escape_routes` appends dense-package escape stubs to
   `router.routes` with `is_escape` left `False` — only the #1603 *sub-grid*
   stubs set that flag — so the "any non-`is_escape` route means pre-routed"
   filter (correct on the negotiated entry point, where the escape pre-phase
   has not run) threw away 18 of board 03's 24 routable nets: the log read
   `Nets to route: 6` against the CLI's `Nets to route: 24`. That alone
   accounted for board 03's collapse to 4/27 nets. Nets whose pads appear in
   `_escape_pad_overrides` are no longer treated as pre-routed (they hold a
   stub, not a finished net — `route_with_escape`'s own contract); nets claimed
   by the #5786 pose-coupled trunk pre-pass still are.

Bug 1 is the one that would have silently produced a "no difference"
conclusion. Bugs 2 and 3 are the ones that would have produced a *falsely
negative* one. The harness guards bug 1 per run (`strategy_applied`, asserted
from the banner) and surfaces 2 and 3 through the per-pass counters.

## Measurement conditions (read before trusting any number)

- Commit `23b32d1b` + this branch, C++ backend built and active (`kct
  build-native --check`: build version 46), Linux `7.0.0-1010-aws`, **8 logical
  CPUs**. Step 1 ran on a 28-CPU host and step 2 on an 18-CPU macOS host:
  **absolute seconds are not comparable across the three documents.**
- Load average during the runs ranged **1.0 → 8.4** on 8 CPUs; other sweep
  builders were active throughout. Per-row `loadavg_end` is in the JSON.
- `--seed 42` in both arms of every pair, so the only difference is the rip-up
  strategy. The `negotiated` rows are therefore not byte-identical to
  `krt_compare.py`'s seedless kct rows.
- Board keys and recipes are copied from `scripts/research/krt_compare.py` (and
  step 2's `net_order_experiment.py`) — `06a` = `--preserve-existing` — and
  grading uses that script's own referee functions, `measure_completion` /
  `measure_copper` from `kicad_tools.benchmark.external.metrics`, on the routed
  artifact. `krt_compare.py` itself was not invoked: it requires `--krt-dir`,
  an out-of-tree KRT checkout, even for `--tools kct`, and a kct-vs-kct A/B
  needs no KRT present. No DRC referee, for the same reason (no cross-tool
  goalpost to normalise) plus step 1's finding that the `kicad-cli` DRC pass is
  the most load-sensitive phase.
- One run per (board, variant) for the quality metrics. They are deterministic
  given the seed; the timing columns are not — see "Timing".
- The `negotiated` rows are from the full-fleet run; the `sequential-n1` rows
  are from the re-run after bug fixes 2 and 3. The default path is untouched by
  those fixes (`failure_callback=None` is byte-identical; the net-universe
  filter lives only in the new module), so the arms are still like-for-like on
  quality. They are *not* contemporaneous, which matters only for the timing
  columns, which were already unusable.

## Results

`negotiated` = documented recipe, no flag. `sequential-n1` = same plus
`--ripup-strategy sequential-n1`. `exit` is `kct route`'s exit code.

| Board | Variant | exit | Nets | Connections | Vias | Wirelength mm | Segments | A\* calls | Routing-loop s | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|
| 00 | negotiated | 0 | 3/3 | 3/3 | 0 | 8.71 | 5 | 2 | 0.07 | 47.7 |
| 00 | sequential-n1 | 0 | 3/3 | 3/3 | 0 | 8.71 | 5 | 4 | 0.12 | 54.1 |
| 01 | negotiated | 0 | 3/3 | 5/5 | 5 | 61.35 | 68 | 72 | 2.79 | 12.5 |
| 01 | sequential-n1 | 0 | 3/3 | 5/5 | **2** | **54.26** | **22** | **20** | **0.53** | 11.3 |
| 02 | negotiated | 0 | 11/12 | 35/36 | 39 | 407.41 | 254 | 186 | 43.73 | 120.1 |
| 02 | sequential-n1 | **3** | 10/12 | **31/36** | 21 | 390.96 | 326 | 108 | 23.45 | 155.4 |
| 03 | negotiated | 3 | 26/27 | 103/115 | 86 | 755.30 | 501 | 251 | 76.97 | 443.9 |
| 03 | sequential-n1 | **4** | 22/27 | **69/115** | 38 | 598.00 | 242 | 412 | **180.33** | 529.2 |
| 06a | negotiated | 3 | 18/18 | 90/90 | 55 | 489.82 | 186 | 56 | 8.42 | 52.8 |
| 06a | sequential-n1 | 3 | 18/18 | 90/90 | 55 | 489.82 | 186 | 48 | 5.79 | 57.9 |

Reading the two bold regressions: a lower via count on 02 and 03 is **not** a
win, because it comes with fewer connections — the prototype places less
copper, not better copper. Board 03's wirelength drops 21% and its segment
count more than halves for the same reason.

Boards 00 and 01 are the no-regression set step 2 measured as exactly inert for
crossing order. Here 00 is likewise byte-identical in copper, 06a is
byte-identical in copper (identical vias, wirelength and segment count on a
different A\* call count), and **01 is a genuine improvement**: same 5/5
connections with 2 vias instead of 5, 12% less wirelength, and 22 segments
instead of 68 — the same board step 2's crossing order also improved, which
suggests board 01's default result is simply loose rather than that either
technique generalises.

### The rip-up machinery never engaged

| Board | Nets attempted | Routed directly | Unrouted | Escalations | Committed rip-ups | "No eligible blocker" | Rip-up pass | No-rip-up baseline |
|---|---|---|---|---|---|---|---|---|
| 00 | 1 | 1 | 0 | 0 | 0 | 0 | 1 conn, 0 vias | 1 conn, 0 vias |
| 01 | 3 | 3 | 0 | 0 | 0 | 0 | 5 conn, 2 vias | 5 conn, 2 vias |
| 02 | 10 | 10 | 0 | 0 | 0 | 0 | 27 conn, 21 vias | 27 conn, 21 vias |
| 03 | 24 | 22 | 2 | 0 | 0 | 2 | 40 conn, 38 vias | 40 conn, 38 vias |
| 06a | 8 | 8 | 0 | 0 | 0 | 0 | 12 conn, 1 via | 12 conn, 1 via |

Every pass tied with its own baseline, so the improvement gate kept the rip-up
arm on all five boards and never had to revert. Two distinct reasons:

- **00/01/02/06a: nothing ever failed.** Every net in the strategy's universe
  routed on its first hard-cost attempt with zero failed RSMT edges, so the
  escalation was never reached. Note this is true on board 02 *even though* the
  graded result is 31/36 connections — the 5 missing connections are on the
  pour nets (`GND`, `VCC`), which `--auto-skip` removes from the routing
  universe entirely and the post-route oracle is responsible for.
- **03: the blockers were not other nets.** Both failing nets reached the
  attribution step and got an **empty** eligible-target list, with 22 nets
  already committed. That is the relaxed search reporting that it cannot reach
  the goal even with every routed net's copper unblocked — a geometric or
  topological blocker (pad access, layer availability, clearance), not
  congestion. N+1 rip-up has no purchase on that failure mode by construction;
  it is what the existing `escape_local_minimum` / relief-rescue machinery in
  `negotiated.py` exists for.

The counters are logged per pass (`[rip-up] …` / `[no-rip-up baseline] …`)
precisely so this distinction is readable from a log instead of inferred from a
metrics tie: "the two passes tied" means something completely different when
`escalations == 0` than when `escalations > 0, committed == 0`.

Cost of the gate: it doubles the per-net routing work, and on this fleet bought
nothing. `enable_improvement_gate=False` (module-level, not a CLI flag) skips
the baseline pass; the pass-level seconds in the table above
(e.g. board 03: 25.7 s rip-up + 23.0 s baseline) show roughly what that saves.

### The board-02 pour-oracle regression

Board 02's `sequential-n1` arm exits **3** — `kct route` *fails*, where the
default succeeds — and the cause is downstream of the routing loop:

```
negotiated:     Oracle round 1: 3 unconnected link(s) on GND, VCC
                3 -> 0 unconnected pour link(s) (converged)
sequential-n1:  Oracle round 1: 5 unconnected link(s) on GND, VCC
                5 -> 5 unconnected pour link(s) (drc_regressed)
                ROUTING FAILED: pour pads left unconnected
```

The prototype's signal copper strands 5 pour pads instead of 3, and the #5785
KiCad-oracle completion loop cannot close any of them without regressing the
DRC count, so it reverts and the run fails. The signal nets themselves all
report success. `scripts/research/ripup_experiment.py` carries a
`sequential-n1-allow-stranded-pour` variant (adds `--allow-stranded-pour-pads`)
that demotes this to advisory: with it, board 02 exits 0 with **identical**
copper (10/12, 31/36, 21 vias, 390.96 mm). So the exit code is the oracle's
verdict on the prototype's copper, not a second independent defect — but it is
a real quality regression, not an artifact: a routing strategy that leaves more
pour pads stranded is worse even when the signal nets are fine.

## Timing

Routing-loop seconds are reported separately from wall seconds (an explicit
acceptance criterion), because per step 1 the loop is only ~36% of wall on
board 03 and ~63% on board 02 — the rest is zone fill, trace optimiser, DRC and
the access-witness replay, none of which any rip-up strategy touches. Board 02
makes the point: the two columns move in **opposite** directions (loop
43.7 s → 23.5 s, wall 120.1 s → 155.4 s), because the wall figure there is
dominated by the pour oracle's extra rounds and host load, not by routing.
Conflating them would have read board 02 as a wall-time regression and hidden
the loop halving — or, with the arms swapped, read the loop halving as a win.

As in step 2, **the timing columns on this host are not trustworthy at the
few-tens-of-percent level** — load averaged 1.0–8.4 on 8 CPUs with other sweep
builders active. Two differences are large enough to survive that noise and are
corroborated by the load-independent A\* call counts:

- Board 01: 2.79 s → 0.53 s loop, with A\* calls 72 → 20. Real.
- Board 03: 76.97 s → 180.33 s loop, with A\* calls 251 → 412. Real, and in the
  wrong direction: the sequential strategy with the gate's second pass does
  **more** search work than the negotiated loop's three iterations.

Board 02's 43.7 s → 23.5 s (A\* calls 186 → 108) is also plausibly real, but it
is the signature of doing less work for a worse result, not of a faster router.

## Not measured

- **N+1 rip-up's actual value.** This is the single biggest gap. The mechanism
  never fired on any of the five boards, so the step-3 question as filed is
  *unanswered*, not answered negatively. Answering it needs a board where a net
  fails for congestion reasons *and* the frontier attribution finds committed
  blockers — none of 00/01/02/03/06a is one under this strategy. Candidates:
  boards 04/05/07 (outside #5894's named set), or running the escalation
  *inside* the negotiated loop (where nets do fail against committed copper)
  rather than as an alternative outer loop.
- **A quiet-host re-run.** Same gap step 2 recorded, same host class of
  problem, and the same conclusion: trust A\* call counts and copper metrics,
  not seconds.
- **Global corridors in the treatment arm.** On the two-phase path the
  prototype replaces Phase 1 (global corridor assignment) as well as Phase 2,
  because a KRT-style sequential router has no notion of a reserved corridor.
  Boards 03 and 06a therefore compare "sequential, no corridors" against
  "negotiated, with corridors". The confound is deliberate — threading the
  strategy into `TwoPhaseRouter` as another callable hook would measure a
  hybrid neither tool actually runs — but it means board 03's regression cannot
  be attributed to the rip-up strategy alone.
- **Post-routing parity.** The prototype returns straight out of
  `route_all_negotiated` / `route_all_two_phase` and so skips those methods'
  post-loop work (seg-seg clearance correction, the demotion safety nets,
  `checkpoint_callback`). Some of board 02's extra stranded pour pads may come
  from that rather than from the routing itself; not separated.
- **Match-group / byte-lane / matrix-topology pre-processing.** The prototype's
  net order applies only the priority sort, pour/net-0 filtering, pre-routed
  skipping and match-group interleaving. Fine for this fleet, which exercises
  none of the others; not validated beyond it.
- **Boards 04, 05, 07** — outside #5894's named set.
- **DRC outcomes** — see conditions.

## Reproduce

```bash
uv run kct build-native --check   # must report "available"; see CLAUDE.md
uv run python scripts/research/ripup_experiment.py \
    --boards 00,01,02,03,06a --variants negotiated,sequential-n1 \
    --work-dir /tmp/ripup-experiment --json /tmp/ripup-experiment/summary.json \
    --timeout 3600
```

Add `sequential-n1-allow-stranded-pour` to `--variants` to reproduce the
board-02 exit-code finding. On a loaded host, expect the timing columns to vary
by several-fold between runs and the quality columns not to vary at all.

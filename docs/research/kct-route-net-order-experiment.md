# Does crossing-aware net ordering help `kct route`? (measured: no)

Issue #5787 step 2 (Epic #5784, Phase 3). Companion to
[`kct-route-phase-profile.md`](kct-route-phase-profile.md) (step 1, where the
time goes) and
[`kicad-routing-tools-comparison.md`](kicad-routing-tools-comparison.md) (the
KRT baseline this epic is chasing).

**Verdict: `--order-method crossing` is NOT proposed as a default.** It ships as
an opt-in flag. On the two target boards it leaves completion unchanged and
makes vias *worse* (02: 37 → 38; 03: 46 → 52 — but see the T-touch caveat
under "Results": 03's row predates a geometry fix that changes 03's order, so
it is stale and unmeasured rather than favourable). It helps board 01
(vias 5 → 3, wirelength −6%, segments 68 → 26) and is exactly inert on 00 and
06a. Runtime could not be measured on this host at all — see "Timing is
unusable here".

Tool: `scripts/research/net_order_experiment.py` (`order-check` and `route`
sub-commands). Implementation under test:
`src/kicad_tools/router/crossing_order.py`.

## What was measured

Step 2 as filed asks whether "turning on our existing inversion counters
(`monotone_certificate`, `bundle_river`) as the default net order" moves
completion, vias or runtime. `order-check` answers the prior question first:

| Board | Routable nets | Byte-lane flags change order? | Crossing order changes it? | Nets displaced |
|---|---|---|---|---|
| 00 | 1 | False | False | 0 |
| 01 | 0 | False | False | 0 |
| 02 | 10 | False | True | 2 |
| 03 | 24 | False | True | 20 |
| 06a | 16 | False | True | 11 |

`--monotone-certificate-order` and `--bundle-river-planner` are **structural
no-ops on all five boards**, not weak orderings. Both act only through
`Autorouter._apply_byte_lane_inner_priority`, which requires a detected
match group of ≥5 members projecting onto one co-located component row
(board 07's mirrored DDR byte). No board here has one, so there is nothing to
turn on as a default. (`tests/test_router_crossing_order.py` pins that
premise: `test_byte_lane_priority_is_identity_without_a_qualifying_group`.)

So step 2 was answered with a **generalisation** of the same idea instead:
`crossing_order.py` reduces each net to a Euclidean-MST flight-line skeleton,
counts how many *other* nets' skeletons it properly intersects, and orders
most-contended-first **within** each net-class priority band. No boundary,
match-group or pin-count precondition; no evaluation route (unlike the four
pre-existing `--order-method` heuristics, which spend a throw-away full route
inside `RoutingOptimizer.optimize_net_order`).

## Two plumbing bugs found first — the flag was a silent no-op

The first A/B pass produced *byte-identical* copper in both arms on every
board. That was not a null result, it was a broken experiment, twice over:

1. **`--order-method` never reached the router on the default recipe.**
   `_apply_order_method` is called from exactly one place: the single-attempt
   tail of `route_cmd._run_main_impl`. `kct route` defaults to
   `--auto-layers` (inner parser default `True`), so `_run_main_impl` returns
   into `route_with_layer_escalation` ~330 lines *before* that tail. Every
   `--order-method` value was discarded for `kct route IN -o OUT`. Same bug
   class as #3952 (`--differential-pairs` silently no-op on the
   escape-routing / auto-layers path).
2. **`_forced_net_order` was not honoured on the two-phase path.** Once (1)
   was fixed, boards 03 and 06a *still* produced identical copper and an
   identical A\* call count. `TwoPhaseRouter` never sees the `Autorouter`, and
   `routing_plan.select_plan_nets` — the shared net-universe selector (#5520)
   — sorted by `_get_net_priority` with no `_forced_net_order` branch. So the
   ordering was discarded again on every board that routes two-phase, i.e.
   every 4-layer board.

Both are fixed under #5787, scoped to the `crossing` method: the four
pre-existing methods (`greedy`, `critical_first`, `congestion`, `hybrid`)
remain no-ops on the escalation paths, because `optimize_net_order` evaluates
its candidate with a throw-away full route and needs a fresh-router factory the
attempt loop does not build — running it against the live router would pollute
the attempt. Tracked separately in #5908.

`net_order_experiment.py` now asserts per run that the treatment arm logged a
`Net order: --order-method` line, and flags the row with a `warning` when it
did not. An arm that silently discarded its treatment is worse than no
measurement.

## Measurement conditions (read before trusting any number)

- Commit `5ab1cbd0e` + this branch, C++ backend built and active
  (`build version 45`), macOS, **18 logical CPUs** (step 1 ran on a 28-CPU
  host — absolute seconds are not comparable between the two documents).
- Load average during the runs ranged **5 → 39** on 18 CPUs; other sweep
  builders were active throughout. One board/variant pair hit a 1800 s cap and
  had to be re-run.
- Board recipes and keys are copied from `scripts/research/krt_compare.py`
  (`06a` = `--preserve-existing`), and grading uses that script's own referee
  functions — `measure_completion` / `measure_copper` from
  `kicad_tools.benchmark.external.metrics` — on the routed artifacts, so a row
  here reads against a row there. `krt_compare.py` itself was not invoked: it
  requires `--krt-dir`, an out-of-tree KRT checkout, even for `--tools kct`,
  and a kct-vs-kct A/B needs no KRT present. No DRC referee here, for the same
  reason (no cross-tool goalpost to normalise) plus step 1's finding that the
  `kicad-cli` DRC pass is the most load-sensitive phase on this host.
- `--seed 42` in **both** arms of every pair, so the only difference is the
  net order. This means the `default` rows are not byte-identical to
  `krt_compare.py`'s seedless kct rows.
- One run per (board, variant) for the quality metrics. They are deterministic
  given the seed; the timing columns are not (below).

## Results

`default` = documented recipe, no ordering flag. `crossing` = same plus
`--order-method crossing`. Boards 00/01/02 from run 2, 03/06a from run 4
(re-run after plumbing fix 2).

| Board | Variant | Order applied | Nets | Connections | Vias | Wirelength mm | Segments | Negotiated iters | A\* calls | Overflow trajectory |
|---|---|---|---|---|---|---|---|---|---|---|
| 00 | default | — | 3/3 | 3/3 | 0 | 8.71 | 5 | 1 | 2 | [0] |
| 00 | crossing | yes | 3/3 | 3/3 | 0 | 8.71 | 5 | 1 | 2 | [0] |
| 01 | default | — | 3/3 | 5/5 | 5 | 61.35 | 68 | 5 | 72 | [2, 2, 2, 2] |
| 01 | crossing | yes | 3/3 | 5/5 | **3** | **57.56** | **26** | 5 | 72 | [2, 2, 2, 2] |
| 02 | default | — | 10/12 | 33/36 | 37 | 401.39 | 250 | 4 | 186 | [21, 16, 27, 20] |
| 02 | crossing | yes | 10/12 | 33/36 | **38** | **405.43** | **464** | 4 | 194 | [22, 17, 27, 20] |
| 03 | default | — | 24/27 | 75/115 | 46 | 668.95 | 288 | 3 | 251 | [34, 34] |
| 03 | crossing | yes | 24/27 | 75/115 | **52** | **670.58** | **428** | 3 | 251 | [34, 34] |
| 06a | default | — | 18/18 | 90/90 | 55 | 489.82 | 187 | 3 | 56 | [4, 4] |
| 06a | crossing | yes | 18/18 | 90/90 | 55 | 489.82 | 187 | 3 | 56 | [4, 4] |

Reading it against #5787's acceptance criteria ("improves at least one of
completion, vias or runtime on 02/03 with no regression on 00, 01 or 06"):

- **Completion: unchanged on every board.** Not one net or connection moved,
  in either direction, on any of the five. The 02/03 completion gap against
  KRT is not an ordering artifact.
- **Vias on the target boards: worse.** 02: 37 → 38. 03: 46 → 52 (+13%).
  Segment count rises sharply on both (250 → 464, 288 → 428) at roughly
  constant wirelength, i.e. the same copper in more, shorter pieces.
- **No-regression set: no regressions, one improvement.** 00 and 06a are
  exactly inert. 01 improves on all three copper metrics (vias 5 → 3,
  wirelength −3.8 mm, segments 68 → 26) with identical completion.
- **Runtime: no conclusion possible** (below).

So the criterion is not met: the only board that improves is in the
no-regression set, and both target boards get slightly worse vias. Adopting
this as a default is not supported by the measurement.

Two further observations worth keeping:

- On board 03 the A\* call count is **identical** (251) while the output
  differs. The order changes *which* paths the same number of searches find,
  not how much searching happens. Consistent with step 1's finding that the
  negotiated loop re-routes most nets every iteration regardless.
- Board 06a is a 100%-complete route (90/90 connections). With nothing
  contending, 11 displaced nets still produce byte-identical copper — ordering
  only matters where corridors are actually contested.

### Caveat: the table predates the `segments_cross` T-touch fix

Review of this PR found `segments_cross` asymmetric at a **T-touch** (one
segment's endpoint lying exactly on the other's interior): the zero-guard
covered only two of the four orientation determinants, so the same pair of
skeletons answered `True` or `False` depending on which net held the lower id.
Grid-aligned pad centres make that configuration common, so it was not
measure-zero. It is fixed, and the fix *lowers* some crossing degrees — a
T-touch is now consistently not a crossing:

| Board | Nets whose degree changed | Resulting `crossing` order |
|---|---|---|
| 02 | 4 of 12 (e.g. 6→2, 5→2, 2→0) | **unchanged** |
| 03 | 10 of 27 (e.g. 50→47, 33→26, 17→15) | **changed** |

So the **02 rows above still describe the current code**, but the **03
`crossing` row (vias 46 → 52) was measured with the pre-fix predicate and has
not been re-measured** — the order the fixed predicate produces on 03 is
different, so that row's copper metrics are not guaranteed to reproduce. It was
not re-run here because the host had no C++20 compiler available, and the
pure-Python A\* makes a 4-layer re-route impractical (see CLAUDE.md).

This does not change the verdict. The criterion needed an *improvement* on 02
or 03; 02 is unchanged and still slightly worse, 03 is now unmeasured rather
than favourable, and the recommendation was already "not a default". But a
re-run of the 03 `crossing` arm on a host with the native backend is the
honest way to restore that row — tracked as part of the quiet-host re-run
below, which 03 needs anyway.

## Timing is unusable here

The same configuration, re-run, differed by up to 3.4x:

| Board | Variant | Routing-loop s, run A | Routing-loop s, run B |
|---|---|---|---|
| 02 | default | 154.49 | 61.87 |
| 03 | default | 89.28 | 303.86 |
| 03 | crossing | 308.46 | 87.60 |

The 03 rows are anti-correlated between runs — a textbook contention
signature, not an effect of the treatment. (Run A's 02 and 03 `crossing` rows
also predate a plumbing fix, so only the `default` repeats are strictly
like-for-like; those alone span 2.5x and 3.4x.) The step-1 profile already
established that routing-loop time is 63% (02) and 36% (03) of wall, and that
the largest post-routing phases are order-independent; nothing here refines
that. **A quiet-host re-run is required before any runtime claim.** Read the
load-independent columns — A\* calls, iteration count, overflow trajectory,
and the copper metrics — which is what the harness now prints first.

## Not measured

- Quiet-host timings. This is the single biggest gap; the runtime half of
  step 2 is unanswered, not answered negatively.
- Board 03's `crossing` arm **after** the `segments_cross` T-touch fix — the
  fix changes 03's net order, so that row is stale (see the caveat above).
- `greedy` / `critical_first` / `congestion` / `hybrid` as defaults: still
  no-ops on the escalation paths (#5908), so they were not benchmarked.
- Boards 04, 05, 07: outside #5787's named set.
- DRC outcomes (see conditions).
- Whether a *different* contention prior (actual congestion map, corridor
  overlap from the global plan, KRT-style frontier blocking) would do better
  than flight-line crossings. The flight-line skeleton is a prior, not a
  proof: real routes are not straight, and a 4-layer board resolves a crossing
  with a via rather than a detour — which is plausibly why board 03's via
  count rose.
- Step 3 (N+1 rip-up) — tracked in #5894.

## Reproduce

```bash
uv run kct build-native --check   # must report "available"; see CLAUDE.md
uv run python scripts/research/net_order_experiment.py order-check \
    --boards 00,01,02,03,06a --json /tmp/net-order/order-check.json
uv run python scripts/research/net_order_experiment.py route \
    --boards 00,01,02,03,06a --variants default,crossing \
    --work-dir /tmp/net-order --json /tmp/net-order/summary.json \
    --timeout 3600
```

On a loaded host, expect the timing columns to vary by several-fold between
runs and the quality columns not to vary at all.

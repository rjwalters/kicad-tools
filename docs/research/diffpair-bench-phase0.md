# Diff-pair benchmark, Phase 0 (Issue #6442, epic #6441)

Phase 0 changes no search code. It makes diff-pair results reproducible, proves
they were drawn by the router, and settles two contradictions before any later
phase is measured against them: the board 06 gap errors on PR #6440 and the
board 03 threshold drift.

## The one command

```bash
uv run kct build-native --check
uv run python scripts/research/expand_route_experiment.py pairs \
    --boards 03,06b,06rf --engines lattice,grid --seeds 42,43 --timeout 600 \
    --label <name> --work-dir /tmp/pairs        # -> data/research/diffpair_bench/<name>-<sha>.json
uv run python scripts/research/diffpair_bench.py determinism <file>   # repeat runs identical?
uv run python scripts/research/diffpair_bench.py compare A.json B.json
uv run python scripts/research/diffpair_bench.py summary <file>...    # markdown table
```

`--router-src DIR --router-rev SHA` routes with a `git archive` of another commit's
`src/` while the measurement stays on the current tree; that is how main and the
#6440 head are compared without touching either checkout. `--inferred-map` drops
`--net-class-map` (see "Board 06" below).

Rows: `03` (USB_D, USB_MCU_D), `06b` (board 06 with `LVDS\d_[PN]` copper stripped,
ported from `krt_compare.py`; plane vias stay, hence `--preserve-existing`) and
`06rf` (the 9-pair regression fixture). Engines: `lattice` (`--route-engine lattice
--strategy basic --differential-pairs`) and `grid` (`--route-engine grid
--differential-pairs --deterministic-rescue`). Both add `--deterministic-budget
--per-net-iterations 1000000 --no-placement-delta-feedback --layers 4
--no-auto-layers` and the board's committed `net_class_map.json`.

Per pair the file records: per-segment outcome (where the log has it), skew per net,
uncoupled mm (KiCad-equivalent: length that is not parallel and within the coupling
window of its partner), vias P vs N, gap min/max (edge to edge), coupled trunk
length, continuity % (info only) and the thresholds `kct check` applies. Per board:
nets routed, kct errors by rule, kicad-cli errors by type and unconnected count,
`manufacturable`, wall time.

## Router-produced guard

A row is only valid if the router drew the pair copper:

1. The input must have **zero** segments and vias on every detected pair net; `06b`
   is stripped first and a row whose input still has pair copper is refused before
   routing (`PairBenchError`).
2. Saved-plan and cache replay flags (`--apply-plan`, `--routing-plan`, `--from-plan`,
   `--saved-plan`, `--replay*`, `--load-plan`, `--plan-file`, `--use-cache`) are
   refused, and a route log that shows a plan apply or cache hit marks the row invalid.
3. The input SHA-256 and the git SHA are recorded.
4. A row whose pair copper equals the committed board's (input or `_routed`) is invalid.

`tests/test_diffpair_bench.py` covers each of these.

## Board 06 gap errors: named root cause

**The `diff_pair_gap_out_of_range` errors come from the project file's board-wide
`rules.min_clearance`, not from the netclass `diff_pair_gap`.** The leading
hypothesis (netclass `diff_pair_gap` left at KiCad's 0.25 default and never synced
from `intra_pair_clearance`) is **falsified**.

Mechanism. `boards/06-diffpair-test/output/diffpair_test.kicad_pro` is authored with
`rules.min_clearance = 0.15` (and `min_track_width = 0.15`). `kct route` keeps an
authored floor, so the routed `.kicad_pro` and the `Clearance - jlcpcb` floor rule in
the `.kicad_dru` stay at 0.15 mm. Without `--net-class-map` the classifier's map gives
the LVDS pairs `intra_pair_clearance = 0.1016`, and the lattice coupled run lays P/N
at a 0.127 mm edge gap. kicad-cli behaves as if the minimum for the diff-pair gap
constraint is the board clearance (changing only that setting clears the gap errors), so each coupled P/N segment pair fails twice: once as
`diff_pair_gap_out_of_range` ("netclass 'HighSpeed' (diff pair) minimum gap 0.1500 mm;
actual 0.1270 mm") and once as `clearance` against the floor rule.

Board 03 is clean for the same geometry because its **input** `.kicad_pro` has no
`rules.min_clearance`; the routed project then carries 0.1016 (written by `kct route`),
and 0.127 clears it. This answers the "03 coupled at 0.15 with 0 errors" counterexample:
0.15 clears both 0.15 and 0.1016, so it never could have failed.

Evidence (#6440 head `fd2f7e19`, `06b` stripped, lattice, seed 42, kicad-cli 10.0.6;
one routed board, one setting changed at a time, DRC re-run):

| Change to the routed project files | gap errors | clearance errors |
|---|---|---|
| none (inferred map, authored min_clearance 0.15) | 9 | 40 |
| every netclass `diff_pair_gap` 0.25 -> 0.1 | 9 | 40 |
| HighSpeed netclass `clearance` 0.15 -> 0.1 (with and without the gap change) | 9 | 40 |
| `.kicad_dru` floor rule 0.15 -> 0.1016 only | 9 | 1 |
| `.kicad_pro` `rules.min_clearance` 0.15 -> 0.1016 only | **0** | 40 |
| both of the above | 0 | 1 |
| `.kicad_dru`: floor rule exempts `isCoupledDiffPair()` plus a `diff_pair_gap`/`clearance` (min 0.1) rule for coupled pairs, `.kicad_pro` untouched | 0 | 1 |
| **re-route** with `min_clearance`/`min_track_width` removed from the *input* `.kicad_pro` | **0** | **0** |

The residual 1 clearance error in the middle rows is an unrelated net pair. The last
row is a full re-route: the same coupled routing (`LVDS1[1] R1.1->U2.2: coupled`)
passes kicad-cli clean once the authored floor is gone.

### Judge versus doctor: one protocol

Both were right; they ran different net-class maps.

| Run (6440 head, `06b`, lattice, seed 42) | pairs fully coupled | gap | kicad-cli errors | unconnected |
|---|---|---|---|---|
| committed `net_class_map.json` (intra-pair 0.15) | 3/4 | n/a (the `_N` copper was demoted) | 0 | **8** |
| `--inferred-map` (classifier map, intra-pair 0.1016) | 0/4 (4 partially coupled) | 0.120-0.127 | **48** (9 gap + 39 clearance) | 2 |

The committed-map row is the judge's "3/4 coupled, 0 errors". The `--inferred-map`
row reproduces the doctor's 48 `diff_pair_gap_out_of_range`-class errors exactly. The
judge's "0 unconnected" is **not** reproduced: with the committed map 4 LVDS `_N` nets
come out disconnected because the post-optimize backstop demotes them (see "Other
findings"). I could not recover the judge's exact command, so which map they used is
inferred, not read.

### Does this decide the #6440 merge gate?

**Not introduced by #6440's chain code; the trigger is newly reachable.** On main, the
same `--inferred-map` protocol couples nothing on `06b` (`pair-no-path` x4), so there is
no coupled gap to fail and 0 kicad-cli errors. #6440 makes the coupled run succeed, and
a coupled run at the classifier's intra-pair clearance is below an authored 0.15 floor.
The router code that picks 0.1016 is not touched by #6440. Recommendation: **merge
#6440** and fix the cause in P1/#6443 (candidates, each verified on the DRC side only:
floor the router's pair gap at the project's `min_clearance`, sync `min_clearance` from
the pair clearance when it is lowered, or emit a `diff_pair_gap` rule for coupled pairs in
the `.kicad_dru`).

Not verified: I did not run main with a coupled pair at a sub-0.15 gap (no main baseline
row has one; main's smallest measured gap is 0.155 mm), so "pre-existing" rests on the mechanism and
the control experiments, not on a failing main row. I also did not locate the code that
writes `min_clearance = 0.1016` when the input has none.

## Router <-> checker threshold parity (board 03)

Cause of the "router 70 %, `kct check` 25 %" drift: the router's in-route DRC ran without
the board's `--net-class-map`, so it used the classifier's map (no
`coupled_continuity_threshold`, default 70 %), while `kct check --net-class-map` read the
committed 25 % for `USB_D`. Separately, two router sites (and the two validate producers) built their detection input
with `synth_routing.setdefault(cls.name, nc)`, so for a class with per-net variants
(`HighSpeed`: `USB_D*` carry 0.25, `USB_MCU_D*` carry none) the class-name key held
whichever net came first.

Fix:

- `validate/diffpair_engagement.resolve_pair_thresholds` is the one resolver (positive net,
  then negative net, then rule defaults; net-name keys only). `derive_engagement_state`,
  `derive_skew_data` and the new router report all call it.
- `router/diffpair_detection.build_synth_routing` replaces the `setdefault` idiom at
  `core.py:4316`, `core.py` (pair-partner annotation), `diffpair_engagement.py` and
  `diffpair_skew.py`: a class-name alias is created only when every member carries the
  identical `NetClassRouting`. The match-group sites keep the old idiom (not in scope).
- After the pair phase the router prints, for every engaged pair,
  `USB_D+/USB_D-: continuity threshold 25.0%, skew tolerance 0.500 mm` on both engines.

Live check (`parity-branch-*.json`): the router's pair report, its in-route DRC finding
(`... below threshold 25.0%`) and `kct check --net-class-map` all say 25 % / 0.5 mm for
`USB_D` and 70 % / 0.5 mm for `USB_MCU_D`. The regression test is
`test_router_report_and_kct_check_agree_on_board03_thresholds`.

## Severity statement (policy deferred to P1)

`diffpair_length_skew`, `diffpair_routing_continuity` and `diffpair_clearance_intra` are
`CATEGORY_ADVISORY` in `validate/checker.py:473-474`, but that category is
presentation-only (the comment above `RULE_CATEGORY` says so): it groups findings in the
human report and changes no verdict, exit code or severity. The rules emit
`severity="error"`, `run_kct_check` counts every error-severity finding, and
`manufacturable` requires `kct_check_errors_total == 0`. So **a diff-pair finding makes a
row `manufacturable: false` today**, even though `kct check` files it under
"advisory/quality". Whether it should is user decision 4 of the epic and is not changed
here. Readers who want the split can use `kct_errors_by_rule`.

## Baselines

`data/research/diffpair_bench/baseline-main-db58570c.json` (main, 21 rows) and
`baseline-6440-fd2f7e19.json` (#6440 head, 7 rows). Timeout 600 s.

What was run, exactly:

- main: seed 42 and 43 for `06b` and `03` (lattice, grid), seed 42 for `06rf`; every
  seed-42 cell twice and the seed-43 cells twice (`--repeat 2`) for the determinism check;
  plus one `06b` lattice `--inferred-map` row.
- #6440: seed 42 only, one run per cell, plus the `--inferred-map` row.
- Not run: seed 43 for `06rf` and for the #6440 head, second runs on the #6440 head,
  second `06rf` seed-43 anything. All runs were sequential or two at a time on a shared,
  loaded host (load average 7-9), so wall times are inflated and not comparable.

Determinism (main, repeat runs, `diffpair_bench.py determinism`): 9 of 10 cells identical.
**`06b` grid seed 42 differed** (16/18 nets and `connectivity: 2` against 18/18 and
`diffpair_length_skew: 2`). Both runs had `--deterministic-rescue`.

Cause (issue #6475). It is **not** the #4781 relief-rescue transaction bound (this
board runs 0 rescues). Passing `--timeout` to a `--deterministic-budget
--deterministic-rescue` route left three wall clocks live in the grid negotiated loop:

1. the oscillation-escape budget `min(60 s, 25 % of remaining)` (#2415): on `06b` a
   strategy re-routes four nets at several seconds each, so 60 s lands between
   strategies on one run and inside a strategy on the next, leaving nets unrouted;
2. checkpoint timing: with `--seed` every `to_sexp()` draws a UUID from the global RNG,
   so the number of checkpoint writes before the escape's `random.shuffle` / `sample`
   (throttled by `--checkpoint-interval`) moved the stream;
3. the per-iteration per-net cap derived from the remaining stage budget (#3989).

Fix: under `--deterministic-rescue` with an active expansion cap, causes 1 and 3 are
switched off (the stage deadline is untouched), and a checkpoint write saves and
restores the RNG state (cause 2).

The "16/18" row was not a finished route. The run exited 1 (the #6351 copper-loss guard
refused the save) and the harness graded the leftover periodic checkpoint, labelled
`final`; a checkpoint has no zone fill, hence the two `connectivity` errors. **The three
exit-1 `06b` grid rows of `baseline-main-db58570c.json` (seed 42 rep 0, seed 43 rep 0 and
rep 1) are checkpoint grades**, so the seed-43 rows that "agree" are too. The harness
now marks exit 1 and 5 rows `valid: false` and labels the artifact `checkpoint`.

Result with the fix (`data/research/diffpair_bench/determinism-06b-grid-b9041b37.json`,
`expand_route_experiment.py pairs --boards 06b --engines grid --seeds 42,43 --repeat 2`,
host load average about 4.5): `diffpair_bench.py determinism` reports both cells
identical; all four rows are `valid: true`, route exit 3, artifact `final`, 18/18 nets,
`{diffpair_length_skew: 2}`, 229 to 243 s wall. Routed-board md5: seed 42 `feb49a72...`
(both reps), seed 43 `39d3fcff...` (both reps). A run on a deliberately loaded host was
not made.

### #6443 piece (b) gap rows

`data/research/diffpair_bench/gap-6567-d6d09a6c.json` (9 rows; issue #6567). These are the
cells PR #6556 left unrun: `03` lattice + grid @43, `06b` lattice @43, `06b` grid @42 + @43,
and `06rf` lattice + grid @42 + @43. They were taken on main `d6d09a6c`, which includes #6556
(`diffpair_uncoupled_length`, native pair rules) and #6603 (floored intra-pair gap). Each
cell is one targeted `diffpair_bench.py run --boards B --engines E --seeds S --timeout 2400`.
They ran sequentially, never as a grid, and the per-cell files were joined with
`diffpair_bench.py merge`. Every row is `valid: true` with artifact `final`. No deadline
was hit and the harness killed nothing.

Route exit codes: 3 everywhere except `06rf` lattice, which exited 4. kicad-cli was the
local 10.0.6 (the CI version). The Docker `kicad/kicad:10.0` daemon did not respond on
this host.

Host load is the 1-min average before → after each cell. Every row uses the
deterministic budget, so load moves only the wall time:

| Board | Engine | Seed | Pairs coupled | Nets | kct err (by rule) | kicad-cli err (by type) | Wall s | Load |
|---|---|---|---|---|---|---|---|---|
| 03 | lattice | 43 | 1/2 | 26/27 | uncoupled 1 | uncoupled 1 | 102.8 | 6.0 → 6.8 (5-min 9.9) |
| 03 | grid | 43 | 0/2 | 27/27 | skew 1, uncoupled 1 | skew 1, uncoupled 1 | 323.6 | 6.8 → 7.2 (5-min 9.0) |
| 06b | lattice | 43 | 3/4 | 18/18 | skew 4 | skew 4 | 77.6 | 7.2 → 7.1 |
| 06b | grid | 42 | 0/4 | 18/18 | skew 2 | skew 2 | 120.3 | 7.1 → 4.8 |
| 06b | grid | 43 | 0/4 | 18/18 | skew 2 | skew 2 | 119.0 | 4.5 → 3.2 |
| 06rf | lattice | 42 | 2/9 | 15/26 | connectivity 8, skew 4, uncoupled 2 | skew 7, uncoupled 1, vias 1 | 382.0 | 3.2 → 3.3 |
| 06rf | lattice | 43 | 2/9 | 15/26 | connectivity 8, skew 4, uncoupled 2 | skew 7, uncoupled 1, vias 1 | 382.8 | 3.1 → 1.9 |
| 06rf | grid | 42 | 0/9 | 22/26 | connectivity 1, skew 8 | skew 8 | 68.7 | 1.9 → 2.7 |
| 06rf | grid | 43 | 0/9 | 22/26 | connectivity 1, skew 8 | skew 8 | 68.5 | 2.8 → 2.2 |

Seed sensitivity: the `pair_digest`s of `06b` grid, `06rf` lattice and `06rf` grid are
identical at 42 and 43. So the per-pair table below lists those cells once, as `42/43`.
Three other rows match earlier files. `06b` grid @42/@43 matches
`determinism-06b-grid-b9041b37.json`, `06rf` lattice @42 matches `fix-6550-68db9374.json`,
and `06b` lattice @43 has the same digest as `fix-6535`'s seed-42 row (`6afd7e125b79f3aa`).
(All committed digests were re-hashed under #6638, which drops continuity/skew limits from
`pair_digest`. This row was `26c276ae7da77a54` before that change.)

Per pair, in PR #6556's format. The verdict columns are kct/kicad-cli. `-` means the rule
has no limit on that pair, or the pair is `unrouted` (one or both nets are not
connected, so the harness gives it no number):

| Board | Engine | Seed | Pair | Skew mm (limit) | Uncoupled P/N mm (limit) | Vias P/N | Skew | Uncoupled | Vias |
|---|---|---|---|---|---|---|---|---|---|
| 03 | lattice | 43 | USB_D | 0.0 (2.5) | 23.03/27.12 (19.05) | 0/0 | pass/pass | FAIL/FAIL | pass/pass |
| 03 | lattice | 43 | USB_MCU_D | 0.0 (2.5) | 0.5/0.5 (12.7) | 0/0 | pass/pass | pass/pass | pass/pass |
| 03 | grid | 43 | USB_D | 3.711 (2.5) | 23.38/25.65 (19.05) | 2/3 | FAIL/FAIL | FAIL/FAIL | pass/pass |
| 03 | grid | 43 | USB_MCU_D | 0.041 (2.5) | 0.87/0.91 (12.7) | 0/0 | pass/pass | pass/pass | pass/pass |
| 06b | lattice | 43 | LVDS1-4 | 0.128-0.236 (0.1) | 3.0-5.1 (no limit) | 0/0 | FAIL/FAIL x4 | - | - |
| 06b | grid | 42/43 | LVDS1, LVDS3 | 0.088, 0.053 (0.1) | 0.8-1.1 (no limit) | 0/0 | pass/pass | - | - |
| 06b | grid | 42/43 | LVDS2, LVDS4 | 0.453, 0.446 (0.1) | 0.7-2.1 (no limit) | 0/0 | FAIL/FAIL | - | - |
| 06rf | lattice | 42/43 | MIPI_CLK | 0.704 (0.3) | 1.51/0.58 (no limit) | 0/0 | FAIL/FAIL | - | - |
| 06rf | lattice | 42/43 | MIPI_D0 | 0.583 (0.3) | 1.46/1.03 (no limit) | 0/0 | FAIL/FAIL | - | - |
| 06rf | lattice | 42/43 | USB2_D | 7.311 (1.27) | 14.22/18.81 (12.7) | 4/6 (max 4) | FAIL/FAIL | FAIL/FAIL | FAIL/FAIL |
| 06rf | lattice | 42/43 | USB3_TX2 | 4.417 (0.5) | 14.11/18.52 (no limit) | 2/2 | FAIL/FAIL | - | - |
| 06rf | lattice | 42/43 | PCIE_RX, PCIE_TX, USB3_RX1, USB3_RX2, USB3_TX1 | unrouted | unrouted | - | - | - | - |
| 06rf | grid | 42/43 | MIPI_CLK | 0.663 (0.3) | 1.56/0.90 (no limit) | 0/0 | FAIL/FAIL | - | - |
| 06rf | grid | 42/43 | MIPI_D0 | 0.663 (0.3) | 1.56/0.90 (no limit) | 0/0 | FAIL/FAIL | - | - |
| 06rf | grid | 42/43 | PCIE_RX | 4.213 (0.5) | 2.71/6.64 (no limit) | 2/2 | FAIL/FAIL | - | - |
| 06rf | grid | 42/43 | PCIE_TX | 3.697 (0.5) | 6.00/5.50 (no limit) | 2/0 | FAIL/FAIL | - | - |
| 06rf | grid | 42/43 | USB2_D | 3.378 (1.27) | 3.76/3.32 (12.7) | 0/2 | FAIL/FAIL | pass/pass | pass/pass |
| 06rf | grid | 42/43 | USB3_RX1 | 0.42 (0.5) | 8.18/8.60 (no limit) | 2/2 | pass/pass | - | - |
| 06rf | grid | 42/43 | USB3_RX2 | 3.546 (0.5) | 1.89/5.44 (no limit) | 0/0 | FAIL/FAIL | - | - |
| 06rf | grid | 42/43 | USB3_TX1 | 3.26 (0.5) | 22.95/23.01 (no limit) | 0/2 | FAIL/FAIL | - | - |
| 06rf | grid | 42/43 | USB3_TX2 | 3.01 (0.5) | 0.95/3.96 (no limit) | 0/0 | FAIL/FAIL | - | - |

**Agreement.** kct and kicad-cli agree on skew, uncoupled length and via count for every
pair that both engines judge. There is no kct-pass / kicad-cli-fail uncoupled case, so
nothing needs citing against #6554. `06rf` names its pairs `X+`/`X-`, which KiCad
recognises, so no row is kct-only.

The per-board error counts differ in `06rf` lattice, but per pair the engines do not
disagree. There are two reasons:

- kct files the per-net via-count error under the `diffpair_uncoupled_length` rule id
  ("net USB2_D- has 6 vias, limit 4"). So kct's `uncoupled 2` is the same two findings as
  kicad-cli's `uncoupled 1` + `too_many_vias 1`.
- kicad-cli's `skew_out_of_range` 7 includes the three half-routed pairs (`PCIE_TX`,
  `USB3_RX2`, `USB3_TX1`), where one net has copper and its partner has none. kct reports
  those nets as `connectivity` and does not compute skew. The harness marks such pairs
  `unrouted` and gives no verdict, as #6556 specified. Both engines fail the board.

**Against `baseline-main-db58570c.json`** (`diffpair_bench.py compare`): every shared cell
is DIFFERENT, and the copper itself changed, not only the limits. Both rule effects and
routing effects show up:

- Rule effects (#6556). `diffpair_routing_continuity` errors are gone everywhere because
  continuity is now info only. `diffpair_uncoupled_length` errors are new on 03 USB_D and
  06rf USB2_D.
- Routing effects (#6475, #6535, #6550). Three of the four 06b grid @42/@43 baseline rows
  (@42 rep 0, @43 rep 0 and rep 1) were exit-1 checkpoint grades (16/18); @42 rep 1 was
  already a finished exit-3 route at 18/18. All four are now finished routes at 18/18. 03 lattice @43 went
  from 24/27 to 26/27. 06b lattice @43 went from 16/18 to 18/18. 06rf lattice @42 went from
  16/26 to 15/26, and 06rf grid @42 from 23/26 to 22/26.
- 06rf stays far from routed on both engines: lattice reaches 2/9 pairs coupled with 5 pairs
  unrouted, and grid reaches 0/9 coupled. These are router gaps for the later phases of
  #6441, not checker findings.

## Other findings

- **Coupled outcome without copper.** `coupled` in the lattice log does not mean the
  pair is on the board. "Post-optimize backstop demoted N net(s) whose copper became a
  cross-net short" removes whole nets after the engine reported success. It happens on
  main (`06b` lattice: 2 nets; `03` lattice: 1 net) and on #6440 (`06b` lattice with the
  committed map: 4 `_N` nets, hence 8 unconnected). In the pair file this shows as a
  `coupled` outcome with `skew_mm: null` and `routed_mm` of `[x, 0]`.
- `kct route --route-engine lattice` needs `--strategy basic`; the issue's command line
  omits it.

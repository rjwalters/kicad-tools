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
at a 0.127 mm edge gap. kicad-cli takes the minimum for the diff-pair gap constraint
from the board clearance, so each coupled P/N segment pair fails twice: once as
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
| committed `net_class_map.json` (intra-pair 0.15) | 3/4 | >= 0.15 | 0 | **8** |
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
row has one; main's smallest gap is 0.1749), so "pre-existing" rests on the mechanism and
the control experiments, not on a failing main row. I also did not locate the code that
writes `min_clearance = 0.1016` when the input has none.

## Router <-> checker threshold parity (board 03)

Cause of the "router 70 %, `kct check` 25 %" drift: the router's in-route DRC ran without
the board's `--net-class-map`, so it used the classifier's map (no
`coupled_continuity_threshold`, default 70 %), while `kct check --net-class-map` read the
committed 25 % for `USB_D`. Separately, three router sites built their detection input
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
**`06b` grid seed 42 differs** (16/18 nets and `connectivity: 2` against 18/18 and
`diffpair_length_skew: 2`). Both runs have `--deterministic-rescue`. The logs agree
through negotiated iteration 3 and diverge at iteration 4 (adaptive history 0.50 vs
3.00, 6 vs 4 failed nets in recovery); the first run spent 62 s in the stagnation-recovery
step the second did not. The log prints a *wall-clock* "Relief-rescue transaction bound"
(issue #4781) that `--deterministic-rescue` does not cover, which is my suspect. That is
not confirmed. So the "identical, including grid with `--deterministic-rescue`"
acceptance criterion is **met for 9 of 10 cells and not met for `06b` grid seed 42**.

## Other findings

- **Coupled outcome without copper.** `coupled` in the lattice log does not mean the
  pair is on the board. "Post-optimize backstop demoted N net(s) whose copper became a
  cross-net short" removes whole nets after the engine reported success. It happens on
  main (`06b` lattice: 2 nets; `03` lattice: 1 net) and on #6440 (`06b` lattice with the
  committed map: 4 `_N` nets, hence 8 unconnected). In the pair file this shows as a
  `coupled` outcome with `skew_mm: null` and `routed_mm` of `[x, 0]`.
- `kct route --route-engine lattice` needs `--strategy basic`; the issue's command line
  omits it.

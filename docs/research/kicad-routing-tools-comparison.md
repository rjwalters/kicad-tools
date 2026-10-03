# KiCadRoutingTools (KRT) vs kicad-tools: comparison and benchmark (#5781, #5790)

[drandyhaas/KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools)
("KRT") is an MIT-licensed Python and Rust A\* autorouter for KiCad 9 and 10.
This note compares its techniques with ours, benchmarks both tools on our demo
boards under one DRC referee, and lists the follow-ups that the measurements
support.

This is research only. KRT is not vendored, not a dependency, and not wired
into CI. No router or placement code changes with this document.

**Pinned versions**

The table was filled in two passes. Both used the same KRT commit and the same
`kicad-cli`; they differ in host and in this repo's commit, so **runtimes are
comparable within a pass, not across passes**.

| Item | Pass 1 (#5781: 00–03, 06) | Pass 2 (#5790: 04, 05, 07) |
|---|---|---|
| KRT commit | `64df3f582e8a862c9289205b5a608466bf21a7ba` (2026-09-28, `VERSION` 0.22.1) | same |
| KRT Rust core | prebuilt `grid_router` v0.22.0 (`build_router.py` downloads it from release v0.22.1, macOS arm64) | `rust_router/grid_router.so`, ELF x86-64, `build_router.py` cargo fallback |
| KRT Python env | CPython 3.12.13, numpy 2.5.3, scipy 1.18.1, shapely 2.1.2 | CPython 3.12.3, same numpy/scipy/shapely |
| kicad-tools | `ae264953` (`kct` 0.22.0) | `95af4f6a` (`kct` 0.22.0) |
| C++ backend | built (router 1.0.0, build 45) | built (router 1.0.0, build 45) |
| Referee | `kicad-cli` 10.0.6 | same |
| Host | Apple M3 Ultra, 28 cores, macOS (Darwin 27) | Intel Xeon Platinum 8488C, 8 vCPU, Ubuntu 24.04 (AWS) |

Two consequences of the split, both load-bearing when reading the table:

- **Runtime.** Pass 2's host is a shared 8-vCPU cloud worker, not a 28-core
  workstation. Compare kct against KRT *inside* one board's rows, never a
  pass-1 runtime against a pass-2 one.
- **`--preserve-existing`.** Pass 2's `95af4f6a` contains the Issue #5788 fix
  (already-connected nets are held out of the route set); pass 1's `ae264953`
  does not. That changes what 06a would measure today, and it is why the 07a
  row below behaves differently from the 06a row above.

Citations below use `path:function` at these commits. KRT paths are relative
to the KRT repo root; ours are relative to this repo.

## Scope

Every demo board in this repo is now measured. Pass 1 (#5781) covered boards
00, 01, 02, 03 (general single-ended routing) and board 06 (differential
pairs). Pass 2 (#5790) added boards 04, 05 and 07 — the dense ones — plus the
fanout and length-matching recipe variants they make possible.

Several boards run in more than one variant, because the two tools' documented
recipes for a technique are not a single command:

- **06a** is board 06's real input. The generator pre-routes the four LVDS
  pairs and the GND and +3V3 plane vias, so only the eight LVTTL nets are open.
- **06b** is the diff-pair test itself. The script rips the LVDS copper (64
  segments) and keeps the plane vias, so each tool must route the four pairs.
- **04f / 05f** are the **fanout** rows: KRT's escape CLI (`qfn_fanout.py`) run
  before `route.py`, against the plain `route.py` result on the same board.
  kct has no fanout CLI — its escape router runs inside `kct route` and
  auto-enables on dense packages — so only KRT re-runs, and the kct/ref rows
  for those boards are 04's and 05's.
- **07m** is the **length-matching** row: both tools told to match the four
  groups board 07 declares. **07t** is KRT's **time-matching** mode on the same
  groups; kct has no time-domain matcher, so that row is KRT-only and is not a
  head-to-head.

**Still not measured:** the external boards from Epic #4932 (STRF,
PocketBeagle, BeagleConnect), and KRT's `bga_fanout.py`. The BGA gap is not a
scope cut any more, it is a fixture gap: **no board in this repo has a BGA.**
The densest packages across the fleet are LQFP-144 0.5 mm (board 07 U1),
LQFP-48 0.5 mm (04 U2), TSOP-II-54 (07 U2) and TQFP-32 (05 U1); boards 05 U2/U3
are exposed-pad HTSSOP/HVSSOP. `qfn_fanout.py` is the KRT CLI that applies, and
it is measured (04f, 05f). Placement is still unmeasured, for the same reason
as in pass 1: every committed board has fixed placement.

## Row-by-row comparison

### 1. Rip-up and reroute

**KRT.** When A\* fails, the frontier cells are the ones it tried and failed
to expand into.

- **Blocker attribution.** `py_router/blocking_analysis.py:analyze_frontier_blocking`
  attributes the frontier cells to routed nets. It ranks sole blockers first,
  then by `unique + near_endpoint_unique + 0.5*shared`. It uses the frontier of
  whichever search direction failed faster.
- **N+1 escalation.** `py_router/reroute_loop.py:run_reroute_loop` rips the top
  blocker, re-analyzes the new frontier, and escalates to N=2 and beyond, up to
  `--max-ripup` (default 3).
- **Slot-0 guard.** If the fresh analysis names a different top blocker, the
  wrong first pick is restored before the ladder grows.
- **Ripped-corridor avoidance.** `py_router/obstacle_costs.py:compute_ripped_route_costs`
  adds a soft cost to a ripped net's old corridor, so the net that caused the
  rip routes beside it.
- **Improvement gate.** `py_router/improvement_gate.py:grade_nets`, called from
  `route.py`, reverts a run that ends worse than its input, measured in nets
  or pads.
- **Blocker selection** can also be `near-target`, `bidir` or `mincut`
  (`--ripup-blocker-select`). This is documented in `docs/rip-up-reroute.md`.

**Ours.** The default strategy is negotiated congestion (PathFinder-style).

- **Negotiated loop.** `src/kicad_tools/router/core.py:Autorouter.route_all_negotiated`
  and `src/kicad_tools/router/algorithms/negotiated.py:NegotiatedRouter` apply
  present and history costs, then rip up every net through overused cells on
  each iteration (`find_nets_through_overused_cells`, `rip_up_nets`).
- **Targeted rip-up.** `NegotiatedRouter.targeted_ripup` is opt-in
  (`--targeted-ripup`). It rips only the named blockers of a failed net.
- **Failure analysis.** `src/kicad_tools/router/failure_analysis.py:RootCauseAnalyzer`
  names blockers for diagnostics, not for search.

**What matters.** KRT's rip-up is sequential and blocker-attributed from the
A\* frontier. Ours is iteration-global: on board 03 the kct log shows
"Iteration 1: ripping up 12 nets", then iteration 2 rips the same 12. On these
boards that difference tracks the runtime gap (below). The runtime gap is not
proof of cause. KRT's improvement gate has no direct counterpart in our
router. `--preserve-existing` protects copper, but nothing reverts a net-worse
run.

### 2. Net ordering (MPS)

**KRT.** `py_router/net_queries.py:compute_mps_net_ordering` builds the default
`--ordering mps`:

1. Unroll each chip's boundary to [0, 1].
2. Two nets conflict when their source order and target order are inverted.
3. Build a conflict graph and repeatedly pick the net with the fewest active
   conflicts. Ties go to the shorter route, measured with a BGA-aware
   routing distance.

`py_router/mps_layer_swap.py:try_mps_aware_layer_swaps` moves conflicting nets
to other layers. `py_router/net_ordering.py:order_nets_inside_out` covers BGA
escape. This is documented in `docs/net-ordering.md` and `docs/MPS.html`.

**Ours.** The default order is a priority tuple from
`src/kicad_tools/router/core.py:Autorouter._get_net_priority`, which uses net
class, pad count and distance. `--order-method` offers `critical_first`,
`congestion` and `hybrid` (`src/kicad_tools/cli/route_cmd.py`). We already have
the same inversion-counting idea as MPS:

- `src/kicad_tools/router/bundle_river.py:compute_facing_row_inversions`
- `src/kicad_tools/router/monotone_certificate.py:monotone_certificate` and
  `_count_inversions`

Both are opt-in only (`--bundle-river-planner`, `--monotone-certificate-order`).

**What matters.** The crossing model is not new to us. The difference is that
KRT makes crossing-aware ordering the default and pairs it with layer swaps.
Ours sits behind flags and feeds a different stage.

### 3. Differential pairs

**KRT.** `py_router/diff_pair_routing.py:route_diff_pair_with_obstacles`
routes a single centerline with pose-based A\*. The state is (x, y, θ,
layer), with θ in 45° steps.

- **Dubins heuristic.** `rust_router/src/pose_router.rs:route_pose` searches
  with `dubins_heuristic`, backed by `rust_router/src/dubins.rs:DubinsCalculator`.
- **Setbacks.** The centerline starts at a setback in front of each stub.
  `_find_open_positions` / `_setback_ladder` scan nine setback angles.
- **P/N legs.** The legs are perpendicular offsets of the centerline, with
  miter compensation.
- **Polarity.** `_detect_polarity` checks polarity. `py_router/polarity_swap.py:apply_polarity_swap`
  is opt-in.
- **Hybrid mode.** Where the pair cannot couple at the ends, a coupled middle
  trunk is joined by single-ended legs (`_route_hybrid_leg`,
  `py_router/diff_pair_loop.py:_maybe_swap_to_hybrid`).

This is documented in `docs/differential-pairs.md`.

**Ours.** The coupled mode is a joint-state A\* over both conductors' positions.

- **Joint-state search.** `src/kicad_tools/router/diffpair_routing.py:route_coupled`
  and `route_differential_pair_coupled`, with the C++ hot loop in
  `src/kicad_tools/router/cpp/src/coupled_pathfinder.cpp:CoupledPathfinder`.
  It moves in four directions and uses a partner-aware heuristic.
- **Fallback.** On failure it falls back to `route_differential_pair_independent`,
  which routes two single-ended legs.
- **Lattice engine.** `src/kicad_tools/router/lattice/coupled.py` has a
  separate coupled engine.

**What matters.** In 06b (measured below), our coupled search reported
`class=joint-A*-plateau ... dominant_rejection=via_blocked_p` for all four
pairs. All four fell back to independent legs, so **0%** of P-leg length was
coupled. KRT routed a coupled trunk for 4/4 pairs, finished the member ends
single-ended (hybrid), and reached **88.9%** coupled on every pair. The
committed reference artifact reaches 87.1%.

The two search spaces differ in size. Ours searches the product of two
positions. KRT searches one centerline pose and derives P and N geometrically.

### 4. Length and time matching

**KRT.** `py_router/length_matching.py:apply_length_matching_to_group` and
`apply_time_matching_to_group` match lengths or delays within a group.

- **Meanders.** `generate_trombone_meander` / `apply_meanders_to_route` add
  trombone meanders to the shorter routes.
- **Via barrels** count toward length (`pair_leg_metric`).
- **Auto-grouping.** `auto_group_ddr4_nets` groups DDR4 nets automatically.
- **Delay model.** Microstrip vs stripline delay comes from `impedance.py`.
- **Order.** For multi-point nets, matching runs between MST phases.

**Ours.** `src/kicad_tools/router/match_group_tuning.py:tune_match_group_v2`
covers single-ended groups and pair groups, including P/N mirroring
(`_mirror_segments_about_centerline`).
`src/kicad_tools/router/optimizer/serpentine.py:SerpentineGenerator` generates
the serpentines. `src/kicad_tools/router/diffpair_length_tuning.py` handles
intra-pair skew.

**What matters.** **Measured on board 07 (07m, 07t).** Handed the same four
groups and the same declared 5.0 mm budget, KRT's matcher pulled three of them
from 15.8/23.1/38.1 mm down to 6.3/4.3/2.7 mm; `kct route
--length-match-groups` moved the same groups only from 32.9/47.4/54.2 to
29.4/38.0/32.8, and lost four connections doing it. Neither run finished the
board, so the comparison is confounded — see "Reading the table honestly" for
the per-group numbers, the confound, and why this is filed as a
measurement-first follow-up (#5798). KRT's **time** matching (07t) was worse
than its own length mode on every group by the length yardstick, which is the
only yardstick we have; its via-barrel accounting remains the feature we do
not obviously have, and is still untested directly.

### 5. BGA/QFN fanout

**KRT.** The `py_router/bga_fanout/` package handles fanout.

- **Escape and layers.** `escape.py` and `layer_assignment.py`.
- **Under-pad escape** for dense BGAs: `underpad.py`.
- **Hungarian target swaps.**
- **Cap cleanup.** `py_placer/place_fanout_clearance.py` moves decoupling caps
  off the new vias.

**Ours.** `src/kicad_tools/router/escape.py` (`is_dense_package`,
`EscapeRoute`), `fine_pitch_escape.py` and `escape_corridor.py`. On board 06,
kct auto-enables escape for the eight SOIC-8 parts and prints "0 pins
escaped". That diagnostic was already filed as #3900. On board 04 it
auto-enables on U2 (LQFP-48) and escapes 8 pins; on board 05, on U1/U2/U3, 43
pins.

**What matters.** **QFP/QFN fanout is measured (04f, 05f); BGA fanout is
not, and cannot be on this fleet.** KRT's `qfn_fanout.py` pre-pass cut KRT's
own via count on board 04 by 45 % (31 → 17) and its fab-referee errors from 41
to 17 — then made board 05 markedly worse (6 → 94 shared-referee errors),
because it lays escape copper at its own default widths instead of the board's
net class. The via-reduction idea is validated; KRT's implementation of it is
not adoptable as-is. `bga_fanout.py` was not run because **no board in this
repo has a BGA** — the densest parts are LQFP-144/LQFP-48/TSOP-II-54/TQFP-32.
Full numbers and the width-policy diagnosis are in "Reading the table
honestly".

### 6. KiCad-oracle reconnect

**KRT.** `py_router/kicad_oracle.py:oracle_reconnect` routes the exact links
that KiCad reports as unconnected. It reads them from pcbnew's exact zone fill
or from `kicad-cli pcb drc`, and repeats for up to three rounds until KiCad is
satisfied. It also runs as the in-run plane finalize (`route.py`
`_plane_finalize_active`). Plane pads are welded by the route step.

**Ours.**

- **Referee.** `src/kicad_tools/drc/geometric.py:run_geometric_drc` runs the
  same `kicad-cli pcb drc --refill-zones` gate as a referee.
- **Completion pass.** `kct route --complete`
  (`src/kicad_tools/cli/route_cmd.py:_resolve_complete_nets`) and
  `src/kicad_tools/router/partial_rescue.py:complete_unfinished_nets` detect
  incomplete nets. They use our own connectivity model, not KiCad's.
- **Pour handling.** `src/kicad_tools/router/auto_pour.py:auto_pour_if_missing`
  and `auto_skip_pour_nets` pour GND and VCC on 2-layer boards and skip routing
  those nets.

**What matters.** This row has the clearest measured gap. With default
settings, kct leaves pour-net pads stranded:

- **Board 02:** GND, 2 kicad-cli unconnected items.
- **Board 03:** VCC 18, VBUS 5 and GND 1, for 24 unconnected items.

Our own `NetStatusAnalyzer` counts all of these as advisory plane residuals
(`nets_blocking_incomplete = 0`), and `kct route` exits 0. KRT's KiCad-driven
finalize closes both boards to 0 unconnected.

### 7. Placement

**KRT.** `py_placer/place_optimize.py` refines a seed placement (perturbative
refinement, not from-scratch placement). `docs/placement-optimization.md` is a
literature survey that ends in that design. `py_tools/board_brief.py` and
`check_floorplan.py` implement the design brief and the graded floorplan
intent.

**Ours.** `src/kicad_tools/placement/` (the `bo_strategy`, `cmaes_strategy`
and `seed` modules), `kct optimize-placement`
(`src/kicad_tools/cli/optimize_placement_cmd.py`), and the router-to-placement
loop `src/kicad_tools/router/placement_feedback.py:PlacementFeedbackLoop.run`.

**What matters.** **Still not measured, and not for want of a board.** Every
committed board in this repo — 04, 05 and 07 included — ships a fixed
placement produced by its own `generate_design.py`, and both tools' routers
are handed that placement unchanged. A placement comparison is a *different
experiment* from this one: it would have to re-place a board from a seed and
then route the result, which changes the input both routers see and so makes
every routing row above incomparable. Nothing in boards 04/05/07 changed that,
so the row stays unmeasured with the reason recorded rather than silently
blank.

The one thing board 05 does add is a hint about where such an experiment would
pay: kct times out on 05 and 07a (below) at placements it did not choose, and
KRT's `py_placer/place_fanout_clearance.py` (moving decoupling caps off fanout
vias) is a placement edit made *for* routability. Neither observation is a
measurement of KRT's placer. KRT's machine-readable "design brief" (connector
edges and intent) is a constraint-capture idea, not an optimizer. It is still
the only part that looks novel relative to us.

### 8. Routing plans as files

**KRT.** `py_router/make_plan.py:make_plan` records a command chain as JSON.
`py_router/run_plan.py:main` replays it headless through the plugin. The GUI
loads the same file. `py_router/make_movie.py` animates a route.

**Ours.** `src/kicad_tools/router/routing_plan.py:RoutingPlan` / `build_plan`
writes the `*.routing_plan.json` sidecar, which is a global-routing corridor
plan. `--plan-gate` aborts on overflow.

**What matters.** The names are the same but the concepts differ. KRT's plan
is a replayable recipe of tool invocations. Ours is a corridor and capacity
plan. Our per-board `generate_design.py` recipes do the job KRT's plans do. No
metric applies.

### 9. Distribution

**KRT.** `.github/workflows/release.yml` builds `grid_router` for four
platforms. `package_pcm.py:stage_plugins` builds a KiCad PCM zip.
`build_router.py` downloads the prebuilt binary, with a cargo fallback. The
release flow is described in `docs/release-pipeline.md`.

**Ours.** We publish to PyPI only. Users must build the C++ extension
themselves (`src/kicad_tools/cli/build_native_cmd.py`, `kct build-native`,
which needs CMake and a compiler).

**What matters.** This matters for Epic #5774, not for routing quality. A
fresh `uv sync` of ours routes with no native extension. KRT's prebuilt
download removes that step.

### 10. AI skills

**KRT.** Ten skills under `.claude/skills/`, including `plan-pcb-routing`,
`diagnose-routing-failures`, `review-routed-board` and `pcb-free-agent`. They
can be driven from the plugin's AI tab through Claude Code or opencode
(`docs/claude-skills.md`).

**Ours.** `.claude/commands/kct/`: `ee-review`, `manufacturing-readiness`,
`tapeout`, `hv-isolation-loop`, `layout-journal` and `board-recipe-scaffold`.

**What matters.** KRT's skills cover the routing loop (plan, diagnose, retry).
Ours cover review and release. `diagnose-routing-failures`, which emits a
retry command, has no kct equivalent. Nothing was measured.

## Benchmark

### Recipes

Each tool ran its documented default. Neither tool was tuned per board.

| Board | kct | KRT |
|---|---|---|
| 00, 01, 02, 03, 04, 05 | `kct route IN -o OUT` | `route.py IN OUT` |
| 04f | (04's row) | `qfn_fanout.py IN -c U2 -o MID` then `route.py MID OUT` |
| 05f | (05's row) | `qfn_fanout.py IN -c U1 -o MID` then `route.py MID OUT` |
| 06a | `kct route IN -o OUT --preserve-existing` | `route.py IN OUT` |
| 06b | `kct route IN -o OUT --preserve-existing --differential-pairs` | `route_diff.py IN MID --nets 'LVDS*'` then `route.py MID OUT` |
| 07a | `kct route IN -o OUT --preserve-existing` | `route.py IN OUT` |
| 07m | 07a's, plus `--length-match-groups --net-class-map <board sidecar>` | `route.py IN OUT --length-match-group <4 groups> --length-match-tolerance 5.0` |
| 07t | (no kct equivalent) | 07m's KRT command, plus `--time-matching` |

Notes on the recipes:

- **06a/06b/07a copper.** KRT keeps existing copper by default. kct needs
  `--preserve-existing`, its documented incremental-routing flag, to keep the
  plane vias (06) and the 119 already-routed connections (07).
- **KRT diff-pair order.** "`route_diff.py` first, then `route.py` for the
  rest" is KRT's own README recipe. The same shape applies to the fanout rows:
  the escape CLI runs first and its output feeds `route.py`.
- **Fanout rows are KRT-only by construction.** kct has no fanout CLI. Its
  escape router is inside `kct route` and auto-enables on dense packages (on
  board 04 it reports `Escape routing: auto-enabled (dense packages: ['U2'])`,
  8 pins escaped). So 04f/05f measure *KRT with its escape pre-pass against
  KRT without it*, on a board where kct's own escape already ran. Naming the
  component is the only argument passed; everything else is `qfn_fanout.py`'s
  default.
- **Length matching is opt-in on both sides**, so 07m is the only row where
  either tool is asked for it. Both are handed the **same** four groups and the
  **same** 5.0 mm skew budget, taken from the board's own
  `output/sdram_constraints.json` (`group_skew_mm`) — kct via its committed
  net-class-map sidecar, KRT via `--length-match-group` /
  `--length-match-tolerance`. KRT's unqualified default tolerance is 0.1 mm,
  which would hold it to a budget kct was never asked to meet; passing the
  board's own number is what makes 07m a single experiment.
- **kct defaults** include auto-pour on 2-layer boards, auto layer escalation,
  and JLCPCB fab floors.
- **KRT defaults** include `--escalation fab`, `--fab-tier auto`, MPS
  ordering, and in-run plane finalize.
- **Committed artifacts.** The `ref` rows grade each board's committed
  `*_routed.kicad_pcb`, produced by that board's own tuned pipeline, as a
  third point of comparison. They were not regenerated. Boards 05 and 07's
  committed artifacts come from heavily board-tuned recipes (board 05's
  `design.py` uses `--timeout 900 --early-stop-patience 4`, per #3096/#3111);
  none of that tuning is in the `kct` rows, which is the point of the `ref`
  column.

### Referees

Both tools rewrite the project file they emit.

- KRT lowers `rules.min_*` to the values it routed at.
- kct emits a JLCPCB-floor `.kicad_pro` and `.kicad_dru`.

Grading each output against its own project would let each tool move its own
goalposts. `scripts/research/krt_compare.py` therefore grades every output
three ways:

1. **Shared referee (headline).** `kicad-cli pcb drc --refill-zones` runs
   against the board's **original input** `.kicad_pro`: the designer's net
   classes, plus KiCad's built-in defaults for any unset Board Setup floor. No
   `.kicad_dru` is used. The script saves the refilled board and measures
   completion, vias and wirelength on it with the Epic #4932 harness
   (`kicad_tools.benchmark.external.metrics.measure_completion` and
   `measure_copper`), never from either tool's log.
2. **Fab referee.** The same gate runs against kct's emitted JLCPCB-floor
   project for that board, applied identically to both tools.
3. **As emitted.** The same gate runs against each tool's own project. This is
   for information only.

The script also runs `kct check --mfr jlcpcb`, our own engine, as a secondary
gate because it grades via-in-pad, which kicad-cli does not grade by default.
It reports two more metrics:

- **Signal on plane layers:** foreign-net track length on inner layers that
  carry a zone.
- **Pair coupling:** the share of P-leg length that has a same-layer N segment
  within 1.5× the declared pair pitch (0.45 mm on board 06).
- **Group length spread:** for a board that declares length-match groups
  (board 07 only), the per-group max-minus-min of 2-D track length, measured
  on the refilled board. Via barrels are excluded on **both** sides, so the
  number is comparable even though KRT's own matcher counts them; the column
  reports the worst group. The declared budget is 5.0 mm.

### Results

Every cell is a single run. **Three runs hit the 20-minute cap**, all of them
`kct route` on the dense boards (05, 07a, 07m); they are marked `(TIMEOUT)` and
are still scored, because how far a tool had got when the cap fired is itself
the measurement. The results JSON holds all DRC classes.

Rows 00–06 are pass 1 (Apple M3 Ultra); rows 04–07 are pass 2 (8-vCPU AWS
worker). See "Pinned versions" — runtimes are comparable within a pass only.

| Board | Tool | Nets complete | Connections | kicad-cli unconnected | DRC errors (shared) | DRC errors (fab) | DRC errors (as emitted) | kct check errors | Vias | Wirelength mm | Signal on plane layers mm | Pair coupling % (min) | Group length spread mm (max) | Runtime s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 00 | kct | 3/3 | 3/3 | 0 | 0 | 0 | 0 | 0 | 0 | 8.71† | 0.0 | -- | -- | 7.43 |
| 00 | krt | 3/3 | 3/3 | 0 | 0 | 0 | 0 | 0 | 0 | 28.1 | 0.0 | -- | -- | 0.76 |
| 00 | ref | 3/3 | 3/3 | 0 | 0 | 0 | n/a | 0 | 0 | 8.6 | 0.0 | -- | -- | n/a |
| 01 | kct | 3/3 | 5/5 | 0 | 0 | 0 | 0 | 0 | 4 | 68.18 | 0.0 | -- | -- | 7.23 |
| 01 | krt | 3/3 | 5/5 | 0 | 0 | 0 | 0 | 0 | 0 | 59.62 | 0.0 | -- | -- | 0.55 |
| 01 | ref | 3/3 | 5/5 | 0 | 0 | 0 | n/a | 0 | 2 | 34.69 | 0.0 | -- | -- | n/a |
| 02 | kct | 11/12 | 34/36 | 2 | 0 | 0 | 0 | 0 | 37 | 412.4† | 0.0 | -- | -- | 49.79 |
| 02 | krt | 12/12 | 36/36 | 0 | 0 | 0 | 0 | 3 (via_in_pad 3) | 14 | 516.33 | 0.0 | -- | -- | 1.24 |
| 02 | ref | 12/12 | 36/36 | 0 | 0 | 0 | n/a | 0 | 28 | 572.21 | 0.0 | -- | -- | n/a |
| 03 | kct | 24/27 | 91/115 | 24 | 161 (track_width 161) | 0 | 0 | 0 | 41 | 659.71 | 264.5 | -- | -- | 105.43 |
| 03 | krt | 27/27 | 115/115 | 0 | 0 | 0 | 0 | 46 (via_in_pad 46) | 73 | 565.03 | 318.0 | -- | -- | 7.79 |
| 03 | ref | 27/27 | 115/115 | 0 | 168 (drill_out_of_range 74, track_width 20, via_diameter 74) | 0 | n/a | 45 (via_in_pad 45) | 153 | 893.69 | 253.3 | -- | -- | n/a |
| 04 | kct | 10/12 | 25/43 | 18 | 2 (track_width 2) | 0 | 0 | 0 | 14 | 179.49 | 0.0 | -- | -- | 17.29 |
| 04 | krt | 12/12 | 43/43 | 0 | 41 (annular_width 11, drill_out_of_range 11, hole_clearance 8, via_diameter 11) | 41 (same) | 0 | 51 (dimension_annular_ring 11, dimension_via_diameter 11, dimension_via_drill 11, hole_to_hole_clearance 5, via_in_pad 13) | 31 | 453.21 | 0.0 | -- | -- | 5.99 |
| 04 | ref | 12/12 | 43/43 | 0 | 36 (annular_width 7, drill_out_of_range 7, track_width 15, via_diameter 7) | 21 (annular_width 7, drill_out_of_range 7, via_diameter 7) | n/a | 21 (dimension_annular_ring 7, dimension_via_diameter 7, dimension_via_drill 7) | 29 | 212.23 | 0.0 | -- | -- | n/a |
| 04f | krt | 12/12 | 43/43 | 0 | 48 (clearance 23, track_width 25) | 17 (clearance 17) | 0 | 26 (clearance_pad_segment 6, clearance_segment_segment 15, via_in_pad 5) | 17 | 466.86 | 0.0 | -- | -- | 2.78 |
| 05 | kct | 28/37 | 87/142 | 51 | 66 (clearance 41, hole_clearance 8, shorting_items 8, solder_mask_bridge 8, tracks_crossing 1) | 66‡ | 66 | 110 (clearance_pad_segment 4, clearance_pad_via 8, clearance_segment_segment 5, clearance_segment_via 42, clearance_via_via 20, connectivity 6, hole_to_hole_clearance 11, via_in_pad 14) | 66 | 871.43 | 183.5 | -- | -- | 1200.22 (TIMEOUT) |
| 05 | krt | 32/37 | 111/142 | 0 | 6 (annular_width 3, via_diameter 3) | 6‡ | 0 | 33 (connectivity 5, dimension_annular_ring 3, dimension_via_diameter 3, pth_hole_clearance 1, via_in_pad 21) | 63 | 1168.69 | 0.0 | -- | -- | 13.35 |
| 05 | ref | 37/37 | 142/142 | 0 | 5 (clearance 5) | 5‡ | n/a | 0 | 136 | 1446.63 | 442.3 | -- | -- | n/a |
| 05f | krt | 33/37 | 112/142 | 0 | 94 (annular_width 3, clearance 60, track_width 28, via_diameter 3) | 94‡ | 0 | 141 (clearance_segment_via 43, clearance_segment_segment 29, dimension_trace_width 28, via_in_pad 26, +15 others) | 78 | 1149.35 | 0.0 | -- | -- | 10.53 |
| 07a | kct | 21/55 | 127/161 | 34 | 20 (clearance 19, starved_thermal 1) | 20‡ | 20 | 47 (clearance_segment_via 12, connectivity 34, hole_to_hole_clearance 1) | 202 | 1073.2 | 294.1 | -- | 54.16 | 1200.18 (TIMEOUT) |
| 07a | krt | 33/55 | 139/161 | 2 | 175 (annular_width 10, clearance 34, drill_out_of_range 10, **shorting_items 61**, **tracks_crossing 43**, via_diameter 17) | 175‡ | 137 (clearance 32, shorting_items 61, tracks_crossing 44) | 295 (clearance_segment_segment 186, clearance_segment_via 17, connectivity 22, via_in_pad 20, +50 others) | 195 | 2645.75 | 18.1 | -- | 42.61 | 67.76 |
| 07a | ref | 55/55 | 161/161 | 0 | 0 | 0‡ | n/a | 0 | 192 | 3332.76 | 0.0 | -- | **2.92** | n/a |
| 07m | kct | 17/55 | 123/161 | 38 | 20 (clearance 19, starved_thermal 1) | 20‡ | 20 | 57 (clearance_segment_via 12, connectivity 38, dimension_annular_ring 7) | 196 | 899.5 | 166.8 | -- | 38.04 | 1200.17 (TIMEOUT) |
| 07m | krt | 39/55 | 145/161 | 2 | 210 (annular_width 10, clearance 64, drill_out_of_range 10, **shorting_items 56**, **tracks_crossing 53**, via_diameter 17) | 210‡ | 171 (clearance 60, shorting_items 57, tracks_crossing 54) | 479 (clearance_segment_segment 376, clearance_segment_via 17, connectivity 16, via_in_pad 20, +50 others) | 195 | 3261.61 | 36.0 | -- | 39.29 | 100.14 |
| 07t | krt | 39/55 | 145/161 | 2 | 214 (annular_width 10, clearance 70, drill_out_of_range 10, **shorting_items 58**, **tracks_crossing 49**, via_diameter 17) | 214‡ | 175 (clearance 65, shorting_items 59, tracks_crossing 51) | 490 (clearance_segment_segment 387, clearance_segment_via 17, connectivity 16, via_in_pad 20, +50 others) | 195 | 3231.66 | 36.0 | -- | 44.34 | 105.3 |

† kct auto-poured GND/VCC on boards 00, 02 and 04, so those nets are copper
fill, not track. Wirelength is not comparable across tools on those rows.

‡ **There is no fab referee on boards 05 and 07.** The fab referee is kct's own
emitted JLCPCB-floor `.kicad_pro` + `.kicad_dru` for that board — and on 05,
07a and 07m kct was killed at the cap *before* it emitted them. What survives
is a checkpoint `.kicad_pro` that still carries the input project's rules and
no `.kicad_dru` at all, so the "fab" column on those boards is the shared
referee run a second time. It is reported rather than blanked so the
duplication is visible; do not read it as "passes at fab floors".

The full `errors_by_type` breakdown for every abbreviated cell is in the
results JSON, not summarised away.

### Reading the table honestly

**The headline from pass 2 is that neither tool's default handles the dense
boards, in two different ways.** On 05 and 07 kct runs out of time and KRT runs
out of correctness. The pass-1 summary ("KRT completes every scoped board")
does **not** generalise: it was a statement about boards 00–03 and 06.

**Completion (boards 02, 03, 04).** On the sparse and medium boards KRT
completes and kct's default leaves pour-net pads stranded: 2 on board 02, 24 on
board 03, and now **18 on board 04**. Every one is on a net kct chose to pour
instead of route (`Auto-pour: created 3 zone(s) for +3.3V, +5V, GND` /
`Auto-skip: … (pour nets — use zone fill)`). Board 04 is the cleanest
instance yet: kct routed 9/9 of the nets it considered in scope and reported
"100% completion", while the shared referee counts 25/43 connections. Our own
net-status calls the rest advisory, and kct exits 0. That discrepancy is itself
a finding: it ties back to the process rule in `CLAUDE.md` that `kct check`
alone is not sufficient. The committed `ref` artifacts complete, but only
because each board's tuned pipeline adds stitching and repair passes on top of
`kct route`.

**kct times out on boards 05, 07a and 07m (new).** All three hit the 20-minute
cap. The logs show where it goes: board 05's negotiated loop spends 519 s and
640 s on two successive iterations that rip 15 then 10 nets, and never reaches
a third. kct got 87/142 connections on 05 and 127/161 on 07a (the 07a input
*arrives* with 119/161 already routed, so that is +8 connections in 20
minutes). KRT finished the same boards in 13 s and 68 s. Board 05's own
committed pipeline reaches 142/142 — but only with the board-tuned
`--timeout 900 --early-stop-patience 4` recipe from #3096/#3111, which is
deliberately not in the `kct` rows.

**KRT shorts nets on board 07 (new, and disqualifying for that board).** Its
07a output carries **61 `shorting_items`** and 43 `tracks_crossing` under the
shared referee — literal shorts, e.g. "Items shorting two nets (nets DQ9 and
A8)". 07m and 07t are the same. This also explains the odd pairing of "33/55
nets complete" with "2 kicad-cli unconnected" on those rows: KiCad's
connectivity walks the copper, so two nets welded together look connected to
the ratsnest, while the completion harness scores each net separately. Treat
the low unconnected count on 07a/07m/07t as an artefact of the shorts, not as
completion. Nothing comparable appears on boards 00–06, so this is specific to
board 07's density, not a general KRT property.

**Track width on board 03.** kct's 161 `track_width` errors come from 0.15 mm
tracks. Those are below KiCad's default 0.2 mm floor, which applies because
board 03's input project leaves `min_track_width` unset. They are legal under
the JLCPCB floor, and kct's own emitted project declares 0.1016 mm. Under the
fab referee the count is 0. KRT honored the net-class 0.25 mm width. It
escalated one net to 0.2418 mm (`--escalation fab`) and reported that in its
own summary. The `ref` row's 168 errors have the same cause, applied to vias
(0.45/0.2 mm JLC vias vs KiCad defaults).

**Via-in-pad.** Our secondary gate flags 46 via-in-pad sites in KRT's board 03
and 3 in its board 02. KRT's log says so itself: "67 via(s) in a pad or paste
opening … requires IPC-4761 Type VII". The committed board 03 has 45, so this
is not unique to KRT. kicad-cli does not flag it by default. Part of KRT's
board 03 completion depends on filled and capped vias, which cost more to
fabricate. kct's output has none.

**Differential pairs (06b).** Both tools connect all 18 nets. Only KRT
produces coupled pairs: 88.9% vs 0%. kct's coupled search plateaued on all 4
pairs and fell back to independent legs.

**Opt-in fix after this benchmark ran (Issue #5786).** With
`KCT_POSE_CENTERLINE=1`, a pair the joint-state search cannot couple is retried
as one centerline over (x, y, heading) poses with a Dubins-length heuristic (a
C++ port of KRT's `dubins.rs`, MIT, attributed). P and N are derived as
miter-compensated offsets, and the single-ended end legs are chosen by a
setback scan. Before it is committed, the finished pair must pass the exact
foreign-pad gate (partner pads included) and the intra-pair clearance audit.
Re-measured with the same recipe (`krt_compare.py --boards 06b,06a`, Apple M3
Ultra, single run each). These rows are not regenerated into the table above:

| Board | kct build | Nets | Connections | DRC errors (shared) | Signal on plane mm | Pair coupling % (LVDS1-4) | Runtime s |
|---|---|---|---|---|---|---|---|
| 06b | default (pose search off) | 18/18 | 90/90 | 0 | 0.0 | 0.0 / 0.0 / 0.0 / 0.0 | 130.43 |
| 06b | `KCT_POSE_CENTERLINE=1` | 18/18 | 90/90 | 0 | 0.0 | 87.0 / 87.0 / 87.0 / 87.0 | 84.58 |
| 06a | `KCT_POSE_CENTERLINE=1` | 18/18 | 90/90 | 0 | 0.0 | 87.1 / 87.1 / 87.1 / 87.1 | 54.49 |

With the search on, 06b's minimum coupling goes from 0% to 87.0% (KRT 88.9%,
committed reference 87.1%). `kicad-cli pcb drc --refill-zones` on the 06b
output also reports 0 errors and 0 unconnected items. 06a never reaches the new
path: since #5788 it keeps the pre-routed pairs, so it scores the reference's
87.1%.

**Why it is off by default.** Board 06's `Diff-Pair Routing Regression` job
(seed 42) fails with the search on. Pose-coupling MIPI_D0 seals MIPI_RST's
corridor, so reach drops from 21/21 to 20/21. The job's error count stays
within its allowlist (18 vs 17). The first version also had two other defects,
now fixed: end legs that grazed the partner's pad, and a stub-failed net that
the main pass never got back. The search is single-layer, so a pair whose ends
need a via between them still falls back to independent legs.

In 06a, kct also discarded the generator's pre-routed, coupled LVDS copper,
even with `--preserve-existing`. That flag only preserves nets outside the
route set, which is why board 06's own README passes `--nets IN1,…,OUT4`. The
06a and 06b outputs are therefore identical. KRT kept the pre-routed pairs
(its cleanup passes simplified them from 8 to 4 segments per leg).

**Fixed after this benchmark ran (Issue #5788).** `--preserve-existing` now
excludes a net that is already fully connected on the input from the route set,
so the 06a recipe above keeps 64/64 LVDS segments (it kept 0/64 when measured
here). The 06a rows in the table above are the pre-fix measurement and were not
regenerated. Pass 2's kct **does** contain the fix, which is why 07a starts
from the input's 119 routed connections instead of discarding them.

**QFP/QFN fanout (04f, 05f — newly measured).** KRT's escape pre-pass is a
real, and genuinely two-sided, effect:

- **Board 04 (2 layers, LQFP-48):** the pre-pass nearly halves KRT's vias,
  31 → **17**, cuts the fab-referee errors 41 → **17** and our secondary gate
  51 → **26**, and is faster (2.78 s vs 5.99 s). Every via-geometry class
  (`annular_width`, `drill_out_of_range`, `via_diameter`) disappears, because
  there are simply far fewer vias to be out of spec.
- **Board 05 (4 layers, zones, TQFP-32):** the same pre-pass makes things
  **worse**. Shared-referee errors go 6 → **94** and our gate 33 → **141**, for
  one extra net and one extra connection.
- **The cause is visible and is not "the idea is bad".** `qfn_fanout.py` emits
  its escape geometry at its own defaults rather than the board's net class:
  0.127 mm tracks on board 04 (KiCad's default floor there is 0.2 mm) and
  0.100 mm tracks at 0.1001 mm clearance on board 05 (whose project declares
  `min_track_width` 0.15 and `min_clearance` 0.127). That is where 04f's 25
  `track_width` + 23 `clearance` errors and 05f's 28 + 60 come from. It is the
  escape *pattern* that wins vias, and the escape *width policy* that loses
  DRC.

So the row is measured, and the conclusion is narrow: a dedicated escape
pre-pass buys a large via reduction, and is only a net win if its geometry is
taken from the board's rules. Our escape router is already inside `kct route`
and already net-class-aware — on board 04 it auto-enabled on U2 and escaped 8
pins at 0 fab-referee errors. There is no evidence here for adopting KRT's
version.

**BGA fanout is still unmeasured, and cannot be measured on this fleet.**
`bga_fanout.py` needs a BGA; no board in this repo has one (see Scope). Note
that kct's own escape diagnostic *prints* "BGA" for board 05's U2 and U3, which
are an exposed-pad HTSSOP-28 and HVSSOP-8 — a mislabel in the diagnostic, not a
BGA (recorded under incidental findings below). Do not read that log line as
evidence a BGA was exercised.

**Length and time matching (07m, 07t — newly measured).** Board 07 declares
four groups and a 5.0 mm skew budget, and both tools were handed both. The
table's single "worst group" column is the wrong lens here — it is dominated by
one group in every run — so the per-group spreads, in mm, budget 5.0:

| Run | SDRAM_BYTE0 (9) | SDRAM_BYTE1 (9) | SDRAM_ADDRESS (14) | SDRAM_COMMAND (6) |
|---|---|---|---|---|
| 07a kct — matching **off** | 32.85 | 47.41 | 26.09 | 54.16 |
| 07m kct — `--length-match-groups` | 29.43 | 38.04 | 23.34 | 32.82 |
| 07a krt — matching **off** | 15.84 | 23.05 | 42.61 | 38.13 |
| 07m krt — `--length-match-group` | **6.34** | **4.34** ✓ | 39.29 | **2.69** ✓ |
| 07t krt — `--time-matching` | 10.67 | 16.46 | 44.34 | 17.02 |
| 07a ref — board 07's tuned kct pipeline | **0.63** ✓ | **2.87** ✓ | **2.92** ✓ | **0.96** ✓ |

Every group is 100 % populated in every run (`members` in the JSON), because
most members arrive pre-routed; the metric excludes via barrels on both sides,
so it is comparable across tools even though KRT's own matcher counts them.

What this supports, and what it does not:

- **KRT's group matcher does work, and by a wide margin, on 3 of the 4
  groups.** Its own before/after on the same board is the cleanest comparison
  available (no cross-tool confound): 15.84 → 6.34, 23.05 → 4.34 and 38.13 →
  2.69, two of them inside the declared budget. This is the strongest new
  evidence in pass 2 for any KRT technique.
- **`kct route --length-match-groups` moves the same four groups far less**:
  32.85 → 29.43, 47.41 → 38.04, 26.09 → 23.34, 54.16 → 32.82. None reaches the
  budget, and it *cost* progress — 21/55 nets and 127/161 connections without
  matching, 17/55 and 123/161 with it, in the same capped 20 minutes.
- **The comparison is confounded, and the confound is ours.** Neither run
  finishes the board (kct 17/55, KRT 39/55), so some of every spread is
  "partially routed net", not "unmatched net". kct is the more confounded of
  the two because it got less far. This is why the finding is filed as a
  *measurement-first* follow-up (**#5798**, Epic #5784 phase 4) rather than
  "port KRT's matcher".
- **SDRAM_ADDRESS resists both tools** (39–44 mm for KRT, 23 mm for kct). It is
  the largest group (14 nets) and the one whose members are least completely
  routed in both runs.
- **Time matching is not a better length matcher**, which is expected — it
  optimises propagation delay, not copper length — but it is worth recording
  that it is *worse on every group* than the same run in length mode (10.67 /
  16.46 / 44.34 / 17.02), 4 shared-referee errors dearer and 5 s slower. We
  have no delay-domain referee, so this row measures time matching by the
  length yardstick and nothing more.
- **Our matcher is not the weak part.** Board 07's committed artifact reaches
  0.63–2.92 mm on all four groups at 0 DRC errors, using
  `match_group_tuning`/`SerpentineGenerator` in that board's tuned pipeline.
  What fails on 07m is `kct route`'s default reaching a routed board to tune.

**Reference planes.** On board 06, kct put 22.8 mm of OUT4 on the In1.Cu GND
plane. It warned about this, and the fix is the opt-in
`--reserve-plane-layers`. KRT put none there. On board 03, all three results
route through the inner planes: KRT 318 mm, kct 264.5 mm, ref 253.3 mm. KRT
has no systematic plane-avoidance advantage, so we do not claim one.

**Vias.** Where both tools complete, KRT uses fewer vias: 01 (0 vs 4), 02 (14
vs 37, where kct was also incomplete) and 06 (54 vs 55, of which 54 are the
preserved plane vias). On board 03, KRT's 73 vias are fewer than the committed
artifact's 153. Board 04 inverts this, and the inversion is informative: kct's
14 vias beat KRT's 31, but only because kct poured three nets it never routed.
Against the 12/12 rows, the committed artifact uses 29 and KRT-with-fanout 17.

**Runtime.** Both times are end to end, including each tool's post-route
passes (KRT: cleanup, KiCad refill, oracle recheck; kct: auto-pour, zone fill,
DRC validation, sidecars). On boards 00–03 and 06 (pass 1) KRT is about 4–40×
faster: 0.55–7.8 s against 7.2–105 s. On boards 00 and 01 (3 nets each), kct's
roughly 7 s end to end is almost all pipeline work around a trivial route.
`uv run kct --version` alone takes 0.25 s. On board 03, the kct log shows the
time going to negotiated iterations that rip and reroute the same 12 nets each
round.

**On the dense boards (pass 2) the gap stops being a ratio and becomes a
cap.** kct did not finish 05, 07a or 07m in 20 minutes; KRT finished them in
13.35 s, 67.76 s and 100.14 s. Board 05's kct log accounts for the whole
budget in two negotiated iterations (519 s, then 640 s). Board 04, which kct
does finish, is 17.29 s against 5.99 s — the same order of magnitude as pass 1.

Three caveats on every number in this column, all of which cut the same way:
timing is a **single run**; pass 2's host is a **shared 8-vCPU** cloud worker
with other jobs on it, so its absolute seconds are soft; and we attribute the
gap to no specific technique. The *timeouts*, by contrast, are robust to all
three — a 2× contention factor does not turn 20 minutes into 13 seconds.

**A benchmark-harness defect found and fixed while measuring pass 2.** The
script's per-run cap used `subprocess.run(timeout=…)`, which kills only the
direct child. Every kct run is `uv run kct route …`, so the router is a
*grandchild*: on timeout `uv` died and the router kept running at full CPU,
reparented, for as long as it liked. A board-07 kct run was observed alive 31
minutes into a 20-minute cap, contending with the KRT and length-matching runs
measured after it. Every pass-2 row was re-measured after the fix; the
inflation it had caused was roughly 2× — 05 KRT read 25.17 s before the fix and
13.35 s after, 07a KRT 130.34 s before and 67.76 s after.

The fix has two halves, and the second is the one that makes the numbers
trustworthy: each run now gets its **own process group**
(`start_new_session=True`) which is signalled SIGTERM→SIGKILL as a group, and
the call **blocks until that group is actually empty** rather than until the
direct child is reaped. The next board therefore starts on an idle machine by
construction. Regression test: `tests/test_krt_compare_timeout.py`.

## Reproduce

```bash
# KRT at the pinned commit, outside this repo (never committed here)
git clone https://github.com/drandyhaas/KiCadRoutingTools.git "$KRT"
git -C "$KRT" checkout 64df3f582e8a862c9289205b5a608466bf21a7ba
(cd "$KRT" && python3 build_router.py)            # prebuilt grid_router v0.22.0
uv venv --python 3.12 "$KRT/.venv"
uv pip install --python "$KRT/.venv/bin/python" -r "$KRT/requirements.txt"

# kicad-tools side, from a worktree (never writes under boards/)
uv run kct build-native && uv run kct build-native --check
uv run python scripts/research/krt_compare.py --krt-dir "$KRT" --work-dir /tmp/krt-bench
# Re-grade existing outputs without re-routing:
uv run python scripts/research/krt_compare.py --krt-dir "$KRT" --work-dir /tmp/krt-bench --rescore
```

The script copies each unrouted board and its `.kicad_pro` into `--work-dir`.
It writes `results.json` there, keeps per-run logs in `<board>/<tool>/run/run.log`,
and prints the table above. It sets no tool options beyond the recipes listed.

**The command above runs exactly the board set in the table**, and that is
structural rather than a promise: `--boards` defaults to `",".join(BOARDS)`, the
command passes no `--boards`, and `tests/test_krt_compare_timeout.py::
test_documented_reproduce_command_matches_the_measured_board_set` fails the
build if either half drifts. Adding a board to the table and adding it to the
reproduce command are therefore the same edit.

Budget about **80 minutes** of wall clock for a full run, almost all of it the
three `kct route` runs that hit the 20-minute cap (05, 07a, 07m). Narrow it
with `--boards` while iterating; re-grade without re-routing with `--rescore`.

## Recommended follow-ups

These are proposals only. The orchestrator files them serially after review.
Each one is backed by a measured row above. Follow-ups 1–3 came out of pass 1
and are Epic #5784's phases 1–3 (#5785, #5786, #5787); follow-up 4 is the only
one pass 2 added, filed as #5798.

**Pass 2 added exactly one.** That is the honest summary of boards 04, 05 and
07: on the dense boards KRT is fast but produces output that is
DRC-invalid (04, 05) or electrically shorted (07), its escape pre-pass is a
wash once its width policy is accounted for, and its time matching is worse
than its own length matching. Only the length matcher earned a follow-up — and
even that one starts as a measurement, because our own run never finished the
board.

1. **KiCad-oracle completion for pour nets.**
   - **Target:** boards 02 and 03. Metric: kicad-cli unconnected from 2 and
     24 to 0 under `kct route` defaults. `NetStatusAnalyzer`'s
     advisory-plane-residual verdict should agree with kicad-cli, or the CLI
     exit code should reflect the difference.
   - **Technique:** after the pour fill, read `unconnected_items` from our
     existing `run_geometric_drc` call. Route exactly those links with
     `--complete`-style fixed-obstacle routing, and repeat for up to N rounds
     (KRT: `py_router/kicad_oracle.py:oracle_reconnect`, plus the plane
     finalize that welds pads into fills).
   - **Port or reimplement:** reimplement. We already have the kicad-cli
     invocation and a completion pass. Only the loop and the link-level
     targeting are new. Credit KRT for the design in the docstring.

2. **Pose-based centerline search for coupled diff pairs.**
   - **Target:** board 06b. Metric: minimum pair coupling from 0% to ≥ 85%,
     with 18/18 nets and 0 shared-referee errors kept. Secondary: board 06's
     diff-pair CI job runtime (#5617). This relates to #5333, #5541 and #5700.
   - **Technique:** route one centerline with (x, y, θ, layer) state, 45°
     heading steps and a Dubins-length heuristic. Derive P and N as offsets,
     start from angle-scanned setbacks, and fall back to hybrid (coupled
     trunk plus single-ended legs) instead of fully independent legs.
   - **Port or reimplement:** `rust_router/src/dubins.rs` (DubinsCalculator)
     is small and self-contained, so it can be **ported to C++ with
     attribution**. Our Grid3D and clearance kernel differ, so the pose A\*
     itself should be reimplemented in `coupled_pathfinder.cpp`.
   - **Status (Issue #5786):** shipped opt-in (`KCT_POSE_CENTERLINE=1`) for
     single-layer pairs. With it on, 06b minimum coupling goes from 0% to
     87.0%, with 18/18 nets and 0 shared-referee errors. See "Opt-in fix
     after this benchmark ran (Issue #5786)" above.
   - **Status (Issue #5895):** on by default (`KCT_POSE_CENTERLINE=0` opts
     out). A corridor guard probes the nets next to a trunk's end pads before
     it is committed and declines a trunk that would seal one; on board 06 it
     declines the MIPI_D0 trunk that stranded MIPI_RST, so the Diff-Pair job
     passes (21/21) while 06b keeps 87.0% minimum coupling.

3. **Crossing-aware default ordering plus frontier-attributed N+1 rip-up.**
   - **Target:** boards 02 and 03. Metrics: completion (12/12, 27/27), vias
     (02: 37 → at or below the committed 28) and runtime (02: 50 s, 03: 105 s,
     against KRT's 1.2 s and 7.8 s).
   - **Technique:** first, measure whether turning on our existing inversion
     counters (`monotone_certificate`, `bundle_river`) as the default order
     moves these metrics. Then prototype KRT-style sequential rip-up:
     - attribute frontier cells to blockers, using the fastest-failing
       direction;
     - escalate N+1 with the slot-0 guard;
     - add ripped-corridor soft cost;
     - revert a net-worse run with an improvement gate.

     This could be an alternative to whole-set negotiated iterations on small
     boards. KRT: `blocking_analysis.py:analyze_frontier_blocking`,
     `reroute_loop.py:run_reroute_loop`, `obstacle_costs.py:compute_ripped_route_costs`,
     `improvement_gate.py`.
   - **Port or reimplement:** reimplement, because the architectures differ.
     This proposal should start as a measurement task, because the runtime gap
     is not yet attributed to a cause.

4. **Group length matching that actually reaches the declared budget
   (added by pass 2, #5790; filed as #5798).**
   - **Target:** board 07, the `07m` recipe. Metric: per-group spread for
     SDRAM_BYTE0 / BYTE1 / COMMAND at or below the board's declared 5.0 mm,
     from today's 29.43 / 38.04 / 32.82 mm — without losing the connections
     that enabling matching currently costs (127/161 → 123/161).
   - **Technique:** **measure first, port nothing yet.** The honest reading of
     07m is that KRT's matcher reached 6.34 / 4.34 / 2.69 mm on three groups
     where ours reached 29.43 / 38.04 / 32.82 — but on a board where neither
     tool finished routing (kct 17/55 nets, KRT 39/55 with 56 shorts), so part
     of every spread is unrouted net rather than unmatched net. Step 1 is to
     separate those: re-run 07m against a *completed* board (board 07's own
     tuned pipeline reaches 161/161 and 0.63–2.92 mm) and see how much of the
     gap survives. Only then is it worth comparing
     `match_group_tuning.tune_match_group_v2` against
     `py_router/length_matching.py:apply_length_matching_to_group` mechanism by
     mechanism — the candidate differences being KRT's trombone meander
     generator, its via-barrel-inclusive length metric (`pair_leg_metric`), and
     its choice to run matching between MST phases on multi-point nets.
   - **Port or reimplement:** undecided, deliberately. This starts as a
     measurement task, like follow-up 3.

Not proposed:

- **KRT's QFP/QFN escape pre-pass** (`qfn_fanout.py`). **Measured** (04f, 05f)
  and the result is split: −45 % vias and −59 % fab-referee errors on board 04,
  but +88 shared-referee errors on board 05, because it lays escape copper at
  its own default widths (0.100–0.127 mm track, 0.10 mm clearance) instead of
  the board's net class. Our escape router is already inside `kct route` and
  already net-class-aware. Nothing to adopt; the via-reduction *idea* is
  already ours.
- **KRT time matching** (`--time-matching`). **Measured** (07t) and worse than
  KRT's own length mode on all four groups by the only yardstick we have. We
  have no propagation-delay referee, so we cannot say it fails at its actual
  objective — only that nothing here argues for it.
- **BGA fanout.** Still unmeasured, and **not measurable in this repo**: no
  board has a BGA (Scope lists the densest packages). This is now a fixture
  gap, not a scope cut. Revisit with the Epic #4932 external boards.
- **Placement and the design brief.** Still unmeasured. Every committed board
  has fixed placement, and re-placing one changes the input both routers see,
  so it is a different experiment — see row 7.
- **Routing plans.** Different concept; no metric.
- **AI skills.** No metric.
- **Distribution (PCM, prebuilt binaries).** Useful input for Epic #5774, but
  not a routing technique.

Incidental kct findings from this benchmark. These did not come from KRT, and
triage should decide whether to file them:

- **`--preserve-existing` help text.** The help says "only unconnected nets
  are routed". In practice, already-complete nets inside the route set are
  re-routed and their copper is dropped (06a lost its coupled LVDS geometry).
  Filed and fixed as Issue #5788: already-complete nets are now held out of the
  route set, and the help text states the contract (including that `--nets` /
  `--region` / `--complete` still re-route what they name).
- **Plane layers on board 06.** kct's default routes signals on board 06's
  declared reference planes. It warns, but `--reserve-plane-layers` is off.
  Board 05 emits the same warning for In1.Cu/In4.Cu and then puts 183.5 mm of
  signal there; board 07a, 294.1 mm.
- **`kct route`'s default does not finish boards 05 or 07 in 20 minutes**
  (new in pass 2). Board 05 reaches 87/142 connections, board 07a 127/161 (from
  an input that already had 119), board 07m 123/161. The same boards' committed
  artifacts complete, using board-tuned recipes (`--timeout 900
  --early-stop-patience 4`, #3096/#3111). The time is visibly going to
  negotiated iterations that rip 10–16 nets and take 500–1000 s each.
- **The escape diagnostic calls exposed-pad packages "BGA"** (new in pass 2).
  Board 05's log prints `Escape routes: U2 (BGA)` and `U3 (BGA)`, but U2 is a
  TI HTSSOP-28 with a thermal exposed pad and U3 an HVSSOP-8; neither is a BGA.
  Cosmetic, but it is exactly the log line a reader would cite as evidence a
  BGA had been exercised — as this document nearly did.

## Licensing

KRT's `LICENSE` at the pinned commit is MIT ("Copyright (c) 2026 drandyhaas").
This repo is MIT too (`LICENSE`, "Copyright (c) 2024 RJ Walters";
`pyproject.toml` `license = "MIT"`), so the licenses are compatible. Any ported
code, such as the Dubins calculator in follow-up 2, must keep KRT's copyright
and permission notice. Put it in the ported file's header and in a
third-party notice. Reimplementations that only follow KRT's documented design
need no notice, but should cite KRT in their docstrings.

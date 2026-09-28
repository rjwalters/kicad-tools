# KiCadRoutingTools (KRT) vs kicad-tools: comparison and benchmark (#5781)

[drandyhaas/KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools)
("KRT") is an MIT-licensed Python and Rust A\* autorouter for KiCad 9 and 10.
This note compares its techniques with ours, benchmarks both tools on our demo
boards under one DRC referee, and lists the follow-ups that the measurements
support.

This is research only. KRT is not vendored, not a dependency, and not wired
into CI. No router or placement code changes with this document.

**Pinned versions**

| Item | Value |
|---|---|
| KRT commit | `64df3f582e8a862c9289205b5a608466bf21a7ba` (2026-09-28, `VERSION` 0.22.1) |
| KRT Rust core | prebuilt `grid_router` v0.22.0 (`build_router.py` downloads it from release v0.22.1, macOS arm64) |
| KRT Python env | CPython 3.12.13, numpy 2.5.3, scipy 1.18.1, shapely 2.1.2 (separate venv, `requirements.txt`) |
| kicad-tools | `ae264953` (`kct` 0.22.0), C++ backend built (`kct build-native --check`: router 1.0.0, build 45) |
| Referee | `kicad-cli` 10.0.6 |
| Host | Apple M3 Ultra, 28 cores, macOS (Darwin 27) |

Citations below use `path:function` at these commits. KRT paths are relative
to the KRT repo root; ours are relative to this repo.

## Scope

We benchmarked boards 00, 01, 02, 03 (general single-ended routing) and board
06 (differential pairs), following the curator's scope cut on #5781. Board 06
runs in two variants:

- **06a** is the board's real input. The generator pre-routes the four LVDS
  pairs and the GND and +3V3 plane vias, so only the eight LVTTL nets are open.
- **06b** is the diff-pair test itself. The script rips the LVDS copper (64
  segments) and keeps the plane vias, so each tool must route the four pairs.

**Deferred to a follow-up:** boards 04, 05 and 07, and the external boards from
Epic #4932 (STRF, PocketBeagle, BeagleConnect). We did not run KRT's
`bga_fanout.py` because no board in scope has a BGA. The length-matching and
fanout rows below are therefore unmeasured. We did not silently drop them.

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

**What matters.** Nothing was measured here: board 07 is deferred. KRT's
explicit via-barrel length and its time-domain matching are the two features
we do not obviously have. Check them when board 07 runs.

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
escaped". That diagnostic was already filed as #3900.

**What matters.** Nothing was measured: boards 04 and 05 are deferred.

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

**What matters.** Nothing was benchmarked. The committed boards have fixed
placement. KRT's machine-readable "design brief" (connector edges and intent)
is a constraint-capture idea, not an optimizer. It is the only part that looks
novel relative to us.

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
| 00, 01, 02, 03 | `kct route IN -o OUT` | `route.py IN OUT` |
| 06a | `kct route IN -o OUT --preserve-existing` | `route.py IN OUT` |
| 06b | `kct route IN -o OUT --preserve-existing --differential-pairs` | `route_diff.py IN MID --nets 'LVDS*'` then `route.py MID OUT` |

Notes on the recipes:

- **06a/06b copper.** KRT keeps existing copper by default. kct needs
  `--preserve-existing`, its documented incremental-routing flag, to keep the
  plane vias.
- **KRT diff-pair order.** "`route_diff.py` first, then `route.py` for the
  rest" is KRT's own README recipe.
- **kct defaults** include auto-pour on 2-layer boards, auto layer escalation,
  and JLCPCB fab floors.
- **KRT defaults** include `--escalation fab`, `--fab-tier auto`, MPS
  ordering, and in-run plane finalize.
- **Committed artifacts.** The `ref` rows grade each board's committed
  `*_routed.kicad_pcb`, produced by that board's own tuned pipeline, as a
  third point of comparison. They were not regenerated.

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

### Results

Every cell is a single run. No run hit the 20-minute cap. The results JSON
holds all DRC classes.

| Board | Tool | Nets complete | Connections | kicad-cli unconnected | DRC errors (shared) | DRC errors (fab) | DRC errors (as emitted) | kct check errors | Vias | Wirelength mm | Signal on plane layers mm | Pair coupling % (min) | Runtime s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 00 | kct | 3/3 | 3/3 | 0 | 0 | 0 | 0 | 0 | 0 | 8.71† | 0.0 | -- | 7.43 |
| 00 | krt | 3/3 | 3/3 | 0 | 0 | 0 | 0 | 0 | 0 | 28.1 | 0.0 | -- | 0.76 |
| 00 | ref | 3/3 | 3/3 | 0 | 0 | 0 | n/a | 0 | 0 | 8.6 | 0.0 | -- | n/a |
| 01 | kct | 3/3 | 5/5 | 0 | 0 | 0 | 0 | 0 | 4 | 68.18 | 0.0 | -- | 7.23 |
| 01 | krt | 3/3 | 5/5 | 0 | 0 | 0 | 0 | 0 | 0 | 59.62 | 0.0 | -- | 0.55 |
| 01 | ref | 3/3 | 5/5 | 0 | 0 | 0 | n/a | 0 | 2 | 34.69 | 0.0 | -- | n/a |
| 02 | kct | 11/12 | 34/36 | 2 | 0 | 0 | 0 | 0 | 37 | 412.4† | 0.0 | -- | 49.79 |
| 02 | krt | 12/12 | 36/36 | 0 | 0 | 0 | 0 | 3 (via_in_pad 3) | 14 | 516.33 | 0.0 | -- | 1.24 |
| 02 | ref | 12/12 | 36/36 | 0 | 0 | 0 | n/a | 0 | 28 | 572.21 | 0.0 | -- | n/a |
| 03 | kct | 24/27 | 91/115 | 24 | 161 (track_width 161) | 0 | 0 | 0 | 41 | 659.71 | 264.5 | -- | 105.43 |
| 03 | krt | 27/27 | 115/115 | 0 | 0 | 0 | 0 | 46 (via_in_pad 46) | 73 | 565.03 | 318.0 | -- | 7.79 |
| 03 | ref | 27/27 | 115/115 | 0 | 168 (drill_out_of_range 74, track_width 20, via_diameter 74) | 0 | n/a | 45 (via_in_pad 45) | 153 | 893.69 | 253.3 | -- | n/a |
| 06a | kct | 18/18 | 90/90 | 0 | 0 | 0 | 0 | 0 | 55 | 487.01 | 22.8 | 0.0 | 20.86 |
| 06a | krt | 18/18 | 90/90 | 0 | 0 | 0 | 0 | 0 | 54 | 484.55 | 0.0 | 88.9 | 4.47 |
| 06a | ref | 18/18 | 90/90 | 0 | 0 | 0 | n/a | 0 | 63 | 503.11 | 0.0 | 87.1 | n/a |
| 06b | kct | 18/18 | 90/90 | 0 | 0 | 0 | 0 | 0 | 55 | 487.01 | 22.8 | 0.0 | 22.65 |
| 06b | krt | 18/18 | 90/90 | 0 | 0 | 0 | 0 | 0 | 54 | 477.42 | 0.0 | 88.9 | 5.75 |
| 06b | ref | 18/18 | 90/90 | 0 | 0 | 0 | n/a | 0 | 63 | 503.11 | 0.0 | 87.1 | n/a |

† kct auto-poured GND/VCC on boards 00 and 02, so those nets are copper fill,
not track. Wirelength is not comparable across tools on those rows.

### Reading the table honestly

**Completion (boards 02, 03).** KRT completes every scoped board under the
shared referee. kct's default leaves pour-net pads stranded: 2 on board 02 and
24 on board 03. Every one is on a net kct chose to pour instead of route
(`Auto-skip: GND, VCC (pour nets — use zone fill)`). Our own net-status calls
them advisory, and kct exits 0. That discrepancy is itself a finding: it ties
back to the process rule in `CLAUDE.md` that `kct check` alone is not
sufficient. The committed `ref` artifacts complete, but only because each
board's tuned pipeline adds stitching and repair passes on top of `kct route`.

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

In 06a, kct also discarded the generator's pre-routed, coupled LVDS copper,
even with `--preserve-existing`. That flag only preserves nets outside the
route set, which is why board 06's own README passes `--nets IN1,…,OUT4`. The
06a and 06b outputs are therefore identical. KRT kept the pre-routed pairs
(its cleanup passes simplified them from 8 to 4 segments per leg).

**Reference planes.** On board 06, kct put 22.8 mm of OUT4 on the In1.Cu GND
plane. It warned about this, and the fix is the opt-in
`--reserve-plane-layers`. KRT put none there. On board 03, all three results
route through the inner planes: KRT 318 mm, kct 264.5 mm, ref 253.3 mm. KRT
has no systematic plane-avoidance advantage, so we do not claim one.

**Vias.** Where both tools complete, KRT uses fewer vias: 01 (0 vs 4), 02 (14
vs 37, where kct was also incomplete) and 06 (54 vs 55, of which 54 are the
preserved plane vias). On board 03, KRT's 73 vias are fewer than the committed
artifact's 153.

**Runtime.** Both times are end to end, including each tool's post-route
passes (KRT: cleanup, KiCad refill, oracle recheck; kct: auto-pour, zone fill,
DRC validation, sidecars). KRT is about 4–40× faster on every board: 0.55–7.8 s
against 7.2–105 s. On boards 00 and 01 (3 nets each), kct's roughly 7 s end
to end is almost all pipeline work around a trivial route. `uv run kct
--version` alone takes 0.25 s. On board 03, the
kct log shows the time going to negotiated iterations that rip and reroute the
same 12 nets each round. Timing is a single run on one host, and we attribute
it to no specific technique.

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

## Recommended follow-ups

These are proposals only. The orchestrator files them serially after review.
Each one is backed by a measured row above.

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

Not proposed:

- **Unmeasured rows.** Length and time matching, BGA fanout, placement and the
  design brief were not measured, so there is no evidence they beat ours. Board
  04, 05 and 07 follow-up runs should revisit them.
- **Routing plans.** Different concept; no metric.
- **AI skills.** No metric.
- **Distribution (PCM, prebuilt binaries).** Useful input for Epic #5774, but
  not a routing technique.

Incidental kct findings from this benchmark. These did not come from KRT, and
triage should decide whether to file them:

- **`--preserve-existing` help text.** The help says "only unconnected nets
  are routed". In practice, already-complete nets inside the route set are
  re-routed and their copper is dropped (06a lost its coupled LVDS geometry).
- **Plane layers on board 06.** kct's default routes signals on board 06's
  declared reference planes. It warns, but `--reserve-plane-layers` is off.

## Licensing

KRT's `LICENSE` at the pinned commit is MIT ("Copyright (c) 2026 drandyhaas").
This repo is MIT too (`LICENSE`, "Copyright (c) 2024 RJ Walters";
`pyproject.toml` `license = "MIT"`), so the licenses are compatible. Any ported
code, such as the Dubins calculator in follow-up 2, must keep KRT's copyright
and permission notice. Put it in the ported file's header and in a
third-party notice. Reimplementations that only follow KRT's documented design
need no notice, but should cite KRT in their docstrings.

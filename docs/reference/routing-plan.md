# RoutingPlan sidecar (`<output_stem>.routing_plan.json`)

> Issue #5519 / #5520 / #5575, Phase 1 of 4 of Epic #5510
> ("capacity-aware routing plan on the default path — layer assignment,
> hard corridors, and an infeasibility certificate"). This phase is
> **report-only**: the sidecar serializes state the tile-based global
> router computes; it never changes which copper `kct route` produces.

> **Not to be confused with** `boards/03-usb-joystick/routing_plan.py` /
> `boards/03-usb-joystick/routing-plan.json`, an unrelated board-03
> copper replay recipe that predates this epic.

## When it is written

**Every default `kct route` writes the sidecar** (Issue #5520, Phase 1b).
The plan stage runs on both routing paths:

- **Dense-package boards** (BGA / fine-pitch QFP — see
  `Autorouter.detect_dense_packages`) reach the tile-based two-phase
  global router (`route_with_escape` -> `route_all_two_phase` ->
  `TwoPhaseRouter.route_all`), whose Phase 1 *is* the plan stage.
- **Every other board** gets the same stage from
  `Autorouter.route_all_negotiated`, which calls
  `Autorouter.plan_routing()` before any copper is committed. Hooking the
  core method rather than each CLI dispatch site covers all seven
  `route_all_negotiated` call sites in `route_cmd.py` — the three
  escalation wrappers (`route_with_layer_escalation`,
  `route_with_rule_relaxation`, `route_with_combined_escalation`) *and*
  the fixed-layer closure a `--layers N` / `--no-auto-layers` run falls
  into — plus `route_with_progressive_clearance` and library callers.

```
kct route boards/00-simple-led/output/simple_led.kicad_pcb \
    -o simple_led_routed.kicad_pcb
# writes simple_led_routed.routing_plan.json alongside the PCB
```

Both paths go through the same `routing_plan.select_plan_nets` /
`routing_plan.build_plan` helpers, so a board's plan does not depend on
which path it took: the same net universe (pour nets and single-pad nets
filtered by the same rules and reported with their own `status`), the same
tile sizing, the same negotiated global pass.

The stage never runs twice: the hook is guarded on `routing_plan is None`,
so a board that already planned via the two-phase path does not plan again
if a later fallback calls `route_all_negotiated`. Each layer-escalation
attempt builds a fresh `Autorouter` (via `load_pcb_for_routing`), so every
attempt still gets its own plan and the **last** attempt's plan is the one
written.

### `--no-routing-plan`

`--no-routing-plan` is the only flag this phase adds, and it is the only
escape hatch (there is deliberately no positive opt-in flag — the plan is
on by default):

```
kct route board.kicad_pcb -o routed.kicad_pcb --no-routing-plan
# no <stem>.routing_plan.json, no summary line, no routing_plan JSON key
```

It sets `Autorouter.emit_routing_plan = False` (applied right after every
`load_pcb_for_routing` call site). Because the stage is report-only, the
flag changes **no copper** — it only saves the stage's wall clock and
suppresses the sidecar.

A blocked write (e.g. a read-only output directory) prints a warning and
the route step still exits 0 -- exactly like the other post-route
sidecars (`net_class_map.json`, `current_paths.json`).

### Why it cannot change routed copper

The plan stage builds its own coarse `RegionGraph` and runs `GlobalRouter`
over it. It never touches `Autorouter.grid`: no `set_corridor_preference`,
no obstacle marking, no RNG draw — and on the `route_all_negotiated` hook
it prints nothing (the text summary and the sidecar remain the CLI's job,
so the many unit tests that call `route_all_negotiated` directly keep
their stdout assertions). The only object it mutates is the
freshly-constructed `RegionGraph` it also owns.

**Verifying that claim needs the determinism protocol.** A bare `kct route`
is not reproducible run-to-run *at all*: its iteration budget is
wall-clock-based, so an A* search sitting near a budget boundary lands
different copper on an otherwise identical invocation. Measured on board 01
(2026-09-19): three unflagged runs produced three distinct copper sets
**with the plan stage disabled**. Any "with vs without `--no-routing-plan`"
comparison must therefore pin the budget the way `--deterministic-budget`
(#3538 / #3799) was built for:

```
PYTHONHASHSEED=0 kct route board.kicad_pcb -o out.kicad_pcb \
    --seed 42 --deterministic-budget [--no-routing-plan]
```

The slow tests in `tests/test_routing_plan_5510.py` use exactly these flags
and ship a separate "route twice with the stage off" control so a failure
distinguishes "the plan stage changed copper" from "the protocol stopped
working".

### The instrument is the copper set, not the file

**Do not compare whole `.kicad_pcb` text**, even with UUIDs normalised — it
does not measure "did the plan stage change copper", and getting this wrong
is what produced the (retracted) report in
[#5578](https://github.com/rjwalters/kicad-tools/issues/5578). Two things in
the file vary run-to-run on their own, with the plan stage **off in both
runs**:

1. **Element emission order.** Board 06, two stage-off runs (2026-09-19):
   2365 diff lines whole-file, yet the multiset of normalised lines was
   byte-identical — the two files were pure permutations of each other.
2. **Zone pour-fill island decomposition.** Board 03's `In2.Cu` plane filled
   as 5 islands on three runs and 20 on a fourth; the fourth happened to be a
   stage-**on** run, which is how the plan stage gets blamed for it. Repeating
   the stage-on route reproduced 5 islands with an identical copper set, so
   the fragmentation is pre-existing pour-fill variance. Pours are filled
   after routing and flow around finished traces — they are not routed copper.

`tests/test_routing_plan_5510.py::_copper_elements` therefore compares the
**sorted multiset of whole `(segment ...)` / `(via ...)` / `(arc ...)`
nodes** (geometry, width, layer and net included; `uuid`/`tstamp` normalised;
emission order and pour fills excluded). Under that instrument every fleet
board measured on 2026-09-19 — **00, 01, 02, 03, 04 and 06** — routes an
identical copper set with and without `--no-routing-plan`, so the assertion
is enforced, not skipped.

`scripts/ci/board_route_determinism_smoke.sh`'s `normalize_copper()` now
applies the same instrument (it shells out to `scripts/ci/normalize_copper.py`,
a paren-balanced whole-node normalizer). Until
[#5580](https://github.com/rjwalters/kicad-tools/issues/5580) it did *not*:
this repo writes copper as multi-line s-expressions, so its
`grep -E '^[[:space:]]*\((segment|via|arc)'` kept only the bare `(segment` /
`(via` header lines and discarded every `(start …)` / `(layer …)` child. Its
output was therefore one line per copper element drawn from a handful of
*identical* strings — on board 03, 1793 lines holding 3 distinct values
(measured 2026-09-19 at `0c261d41`; #5580 measured 1913 lines = 1872 segments
+ 41 vias on an earlier route). The element **count** was the only thing that
could differ. Do not reintroduce a line-based filter in either place.

Board 05 is out of scope for this comparison by construction: its recipe is a
wall-clock re-route loop that the repo's own determinism smoke deliberately
excludes as nondeterministic (#3894), and a bare `kct route` on it does not
converge within an hour on the fleet host. Its plan stage was measured
directly instead (`Autorouter.plan_routing()` on the committed unrouted
board: 37 nets, `elapsed_s` 0.057 s, total overflow 4).

## Text and JSON output

Text mode prints one summary line:

```
Routing plan: 24 nets, 41 edges, overflow 3 on 2 edges (0.4s)
```

`--format json` adds a `routing_plan` key (present only when a plan was
built -- absent, not `null`, otherwise):

```json
{
  "routing_plan": {
    "overflow_report": { "iterations": 15, "total_overflow": 3, "overflowed_edges": 2,
                          "failed_nets": [], "feasible": false, "elapsed_s": 0.4 },
    "sidecar": "/path/to/usb_joystick_routed.routing_plan.json"
  }
}
```

## Schema (`schema_version: 1`)

```json
{
  "schema_version": 1,
  "source": {
    "pcb": "usb_joystick_routed.kicad_pcb",
    "kct_version": "0.20.0",
    "layer_stack": "4-Layer SIG-GND-PWR-SIG",
    "tile_mm": 4.0,
    "cols": 40,
    "rows": 25
  },
  "layers": { "signal": [0, 3], "plane": [1, 2] },
  "regions": {
    "17": { "row": 3, "col": 5, "min_x": 20.0, "min_y": 12.0, "max_x": 24.0, "max_y": 16.0 }
  },
  "nets": {
    "12": {
      "name": "/DQ3", "class": "DDR", "pitch_mm": 0.35,
      "layer_set": [0], "region_path": [17, 18, 58], "status": "assigned"
    }
  },
  "edges": [
    {
      "a": 17, "b": 18, "capacity": 9, "demand": 14.0, "overflow": 5,
      "blockage_mm": 1.2, "nets": [12, 13, 14],
      "layers": { "0": { "capacity": 5, "demand": 8.0 }, "3": { "capacity": 4, "demand": 6.0 } },
      "refs_a": ["U1", "C4"], "refs_b": ["U3"]
    }
  ],
  "overflow_report": {
    "iterations": 15, "total_overflow": 5, "overflowed_edges": 1,
    "failed_nets": [], "feasible": false, "elapsed_s": 0.4
  },
  "relief": [
    { "edge": [17, 18], "kind": "move_component", "ref": "U3",
      "dx": 2.0, "dy": 0.0, "expected_overflow": 0 },
    { "edge": [17, 18], "kind": "add_signal_layer", "layer_index": 1,
      "expected_overflow": 1 },
    { "edge": [17, 18], "kind": "swap_pins", "deferred": "#5511" }
  ],
  "relief_meta": {
    "candidates_evaluated": 9, "elapsed_s": 1.25, "truncated": false,
    "baseline_overflow": 5, "budget_s": 5.0
  }
}
```

### Top-level fields

- **`source`** -- provenance. `pcb` is filled in by the CLI writer (unset,
  i.e. `null`, when a `RoutingPlan` is built and inspected in-process
  without going through `write_sidecar`). `tile_mm` / `cols` / `rows`
  describe the coarse tile grid the plan was computed on.
- **`layers.signal` / `layers.plane`** -- layer indices by role, and since
  Issue #5575 **the set capacity is actually allocated on**. `signal` is
  deliberately *not* `LayerStack.signal_layers`: that property filters on
  `LayerDefinition.is_routable`, which returns `True` unconditionally --
  PLANE layers included, by design (#5014). The plan uses the stricter
  "routable **and** not a plane" set
  (`routing_plan.signal_layer_indices()`), so a `sig/gnd/pwr/sig` stack
  reports `"signal": [0, 3]` and the two planes carry **zero** capacity on
  every edge (they get no `layers` row at all). The round-robin layer
  assignment indexes into this same list, so no net is ever planned onto a
  plane. This is a *planning* restriction only -- what the detailed router
  is allowed to do is still `--reserve-plane-layers`' business.
- **`regions`** -- bounds (mm) for every coarse-grid region referenced by
  at least one net's `region_path`. Regions the plan never touches are
  omitted to keep the sidecar bounded on large tile grids.
- **`nets`** -- one entry per net considered by the global pass (assigned,
  failed, or filtered out before the pass -- see `status` below).
  - `pitch_mm` is `trace_width + clearance` from the net's resolved
    net-class entry, or the design-rule default when the net has none.
  - `layer_set` is a list from day one. Phase 1 always emits a single
    round-robin layer (`[0]`, `[3]`, ...); later phases may assign
    multiple layers per net.
  - `region_path` is the sequence of region IDs the net's corridor
    traverses (empty when the net has no corridor).
  - `status` is one of:
    | Status | Meaning |
    | --- | --- |
    | `assigned` | The global router assigned a corridor. |
    | `failed` | The global router attempted but failed to route this net (no corridor). |
    | `pour_skipped` | Filtered out before the global pass as a pour net (zone-filled, not routed as a signal). |
    | `single_pad` | Filtered out before the global pass as trivially-connected (fewer than 2 pads). |
    | `no_endpoints` | Passed to the global pass as a candidate, but fewer than two of its pads resolved to a position, so `GlobalRouter.route_all` silently dropped it (neither assigned nor failed). |
- **`edges`** -- one row per *undirected* region-graph edge that carries
  at least one net (edges with zero demand are omitted). Each pair is
  backed by two directed `RegionEdge` objects, and `update_utilization()`
  only bumps the one matching a corridor's traversal direction, so
  `demand` / `overflow` sum **both** directions -- matching
  `RegionGraph.get_total_overflow()` / `get_overflowed_edges()` no matter
  which direction carried the traffic (issue #5544). `overflow` is
  therefore the per-direction sum of `max(0, utilization - capacity)`,
  **not** `max(0, demand - capacity)`. `capacity` / `blockage_mm` are read
  from the ascending edge (the one whose `source` is the smaller region
  ID) alone, because they are symmetric across the pair by construction.
  `layers` gives the same capacity/demand split per layer index (`demand`
  likewise summed across both directions; empty `{}` on a single-layer
  graph, and never a row for a PLANE layer).

  `refs_a` / `refs_b` (Issue #5521) name the component refs with at least
  one pad in region `a` / `b`, ranked by how many of *this edge's*
  crossing nets they touch (ties broken lexically). They are populated
  **only on overflowed edges** — the ranking is what names the "nearest
  component on each side" in the report text and what seeds the relief
  search, and indexing every edge would bloat the sidecar on a large tile
  grid for no reader. A feasible board carries `[]` on every edge.

  **`demand` is measured in base-pitch units, not net count** (Issue
  #5575). The base pitch is `DesignRules.trace_width + trace_clearance`;
  a net whose class pitch (`NetClassRouting.trace_width + clearance`) is
  `k` times the base pitch contributes `k`, so a 2.6 mm power trunk on a
  0.4 mm-pitch board costs `2.8 / 0.4 = 7` tracks rather than 1. `demand`
  is consequently a float in the JSON (`15.0`, not `15`); `capacity` and
  `overflow` stay integers, with `overflow` rounding any fractional
  excess **up** (half a track's worth of copper still does not fit).
- **`overflow_report`** -- named `overflow_report`, never "certificate":
  that word is owned by `monotone_certificate.py`'s planarity proof.
  This block is a report of measured congestion, not a proof of
  (in)feasibility. `total_overflow` / `overflowed_edges` are always
  computed from `RegionGraph.get_total_overflow()` /
  `get_overflowed_edges()` directly (never a hand-summed value), so they
  can never drift from what the graph itself reports.
- **`relief`** / **`relief_meta`** -- the measured relief candidates and
  the bookkeeping of the search that produced them (Issue #5521). Both
  are empty on a feasible board: the search runs only when
  `total_overflow` is non-zero, so boards 00-06 pay nothing for it. See
  [Computed relief](#computed-relief-issue-5521) below.

## The capacity model (Issue #5575)

Capacity on a tile boundary is geometric:

```
capacity(edge) = sum over SIGNAL layers l of
                 floor( (edge_length - pad_blockage - rect_blockage[l]) / base_pitch )
```

and a net's demand against it is `class_pitch / base_pitch`. Three
independent inputs, each of which the pre-#5575 model got wrong:

### 1. Per-class pitch

`RegionGraph(pitch_for_net=...)` maps a net id to its class pitch
(`NetClassRouting.trace_width + clearance`, resolved through
`Autorouter.net_class_map`, which is keyed by net *name*). `RegionGraph.
demand_weight(net)` turns that into base-pitch units, and
`GlobalRouter.route_net` places -- and the rip-up path releases -- that
same weight. Nets with no net-class entry weigh exactly `1.0`, so a board
with no `--net-class-map` behaves exactly as it did before.

### 2. Plane-layer exclusion

`RegionGraph(signal_layer_indices=...)` restricts which layer indices get
capacity at all (see `layers.signal` above). `GlobalRouter.route_all`'s
round-robin indexes into the same list -- without that one-line change,
every other net on a `sig/gnd/pwr/sig` board would be assigned to a
zero-capacity plane index and overflow instantly.

### 3. Blockage beyond pads

`RegionGraph.register_blockage_rects(rects, layers)` subtracts, per layer,
the length of the boundary segment each rectangle covers. Two sources
(`routing_plan.collect_blockage_rects`):

| Source | Rectangle |
| --- | --- |
| **Keepout rule areas** | Each track/via-blocking `(zone … (keepout …))`'s polygon **bounding box** (conservative). Parsed once by `Autorouter._keepout_rule_area_polygons()`, the helper the lattice engine's `_lattice_keepout_projection` also consumes, so there is no second reader of `pcb.rule_areas` to drift. |
| **Preserved copper** (`--preserve-existing` / `--nets` / `--complete`) | Each `Segment`'s axis-aligned bbox padded by `width/2 + trace_clearance`; each `Via` a square of `diameter + clearance` on every signal layer. |

Three deliberate exclusions:

- **Copper pours are NOT blockage.** Zones are never loaded into the
  routing grid on the `kct route` path -- pours are filled *after* routing
  and flow around finished traces -- so counting them would make the plan
  stricter than the router it describes and report overflow the detailed
  router never experiences.
- **`spatial_keepouts` per-class filters are ignored** for capacity. A
  per-class exemption cannot be expressed as a scalar edge blockage, so
  the plan treats a filtered rule area as blocking for everyone (the
  conservative direction). Those filters remain fully in force in the
  lattice search, which is what they were built for.
- **The blockage model is boundary-based.** A rectangle strictly inside a
  tile blocks nothing, because capacity is only ever counted *across* tile
  boundaries. Only a rectangle that straddles a boundary reduces it. (This
  is why `tests/test_routing_plan_5510.py`'s channel fixture places its
  walls at `x_boundary ± 0.5`.)

The existing `max(1, …)` floor on an edge's total capacity is kept for the
**pad heuristic** (which estimates blockage from pad sizes and can
overshoot, so flooring keeps the graph connected) but is **not** applied to
an edge carrying measured rectangle blockage: a boundary fully covered by a
keepout or by preserved copper really does have zero capacity, and flooring
it to 1 would let the plan report a net squeezing through a wall.

## The overflow report text (Issue #5521)

`RoutingPlan.format_overflow_report()` turns the `edges` / `relief` data
above into the text a human acts on. It reads **only the plan object**, so
it produces identical output from a plan just built in-process and from one
loaded out of a sidecar with `RoutingPlan.from_dict` — that is what lets a
later consumer (`kct net-status --why`, Phase 1c PR 2) print the same
wording the gate does without re-running anything.

One block per overflowed edge, worst first:

```
Routing plan: overflow 5 on 1 edge(s) -- NOT feasible
corridor U1 -> U3 (tiles 17-18, layer 0): demand 14.0 tracks, capacity 9, overflow 5
  tile 17: (20.000, 12.000)-(24.000, 16.000)
  tile 18: (24.000, 12.000)-(28.000, 16.000)
  nets: /DQ0 /DQ1 /DQ2
  relief: move U3 +2.0/+0.0 mm -> total overflow 0 | add signal layer 1 (currently a plane) -> total overflow 1 | swap_pins deferred (#5511)
  relief search: 9 candidate(s) in 1.2s
```

- **`corridor A -> B`** names the top-ranked ref from `refs_a` and `refs_b`.
  A region with no ref of its own prints its centre instead
  (`tile 17 @ (22.000, 14.000)`), so the block always says *where*.
- **`layer N`** lists only the layer indices whose own demand exceeds their
  own capacity. A single-layer graph carries no per-layer rows at all and
  prints `all layers`.
- The net list is elided after 8 names (a DDR byte stays whole) with a
  `... (N total)` count.

### Computed relief (Issue #5521)

Every relief candidate is **measured, never guessed**: it re-runs the exact
same global pass under one hypothetical change and records the resulting
board-wide `expected_overflow`. Only candidates that beat the baseline are
kept, sorted best-first.

| `kind` | Change evaluated | Keys |
| --- | --- | --- |
| `move_component` | One adjacent component shifted by a single 2.0 mm step in one of the four axis directions | `ref`, `dx`, `dy`, `expected_overflow` |
| `add_signal_layer` | One PLANE index treated as a signal layer | `layer_index`, `expected_overflow` |
| `swap_pins` | **Nothing** — a deferred stub for Issue #5511 | `deferred` |

Three properties this design buys, all of which the tests pin:

- **Nothing is ever moved.** A `move_component` candidate re-plans against
  a *copy* of the pad dict (`dataclasses.replace`), so the router, the grid
  and the PCB are untouched by construction — "placement is restored
  exactly" is a property of the design rather than of a restore path that
  an exception could skip. Relief is **advice**; acting on it is the user's
  call.
- **`add_signal_layer` is advice about capacity**, not a claim that the
  plane can be deleted. It answers "does this board need another routing
  layer's worth of room here?", nothing more.
- **`swap_pins` is a stub only.** No evaluation, no `expected_overflow`, no
  import from the #5511 line. #5522 is the first concrete slice that will
  replace it.

The search is bounded on two axes, because each candidate costs a full
global pass (≈ the plan stage itself):

1. A deterministic cap — at most `RELIEF_MAX_EDGES` (3) overflowed edges ×
   `RELIEF_MAX_REFS` (3) adjacent refs × 4 unit steps, plus at most one
   `add_signal_layer` candidate per plane index.
2. A `RELIEF_BUDGET_S` (5.0 s) wall-clock budget, checked **before** each
   re-plan. Exhausting it stops the search and records
   `relief_meta.truncated = true` rather than reporting a partial search as
   a complete one.

Both are module constants in `router/routing_plan_relief.py`, deliberately
**not** CLI flags — this slice adds exactly one flag (`--plan-gate`).

## `--plan-gate` (Issue #5521)

Off by default; the plan stage stays report-only unless you ask for the
gate. With `--plan-gate`, `kct route` builds the plan, and if
`overflow_report.feasible` is false it prints the report above and exits
**9** *before any detailed routing* — on both the negotiated and the dense
two-phase paths, because the gate is a CLI preflight that runs before the
router picks a path at all.

```bash
kct route board.kicad_pcb --plan-gate          # exit 9 + report when infeasible
kct route board.kicad_pcb --plan-gate --force  # route anyway
```

Exit 9 is shared with `--census-advisory-gate` (#4799); the stderr prefix
(`[plan-gate]` vs `[crosstail-gate]`) says which fired, and the difference
in what was spent is real — the census gate refuses before any router or
component loading, the plan gate after the (sub-5 s) plan stage.

The override is the **existing** `--force`, not a second flag. Note that
`--force` also disables grid/DRC validation: to simply not gate, omit
`--plan-gate` rather than adding `--force`.

The plan the gate builds is discarded afterwards, so the sidecar a
`--plan-gate --force` run writes is still the one the normal in-route stage
produces. That costs one extra plan pass on that path, and buys the
guarantee that the flag cannot change what lands in the sidecar.

## `kct net-status --why` reads the sidecar (Issue #5521)

`--why` classifies a *saved* board: it has no live router, so its verdicts
are inferred from finished copper. The routing plan is the other kind of
evidence — a capacity measurement taken *before* anything was routed — and
`--why` now prints it when it is available.

No flag. `output_why` looks for `<pcb_stem>.routing_plan.json` beside the
board (the exact inverse of what `kct route` writes) and, for each stuck
net that **crosses an overflowed corridor**, prints the corridor and its
measured relief immediately *before* the `recommendation:` line:

```
[PLACEMENT_BOUND] /DQ3
  unconnected pads: U2.14, U3.9
  evidence:         ...
  routing plan:     corridor U2 -> U3 (tiles 17-18, layer 0): demand 14.0 tracks, capacity 9, overflow 5
                    relief: move U3 +2.0/+0.0 mm -> total overflow 0
  recommendation:   [medium] ...
```

The corridor line is rendered by `RoutingPlan.format_edge_headline()`, the
same method the route-time report uses, so the two renderings of one edge
cannot drift.

Three properties are load-bearing:

- **No sidecar ⇒ nothing changes.** Not a "no plan found" line, not a
  `null` JSON key — byte-identical output, pinned by golden fixtures in
  `tests/fixtures/net_status_why_golden/` (regenerate deliberately with
  `scripts/regen_net_status_why_golden.py`).
- **The plan never reclassifies.** The `[PLACEMENT_BOUND]` header and the
  counts above it come from copper evidence; the plan is a second witness
  printed underneath, not a new verdict.
- **A sidecar that cannot be trusted is not used.** Built for a different
  board (`source.pcb` basename mismatch) or older than the PCB ⇒ one stderr
  line and it is ignored; malformed or unreadable ⇒ ignored silently. A
  stale plan's guess is worse than no guess.

In `--format json` the additions are exactly two, and only when a sidecar
was actually loaded: a top-level `"routing_plan"` block (path,
`schema_version`, `total_overflow`, `overflowed_edges`, `feasible`) and an
`"overflow_edge"` key on each crossing diagnosis. Both are injected in
`output_why`; `StuckNetDiagnosis.to_dict()` is untouched, because other
consumers share it (the same discipline `bundle_orientation` follows).

### What "crosses" means

One definition, `RoutingPlan.crossings_by_net_name()`: a net crosses an
overflowed edge iff its **name** resolves through the sidecar's `nets`
table to a net ID listed in that edge's `nets`, for some edge with non-zero
`overflow`. There is deliberately no pad-in-region fallback — the fleet
table below scores recall with the same method call, and two definitions
would let the diagnostic and the measurement disagree about one board.

## Fleet measurement (Phase 1c)

Epic #5510 Phase 1's original acceptance lines ("the report is useful")
were not measurable. This table replaces them. For each board it asks two
falsifiable questions about the plan's predicted congestion:

- **recall** — of the nets that really ended unrouted, what fraction cross
  at least one overflowed corridor? Low recall ⇒ the plan did not see the
  congestion that actually stopped the router.
- **precision** — of the overflowed corridors, what fraction are crossed by
  at least one unrouted net? Low precision ⇒ the plan cried wolf.

`scripts/routing_plan_fleet_table.py DIR...` prints the rows. It **does not
route**: it reads a `*.routing_plan.json` sidecar plus a `kct net-status
--strict --format json` dump from a directory a `kct route` run already
produced. Each row's routing argv is listed under the table so any row can
be reproduced.

The denominator is "nets the plan actually planned": a net the global pass
filed as `pour_skipped`, or that has no row in the plan's `nets` table at
all, is excluded — the plan makes no claim about it, so it cannot be scored
on it. (Board 04 is why the second exclusion exists: it auto-pours
`+3.3V`/`GND` rather than passing them to `--skip-nets`, so they never
reach the plan's net table, yet `net-status` reports them `incomplete` as
advisory plane residuals.)

<!-- FLEET_TABLE_PLACEHOLDER -->

## Non-goals of this phase

- No per-board corridor / tile-size / keepout configuration.
- The plan still **reserves nothing** and does not change routed copper.
  Since Issue #5521 it computes relief and can *gate* a run on request
  (`--plan-gate`), but the default path is unchanged: report-only, exit
  zero regardless of overflow. Hard corridors are Phase 3 of Epic #5510.
- Relief is **advice only** — no component is moved, no layer is
  reassigned, no pin is swapped (`swap_pins` is #5511/#5522).
- The round-robin layer *heuristic* is unchanged -- Issue #5575 only made
  it index into the signal-layer list. Replacing it is Phase 2.
- Building the plan never mutates `RegionGraph` state (utilization,
  history costs) and never changes routed copper -- see the byte-identity
  tests in `tests/test_routing_plan_5510.py` and
  `tests/test_global_router.py`.

## Cost

The stage is a coarse-graph pass, not a detailed route: graph build is
~0.01 s at 53x33 tiles, and the negotiated global pass is cheap whenever
nothing overflows. On the fleet boards (00-06, <= 38 nets) the measured
`overflow_report.elapsed_s` ranges from ~0.1 ms to 57 ms, with the maximum
on board 05 (37 nets, total overflow 4 -- see the measurement above).
`tests/test_routing_plan_5510.py::test_board_copper_unchanged_by_plan_stage`
encodes the actual acceptance bound: `plan["overflow_report"]["elapsed_s"]
< 5.0`, i.e. the guarantee is "well under 5 s on fleet-sized boards," not
sub-millisecond. Boards that *honestly overflow* with several hundred nets
are the expensive case (the pass then runs all 15 negotiated iterations,
rerouting every net through the hot edge); `elapsed_s` records that cost
rather than gating on it. `--no-routing-plan` is the escape hatch if the
stage is ever unwelcome.

The relief search (Issue #5521) is the only thing that can multiply that
cost, and it runs **only when the board actually overflows** — every fleet
board that reports `total_overflow == 0` pays exactly 0 s for it, which is
why the numbers above are unchanged. When it does run, its own
`RELIEF_BUDGET_S = 5.0` wall-clock budget caps it, and
`relief_meta.elapsed_s` / `relief_meta.truncated` record what it actually
spent.

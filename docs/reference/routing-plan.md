# RoutingPlan sidecar (`<output_stem>.routing_plan.json`)

> Issue #5519, Phase 1 of 4 of Epic #5510 ("capacity-aware routing plan on
> the default path — layer assignment, hard corridors, and an
> infeasibility certificate"). This phase is **report-only**: the sidecar
> serializes state the tile-based global router already computes; it
> never changes which copper `kct route` produces.

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
      "a": 17, "b": 18, "capacity": 9, "demand": 14, "overflow": 5,
      "blockage_mm": 1.2, "nets": [12, 13, 14],
      "layers": { "0": { "capacity": 5, "demand": 8 }, "3": { "capacity": 4, "demand": 6 } }
    }
  ],
  "overflow_report": {
    "iterations": 15, "total_overflow": 5, "overflowed_edges": 1,
    "failed_nets": [], "feasible": false, "elapsed_s": 0.4
  },
  "relief": []
}
```

### Top-level fields

- **`source`** -- provenance. `pcb` is filled in by the CLI writer (unset,
  i.e. `null`, when a `RoutingPlan` is built and inspected in-process
  without going through `write_sidecar`). `tile_mm` / `cols` / `rows`
  describe the coarse tile grid the plan was computed on.
- **`layers.signal` / `layers.plane`** -- layer indices by role
  (`LayerStack.signal_layers` / `plane_layers`), for reporting only. In
  this phase capacity still counts every grid layer including planes, and
  the round-robin layer assignment may land nets on a plane layer; a
  later phase reserves plane layers from routing.
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
  graph).
- **`overflow_report`** -- named `overflow_report`, never "certificate":
  that word is owned by `monotone_certificate.py`'s planarity proof.
  This block is a report of measured congestion, not a proof of
  (in)feasibility. `total_overflow` / `overflowed_edges` are always
  computed from `RegionGraph.get_total_overflow()` /
  `get_overflowed_edges()` directly (never a hand-summed value), so they
  can never drift from what the graph itself reports.
- **`relief`** -- always `[]` in this phase; reserved for a later phase's
  relief-corridor data.

## Non-goals of this phase

- No per-board corridor / tile-size / keepout configuration.
- **Capacity is not yet honest.** It still uses one global pitch
  (`DesignRules.trace_width + trace_clearance`) for every net regardless
  of net class, counts *every* grid layer including PLANE layers as
  signal capacity, and treats only pads as blockage (keepout rule areas
  and preserved copper do not reduce it). Per-class pitch, plane-layer
  exclusion and blockage beyond pads are the next slice of Epic #5510
  (see the Phase 1b follow-up issue), which is why `layers.signal` /
  `layers.plane` below are still reporting-only.
- Building the plan never mutates `RegionGraph` state (utilization,
  history costs) and never changes routed copper -- see the byte-identity
  tests in `tests/test_routing_plan_5510.py` and
  `tests/test_global_router.py`.

## Cost

The stage is a coarse-graph pass, not a detailed route: graph build is
~0.01 s at 53x33 tiles, and the negotiated global pass is cheap whenever
nothing overflows. On the fleet boards (00-06, <= 38 nets) the measured
`overflow_report.elapsed_s` is well under a millisecond. Boards that
*honestly overflow* with several hundred nets are the expensive case (the
pass then runs all 15 negotiated iterations, rerouting every net through
the hot edge); `elapsed_s` records that cost rather than gating on it.
`--no-routing-plan` is the escape hatch if the stage is ever unwelcome.

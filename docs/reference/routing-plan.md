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

`kct route`'s default path (`--strategy negotiated`, the default) reaches
the tile-based two-phase global router (`TwoPhaseRouter.route_all`) for
boards with at least one detected dense package (BGA / fine-pitch QFP —
see `Autorouter.detect_dense_packages`). On that path the sidecar is
written next to the routed PCB:

```
kct route boards/03-usb-joystick/output/usb_joystick.kicad_pcb \
    -o usb_joystick_routed.kicad_pcb
# writes usb_joystick_routed.routing_plan.json alongside the PCB
```

Non-dense boards (no `route_with_escape` call) do not reach the two-phase
global pass on the default path in this slice, so no sidecar is written
and `kct route ... --format json` has no `routing_plan` key. The
`--two-phase` flag forces the two-phase path (and therefore a sidecar)
even on a non-dense board.

A blocked write (e.g. a read-only output directory) prints a warning and
the route step still exits 0 -- exactly like the other post-route
sidecars (`net_class_map.json`, `current_paths.json`).

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
  at least one net (edges with zero demand are omitted). `capacity` /
  `demand` / `overflow` are read from the same directed `RegionEdge`
  object `RegionGraph.get_total_overflow()` / `get_overflowed_edges()`
  count internally (the edge whose `source` is the smaller region ID) --
  see the docstring in `routing_plan.py` for why this makes the two
  totals derivable by construction. `layers` gives the same
  capacity/demand split per layer index (empty `{}` on a single-layer
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

- No CLI flag exists yet to disable the plan (`--no-routing-plan` is a
  later phase); it is always built on the default path when the
  two-phase global router runs.
- No per-board corridor / tile-size / keepout configuration.
- Capacity still counts every grid layer, including planes.
- Building the plan never mutates `RegionGraph` state (utilization,
  history costs) and never changes routed copper -- see the byte-identity
  tests in `tests/test_routing_plan_5510.py` and
  `tests/test_global_router.py`.

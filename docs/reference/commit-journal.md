# Commit-journal sidecar (`<output_stem>.access_witness.json`)

> Issue #5517, Phase 1b of Epic #5508 ("pad-access invariant — no committed
> route may strand an unrouted pad"). Like Phase 1a
> (`kicad_tools.router.pad_access`), this phase is **read-only**: the journal
> records what the router did; it never changes which copper `kct route`
> produces.

## What it is for

Phase 1a answers *"given the copper committed so far, can this pad still be
reached?"* — a pure geometric read of one moment in time. That is not yet an
explanation. To say *"commit #142, net `COMP`, iteration 0, is what closed
U3.1"*, something has to remember the **order copper landed in**.

The commit journal is that memory. It is an append-only, ordered record of
every copper mutation the routing grid saw during a run, and it is what
replaces `net-status --why`'s `PLACEMENT_BOUND` / `CONGESTION_SATURATED`
*hypotheses* (issue #4507) with a reproducible witness.

`kct net-status --why` classifies a **saved board**: it loads a `.kicad_pcb`
and has no live `Autorouter`, so the commit order is gone by the time anyone
asks. This sidecar is what carries it across that boundary — the same shape as
`net_class_map.json` carrying net classes into `kct check`.

## When it is written

Every `kct route` that commits copper writes the sidecar next to the routed
PCB, stem-keyed off the output file:

```
kct route boards/00-simple-led/output/simple_led.kicad_pcb \
    -o simple_led_routed.kicad_pcb
# writes simple_led_routed.access_witness.json alongside the PCB
```

There is no flag. The journal is always on, because it has to be cheap enough
to need no knob: recording is one list append plus one shallow geometry copy
per grid mutation, against `mark_route`'s O(blocked cells) — nothing in the
observer reads the grid, evaluates clearance, or computes an access set.

A blocked write (read-only or missing output directory) prints a warning and
the route step still exits 0 — exactly like the other post-route sidecars
(`net_class_map.json`, `current_paths.json`, `routing_plan.json`). A router
that committed nothing writes no sidecar: an empty witness would read as
"nothing was committed", which is never true of a routed board.

## Format

Written with compact separators (no indentation) — it carries every segment of
every commit and rip-up, so pretty-printing doubles the file for no reader.
Expanded here for legibility:

```json
{
  "schema_version": 1,
  "source": { "pcb": "simple_led_routed.kicad_pcb" },
  "journal": {
    "schema_version": 1,
    "truncated": false,
    "record_count": 38,
    "records": [
      {
        "index": 0,
        "kind": "commit",
        "added": true,
        "pass": "initial",
        "iteration": 0,
        "net": 2,
        "net_name": "COMP",
        "route_id": 139742036352208,
        "is_escape": false,
        "segments": [[1.0, 1.0, 6.0, 1.0, 0.2, 0, 2]],
        "vias": []
      },
      {
        "index": 1,
        "kind": "rip",
        "added": false,
        "pass": "iteration",
        "iteration": 1,
        "net": 2,
        "net_name": "COMP",
        "route_id": 139742036352208,
        "is_escape": false,
        "geom_ref": 0
      }
    ]
  }
}
```

### Record fields

| Field | Meaning |
|---|---|
| `index` | Position in the journal, 0-based. Also the replay's step number. |
| `kind` | `commit` / `restore` / `rip` / `rollback` — see below. |
| `added` | **The one field a replay must obey.** `true` when this record added copper, `false` when it removed copper. |
| `pass` | Which routing stage was running (see the stage table). |
| `iteration` | Negotiated-loop iteration. `0` for `escape` / `initial` / `grace`. |
| `net`, `net_name` | The net whose copper moved. |
| `route_id` | `id()` of the live `Route` object, so a re-mark can be tied back to its rip. Process-local; meaningful only *within* one journal. |
| `is_escape` | True for escape-pre-phase stubs. |
| `segments` | `[x1, y1, x2, y2, width, layer, net]` per segment (`layer` is the `CopperLayer` integer value; coordinates in mm, rounded to 6 dp — the precision a `.kicad_pcb` itself stores). |
| `vias` | `[x, y, drill, diameter, layer_from, layer_to, net, flags]` per via; `flags` is a bitmask, `1` = in-pad, `2` = microvia. |
| `geom_ref` | Present *instead of* `segments`/`vias`: the index of an earlier record with identical geometry. |

Geometry is snapshotted rather than referenced on purpose: the post-route
optimizer and the DRC nudge mutate `Route` objects *in place*, so holding a
reference would silently rewrite history.

Segments and vias take their `net_name` from the owning record, which is why
`geom_ref` is only shared between records that agree on it.

### Size

The sidecar is written on every route with no flag to turn it off, so its size
has to stay proportionate to the board. Two things do that:

- **Fixed-order arrays** instead of a `{"x1": …, "y1": …}` object per segment —
  ~34 bytes rather than ~136.
- **`geom_ref` back-references.** Most of a journal *is* repeated geometry: a
  rip re-states what its commit stated, a restore states it a third time. On
  board 03, 186 of 261 records are exact geometry duplicates.

Board 03 (~42 000 journaled segments across 261 records) lands at ~0.4 MB;
written naively it would be ~11 MB.

### Kinds

| `kind` | `added` | What happened |
|---|---|---|
| `commit` | `true` | New search-derived copper landed (the common case). |
| `restore` | `true` | A route that was committed and later ripped came back — the negotiated rip-up paths re-mark the *same* `Route` object when a reroute attempt fails and the victim has to be put back. |
| `rip` | `false` | Copper was removed (rip-up, victim eviction, corridor clearing). |
| `rollback` | either | A wholesale occupancy resync: best-iteration rollback, the post-route optimizer swapping new geometry in for old, a connectivity-invariant revert, or a Monte-Carlo trial reset discarding the grid. Emitted as a `false` record for the stale geometry and a `true` record for the current geometry. |

**Rip-ups are journaled for a reason.** A commit-only journal cannot
reconstruct the copper present when a pad's access set closed: once any rip-up
has happened, a replay that only knows about commits counts ripped copper as
still present and blames the wrong net.

### Stages

| `pass` | Stage |
|---|---|
| `fixed` | Preserved / input-board copper marked before routing. |
| `escape` | Escape pre-phase stubs. This is the replay's **baseline** — Epic #5508's "access at escape-prephase end" reference point. |
| `initial` | Initial negotiated pass (iteration 0). |
| `grace` | Budget-cliff grace pass (#3452), still iteration 0. |
| `iteration` | Rip-up / reroute iteration N. |
| `relief` | Relief-rescue probe / victim re-land. |
| `reset` | A Monte Carlo / evolutionary trial reset discarded the grid. |
| `post` | Post-route optimizer, DRC nudge, clearance correction. |

Records appear in stage order, and `index` order **is** commit order — even
under region-parallel routing, because the observer runs inside the routing
grid's own lock rather than in the main thread's completion-order loop.

### `truncated`

`MAX_JOURNAL_RECORDS` (200 000) caps memory on a pathological run. Past the
cap the journal stops recording and sets `truncated: true`, so a consumer can
say the history is incomplete rather than silently replaying a prefix as if it
were the whole run. A dense board's negotiated loop commits and rips a few
thousand routes, two orders of magnitude below the cap.

## How it is collected

Every physical copper mutation on the routing grid flows through exactly three
`RoutingGrid` methods — `mark_route`, `unmark_route` and
`resync_route_occupancy` — so a single observer on the grid
(`RoutingGrid.commit_observer`, installed by `Autorouter`) sees them all,
including the paths that bypass `Autorouter._mark_route` entirely:

- the escape pre-pass marks through `EscapeRouter.apply_escape_routes`;
- the negotiated rip-up paths unmark through `NegotiatedRouter` directly.

That is why this is one hook rather than ~25 per-site edits, and why adding a
new commit site later cannot silently fall out of the journal.

`mark_route_usage` sites are deliberately **not** journaled: those are
negotiation usage counts, not physical copper (and are explicitly not mirrored
to the C++ grid — see `grid.py`, issue #3438). Journaling them would
double-count every commit.

## Backends

The journal is backend-agnostic. The C++ backend accelerates the A* search
only; commits are Python-side either way and mirrored onto the C++ grid by
`Autorouter._mark_route_on_cpp_grid`. No C++ change was needed.

The two backends produce the **same journal order** — same stage sequence,
same net-level commit order — on a fixture both can route. They do *not*
produce record-for-record identical journals, because their A* emits different
route geometry (the C++ search emits more, shorter segments) and the rip-up
loop diverges from there. A journal diff on a fixture where the routes
themselves match is a real finding, not a flaky test.

## Consumers

Phase 1b's second half adds `access_witness.replay(journal, router)`, which
walks these records against `pad_access.compute_access_set` to name, for each
pad that ended a run unrouted, the first pass at which its access set became
empty and the committed nets responsible — surfaced in `kct net-status --why`
and `rescue_diagnostics.format_stranding_report`.

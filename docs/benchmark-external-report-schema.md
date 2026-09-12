# External benchmark report schema (v1)

The external-benchmark report is the per-board data contract produced by
`kicad_tools.benchmark.external` (Epic #4932, Phase 1, issue #4934) and consumed
by the `kct bench external` CLI (Phase 2) and the kicad-tools.org results section
(Phase 3).

It exists so our numbers can be placed **side by side with DeepPCB's published
figures honestly**: the four headline fields use DeepPCB's own definitions, and
the remaining fields carry the stricter gates this project runs — the ones
Quilter's 2026 guide correctly notes that completion% + DRC alone do not cover.

```python
from kicad_tools.benchmark.external import collect_report, render_markdown

report = collect_report(
    "boards/strf/routed.kicad_pcb",
    board_id="strf",
    board_commit="a1b2c3d",
    board_source="https://github.com/pms67/STRF-Kicad",
    protocol="zero-touch",
    wall_clock_s=142.7,
)
report.write_json("output/bench/strf.zero-touch.json")
print(render_markdown([report]))
```

## Design rules

1. **Every headline number is measured from the board file, never from a
   router-internal counter.** Wirelength, via count, and completion are all
   re-derived from the `.kicad_pcb` on disk, so any router path — ours, a
   vendor's, or a human's — produces numbers on the same footing.
2. **Completion is counted in ratsnest connections**, matching DeepPCB's
   "98 of 98 airwires" / "210 of 210 connections" framing. Both the numerator
   and the denominator are reported so a reader never has to trust a bare
   percentage.
3. **Timing is refused, not fudged, without the C++ backend.** `timing.valid`
   is `false` and `timing.wall_clock_s` is `null` unless the C++ router
   extension was live. The number is *dropped*, not merely flagged, so a
   downstream renderer cannot accidentally publish a Python-fallback runtime.
4. **Both DRC engines are required slots.** `kct check` is blind to
   connectivity shorts that only surface once copper pours are re-filled;
   `kicad-cli` is blind to the diff-pair / match-group rules that have no
   KiCad-native expression. A report carrying only one is not evidence of a
   clean board.
5. **"Did not run" never renders as "clean."** `kicad_cli_drc.violation_count`
   is `null` (not `0`) when the tool could not run, and the markdown renderer
   prints `not run`.
6. **Outcome and artifact provenance are separate, structured fields, never
   inferred from a bare completion % or a valid-looking timing** (issue
   #5280, Epic #5278 Phase 1). `route_outcome.outcome` says what happened
   (`completed` / `partial` / `stopped_before_routing` / `failed` /
   `timeout` / `unknown`); `route_outcome.artifact_source` says whether the
   measured board is what the router wrote (`router_output`) or the
   pre-route input used as a fallback because the router produced nothing
   (`fallback_input`). A missing output file can never silently look like a
   routed board.
7. **Pre-existing connectivity is measured, never fabricated.**
   `pre_route_completion` is the same `CompletionMetrics` shape measured on
   the pre-route (post rip-up) input -- the baseline this attempt started
   from. It is `null` when not measured, never a fabricated `0`. Comparing
   it against `completion` (the *result*, measured on whatever board was
   actually reported on) is how a reader tells newly-routed progress apart
   from connectivity that was already there (single-pad nets, zone-stitched
   nets, ...) before this attempt ran.
8. **A valid timing on a non-completed attempt is real elapsed time, but
   NOT a completed-routing performance number.** `timing.measured_phase`
   mirrors `route_outcome.outcome` (or `unknown` when no route-attempt
   evidence was supplied) -- native C++ backend availability alone does not
   relabel a `stopped_before_routing` or `failed` attempt's elapsed time as
   a routing benchmark figure. Every renderer must show the phase whenever
   it is not `completed`.

## Example

```json
{
  "$schema": "https://kicad-tools.org/schemas/benchmark-external/v1.json",
  "schema_version": 1,
  "generated_at": "2026-08-24T18:40:00+00:00",
  "board_id": "strf",
  "board_commit": "a1b2c3d",
  "board_source": "https://github.com/pms67/STRF-Kicad",
  "board_file": "routed.kicad_pcb",
  "protocol": "zero-touch",
  "tool_commit": "9f3c21b",
  "route_outcome": {
    "outcome": "completed",
    "artifact_source": "router_output",
    "exit_code": 0,
    "reason": null
  },
  "completion": {
    "connections_routed": 98,
    "connections_total": 98,
    "completion_pct": 100.0,
    "nets_total": 42,
    "nets_complete": 42,
    "nets_incomplete": 0,
    "nets_unrouted": 0,
    "nets_blocking_incomplete": 0
  },
  "pre_route_completion": {
    "connections_routed": 12,
    "connections_total": 98,
    "completion_pct": 12.24,
    "nets_total": 42,
    "nets_complete": 6,
    "nets_incomplete": 36,
    "nets_unrouted": 36,
    "nets_blocking_incomplete": 36
  },
  "newly_routed_connections": 86,
  "copper": {
    "via_count": 68,
    "wirelength_mm": 1182.9,
    "segment_count": 731,
    "arc_count": 0,
    "wirelength_by_layer_mm": { "F.Cu": 640.12, "B.Cu": 542.78 }
  },
  "timing": {
    "wall_clock_s": 142.7,
    "valid": true,
    "refusal_reason": null,
    "measured_phase": "completed"
  },
  "backend": {
    "backend": "cpp",
    "available": true,
    "version": "1.0.0",
    "build_version": 21,
    "unavailable_reason": null
  },
  "kct_check": {
    "ran": true,
    "passed": true,
    "error_count": 0,
    "warning_count": 3,
    "errors_by_rule": {},
    "note": null
  },
  "kicad_cli_drc": { "ran": true, "violation_count": 0, "by_type": {}, "note": null },
  "diff_pairs": {
    "pairs_total": 1,
    "pairs_complete": 1,
    "completion_pct": 100.0,
    "pairs": [
      {
        "net_positive": "USB_D+",
        "net_negative": "USB_D-",
        "positive_complete": true,
        "negative_complete": true,
        "complete": true
      }
    ]
  },
  "notes": []
}
```

## Field reference

### Identity

| Field | Type | Meaning |
|---|---|---|
| `$schema` | string | Schema URL for the version below |
| `schema_version` | integer | `1` |
| `generated_at` | string | ISO-8601 UTC timestamp of measurement |
| `board_id` | string | Manifest slug (e.g. `strf`, `pocketbeagle`) |
| `board_commit` | string \| null | **Pinned upstream commit** of the board source. Without it the numbers are unreproducible |
| `board_source` | string \| null | Upstream repository URL |
| `board_file` | string \| null | File name of the measured `.kicad_pcb` |
| `protocol` | string | `zero-touch` (rules as shipped, no tuning) or `tuned` (declared netclass / diff-pair config). Free-form tags allowed |
| `tool_commit` | string | kicad-tools commit that produced the report (`unknown` outside a git checkout) |

### `route_outcome` — what happened, and where the measured board came from

`null` when no route-attempt evidence was supplied to `collect_report` --
including every report generated before issue #5280 (schema v1, before this
field existed). **`null` must be treated as unknown, never as success.**

| Field | Type | Meaning |
|---|---|---|
| `outcome` | string | One of `completed`, `partial`, `stopped_before_routing`, `failed`, `timeout`, `unknown` |
| `artifact_source` | string | `router_output` (the measured board is what the router wrote for this attempt) or `fallback_input` (the router produced nothing usable; the measured board is the pre-route input) or `unknown` |
| `exit_code` | integer \| null | The router's reported exit code, or `null` when it raised/never ran |
| `reason` | string \| null | Human-readable explanation, present whenever `outcome` is not `completed` |

`outcome` values, precisely:

- **`completed`** — the router exited `0`, wrote output, and the measured
  board is at 100% connection completion (or there was nothing to route).
- **`partial`** — real progress was made (router output exists) but the
  board is not fully routed. This is a **normal**, not a failure, result on
  a hard board under the zero-touch protocol.
- **`stopped_before_routing`** — an explicit preflight refusal was captured
  (currently the census gate exit code 9), with no output for this attempt.
  Generic nonzero exits without output do not establish this phase.
- **`failed`** — the router raised or exited nonzero without output, with
  no explicit timeout or preflight-refusal evidence.
- **`timeout`** — deadline exit 124, `TimeoutError`, or `TimeoutExpired`
  records an exceeded budget. Partial output provenance remains independent;
  elapsed native attempt time is retained even when the call raises.
- **`unknown`** — no route-attempt evidence was supplied, or the evidence
  contradicts itself (e.g. exit `0` but no output file).

### `completion` — DeepPCB headline metric #1

| Field | Type | Meaning |
|---|---|---|
| `connections_routed` | integer | Numerator: ratsnest connections satisfied by copper |
| `connections_total` | integer | Denominator: `Σ max(pads − 1, 0)` over all named nets — the airwire count a fully ripped-up copy would show |
| `completion_pct` | float | `routed / total × 100`, rounded to 2 dp. `100.0` when there is nothing to route |
| `nets_total` | integer | Named nets analyzed (net 0 excluded) |
| `nets_complete` / `nets_incomplete` / `nets_unrouted` | integer | Per-net rollup |
| `nets_blocking_incomplete` | integer | Incomplete nets after plane/pour stitching residuals are reclassified advisory — the count the ship-ready gate uses |

Connections, not pads: three pads stranded together on one island are **one**
missing connection, not two. This is derived from `NetStatus.island_count`
(surfaced by this issue) via
`kicad_tools.analysis.net_status.NetStatusResult.routed_connections`.

**Important:** `completion` is always the *result* — measured on whatever
board was actually reported on (router output, or the pre-route input when
the router produced nothing; see `route_outcome.artifact_source`). It is
**not** guaranteed to reflect new routing progress by itself — compare it
against `pre_route_completion` below.

### `pre_route_completion` and `newly_routed_connections` — separating baseline from result

| Field | Type | Meaning |
|---|---|---|
| `pre_route_completion` | object \| null | Same shape as `completion`, measured on the pre-route (post rip-up) input — the baseline this attempt started from. `null` when not measured, **never a fabricated `0`** |
| `newly_routed_connections` | integer \| null | `completion.connections_routed − pre_route_completion.connections_routed`. `null` whenever `pre_route_completion` is `null` |

This is the fix for the specific failure this issue was filed over: a
report whose router produced no output measures the SAME board for both
`completion` and `pre_route_completion` (so `newly_routed_connections` is
`0`), rather than relabeling pre-existing/trivial connectivity (single-pad
nets, zone-stitched nets, ...) as if it were new routing progress.

### `copper` — DeepPCB headline metrics #2 and #3

| Field | Type | Meaning |
|---|---|---|
| `via_count` | integer | Every `(via …)` element on the board. On a ripped-up benchmark input every via is router-placed |
| `wirelength_mm` | float | Total copper track length: straight `(segment …)` plus copper `(arc …)` tracks |
| `segment_count` | integer | Copper segments counted |
| `arc_count` | integer | Copper arc tracks counted |
| `wirelength_by_layer_mm` | object | Per-copper-layer breakdown |

Copper `(arc …)` tracks (KiCad 7+ rounded tracks) are read straight from the
S-expression because `kicad_tools.schema.pcb.PCB` does not model them —
measuring from `pcb.segments` alone would silently under-report any external
board that uses them.

### `timing` and `backend` — environment validity

| Field | Type | Meaning |
|---|---|---|
| `timing.wall_clock_s` | float \| null | Runtime of the routing pass. **`null` whenever `valid` is `false`** |
| `timing.valid` | boolean | `true` only when the C++ router backend was active |
| `timing.refusal_reason` | string \| null | Why the number was dropped |
| `timing.measured_phase` | string | Mirrors `route_outcome.outcome` (`unknown` when no route-attempt evidence was supplied). **A `valid` timing whose phase is not `completed` is real elapsed time, but is time-to-refusal/partial-progress, NOT a completed-routing performance number** — every renderer must show the phase in that case |
| `backend.backend` | string | `cpp`, `python`, or `unknown` |
| `backend.available` | boolean | Whether the C++ extension imported |
| `backend.version` / `backend.build_version` | string / integer \| null | Extension version and compiled build version |
| `backend.unavailable_reason` | string \| null | Why the extension is not active |

`backend` is populated by the same probe `kct build-native --check` uses, so a
report and the CLI can never disagree about the environment.

### `kct_check` — internal engine

| Field | Type | Meaning |
|---|---|---|
| `ran` | boolean | Whether the engine executed |
| `passed` | boolean \| null | `error_count == 0` |
| `error_count` / `warning_count` | integer \| null | Violation counts |
| `errors_by_rule` | object | `rule_id → count` for error-severity findings |
| `note` | string \| null | Failure text when `ran` is `false` |

### `kicad_cli_drc` — mandatory cross-gate

| Field | Type | Meaning |
|---|---|---|
| `ran` | boolean | Whether `kicad-cli pcb drc --refill-zones` executed |
| `violation_count` | integer \| null | Error-severity violation count. **`null` when the tool did not run** |
| `by_type` | object | `type → count` for error-severity violations |
| `note` | string \| null | Skip/failure reason (kicad-cli absent, timeout, no report) |

### `diff_pairs` — strict gate, where pairs are defined

`null` when the board defines no differential pairs. That is different from
"its pairs are unrouted".

| Field | Type | Meaning |
|---|---|---|
| `pairs_total` / `pairs_complete` | integer | A pair counts complete only when **both** members are fully connected |
| `completion_pct` | float | `complete / total × 100` |
| `pairs[]` | array | `{net_positive, net_negative, positive_complete, negative_complete, complete}` |

Pairs come from an explicit caller-supplied list (e.g. a net-class map sidecar)
or, when omitted, from `TraceLengthAnalyzer.find_differential_pairs` naming
conventions (`_P`/`_N`, `+`/`−`, `D+`/`D−`).

### `notes`

Free-form annotation strings, rendered under the markdown table. Used for the
per-board caveats the published comparison carries (e.g. "3 nets left unrouted,
filed as #NNNN").

## Versioning

`schema_version` is bumped when a field is **removed** or its meaning changes.
Purely additive fields do not require a bump — same policy as
[`board-json-schema.md`](board-json-schema.md). Issue #5280 added
`route_outcome`, `pre_route_completion`, `newly_routed_connections`, and
`timing.measured_phase` — all purely additive, so `schema_version` stays `1`.

### Legacy reports (schema v1, pre-#5280)

Every report generated before issue #5280 landed (concretely: the two
committed 2026-08-25 records under `benchmarks/external/results/`, tool
commit `636fd368`) simply **omits** `route_outcome`, `pre_route_completion`,
`newly_routed_connections`, and `timing.measured_phase` — additive fields
that did not exist yet. Those files are never rewritten to backfill them
(this project does not fabricate provenance for historical measurements):

- A consumer reading a schema-v1 report MUST treat a missing
  `route_outcome` as **unknown**, never as an implicit success. The CLI
  markdown renderer and the kicad-tools.org loader both render this as
  `unknown (legacy)` with a footnote explaining the report predates
  outcome/provenance tracking.
- A missing `pre_route_completion` means "not measured" — never treated as
  a `0` baseline, so `newly_routed_connections` is likewise left
  unavailable rather than computed against a fabricated zero.
- The historical committed reports' own free-form `notes` (e.g. "router
  produced no output file ... (0% complete)") predate this issue's fix to
  that wording and are **not** edited retroactively; the numeric
  `completion.completion_pct` in those files was always correct (in the
  30s for both boards) — only the prose in `notes` was misleading. Current
  code's own generated notes no longer make that claim.

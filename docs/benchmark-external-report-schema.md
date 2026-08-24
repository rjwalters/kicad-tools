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
  "copper": {
    "via_count": 68,
    "wirelength_mm": 1182.9,
    "segment_count": 731,
    "arc_count": 0,
    "wirelength_by_layer_mm": { "F.Cu": 640.12, "B.Cu": 542.78 }
  },
  "timing": { "wall_clock_s": 142.7, "valid": true, "refusal_reason": null },
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
[`board-json-schema.md`](board-json-schema.md).

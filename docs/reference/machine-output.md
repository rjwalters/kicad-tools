# Machine output: the canonical `--format json` idiom

> Design note for issue #4543 (copperhead workflow survey #4520, idea 6).
> Inventory measured against `fa98ac8b` (2026-08-07) with
> `scripts/audit_machine_output.py`; re-run that script to refresh the counts.

## Decision

**The canonical machine-output spelling for every `kct` subcommand is
`--format json`** — a `--format` choice flag whose choices include `json`
(commonly `{text,json}`; richer commands add `table`, `summary`, `csv`,
`markdown`, …).

The boolean `--json` flag is a **legacy alias**. Where it already shipped it
is kept working forever (backward compatibility: no flag is ever removed or
repurposed), documented with help text `"... (same as --format json)"`. New
commands must NOT add `--json`; they add `--format` with a `json` choice.

Rationale (from the audit below): `--format` already dominated the user-facing
surface 124 : 2 (plus 8 passthrough `--json` subparsers), every recent
machine-output addition chose it (`sch tidy` #4654, `check` families #4656,
`pcb add-3d-models` #4660, `net-status` #4557), and a choice flag is strictly
more extensible than a boolean — `table`/`summary`/`csv`/`markdown` variants
already exist across the tree and a boolean `--json` cannot grow.

### Precedence rule

When a command carries both spellings, **either flag requesting JSON wins**.
The two flags cannot conflict, because `--json` only ever means
`--format json`. `kct <cmd> --json --format json` is valid and equivalent to
either flag alone.

### Shared machinery

`src/kicad_tools/cli/format_options.py` provides the helpers commands should
use:

- `add_format_flag(parser)` — declare the canonical `--format` choice flag.
- `add_legacy_json_flag(parser)` — declare the `--json` alias (only for
  commands that already shipped it).
- `normalize_format_alias(args)` — call once after `parse_args`; reconciles
  the two spellings so downstream code can keep checking a single attribute
  (`args.json` or `args.format`, whichever it historically used).
- `wants_json(args)` — one-shot predicate that checks both spellings without
  mutating the namespace.

The mechanical sweep that adds `--format json` to the prose-only backlog
below is issue **#4674** and should build on these helpers.

## Inventory (as of `fa98ac8b`, 199 leaf subcommands)

Measured by programmatic introspection of the real argparse tree
(`create_parser()`, recursive walk of `_SubParsersAction` leaves) — not grep:

| Idiom (outer `kct` parser) | Before #4543 | After #4543 | After #4674 b1 | b2 | b3 | b4 | b5 | b6 |
|---|---|---|---|---|---|---|---|---|
| `--format` with a `json` choice | 124 | 124 | 148 | 164 | 174 | 179 | 184 | 189 |
| Both `--format json` and legacy `--json` alias | 0 | 2 (`placement refine`, `calibrate`) | 2 | 2 | 2 | 2 | 2 | 2 |
| `--json` boolean only | 2 | **0** | 0 | 0 | 0 | 0 | 0 | 0 |
| `--format` without a `json` choice | 1 (`benchmark report`, `text,markdown`) | 1 | **0** | 0 | 0 | 0 | 0 | 0 |
| Neither (prose-only) | 72 | 72 (backlog for #4674, below) | 49 | 33 | 23 | 18 | 13 | 8 |

The first #4674 batch swept the grouped-subcommand families -- `mfr` (7),
`spec` (5), `placement fix/nudge/snap/align/distribute` (5), `zones
add/batch/fill/hv-keepout` (4), `benchmark run/compare` (2) -- and added the
`json` choice to `benchmark report`, closing the `format-nojson` bucket.

The second batch swept the 16 mutating `sch` leaves.

The third batch finished the four families that already spoke JSON on most
of their leaves but still had prose-only holdouts -- `datasheet` (3), `lib`
(3), `parts` (2), `pcb` (2).

The fourth batch swept the environment/integration singles -- `config`,
`ipc status/connect/push-routes`, `mcp setup` (5) -- finishing the `ipc`
family outright and finishing `mcp` (`mcp serve` is exempt).

The fifth batch swept the board-artifact producers -- `board-metrics`,
`create-pcb`, `panel`, `report generate`, `screenshot` (5).

The sixth batch swept the board-improvement / rule-derivation drivers --
`optimize-placement`, `optimize-traces`, `route-auto`, `reason`,
`creepage-export-rules` (5). The remaining 8 prose-only leaves (`build`,
`pipeline`, `stitch`, plus the 4 exempt and the deferred `route`) stay on the
#4674 backlog below.

Issue #4543 closed the `--json`-only bucket by adding `--format {text,json}`
alongside the existing `--json` on both commands (outer parser, forwarding
shim, and inner parser), and did the same for the 7 `footprint generate`
shape subparsers (`soic/qfp/qfn/chip/sot/dip/pin-header`) plus the
passthrough's top-level `--list --json` — the de-facto `--json` surfaces that
bypass `parser.py` entirely (`cli/__init__.py` hands raw argv to
`footprint_generate.main`).

Note on `kct placement refine`: its `--json` is a **JSON API mode** flag
(read commands from stdin, write JSON responses to stdout), not a plain
output format. `--format json` selects the same machine interface for the
session; the alias is still exact — the two spellings are equivalent — but
the help text says "JSON API mode" rather than "output format".

### Inner-only / dead surfaces (documented, not user-facing)

The CLI has a two-layer architecture: `parser.py` defines the user-facing
tree, and `commands/*.py` shims re-serialize parsed args into standalone
inner-module parsers. Some inner `--json`/`--format` flags are therefore not
reachable through `kct`:

- `sch_symbol_info.py` `--json` — inner only; the user-facing `kct sch info`
  contract is `--format {text,json}`, translated by `commands/schematic.py`.
- `mfr.py` `mfr rules --json` — **retired** (first #4674 batch): the flag was
  a dead surface, unreachable through `kct` because the shim rebuilds
  `sub_argv` from outer args and the outer `kct mfr rules` had no
  machine-output flag. `kct mfr rules --format json` is now wired end-to-end
  (outer parser → shim → inner parser) and the inner `--json` boolean was
  removed rather than aliased — it never shipped on any reachable surface.
- `route_cmd.py` inner `--format` — deliberately allowlisted as inner-only
  (`tests/test_cli_parser_drift.py`, `INNER_ONLY_ALLOWLIST`), so `kct route`
  is prose-only at the user-facing surface today. See "Deferred" below.

## Classification of the 72 prose-only subcommands

This issue owns the exemption classification; implementing the machine paths
is the separate mechanical sweep **#4674**.

### Exempt — no machine-output flag warranted (4)

| Command | Why exempt |
|---|---|
| `interactive` | Interactive REPL session; no single machine payload to emit. |
| `mcp serve` | Long-running server; the machine contract *is* the MCP protocol. |
| `run` | Executes a user-supplied Python script with kct's interpreter; stdout belongs to the script — the wrapper must not impose a format. |
| `footprint generate` (outer stub) | Argv passthrough; the real machine surface lives on the passthrough shape subparsers, which have `--format json` as of #4543. |

### Deferred — owned by another workstream (1)

| Command | Why deferred |
|---|---|
| `route` | Inner `route_cmd.py` already has `--format`, but it is intentionally allowlisted as inner-only. Promoting it to the outer parser requires removing the `INNER_ONLY_ALLOWLIST` entry and adding shim forwarding under the route drift guard — owned by the route workstream, not the mechanical sweep. |

### Sweep backlog for #4674 — should gain `--format {text,json}`

Read-only/reporting commands should emit their report as JSON; mutating
commands should emit a JSON change-summary (precedent: `kct sch tidy`,
`kct sch cleanup-wires`, `kct pcb sync-netlist` all do).

**Done (first #4674 batch, 24 surfaces):** `mfr list/info/rules/compare/`
`export-dru/apply-rules/validate`, `spec init/validate/status/decide/check`,
`placement fix/nudge/snap/align/distribute`, `zones add/batch/fill/`
`hv-keepout`, `benchmark run/compare`, plus the `json` choice on
`benchmark report` (the former `format-nojson` holdout).
`tests/test_format_json_sweep.py` guards these surfaces.

**Done (second #4674 batch, 16 surfaces):** the mutating `sch` family --
`add-bypass-cap`, `add-component`, `add-junction`, `add-label`,
`add-no-connect`, `add-pull-resistor`, `add-wire`, `disconnect`,
`insert-inline`, `reconnect-pin`, `replace`, `set-footprint`,
`set-label-direction`, `set-reference`, `set-symbol-property`, `set-value`.

Unlike the grouped families, each of these lives in its own inner module, so
they share one wrapper -- `src/kicad_tools/cli/sch_json.py` -- rather than 16
hand-written JSON writers. It brackets the existing prose implementation:
in JSON mode the prose is captured and discarded, the inner module's
`record(...)` / `append(...)` calls accumulate the per-command change
summary, and exactly one document is printed. Captured stderr is replayed
after the document (and becomes the `error` value when the exit code is
non-zero), so the exit codes and the diagnostics both survive unchanged.
The shared envelope is:

```json
{
  "command": "add-junction",
  "schematic": "board.kicad_sch",
  "dry_run": true,
  "success": true
}
```

plus the command's own keys (e.g. `changed` / `not_found` / `files_modified`
for the `set-*` batch commands, `planned` / `placed` for the `add-*`
placement commands). `commands/schematic.py`'s own "file not found" guard
runs before any inner module, so it emits the same `{"error": ...}` document
itself. `tests/test_format_json_sweep_sch.py` guards these 16 surfaces
(outer flag, both shim shapes, emission, determinism, error documents, and
that `--dry-run` still writes nothing).

**Done (third #4674 batch, 10 surfaces):** the prose-only holdouts inside
four families whose other leaves already spoke JSON --
`datasheet cache/convert/download`, `lib create-symbol-lib/`
`create-footprint-lib/generate-footprint`, `parts cache/sync-catalog`,
`pcb export-dsn/import-ses`. Each emits one document keyed by `command`
plus that command's own summary (`cleared` / `action` for the two cache
commands, `layers`/`nets`/`components` for `export-dsn`,
`wires`/`vias` for `import-ses`, `path`/`file_size_bytes`/`source` for
`download`). Three details worth reusing:

- The three `lib` commands are **unimplemented placeholders** that exit 2.
  Under JSON the non-implementation *is* the payload
  (`{"implemented": false, "error": ..., "tracking_issue": ...}`) so a
  caller can tell "not implemented" from a crash without scraping stderr;
  the exit code is unchanged.
- `datasheet convert` writes markdown to stdout when no `-o` is given, so
  in JSON mode it carries the conversion under a `markdown` key; with
  `-o` it reports `output` and omits `markdown`.
- `parts cache` is the one swept surface whose *inner* parser has its own
  subparsers. Its per-action subparsers declare `--format` with
  `argparse.SUPPRESS` as the default, so an unspecified flag on the
  action cannot clobber the value parsed by the `cache` parser
  (`kct parts cache --format json stats` and
  `kct parts cache stats --format json` both resolve to json).

`commands/pcb.py`'s shared "file not found" guard runs before every `pcb`
handler, so — like the `sch` shim in batch 2 — it now emits the same
`{"error": ...}` document; that closes the hole for the ~20 `pcb` leaves
that already had `--format json`. `tests/test_format_json_sweep_families.py`
guards these 10 surfaces.

**Done (fourth #4674 batch, 5 surfaces):** the environment/integration
singles — the leaves that report on or configure `kct`'s *surroundings*
rather than a board file: `config`, `ipc status/connect/push-routes`,
`mcp setup`. This finishes the `ipc` family outright and finishes `mcp`
(`mcp serve` is exempt, above). Shapes:

| Command | Document |
|---|---|
| `config` | `{"command": "config", "action": "show"\|"paths"\|"init"\|"get"\|"set", …}` — `show` carries `sections` (`{section: {key: {value, source}}}`) plus `native_backends` (`{backend: {available, version}}`); `paths` carries `project_config`/`user_config` (`{path, exists}`, project also `search_filenames`); `init` carries `path`/`scope`/`created`; `get` carries `key`/`section`/`option`/`value`/`source`; `set` carries the same plus `applied: false` and the `toml` snippet to paste |
| `ipc status` | `{"command": "status", "socket", "connected", "kicad_version", "instances", "open_documents", "success"}` |
| `ipc connect` | `{"command": "connect", "socket", "connected", "kicad_version", "success"}` |
| `ipc push-routes` | `{"command": "push-routes", "pcb", "net_filter", "tracks", "vias", "dry_run", "pushed", "success"}` (`socket` once resolved) |
| `mcp setup` | `{"command": "setup", "client", "config_path", "dry_run", "written", "replaced", "server", "success"}` |

Two conventions this batch establishes for the remaining long tail:

- **`set`-style commands that only *advise*** (`kct config set` prints the
  TOML to paste rather than writing it) must say so structurally —
  `"applied": false` — so a machine caller cannot mistake exit 0 for a
  persisted change.
- **Text-mode prose is preserved verbatim**, including the several `ipc`
  failure paths that print *without* an `Error:` prefix. The shared
  `_fail(...)` helper in `commands/ipc.py` takes a `text_lines` override
  for exactly that reason; copy it rather than normalizing prose in a
  formatting-only batch.

`kct ipc push-routes` reaches its "PCB parser not available." branch for
every existing board, because it imports `kicad_tools.pcb.parser` — a
module that has never existed (dead since #2363). The JSON path turns that
into a visible `{"error": …, "success": false}` document; the underlying
wiring bug is tracked separately in #4788 and is out of scope for this
sweep. `tests/test_format_json_sweep_env.py` guards these 5 surfaces and is
written to pass both before and after #4788 is fixed.

**Done (fifth #4674 batch, 5 surfaces):** the board-artifact producers — the
singles that turn a board or schematic into a *file on disk*:
`board-metrics`, `create-pcb`, `panel`, `report generate`, `screenshot`.
Shapes:

| Command | Document |
|---|---|
| `board-metrics` | `{"command": "board-metrics", "mode": "single"\|"all", "dry_run", "boards": [{"slug", "status", "output_path", "metrics"}], "success"}` — `metrics` is the board's full `board.json`; `output_path` is `null` under `--dry-run` |
| `create-pcb` | `{"command": "create-pcb", "schematic", "output", "board": {width_mm, height_mm, layers}, "components_found", "placement": {skipped, placed, failed[], warnings[]}, "nets": {assigned, missing_footprints[]}, "summary", "dry_run", "saved", "success"}` |
| `panel` | `{"command": "panel", "input", "output", "grid": {rows, cols, spacing_mm}, "board_count", "tabs", "cut_method", "tab_width_mm", "tab_count", "frame", "tooling_holes", "fiducials", "success"}` |
| `report generate` | `{"command": "generate", "input", "manufacturer", "output_dir", "project_name", "report_path", "pdf_path", "data_source": "auto-collect"\|"data-dir"\|"skeleton", "figures": {generated, skipped_reason}, "success"}` |
| `screenshot` | `{"command": "screenshot", "input", "output", "width_px", "height_px", "layers_rendered", "success"}` |

Three conventions this batch adds:

- **The envelope is not the artifact.** `board-metrics` already printed a
  JSON *artifact* (`board.json`) under `--dry-run`; `--format json` wraps a
  distinct run envelope around it (`boards[].metrics`) rather than
  overloading the artifact's schema, and text mode still prints the bare
  artifact. A command whose normal output is already JSON should do the
  same — the document describes the *run*, the artifact keeps its own
  contract. `metrics.generated_at` is the one deliberately volatile field
  (it belongs to the board.json contract), so determinism is asserted
  modulo that key.
- **Producers say whether they produced.** Anything that can run without
  writing (`--dry-run`) reports it structurally: `saved`/`output_path`
  rather than exit 0 alone.
- **Third-party stdout chatter must be diverted, not trusted.**
  `report generate` calls into a data collector, a figure generator, and
  WeasyPrint — the last of which prints a multi-line "install my native
  libraries" banner *on stdout* when they are missing, which would corrupt
  the single-document contract on any machine without them. The
  `_stdout_to_stderr_when(as_json)` helper in `report_cmd.py` captures those
  regions and replays them on stderr; copy it for any leaf that calls
  third-party code it does not own.

`tests/test_format_json_sweep_artifacts.py` guards these 5 surfaces.

**Done (sixth #4674 batch, 5 surfaces):** the board-improvement /
rule-derivation drivers — the singles that take an existing board and derive
an improvement or an enforceable rule set from it: `optimize-placement`,
`optimize-traces`, `route-auto`, `reason`, `creepage-export-rules`. Shapes:

| Command | Document |
|---|---|
| `optimize-placement` | `{"command": "optimize-placement", "pcb", "output", "strategy", "seed_method", "max_iterations", "board": {width_mm, height_mm, components, nets}, "mode": "evaluate"\|"optimize", "scores": {…}, "feasible", "infeasible_detail", "iterations", "wall_time_s", "interrupted", "overlaps_remaining", "allow_infeasible", "dry_run", "saved", "written_to", "success"}` — `scores` is `{"current": …}` under `--dry-run` and `{"initial", "final"}` otherwise; each score is `{total, feasible, breakdown}` with the breakdown taken straight off the cost dataclass |
| `optimize-traces` | `{"command": "optimize-traces", "pcb", "output", "net_filter", "optimizations": {merge_collinear, eliminate_zigzags, convert_45_corners, chamfer_size_mm}, "drc_aware", "manufacturer", "layers", "copper_oz", "stats": {segments_before/after, corners_before/after, length_before_mm/after_mm, …}, "dry_run", "saved", "written_to", "success"}` |
| `route-auto` | `{"command": "route-auto", "pcb", "output", "strategy", "dry_run", "nets": [...], "nets_requested", "nets_routed", "success"}` — one `nets[]` entry per requested net in request order: routed entries carry `success`/`partial`/`strategy_used`/`metrics`/`segments_written`/`warnings`/`pads_connected`/`pads_total`/`error`/`alternative_strategies`, `--dry-run` entries carry the preview (`would_route`, `via_drill_mm` + `via_drill_source`, …) |
| `reason` | `{"command": "reason", "pcb", "output", "dry_run", "warnings", "drc": {ran, source, …}, "board": {…}, "mode": "prompt"\|"analyze"\|"export-state"\|"auto-route", …, "success"}` — plus the mode's own payload: `prompt`, `analysis`, `state` + `state_output`, or `auto_route` (`attempted`/`routed`/`nets[]`) with `saved`/`written_to` |
| `creepage-export-rules` | `{"command": "creepage-export-rules", "project", "pcb", "dru", "voltage_map", "standard", "pollution_degree", "material_group", "hv_threshold_v", "dru_floor_mm", "domains", "net_domains", "nets_assigned", "rules": [{name, condition, min_mm}], "bridging_exemptions", "dru_block", "dry_run", "written", "skipped_reason", "success"}` — `dru_block` is carried only on the `--dry-run` path (mirroring the prose that prints it instead of writing it) |

Three conventions this batch adds or reinforces:

- **A mode with no single-document form is refused, not half-emitted.**
  `kct reason --interactive` is a stdin/stdout dialogue; combining it with
  `--format json` emits `{"error": …}` and exits 2 rather than pretending
  to have a payload. Prefer this over silently ignoring the flag when only
  *part* of a command is interactive (a whole-command exemption belongs in
  the table above instead).
- **Volatile fields are named, not hidden.** `optimize-placement` reports
  `wall_time_s`; like `board-metrics`'s `generated_at` it is the one field
  determinism is asserted modulo.
- **The `_stdout_to_stderr_when` diversion is now the standard treatment**
  (third use, after `report generate`): `route-auto` and `reason --auto-route`
  drive the negotiated router, whose per-iteration progress log goes to
  **stdout**. It is captured and replayed on stderr so the JSON stream stays
  parseable. Any leaf that calls the router, a collector or third-party code
  needs it.

`tests/test_format_json_sweep_drivers.py` guards these 5 surfaces.

**Remaining actionable (3)** — the audit's `prose-only` bucket reads 8
because it also counts the 4 exempt commands and the deferred `route`
(sections above):

- `build`, `pipeline` (multi-stage orchestrators)
- `stitch` (single command, but its 6k-line inner module has a bespoke
  multi-phase report — batch it alone)


## Interop note (survey idea 7)

A stable machine-output contract is the enabling precondition for external
agents (e.g. copperhead) delegating routing/DRC/LVS/tapeout to `kct`: with
`--format json` canonical, an orchestrator can invoke any covered subcommand
and parse a predictable payload instead of scraping prose. Once #4674 closes
the prose-only backlog, the contract is: **every non-exempt `kct` subcommand
accepts `--format json` and emits a single JSON document on stdout.** No
separate issue tracks idea 7; it rides on this note plus #4674.

## Rules for new commands

1. Machine output = `--format` with a `json` choice. Never a new `--json`.
2. Reuse `format_options.py` helpers; call `normalize_format_alias` right
   after `parse_args` if the command carries the legacy alias.
3. If the command goes through a `commands/*.py` shim, forward `--format`
   explicitly and keep the outer/inner parsers in sync (see
   `tests/test_cli_parser_drift.py` for the bug class this prevents).
4. JSON goes to stdout as a single document; human/progress chatter goes to
   stderr or is suppressed in JSON mode.

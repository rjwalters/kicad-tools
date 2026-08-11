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

| Idiom (outer `kct` parser) | Before #4543 | After #4543 | After #4674 b1 | b2 | b3 |
|---|---|---|---|---|---|
| `--format` with a `json` choice | 124 | 124 | 148 | 164 | 174 |
| Both `--format json` and legacy `--json` alias | 0 | 2 (`placement refine`, `calibrate`) | 2 | 2 | 2 |
| `--json` boolean only | 2 | **0** | 0 | 0 | 0 |
| `--format` without a `json` choice | 1 (`benchmark report`, `text,markdown`) | 1 | **0** | 0 | 0 |
| Neither (prose-only) | 72 | 72 (backlog for #4674, below) | 49 | 33 | 23 |

The first #4674 batch swept the grouped-subcommand families -- `mfr` (7),
`spec` (5), `placement fix/nudge/snap/align/distribute` (5), `zones
add/batch/fill/hv-keepout` (4), `benchmark run/compare` (2) -- and added the
`json` choice to `benchmark report`, closing the `format-nojson` bucket.

The second batch swept the 16 mutating `sch` leaves.

The third batch finished the four families that already spoke JSON on most
of their leaves but still had prose-only holdouts -- `datasheet` (3), `lib`
(3), `parts` (2), `pcb` (2). The remaining 23 prose-only leaves (`stitch`
and the long tail of single commands, plus the 4 exempt and the deferred
`route`) stay on the #4674 backlog below.

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

**Remaining actionable (18)** — the audit's `prose-only` bucket reads 23
because it also counts the 4 exempt commands and the deferred `route`
(sections above):

- `board-metrics`, `build`, `config`, `create-pcb`, `creepage-export-rules`
- `ipc connect`, `ipc push-routes`, `ipc status`
- `mcp setup`
- `optimize-placement`, `optimize-traces`
- `panel`
- `pipeline`
- `reason`, `report generate`, `route-auto`
- `screenshot`
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

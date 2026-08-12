# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`--format json` on the 5 environment/integration singles** (part of
  #4674, fourth batch of the #4543 machine-output sweep) — `config`,
  `ipc status`, `ipc connect`, `ipc push-routes` and `mcp setup` now accept
  the canonical `--format json` and emit one deterministic document on
  stdout instead of prose. This finishes the `ipc` family outright and
  finishes `mcp` (`mcp serve` stays exempt: it is a long-running server
  whose machine contract *is* the MCP protocol). `config` covers all five
  modes — `show` (a `sections` map of `{value, source}` per key, plus
  `native_backends`), `paths`, `init`, `get` and `set`; because `config set`
  only *advises* the TOML to paste rather than writing it, its document says
  so structurally with `"applied": false` and carries the `toml` snippet.
  `ipc status`/`connect` report `socket`/`connected`/`kicad_version` (status
  also `instances` and sorted `open_documents`); `ipc push-routes` reports
  `tracks`/`vias`/`net_filter`/`dry_run`/`pushed`; `mcp setup` reports
  `client`/`config_path`/`written`/`replaced` plus the resolved `server`
  entry. Failure paths emit `{"error": ..., "success": false}` documents
  with exit codes unchanged, and text-mode output is byte-identical to
  before (including the several `ipc` failure paths that deliberately print
  without an `Error:` prefix). Audit
  (`scripts/audit_machine_output.py`): prose-only 23 → 18, format-json
  174 → 179. `tests/test_format_json_sweep_env.py` guards the new surfaces;
  `docs/reference/machine-output.md` records the per-command shapes.

- **`docs/research/kipy-ipc-api-evaluation.md`** (#4779) — evaluation of `kipy` (official KiCad IPC-API bindings) and `kicad-mcp-kipy` as a parser cross-check; verdict: adopt nothing (live-editor-only RPC client, no offline path, cannot run DRC); also corrects the `README.md` "Related Projects" `kipy` entry to the canonical GitLab URL with the live-editor constraint stated.

- **A path-level pairwise (HV) clearance predicate the copper-moving post-passes
  can consult** (#4507, epic #4431 Phase 2) — `pairwise_clearance` gains
  `path_pairwise_violation(x1, y1, x2, y2, layer, width, exclude_net, …)` (the
  coordinate-level form of the `route_pairwise_violation` acceptance gate; both
  now delegate to one shared walk, so predicate and gate agree by construction)
  and `PairwisePathChecker` — a bound checker whose `path_is_clear` matches the
  optimizer's `CollisionChecker` calling convention positionally, built from a
  live router via `PairwisePathChecker.from_router` (returns `None` without a
  `--voltage-map`, the standard dormancy contract). This is the API surface the
  `--lattice-optimize` pairwise-blindness fix (#4766) composes with the scalar
  collision checker; no existing behavior changes in this slice.
- **`--format json` on the 10 prose-only holdouts inside the `datasheet`,
  `lib`, `parts` and `pcb` families** (part of #4674, third batch of the
  #4543 machine-output sweep) — `datasheet cache`, `datasheet convert`,
  `datasheet download`, `lib create-symbol-lib`, `lib create-footprint-lib`,
  `lib generate-footprint`, `parts cache`, `parts sync-catalog`,
  `pcb export-dsn` and `pcb import-ses` now accept the canonical
  `--format json` and emit one deterministic document on stdout instead of
  prose, wired end-to-end (outer parser → shim → inner parser/handler).
  Each document is keyed by `command` plus that command's own summary:
  `action`/`cleared` for the two cache commands, `path`/`file_size_bytes`/
  `source` for `download`, `layers`/`nets`/`components` for `export-dsn`,
  `wires`/`vias` for `import-ses`. The three `lib` commands are
  unimplemented placeholders, so under JSON the non-implementation *is* the
  payload (`{"implemented": false, "error": ..., "tracking_issue": ...}`)
  and the exit code stays 2 — a caller can now distinguish "not
  implemented" from a crash without scraping stderr. `datasheet convert`
  carries its markdown under a `markdown` key when no `-o` sink was given.
  `parts sync-catalog` suppresses its download progress chatter in JSON
  mode so stdout stays a single document, and the inner `parts cache`
  action subparsers default `--format` to `argparse.SUPPRESS` so the flag
  works on either side of the action word. `commands/pcb.py`'s shared "file
  not found" guard runs before every handler, so it now emits the same
  `{"error": ...}` document — closing that hole for the ~20 `pcb` leaves
  that already had `--format json`. Text-mode output and exit codes are
  unchanged throughout. Audit (`scripts/audit_machine_output.py`):
  prose-only 33 → 23, format-json 164 → 174.
- **`kct drc --waivers`: the `.kct_waivers.json` schema now covers the
  kicad-cli DRC cross-gate** (closes #4691) — the mandatory second-opinion
  gate can finally state "0 unwaived errors" instead of a count comparison
  against a known defect signature. `kct drc` reads the **same** schema-v2
  sidecar as `kct check --waivers` (#4417) — same loader, same discovery
  probe (`<dir>/`, `<dir>/output/`, `<dir>/../output/`), same exact-set
  order-insensitive matching, same explicit-wins / discovered-degrades
  contract — so one committed file carries every documented exception for a
  board. Two normalizations bridge the engines: `rule` is matched against
  KiCad's own violation type string verbatim (no translation table, so
  kct-keyed ids such as `clearance_pad_pad` simply read as unused on this
  side), and the kicad-cli item *descriptions* (the parser discards item
  uuids) are normalized to component refs — `"Footprint C52"` → `C52`,
  `"Pad 6 [<no net>] of U3 on F.Cu"` → `U3` — before matching, with
  ref-less track/via findings waivable via the `nets` axis. Matched findings
  render in a dedicated `WAIVED` section with their own count bucket in
  table/summary output (never folded into warnings — the defect #4696
  reports against `kct check --format summary`) and are excluded from the
  exit gate, which is now nonzero iff **unwaived** errors remain;
  `--format json` keeps the underlying `"severity": "error"` and adds
  `"status": "waived"` plus `waiver_reason`/`waiver_issue`, so the
  severity-keyed `kct audit` manufacturing gate stays blocking by default.
  Waiver entries that match nothing surface as non-gating `UNUSED WAIVERS`
  advisories (`unused_waivers` in JSON) rather than being injected into the
  parsed report. `--mfr` compatibility mode does not apply waivers (a
  design-intent waiver does not change what a fab can build) and says so.
  With no flag and no sidecar present, output and exit codes are unchanged.
  Replaces the downstream `drc_waivers.py` re-implementation in
  project-shamrock/chorus.
- **`--format json` on the 16 mutating `kct sch` subcommands** (part of
  #4674, second batch of the #4543 machine-output sweep) — `sch
  add-bypass-cap`, `add-component`, `add-junction`, `add-label`,
  `add-no-connect`, `add-pull-resistor`, `add-wire`, `disconnect`,
  `insert-inline`, `reconnect-pin`, `replace`, `set-footprint`,
  `set-label-direction`, `set-reference`, `set-symbol-property` and
  `set-value` now accept the canonical `--format json` and emit one
  deterministic change-summary document on stdout
  (`{"command", "schematic", "dry_run", "success", ...}`) instead of prose.
  Each of the 16 lives in its own inner module, so they share a new
  wrapper (`cli/sch_json.py`) that brackets the existing prose
  implementation rather than rewriting 16 report writers: prose is
  captured and discarded, `record(...)` accumulates the per-command
  summary, and captured stderr is replayed after the document (and
  becomes the `error` value on failure). Exit codes and text-mode output
  are unchanged. The `sch` shim's own "file not found" guard now emits
  the same `{"error": ...}` document. Audit
  (`scripts/audit_machine_output.py`): prose-only 49 → 33,
  format-json 148 → 164.
- **`kct route --report-stage-quality`: per-stage routing-quality
  instrumentation** (part of #4732, slice 1) — an opt-in, read-only probe
  that measures fragment/staircase fractions, the true-45° share, the
  zero-length count and the median segment length at each of the four
  post-route mutation boundaries (`pre-optimize` → `post-optimize` →
  `post-nudge` → `post-finalize`) and prints an advisory table with the
  stage-to-stage deltas. #4615 measured only the *end* of that chain, so
  nothing said which stage leaves the artifacts behind; this attributes
  them. The metric core moved into a shared
  `compute_routing_quality_from_records()` in
  `analysis/routing_quality.py` (a neutral
  `(start, end, layer, net)` record shape) so the router's in-memory
  segments are measured with exactly the definitions the `kct check`
  reporting (#4646) and opt-in gates (#4651) consume — per-stage numbers
  are directly comparable to what `kct check` reports for the same
  board. The probe never touches routes, the grid, or any optimizer
  config: with the flag on vs. off, the routed copper and exit code are
  identical, and the flag-off argv/output are unchanged. Wired into all
  four `kct route` output paths (base + the three escalation paths).
  First measurements: on board 03 the optimizer cuts segments 6838 → 621
  (-91%) but the fragment *fraction* only moves 99.1% → 82.8%, and the
  DRC nudge is essentially fraction-neutral while introducing a
  zero-length segment — evidence for the "optimizer never targets
  sub-0.25 mm fragments as such" hypothesis rather than the "nudge
  re-introduces jogs" one.

- **`kct check` now detects isolated copper natively: `isolated_copper`**
  (closes #4680, second slice — the dangling pair shipped separately
  below) — a new `IsolatedCopperRule` (`validate/rules/zone_fill.py`,
  CLI category `isolated_copper`) flags committed zone-fill islands
  connected to nothing, closing the last of the three kicad-cli rule
  classes previously invisible to a kct-only workflow (199 of the 268
  missed findings on the reporting board). Each committed
  `filled_polygon` is one island; an island is isolated when its
  transitive same-net copper cluster contains no pad (see the #4729
  entry under **Changed** — this shipped as a narrower "touched by no
  same-net copper" predicate and was completed to KiCad parity before
  release). Detection consumes only the *committed* fill
  polygons (this repo's copper source of truth): when zones were never
  filled the rule degrades silently rather than presenting "0 isolated"
  as a clean bill (`zone_unfilled` separately warns about those zones).
  Findings are `severity="warning"` (KiCad default-severity parity —
  `project_generator.py` already pins `isolated_copper: "warning"`),
  classified reporting-advisory but NOT gating-advisory, with
  kicad-cli-parity messages (`Zone [GNDD] on In1.Cu, priority 0`).
  Cross-verified against `kicad-cli pcb drc` 10.0.5 on the committed
  fills: 0/0 on all 8 repo boards (no false positives), and matching
  zone/layer attribution + message shape on a synthetic orphan-island
  fixture. Remaining known divergences (documented in the module):
  KiCad's zone island-removal settings (`island_removal_mode`,
  `island_area_min`) are not modeled, and track arcs participate as
  chord-approximated conductors only. The shared per-layer copper indexing
  moved to a reusable `build_copper_layer_indexes()` in
  `dangling_copper.py` so both #4680 detectors consult identical
  committed-copper geometry.

- **`--format json` on the grouped-family subcommands (first #4674 batch:
  24 surfaces)** — the mechanical sweep executing #4543's canonical
  machine-output idiom, batch 1. `kct mfr
  list/info/rules/compare/export-dru/apply-rules/validate`, `kct spec
  init/validate/status/decide/check`, `kct placement
  fix/nudge/snap/align/distribute`, `kct zones add/batch/fill/hv-keepout`,
  and `kct benchmark run/compare` all accept `--format {text,json}`
  (threaded outer parser → shim → inner parser via the
  `format_options.py` helpers), and `kct benchmark report` gains a `json`
  choice — closing the `format-nojson` audit bucket. JSON mode emits one
  deterministic document on stdout (sorted keys; progress/chatter
  suppressed or on stderr; errors become `{"error": ...}` documents with
  the same exit codes); text output is byte-identical to before. The dead
  inner-only `mfr rules --json` surface (unreachable through `kct`;
  documented in `docs/reference/machine-output.md`) is retired in favour
  of the wired `--format json`. Audit: prose-only 72 → 49; `--format`
  with `json` 124 → 148. New regression guards in
  `tests/test_format_json_sweep.py`; remaining backlog (16 `sch` mutating
  commands, `stitch`, long-tail singles) tracked on #4674.

- **`kct check` now detects dangling copper natively: `track_dangling` +
  `via_dangling`** (part of #4680, the #4612 gap shape for the remaining
  rule classes) — a new `DanglingCopperRule`
  (`validate/rules/dangling_copper.py`, CLI category `dangling_copper`)
  flags tracks with a free end (one warning per track, at the first
  dangling endpoint — antenna stubs / abandoned routes) and vias bonding
  copper on fewer than two layers, closing two of the three kicad-cli
  rule classes previously invisible to a kct-only workflow. The
  termination predicate mirrors KiCad's geometric connectivity: an
  endpoint is terminated by any other same-layer copper (segment cap or
  body, track-arc chord, via barrel, pad, or **same-net** committed zone
  fill) within `DRC_TOLERANCE`; via bonding is counted per spanned
  copper layer. Both emit `severity="warning"` (KiCad default-severity
  parity — never fab-blocking) and are classified reporting-advisory but
  NOT gating-advisory. Cross-verified against `kicad-cli pcb drc`
  10.0.5: exact count+location parity on a synthetic fixture and on 8/9
  repo boards (including 3/3 track_dangling on board-05 and 12/12
  via_dangling on board-07); the single divergence is a stub whose end
  lies 0.14 mm outside a *committed* fill that `--refill-zones`
  recomputes over — without refill kicad-cli agrees 3/3, i.e. kct is
  exact on the committed copper it treats as source of truth. Known
  limits documented in the module: track arcs terminate neighbours but
  are not themselves tested for dangling; `isolated_copper` (the third
  #4680 class) is a follow-up slice. `kct fix-drc` is deliberately
  unaffected: its pure-Python DRC fallback filters both advisories out
  (`_UNREPAIRABLE_TOPOLOGY_RULE_IDS`) because no clearance nudge can
  repair a free track end, so a pre-existing stub cannot flip `fix-drc`
  to the "non-repairable violations detected" exit code — `kct check`
  remains the place these findings surface.

- **`kct doctor` environment-preflight check group** (#4542, from the
  copperhead survey #4520 idea 5) — alongside the existing version-record
  drift check, `kct doctor` now runs four fail-soft runtime-prerequisite
  preflights, each reported `ok`/`warn`/`fail` with a human detail and an
  actionable remedy: `native-backend` (wraps `probe_backend_info()`; missing
  `.so` is a loud `warn` — the most common cause of "router is slow" false
  alarms), `kicad-cli` (presence is `fail` when absent, KiCad-8+ version
  floor is `warn`), `python-env` (Python >= 3.10 floor + shapely core-dep
  importability, both `fail` — a broken/partial install), and `kct-path`
  (warns when a PATH-resolved `kct` shadows a different project-pinned
  version — a stale global install makes new subcommands read as
  `invalid choice`). `--format json` output is additive: the version-drift
  keys are unchanged and the new group lands under a top-level
  `"environment"` key. Default exit stays advisory 0; `--strict` now exits
  1 on drift **or** any `fail`-status preflight (`warn` never affects the
  exit code). A missing tool is always a reported `fail` row, never a
  traceback.
- **`--transactional` snapshot/rollback for in-place board mutations**
  (#4541, from the copperhead survey #4520 idea 4) — new file-scoped
  transaction helper `kicad_tools.transaction.board_transaction()` snapshots
  the mutated file's bytes on entry and, on failure (exception, Ctrl-C, or
  an explicit `rollback()` on a non-zero exit path), restores it
  byte-identical via an atomic tmp-sibling + `os.replace` write while
  preserving the failed attempt as a `<file>.failed-<UTC-timestamp>`
  forensic sidecar (suppressible with `keep_failed=False`). Success leaves
  no litter; unchanged files are skipped on rollback; files created inside
  the transaction are removed again; subprocess mutations of the file are
  covered too. Adopted as an opt-in `--transactional` flag on two flagship
  commands: `kct fix-drc` (wraps the whole multi-pass run; rolls back on
  exit 1 no-progress and exit 3 connectivity-rollback, while exit 2 partial
  repair keeps the applied fixes; the existing per-pass connectivity
  snapshot machinery is unchanged) and `kct pcb sync-netlist` (rolls back
  on any non-zero exit; with `-o`, a partially-written output file is
  removed and the input is never touched). Default behavior of both
  commands without the flag is byte-for-byte unchanged. The git-scoped
  variant (repo-wide `reset --hard` + `clean -fd` per copperhead) was
  rejected — see the issue for rationale; broader adoption across the other
  in-place mutating commands is a follow-up backlog.

- **Opt-in routing-quality threshold gate for `kct check`** (#4651) — new
  `--max-fragment-fraction` / `--max-staircase-fraction` flags (on both the
  `kct` and standalone `kct check` parsers) turn the #4623 advisory
  routing-quality metrics into a real gate: when a supplied ceiling
  (0.0–1.0) is exceeded, an error-severity
  `routing_quality_fragment_fraction` / `routing_quality_staircase_fraction`
  violation is emitted and the check exits 2 (a fraction exactly equal to
  the ceiling passes). The `routing_quality` JSON object gains the applied
  `thresholds`, a `gate_passed` verdict, and per-metric `gate_breaches`
  when gating is enabled, so CI consumers can see why the gate fired.
  Without the flags, behavior is byte-identical to the advisory-only
  contract — no board starts failing without opting in. Because gating is
  an explicit opt-in it also engages under `--drc-only`, and a metrics
  crash with gating requested is a hard exit-1 (an explicitly requested
  gate never silently passes because it could not measure). Threshold
  evaluation lives in `kicad_tools.analysis.routing_quality`
  (`evaluate_routing_quality_thresholds` / `ThresholdBreach`, exported
  from `kicad_tools.analysis`).

- **`zero_length_segment` DRC rule — zero-length copper segments are now
  real, waivable findings** (#4651) — zero-length segments (start == end)
  are always routing artifacts; the #4623 metrics counted them
  (`zero_length_count`) but nothing reported them. A new
  `zero_length_segment` rule (registered in `DRCChecker.check_all` and as
  a `kct check` `--only`/`--skip` category) emits one warning per
  zero-length segment with net, layer, and position, and flows through
  the central `.kct_waivers.json` mechanism (#4417) — waivable per
  segment (by the `layer@(x,y)` `items` id, which matches the reported
  location) or per net (by `nets`). Detection reuses the analysis
  module's coordinate tolerance, so the advisory stanza's count and the
  rule's finding count always agree. Severity is warning (fatal under
  `--strict`), not error: the #4651 fleet pre-check found board-05's
  routed artifact carrying 5 zero-length segments, so error-by-default
  would have broken a shipping board.

- **LLM-free doc-drift sub-gate in `kct check`** (#4540, from the
  copperhead workflow survey #4520 idea 3) — new advisory `doc_drift`
  category (`--only doc_drift` / `--skip doc_drift`) that diffs opt-in
  `<!-- kct:doc-pin <resolver> <key> = <value> -->` markers in the board
  README against machine ground truth via a resolver registry
  (`src/kicad_tools/validate/doc_drift.py`). v1 ships one resolver,
  `drc-tolerance` (the per-board pin in
  `.github/routed-drc-tolerance.yml`; an absent entry resolves to the
  yml's strict-0 convention), and two INFO-severity rule ids:
  `doc_drift_stale_pin` (claimed value != resolved ground truth, message
  quotes doc `file:line`, both values, and the source) and
  `doc_drift_unresolvable_pin` (unknown resolver / typo'd routed-PCB
  path, so the gate can't rot silently). Findings never enter
  `error_count`, the verdict, exit codes, or any fab/tapeout gate; free
  prose is never parsed, and zero markers / missing README / missing
  tolerance file all pass silently (bootstrap carve-out). Markers are
  onboarded for board 06 (18) and board 07 (8) — fixing board 07's stale
  "currently 80" README prose to the actual pin of 8 — so a future yml
  ratchet without the README edit trips the check. v2 candidates
  (BOM-vs-schematic fixed-column contract, version strings, escalation
  to WARNING) are deferred.

- **`--format json` is now the canonical machine-output spelling across the
  `kct` CLI** (#4543, from the copperhead survey #4520 idea 6) — new design
  note `docs/reference/machine-output.md` records the decision, the audited
  per-subcommand inventory (199 leaves as of this change: 124 already on
  `--format json`, 72 prose-only, 4 exempt, 1 deferred to the route
  workstream), and the rules for new commands; a new audit tool
  `scripts/audit_machine_output.py` re-measures the inventory from the real
  argparse tree. The two remaining `--json`-only commands gained the
  canonical spelling additively: `kct placement refine --format json` and
  `kct calibrate --format json` now work end-to-end (outer parser, shim
  forwarding, inner parser) with the existing `--json` kept forever as a
  documented legacy alias (either spelling requesting JSON wins), and the
  7 `kct footprint generate` shape subcommands accept `--format json`
  alongside their legacy `--json`. Shared helpers live in
  `src/kicad_tools/cli/format_options.py`, and a new regression guard
  (`tests/test_machine_output_idiom.py`) fails if any leaf subcommand ever
  carries `--json` without the canonical `--format json`. Adding machine
  output to the 72 prose-only subcommands is the separate mechanical sweep
  (#4674).

- **Mirror (layer-flip) strategy for the placement-delta feedback loop**
  (#4560) — the stuck-classifier's `DE_REVERSE_BUNDLE` verdict (a reversed
  facing pad column, e.g. board-07's DDR byte on U2) now proposes an
  actionable **mirror** delta instead of `rotate_180`: rotation preserves
  the pin column's chirality, so it structurally cannot un-reverse a
  mirrored pin order (measured on board-07 CI: routed 25 → 14, reverted).
  The new vertical slice: `StrategyType.MIRROR_COMPONENT` +
  `StrategyApplicator._apply_mirror_component` performs a KiCad-semantics
  **left/right flip about the footprint's own anchor** — footprint layer
  side-swap, rotation `θ → 180 − θ`, pad local `(x, y) → (x, −y)`, pad
  absolute angle `a → 180 − a`, pad layer lists side-swapped
  (through-hole `*.Cu`/`*.Mask` untouched), texts/graphics moved to the
  correct side. The transform is pinned **empirically against KiCad
  10.0.5**: a committed golden fixture pair
  (`tests/fixtures/mirror_flip/`, generated by KiCad's own
  `FOOTPRINT.Flip`) is asserted equal to the applicator's output, plus a
  `kicad-cli pcb drc` cross-gate. Serialization correctness:
  `Pad.__setattr__` write-through now also syncs `position` and `layers`
  into the S-expression tree (previously silently in-memory-only — a
  flip would have saved a half-flipped board). The feedback loop gained a
  `kind="mirror"` delta (`_strategy_from_delta`,
  `_apply_delta_to_router_pads` mirrors router pads across the anchor's
  vertical axis and swaps SMD `F_CU ↔ B_CU`), and **all four
  snapshot/revert paths are now layer-aware** (footprint layer, pad
  positions/angles/layer lists, router-pad layers), so a reverted probe
  restores the board exactly. Because footprint texts/graphics carry no
  S-expression back-reference and so cannot be snapshotted, a reverted
  **mirror** additionally re-applies the flip (an involution) before the
  placement restore — without that, a run that reverts a mirror and then
  keeps a later delta would `save()` a board whose copper is on the front
  while its silk/fab is mirrored onto the back. The placement-diff artifact
  records
  `old_layer`/`new_layer` (a flip at rotation 90 changes neither position
  nor rotation, so the layer is the signal). The `rotate_180` delta kind
  remains fully supported for committed artifacts; only the
  `DE_REVERSE_BUNDLE` proposal changed. Acceptance thresholds, budgets,
  and the board-07 committed artifacts are unchanged.

- **`kct pipeline` route-skip under `--voltage-map` now auto-runs a
  `kct creepage` audit as the skip gate** (#4649) — refining the #4607 loud
  refusal: when the board is already `>=`95% routed (recommend_skip) and no
  `--force` is given, the route step spawns a non-destructive
  `kct creepage <board> --voltage-map ... --standard ... --pollution-degree
  ... --material-group ... --census-threshold ...` subprocess on the
  existing copper instead of refusing outright. The destructive re-route
  stays skipped, and the route step succeeds iff the audit exits clean; any
  non-zero exit (including exit 2, the #4354 EXIT_HV_UNCLASSIFIED vacuity
  guard, which is called out distinctly in the failure message) aborts the
  pipeline. The gate is strict `returncode == 0` — deliberately not routed
  through the shared soft exit-2-5 subprocess semantics. If the audit
  subprocess cannot launch at all, the step falls back to the #4607
  refusal message (manual `kct creepage` or `--force`), and `--dry-run`
  reports the would-be audit without spawning anything. Skips without a
  voltage map, forced re-routes, and the actual-route exit-3/4 escalation
  are byte-identical to before.

- **Lattice keepout rule areas: `*.In.Cu` wildcard + unknown-zone-name
  warning + pair-leg decline coverage** (#4672, the #4605 review follow-ups)
  — the `*.In.Cu` layer spec on a keepout rule area now resolves to every
  inner copper layer of the routing stack (previously it was silently
  dropped, leaving multilayer boards unprotected on inner layers; on a
  2-layer stack it correctly resolves to the empty set). A
  `spatial_keepouts` sidecar entry naming a nonexistent rule-area zone now
  emits a stderr warning listing the known zone names (with a distinct
  message when the name matches a pour-void-only area that never constrains
  routing, e.g. `kct zones hv-keepout` output); exit code is unchanged. The
  previously untested `pair-leg-keepout` finish-gate decline (a fat coupled
  pair whose emitted leg would enter a rule area declines and emits
  nothing) now has direct test coverage. No behavior change for well-formed
  inputs.
- **Board-05 escape-corridor-reservation opt-in re-added after CI
  blocking-count validation** (#4548) — `boards/05-bldc-motor-controller/design.py`
  now passes `--escape-corridor-reservation` in its `kct route` recipe. The
  untuned reservation regressed the board's blocking gate when first opted
  in (PR #4509, reverted); #4519 / PR #4547 made it a selective, bounded,
  inner-layer signal (2886 reserved cells over layers [1,2] on this board
  vs the 25875-cell / all-layer regression footprint), and this opt-in is
  gated on the CI-measured `blocking_incomplete_count` staying at or below
  the pre-#4548 main baseline of 6 (run 31150150866; per #3822 the CI job
  is the only authoritative instrument for board-05). The guard test
  `tests/test_board_05_batch_completion.py` is flipped to pin the flag IN
  the recipe (same drift protection, inverted). PHASE_A/B/C remain in
  `skip_nets`; un-skipping them is the follow-on payoff step, taken only on
  CI-measured evidence.

- **Local CI-equivalent gate script as an Actions-outage backstop** (#4671)
  — new `scripts/ci/local-gate.sh` mirrors the `.github/workflows/ci.yml`
  job set locally: `--cheap` (default) runs the six cheap gates,
  `--full` adds the long board end-to-end jobs, `--release` adds the
  board-03 routing baseline + changelog gap report used to ship v0.20.0
  through the 2026-08-06 outage, and `--list`/per-job selection are
  supported. Advisory jobs (diffpair/matchgroup regressions) report but
  never flip the exit code, and a missing `kicad-cli` yields SKIP, both
  mirroring CI. A new drift guard
  (`tests/test_local_gate_manifest.py`) asserts the script's manifest
  stays in sync with the ci.yml job ids. RELEASING.md documents the
  outage-fallback procedure (operator sign-off required) and records the
  ephemeral-runner evaluation verdict.

- **Stderr warning when `merge_dru_floors` migrates a legacy sidecar**
  (#4676) — the pre-#4600 legacy-replace branch added in #4667 now emits a
  one-line stderr notice naming the `.kicad_dru` file (threaded from both
  `kct check --emit-dru` and `write_drc_constraints` via a new optional
  `path` kwarg), so a hand-authored file caught by the detector's narrow
  documented false-positive window is replaced loudly instead of silently.
  The other merge paths (fresh write, marked-block replace, user-content
  append) stay silent, and merged output is byte-identical to before.

- **`kct creepage --format json` now serializes the exit-code verdict**
  (#4687) — the report-level JSON always carries `gate_passed` (the
  waiver-aware verdict the CLI exit code follows) and `waived_count`
  alongside the existing raw, waiver-blind `passed`, in both the phase-1
  (`--min`) and standard-derived schemas. Previously the only verdict field
  in the document was `passed`, so under `--waive-same-footprint` a JSON
  consumer read `passed: false` while the process exited 0. Additive keys
  only; `passed` semantics, per-pair `pass`/`waived` serialization, and the
  `kct audit` manufacturing-readiness gate are unchanged.

### Changed

- **`kct route` now runs a topology-preserving collinear consolidation pass
  after the DRC nudge** (#4732, slice 2 — follows the slice-1
  `--report-stage-quality` instrumentation in #4746) — the post-route
  pipeline ended `optimize -> nudge -> finalize` with nothing that
  consolidated the copper the nudge produced, and slice-1 measurement showed
  the existing `TraceOptimizer` leaving **93.1% sub-0.25 mm fragments** on
  board 03 with the median segment length pinned exactly at the 0.05 mm grid
  step. Per-pass instrumentation pins the cause on the issue's **hypothesis 1
  (collision conservatism)** and rules the rest out: `merge_collinear` takes
  6838 -> 1490 segments but has **2725 of its 8075 candidate merges (33.7%)
  vetoed by `path_is_clear`**, each veto chopping a collinear run in two;
  every later pass is flat (zigzag 1490 -> 1490, staircase 1490 -> 1485,
  45-corner 1485 -> 1488, pull-tight 1488 -> 1486), the DRC nudge is flat
  (hypothesis 4 is *not* the cause on this board), and
  `TraceOptimizer.optimize_route`'s revert-on-regression connectivity guard
  fires **zero** times across all 37 routes. Those vetoes are false positives
  by construction — a merged `A-B` + `B-C` adds no copper anywhere, so the
  grid checker rejects it only because rasterizing one long clearance
  envelope touches cells the two short ones did not. The new
  `router/optimizer/consolidate.py` pass therefore needs no collision checker
  to be safe, because it changes neither the copper nor the topology it would
  be judged on: it removes only vertices of degree **exactly 2** whose two
  incident segments are collinear, anti-parallel about the vertex, and share
  layer / net / width, so every junction, terminal, via and pad vertex
  survives by construction and the merged segment is the **exact union** of
  what it replaces (checked, not asserted — a length guard rejects any run
  whose merged length differs from the sum of its parts, and on the written
  artifact every point of the pre-pass copper lies on the post-pass copper
  and vice versa to 3e-14 mm, with per-`(net, layer)` trace length identical
  to 1e-6 mm). Vertex keys are
  deliberately layer-blind to match `validate_net_connectivity`'s own
  layer-blind endpoint union-find; pad and via positions are protected
  points; zero-length segments are counted toward vertex degree (so they
  block a smoothing) but never merged, and the pass provably introduces
  none. It commits through the same grid transaction the optimizer uses
  (`apply_route_transform_grid_synced`, factored out of
  `optimize_routes_grid_synced` — #3507/#3511 semantics unchanged) and is
  wrapped in the same connectivity snapshot/enforce guard, with
  `_finalize_committed_copper_or_demote` still the unconditional backstop.
  Measured with everything else held fixed (same seed, same flags, only the
  pass toggled): **board 03** 1486 -> 252 segments, `fragment_fraction`
  93.1% -> 57.5%, 45°-diagonal share 7.3% -> 38.1%, 13/13 nets;
  **board 02** 476 -> 295 segments, `fragment_fraction` 80.7% -> 58.3%,
  8/8 nets. DRC state is identical before/after on both boards under
  `kct check` (0 errors) *and* the `kicad-cli pcb drc --refill-zones`
  cross-gate (identical violation and unconnected-item histograms), and
  `kct pcb net-audit` output is byte-identical apart from the input path.
  Gated exactly like the optimizer: `--no-optimize` / `--raw` bypasses it,
  and `--route-engine lattice|mesh` without `--lattice-optimize` never
  reaches it (#4281). `--report-stage-quality` gains a `post-consolidate`
  row, recorded only when the pass actually ran. No board artifacts were
  regenerated — all measurement ran in a scratch directory.
- **`isolated_copper` now implements KiCad's pad-in-cluster predicate**
  (closes #4729, follow-up to #4680 / #4728) — an island is isolated iff
  its **transitive** same-net copper cluster contains **no pad**, where
  tracks, track arcs and via barrels are *conductors* that extend the
  cluster rather than terminals that satisfy it. Previously any same-net
  copper touch cleared an island, making kct's findings a strict subset
  of kicad-cli's: an island held only by pad-less copper (a floating
  stub, an orphaned via, a dead pour arm) was silently under-reported,
  and the blind spot compounded with #4680's first slice, where the same
  stub terminates on the committed fill and so is not `track_dangling`
  either. The clustering pass is a new `cluster_copper_kinds()` in
  `validate/rules/dangling_copper.py` (Option A of the issue's
  architecture choice: it extends the shared committed-copper index
  `build_copper_layer_indexes()` whose `_CopperItem.kind` already carried
  the pad signal, leaving `connectivity.py` and both dangling-copper
  predicates untouched). Union-find over islands + copper items with
  same-layer geometric touch inside `DRC_TOLERANCE` plus a cross-layer
  union of the per-layer instances of one via barrel / through-hole
  `*.Cu` pad, so a pad reached over a multi-hop track chain or across a
  via on another layer still clears an island. Every edge requires a net
  match (resolved number first, name fallback) and a feature with no net
  identity at all never unions — deliberately conservative, because an
  over-eager union *suppresses* findings invisibly. **Finding counts can
  only grow**; severity (`warning`), the advisory reporting category,
  gating behavior and the degrade-silently guard on unfilled zones are
  unchanged. Verified against kicad-cli 10.0.5 on the committed fills
  (no `--refill-zones`) using the new
  `tests/fixtures/drc/orphan_island{,_pad,_transitive}.kicad_pcb`
  fixtures — exact parity at 2 / 1 / 1 findings (was 1 / 1 / 1) — and the
  8-board fleet stays 0/0, cross-checked with kicad-cli on boards 03 and
  05.

- **`kct check`'s connectivity DRC rule now uses the strict real-geometry
  model by default** (#4673, the #4557 follow-up) — `ConnectivityRule`,
  `DRCChecker(strict_connectivity=...)`, and both `kct check` CLI parsers
  now default to deciding segment↔segment / segment↔pad / segment↔via
  unions by real shapely copper-shape intersection (KiCad semantics,
  #4176) instead of the legacy 0.01mm endpoint-proximity tolerance, so
  `kct check` finally agrees with `kct net-status` (strict default since
  #4557) and with `kicad-cli`: endpoint-proximity false opens on poured
  boards can no longer appear, and over-connected nets the legacy model
  passed now correctly fire. Fleet verification (boards 00-07 committed
  routed artifacts) measured **zero count change** — the pour residuals
  the legacy model once reported are already absorbed by the #3914
  advisory suppression and the #4229 pad-in-pour fix, which apply in
  both models, so the flip removes the model divergence without moving
  any baseline. New `--legacy-connectivity` flag opts back
  into the old proximity model; `--strict-connectivity` is kept as an
  accepted no-op for script compatibility (it now restates the default)
  and `--legacy-connectivity` wins when both are given. Strict mode
  requires shapely (a core dependency since #3824) and fails loud if it
  is missing. `connectivity` remains classified advisory and excluded
  from the routed-DRC CI gate, so blocking-gate behavior is unchanged.
  The known blind-via/unspanned-pad over-connect residual (a blind via
  whose 2D copper overlaps a pad on a layer the via does not span still
  bonds to it) is documented and pinned by a test rather than fixed —
  rare geometry, and the failure direction matches the legacy model's
  over-connect bias.

- **`tests/benchmark_results.json` untracked and gitignored** (#4684) — the
  routing-benchmark test (`tests/test_router_integration.py`) rewrites this
  file with wall-clock timings on every run, leaving the working tree
  permanently dirty. Nothing reads the file and the tracked snapshot's
  content was vacuous (all-zeros results), so it is now a local-only
  artifact: removed from the index, added to `.gitignore`, and documented
  as such in `tests/README.md`. The test still writes it locally —
  behavior is unchanged for local inspection.

- **CI: GitHub Actions bumped off deprecated Node 20 runtimes** (#4677) —
  every job log printed "Node.js 20 is deprecated" because six actions
  across five workflow files were pinned to Node-20-era majors. Bumped:
  `actions/checkout` v4→v7, `actions/setup-python` v5→v7,
  `actions/github-script` v7→v9, `actions/upload-artifact` v4→v7,
  `actions/download-artifact` v4→v8, and `astral-sh/setup-uv` v4→v9.0.0
  (exact pin — setup-uv stopped publishing major tags at v8). setup-uv
  invocations gained an explicit `enable-cache: false` to preserve the
  pre-v5 no-cache behavior (v5 auto-enables the Actions cache on hosted
  runners and v9 stopped pruning it before upload). The `ci.yml` container
  jobs keep their deliberate curl one-liner installs. No behavior change
  intended beyond the runtime bump.

### Fixed

- **The pure-Python fallback A* is now pairwise (HV-isolation) aware at search time** (#4507, epic #4431 Phase 2) — the hot-loop bitmap, `_is_trace_blocked`/`_is_via_blocked` kernels and the C++ backend's fallback-router threading now mirror the C++ `cross_domain_*` search kernels (same domain projection, per-pair widened radius, layer-scoped #4506 attach-zone waiver, out-of-bounds-is-empty ring dilation), so a net that falls back on an HV board converges instead of thrashing against the Phase-1 post-route gate; fully dormant without `--voltage-map`.

- **`kct ipc push-routes` can now actually read a board** (#4788) — the
  handler imported the nonexistent `kicad_tools.pcb.parser` (dead since
  #2363) behind an `except ImportError`, so every invocation exited 1 with
  "PCB parser not available." It now loads boards via the first-party
  `kicad_tools.schema.pcb.PCB.load()` (unguarded, so wiring regressions fail
  loudly), reads the real model attributes (`segments`, `start`/`end`/
  `position` tuples, `net_number`) instead of `getattr(..., 0)` fallbacks
  that would have pushed zero-length tracks at the origin on net 0, and the
  `--net` filter matches by net name against `PCB.nets`; an unknown net name
  or unparseable board is now a clear error (exit 1) in both text and JSON
  modes.

- **A single relief-rescue transaction is now proportionally bounded** (#4781)
  — a rescue (including its depth-1 nested rescues, which share the parent's
  allowance) may spend at most `max(25% of the remaining stage budget, 90 s)`
  before rolling back verbatim at the existing `_past_deadline()` check sites;
  previously one board-07 transaction burned 279 s (46% of the 600 s stage)
  and still rolled back, starving the rip-up iteration that lands the 26th
  net. No-deadline stages (board 06's deterministic regen, the two-phase
  stall-relief hook) are byte-identical; a `Relief-rescue transaction bound:`
  log line records which arm was live.

- **The copper-moving `--lattice-optimize` post-passes now consult the pairwise
  HV creepage predicate before accepting a move** (#4766, epic #4431) — PR
  #4756 widened the post-route pairwise audit but left the geometric
  post-passes pairwise-*blind*: `TraceOptimizer` (via
  `optimize_routes_grid_synced`) gates its moves only on
  `CollisionChecker.path_is_clear`, and both checker implementations resolve
  clearance as the **scalar** `grid.rules.trace_clearance`, while
  `drc_verify_and_nudge` translates segments by up to 0.2 mm with the same
  scalar model. Either pass could therefore close a creepage gap the search had
  deliberately opened, leaving the audit to fail a run the optimizer had just
  broken. Both now run the same layer-scoped predicate the search and the #4588
  gate run: the optimizer gets a route-level accept hook on
  `apply_route_transform_grid_synced` that vetoes an optimized route
  (`route_pairwise_violation` against `grid.routes`) **before** the grid
  transaction is entered, and the nudge brackets itself with a board-level
  `find_pairwise_violations` scan and reverts every route named in a net pair
  that is present after but not before, using the entry snapshot it already
  takes for the #3507 resync. Both gates only undo what the pass would
  *introduce* — an inherited shortfall on the same net pair keeps its
  optimization and stays the audit's to report, so the passes never mask copper
  the gate exists to fail on. Collinear consolidation is deliberately **not**
  gated and now says why in its module docstring: its merged segment is the
  exact union of the segments it replaces, so no gap to foreign copper can
  shrink. The veto is coarse (a route loses its whole optimization for one bad
  move), so the counts are surfaced — vetoed routes on stderr after the
  optimize pass and `pairwise_reverts` in the DRC-nudge summary — and the nudge
  re-reports `remaining_violations` post-revert rather than the flattering
  pre-revert count. Both passes derive their context through the **single**
  router resolver #4785 landed for exactly this — `PairwisePathChecker.
  from_router` (`rules.pairwise_clearance`, `_pairwise_attach_zones_cache`,
  `net_names` + `_net_name_to_id()`, `grid.routes`) — so the two post-passes,
  the path-level predicate and the #4588 audit cannot resolve the table, the
  id→name map or the #4506 zones differently; that resolver picks up three
  fixes in the process (dormant on a table that widens nothing, `sorted()`
  net-name tie-break so an id collision is deterministic, and it now arms on a
  grid-less router by falling back to `router.routes`). All eight CLI call
  sites are untouched. Fully dormant without `--voltage-map`: no table means no
  checker, no gate and no scan, and no board under `boards/` uses that flag. No
  C++ change (`ROUTER_CPP_BUILD_VERSION` stays 19).
- **The C++ router's HV attach-zone waiver is now layer-scoped, so the search
  stops proposing copper the pairwise gate rejects** (#4507, epic #4431 Phase
  2) — PR #4756 narrowed the #4506 rated-footprint exemption on the Python side
  (a zone waives the pairwise creepage widening only on layers where *both*
  nets have pad copper) but deliberately left the C++ `Grid3D` zone halo
  layer-agnostic, with the layer-scoped Python `route_pairwise_violation` as
  the acceptance check. That left the C++ search and the Python gate
  disagreeing exactly where it hurts: the search would neck an HV net through a
  rated part's pad field on a layer that part has no copper on, its own
  `validate_route` waived it, and the layer-scoped post-check then rejected the
  result — burning the net's resume budget and dropping it into the 10-100x
  slower pure-Python fallback. `AttachZone` gains a per-net `net_layers` map
  (net id -> grid layer indices), `Grid3D::attach_zone_exempts` gains a `layer`
  argument, and every consult in `validate_route` and the search-time kernels
  (`cross_domain_trace_blocked`, `cross_domain_via_blocked`) passes the layer
  the compared copper actually shares — via-vs-via, where no single layer
  applies, stays agnostic, matching the lattice engine's `exempt_pt_pt`
  convention. The segment-vs-pad branch keys on the *pad's* layer: that loop
  already filters mismatched layers before the zone consult, so the only shape
  where the two candidate keys differ is a through-hole pad, which keeps waiving
  on every layer because the barrel spans every layer. On a two-domain fixture
  whose only cheap path threads a rated part's pad field on a layer it does not occupy,
  the layer-agnostic baseline exhausts 5 resume attempts, falls back to the
  Python A* and **fails to route**; the layer-scoped search dives to the layer
  the part actually licences and converges on the first attempt, gate-clean.
  The layer projection now has a single implementation
  (`pairwise_clearance.project_zone_layers`) shared by the lattice and C++
  paths, so no engine can drift from the gate. Fully dormant without
  `--voltage-map`; a zone with no layer data keeps the previous agnostic
  verdict. `ROUTER_CPP_BUILD_VERSION` 18 -> 19 (run `kct build-native`).
- **`DETERMINISTIC_RESCUE_DEFAULT`'s docblock now records *why* board 07
  regressed instead of naming the investigation as future work** (#4770) —
  documentation only; the constant's value is unchanged (`False`) and no
  DRC allowlist or reach floor moved. The A/B was re-mined from the three
  recorded CI runs and re-run locally at `HEAD` with the C++ backend built,
  and both halves of the intuitive reading turn out to be wrong. **No net's
  rescue flips commit-vs-rollback**: on board 07 the deterministic bound is a
  straight reach *loss* in the negotiated pass (26/31 → 24/31), because a
  single `DQ3` relief transaction that gives up in 2.6 s under the 10 s wall
  clock instead runs **279.1 s — 46 % of the 600 s stage budget** and rolls
  back anyway, so the pass completes 2 rip-up iterations instead of 5 and
  never reaches the one that produces board 07's 26th net. The advertised
  `+1 net` is then produced downstream by `PlacementDeltaFeedbackLoop`'s
  `run_delta`, whose accept-gate is *relative* (`new_count > pre_count`): the
  depressed baseline admits the `U3 translate` placement delta that `main`
  refuses at 25 → 25, and that delta — not the rescue — carries the `+5 DRC`
  (4× `clearance_pad_segment` at 0.094 mm on `DQ1`/`DQ2` and 0.076 mm on
  `DQ4`/`DQS_N`, 2× `clearance_segment_via` at 0.049 mm on `DQ1`/`DQ2`, 1×
  `match_group_length_skew` with `ADDR_BUS` at 11.030 mm instead of 0.000 mm,
  less the `DQS_N`/`DQS_P` diff-pair pair that stops firing only because
  `DQS_N` went unrouted). Five of the seven new errors sit on copper with no
  connection to the reach delta at all. Recommendation recorded on #4730:
  keep the opt-in permanently. The full journal (per-pass rescue tables,
  per-violation attribution, reproduction recipe) lands in
  `boards/07-matchgroup-test/diagnostic-runs/README.md`;
  `_normalize_deterministic_budget`'s docstring and board 07's README are
  updated in lockstep, and a new `TestPlacementDeltaFeedbackCaveat` pins the
  two structural claims the finding rests on — that
  `PlacementDeltaFeedbackLoop.run_delta` threads no `deterministic_rescue`
  (so "flip the constant, not the CLI" stays true) and that
  `Autorouter._post_negotiation_sweep_bounds` cannot see the flag (so the
  #4159 sweep stays excluded as a confound).
- **Nine polish fixes from the 2026-08-08/09 sweep's judge nits** (#4765) —
  each a small correctness defect in code that shipped last sweep; none
  changes routed copper or an exit code. (A) The `kct route` banner decided
  its `UNBOUNDED` line from the raw `--timeout`/`--per-net-timeout` flags, so
  `--complete --per-net-timeout 0` announced an unbounded lattice negotiation
  while `_apply_complete_localization`'s 60 s per-link backstop was already
  armed; it now keys off the resolved `_resolve_lattice_link_budget` /
  `_lattice_absolute_deadline` values and reports the budget actually in
  force. (B) `--quiet` silently discarded an explicitly-requested
  `--report-stage-quality` table; the opt-in now wins (the `recorder is None`
  guard already carried the entire "not requested" case). (C) The
  "lazy" `StageQualityRecorder` import was a bare `sys.modules` hit — the
  module is already executed by the eager stage-constant import — and is
  hoisted to module scope with an accurate comment. (D) The `45deg%`
  denominator moves from the renderer to a read-only
  `RoutingQualityMetrics.diagonal_45_fraction` property, beside the
  `fragment_fraction` / `staircase_fraction` contract (no new dataclass
  field: `to_dict()` is a stable JSON contract). (E) A DRC finding that mixes
  a pad with a ref-less track item normalizes to a smaller ref set than
  "exact-set" suggests, so a one-ref waiver covers a *class* of findings; the
  behaviour is kept (narrowing it would retroactively un-waive shipped
  sidecars) but is now documented on `apply_waivers_to_report` and in
  `docs/reference/cli.md`, pinned by regression tests, with `nets` as the
  documented narrowing remedy. (F) `kct drc boards/NN/output/drc.json` never
  probed `boards/NN/` for `.kct_waivers.json` (all three of the shared
  discovery probes collapse into the report's own directory); report input
  now falls back to the report directory's parent. (H) The documented
  "4+ trailing tokens are never trimmed" path of `_sync_at_angle` gets its
  first test. (I) The residual #3441 lattice-rescue `UserWarning`
  ("quantisation margin is reduced at fine-pitch pads") predicts a grid-only
  failure mode and is demoted to INFO for `--route-engine mesh|lattice`,
  completing #4761's gate on the sibling branch; every `GridAutoSelection`
  field stays engine-invariant. (J) All ~16 `args.route_engine` reads in
  `route_cmd.py` now go through one `_resolve_route_engine` helper, so a
  `route_engine=None` namespace can no longer resolve to `"grid"` at ten
  sites and `None` at six (including three `strategy=` arguments to
  `load_pcb_for_routing`). (K) The non-grid "Fine-pitch grid analysis:
  skipped" line printed even on boards where the grid path prints nothing;
  it is now gated on the same `has_warnings` predicate the grid path uses.
- **`PCB.import_from_schematic()` no longer drops a `*-netlist.kicad_net`
  beside the user's schematic** (#4763) — the same defect #4750 fixed in
  `pcb sync-netlist`, at the second call site. `export_netlist` defaults
  `output_path` to `<schematic dir>/<stem>-netlist.kicad_net` and leaves the
  file in place after parsing, so a bare call from `import_from_schematic`
  (and from `PCB.from_schematic()`, which delegates to it) littered an
  unrequested export byproduct into the project directory on every import
  where kicad-cli is installed. The export now goes to an explicit path inside
  a `tempfile.TemporaryDirectory`, which is torn down before the call returns;
  `export_netlist` returns a fully parsed `Netlist`, so nothing downstream
  re-reads the file and import results are unchanged. This also stops the test
  suite from rewriting a **tracked** fixture: `TestImportFromSchematicIntegration`
  runs the real kicad-cli against `tests/fixtures/simple_rc.kicad_sch`, so every
  developer with kicad-cli got a dirty working tree from ` M
  tests/fixtures/simple_rc-netlist.kicad_net` after a test run. That fixture —
  an orphaned byproduct with zero consumers, committed by this very bug and
  carrying an absolute developer path in its `(source ...)` line — is deleted.
  New byproduct-location tests (a guard that the stubbed kicad-cli path really
  runs, plus project-dir-listing, temp-cleanup, `str`-path, Python-fallback,
  and export-failure cases) pin the behavior, and the integration tests now
  assert the fixtures directory is unchanged across the call.
- **The mypy baseline gate now runs cold, so a stale `.mypy_cache/` can no
  longer invent a "NEW type error"** (#4767) —
  `scripts/ci/check_mypy_baseline.py` invoked a bare `mypy src/` with no cache
  control of any kind (no `--cache-dir`, no `cache_dir` in `[tool.mypy]`, no
  `MYPY_CACHE_DIR` anywhere in the tree), so every local run reused the
  incremental cache at `<cwd>/.mypy_cache`. CI is structurally the opposite —
  there is no `actions/cache` in `.github/workflows/` and the Type Check job
  pins `astral-sh/setup-uv` with `enable-cache: false` — so CI is always cold
  and local is always warm, and only the local verdict was untrustworthy. The
  cache outlives the tree it was computed from: `.mypy_cache/` is gitignored,
  so `git reset --hard`, `git clean -fd` (ignored paths need `-x`), a rebase,
  a force-push, and a Loom worktree reuse all leave it in place. Because the
  gate diffs a signature multiset over all of `src/`, one replayed cached
  error anywhere trips exit 2 and prints a filename unrelated to the diff —
  which is what cost two independent PR reviews a cycle each during the
  2026-08-08/09 sweep (PRs #4746, #4762), both on a phantom error in
  `recovery/strategy.py`, a file neither PR touched. `run_mypy()` now passes
  `--no-incremental` by default, and `--incremental` (alias `--warm-cache`)
  is the documented opt-in for a tight burn-down loop. Measured cost of the
  default: ~20 s vs ~0.5 s warm on `src/` — a price CI already paid on every
  push. Exit-code semantics (0 within baseline / 1 tool failure / 2 new
  errors), `diff_against_baseline`, `write_baseline`, and the #4558
  version-drift guard are all unchanged, and `.github/mypy-baseline.txt` is
  byte-identical (a new test pins both nanobind signatures so a future
  `--update` cannot silently drop one). `pnpm typecheck` / `pnpm check:all`
  still run a bare incremental `uv run mypy src/`, so the trap and its
  `rm -rf .mypy_cache` remedy are now documented in `CLAUDE.md`, `README.md`'s
  fresh-worktree checklist, and `docs/contributing/development.md`.
- **Guide source citations are symbol-anchored, and a test now keeps them
  that way** (#4764) — a follow-up to #4749 measured 20 of 32 `<file>.py:NNN`
  citations under `docs/guides/` wrong (62.5%), the worst off by 8,569 lines
  (`docs/guides/diff-pairs/04-length-matching.md` sent readers to
  `router/core.py:6926` for `update_diffpair_skew`, which lives at 15495).
  Two could not be repaired by refreshing a number at all — the guides cited
  `_collect_explicit_match_groups`, renamed to `_gather_explicit_groups`, and
  claimed `detect_match_groups` records the reference policy verbatim on
  `MatchGroup.length_match_reference`, when `_resolve_reference` actually
  resolves it into a concrete `MatchGroup.reference_net_id`. All 32 citations
  in the diff-pair / match-group guides are now `symbol` + file-path anchors
  (the convention PR #4753 established), as are the three rotten ones in
  `docs/reference/api.md`, `docs/reference/cli.md`, and
  `boards/03-usb-joystick/README.md`, plus the equally stale `Line` /
  `Source line` table columns in `02-clearance-and-classes.md`,
  `03-impedance-and-sizing.md`, and `04-cascade-safety.md`. Because the rot
  reaccumulated across 8 files even after #4749 was filed, the fix is not
  just the sweep: `tests/test_diffpair_docs.py` and
  `tests/test_match_group_docs.py` each gain `test_no_line_number_citations`
  (bans `\.py:\d+` outright in their guide tree) and `test_cited_symbols_exist`
  (whole-word checks each anchored symbol in **both** the guide and the source
  file it names, so a doc rewrite that drops the anchor and a source-side
  rename both go red). A third suite, `tests/test_docs_source_citations.py`,
  runs the same two checks over the docs the per-subtree suites cannot see —
  `docs/reference/*.md`, `boards/*/README.md`, and the top-level
  `docs/guides/*.md` — which is exactly where an unguarded rewrite of
  `docs/reference/api.md` invented a `Footprint.to_sexp` that has never
  existed (the real round-trip path is `Footprint.__setattr__` →
  `Footprint._sync_attr_node`). That sweep also corrected two further false
  claims the new coverage exposed: `api.md` said `locked` is emitted inside
  `(attr ...)` when KiCad 10 requires the top-level `(locked yes)` form
  (#3457), and `cli.md` attributed the `kct route` exit-code epilog to a
  `build_parser` that does not exist in `route_cmd.py` (both it and the
  `# Exit codes:` block live in `_main_impl`). The 26 citations under
  `docs/investigations/`,
  `docs/research/`, and `boards/07-matchgroup-test/diagnostic-runs/` are
  deliberately untouched — those are dated forensic records where the line
  number *is* the evidence.
- **Host resource exhaustion aborts a route instead of silently changing
  one, and the post-negotiation rescue sweep stops using a wall clock in
  deterministic mode** (#4724) — the residual, load-correlated divergence
  left over from #4536: one board-06 regen taken on a host at load-average
  ~90 re-landed one fewer net in stagnation recovery (`restored 5 / rerouted
  6`) than five same-code runs on a quiet host (`restored 4 / rerouted 7`),
  then diverged chaotically. Both artifacts were valid and passed every
  gate, so nothing failed loudly — the run was simply not the run the same
  command produces on a quiet machine. Two environment-sensitive decision
  points are closed. **(1) Exception transparency:** the router's broad
  `except Exception` handlers exist so a malformed board or a fixture grid
  cannot abort a route, but they also absorbed `MemoryError` — converting a
  *host* failure into a routing *decision* ("this Steiner cell is free",
  "this component has no pitch", "this rip-up did not fire", "use the
  10-100x slower Python backend"). A new `router/resource_guard.py`
  re-raises resource exhaustion (`MemoryError`, `RecursionError`, `ENOMEM`
  `OSError`, and a native `bad_alloc` rethrown as `RuntimeError`) with a
  named `[resource-exhaustion]` line, applied to the seven broad handlers on
  the negotiated reroute path; every ordinary exception keeps its historical
  fallback byte-for-byte. The C++ grid-mirror fallback also now names the
  failing exception type in its log instead of reporting "unknown reason".
  **(2) Sweep budget semantics:** the #4159 post-negotiation rescue sweep
  applied its 60 s whole-pass / 10 s per-net wall clocks even to callers
  running unbudgeted (`timeout=None` / `per_net_timeout=None`) in the
  deterministic-budget mode #4536 established precisely so no wall clock
  decides anything — and a per-net search that runs to its 1M-expansion cap
  costs ~11 s on a CI runner, so the 10 s bound straddled it the same way
  the relief rescue's 10 s sub-search budget did. `_post_negotiation_sweep`
  now selects its bounds through `_post_negotiation_sweep_bounds`, which
  hands an unbudgeted run with an active node-expansion cap to the cap
  (keeping 600 s / 120 s as non-binding Python-fallback backstops) and logs
  which bound is live, mirroring #4536's evidence discipline. The arm
  self-scopes by construction: every caller that passes a `timeout`, and
  every capless run, keeps the historical numbers exactly. Also: the gated
  board-06 determinism smoke test now prints host load-average before,
  between and after its regens (and in its failure message) so a flake is
  attributable to load rather than argued about. No board recipe, DRC
  allowlist or reach floor is touched; reproducing the load-90 divergence
  itself remains out of reach by construction.
- **The relief rescue's deterministic sub-search bound stays opt-in — the
  fleet-default flip is withdrawn** (#4730) — #4536 gave
  `Autorouter.route_all_negotiated` a `deterministic_rescue=` opt-in that
  bounds the rescue's probe / displaced-victim re-land sub-searches by the
  per-net node-expansion cap instead of the 10 s wall clock, taking machine
  speed out of a decision that determines routed reach. The follow-up proposed
  making it the fleet default; the A/B says no. With the bound on by default,
  board 07 — routed through the **negotiated** entry point, not two-phase —
  came back with a changed copper-LVS open set (`DQ3, DQ4, MIPI_DAT0_N` →
  `DQS_N, MIPI_DAT0_P`, breaking the `--expect-opens` drift guard) and
  routed-DRC 8 → 13 against an allowlist main sits exactly at, for +1 net of
  reach. Measured twice, on two heads and two runners. Per the issue's own
  acceptance criteria that is not absorbable, so nothing was re-baselined (no
  DRC tolerance raised, no expected-opens set rewritten, no reach floor
  lowered) and `DETERMINISTIC_RESCUE_DEFAULT` is `False`. **What did ship is
  the switch**: `deterministic_rescue=` is now a per-call parameter on every
  entry point that can reach a rescue — `route_all_negotiated`,
  `route_all_two_phase`, `_create_two_phase_router`, `route_with_escape`,
  `route_with_escape_and_diffpairs`, and the `hierarchical=True` delegation
  (which previously dropped an explicit opt-in on the way to the path that
  actually runs the rescue). The #3471 two-phase stall-relief hook calls
  `_relief_rescue` with 8 **positional** arguments, so the value rides on a
  `functools.partial` bound in `_create_two_phase_router`; before this that
  path had no switch at all. Both paths log which bound is live, so a future
  A/B has its evidence line on every board. Board 06 keeps its explicit
  `deterministic_rescue=True`, which is again the only thing switching the
  bound on for its 21/21 reach gate. Behavior on every board is unchanged;
  reattempting the flip means first understanding why board 07's rescues keep
  copper that carries +5 DRC.
- **`--timeout` / `--per-net-timeout` now actually bound the lattice
  engine** (#4697) — `kct route --route-engine lattice` was time-bounded
  **only** under `--complete`. Everywhere else both flags were accepted,
  echoed in the routing banner, and silently discarded, producing
  observed 60+ minute unbounded runs on `--strategy basic` — the only
  strategy the #4280 gate allows on the lattice. Root cause: the lattice
  negotiates the whole netset in a single `route_netset()` call, so
  `route_all()`'s between-nets `timeout` check can never fire mid-run,
  and the negotiation's only bound (`deadline`) was derived solely from
  `args._complete_link_budget_s`, which nothing but `--complete` stamps.
  Fixed at that seam: `--per-net-timeout` now supplies the per-link
  budget on **every** lattice run (same `budget x link-count` scaling
  `--complete` already used), and `--timeout` supplies an absolute
  monotonic ceiling threaded through `load_pcb_for_routing(...,
  lattice_deadline=)`; when both are present the tighter bound wins.
  `--complete`'s existing stamp still wins verbatim (#4472 semantics
  unchanged), and `--per-net-timeout 0` (e.g. under
  `--deterministic-budget`, where a wall-clock bound would destroy
  reproducibility) still yields an unbudgeted negotiation. A truncated
  run reports partial results with the existing `deadline-exceeded`
  decline reason rather than dying silently. Also: the six bare
  `router.route_all()` CLI dispatch sites now forward the user's budgets
  (binding them on the grid engine and silencing the #2794 no-timeout
  guard that fired pointing straight back at those lines), and the
  routing banner no longer claims a budget the dispatched engine does
  not enforce — on the lattice it labels `--per-net-timeout` as the
  per-link budget it really is, labels `--timeout` as a hard cap, and
  says loudly when a run is unbounded.
- **Board-06 regen is same-host run-to-run deterministic again** (#4536) —
  two same-seed flag-OFF regens could differ by thousands of lines,
  making the "flag-OFF run must produce a byte-identical committed
  artifact" scope-guard convention undecidable for board-06. Two
  instrumented run matrices localized two sources. **Dominant:** the
  negotiated stage's `timeout=360.0` wall-clock backstop straddled the
  phase's natural runtime (363–367 s) and fired mid-iteration at a
  load-dependent net boundary in every run (different copper counts,
  1599–1602 segments); with any finite stage timeout the #3989
  remaining-budget per-net wall-clock caps also stayed active. Fixed:
  `boards/06-diffpair-test/generate_design.py` now runs the negotiated
  stage with `timeout=None` — bounded by its deterministic iteration
  exits (per-net node-expansion budget, memory backstop, a
  `max_iterations=3` count bound that reproduces where the wall clock
  empirically cut, best-stall patience, #4463 zero-overflow
  fixed-point) instead of wall clock — and pins `PYTHONHASHSEED=42` by
  construction (one-shot re-exec at the entry point). **Reach-deciding:**
  the relief rescue's probe / displaced-victim re-land sub-searches were
  bounded by a flat 10 s wall clock that straddles their 8–12 s natural
  time on CI runners; because a rescue rolls back entirely when a victim
  does not re-land, that value decided routed reach on machine speed
  alone (board-06 seed-42 landed 21/21 on one runner and 20/21 on
  another from line-identical logs). `Autorouter.route_all_negotiated`
  gained `deterministic_rescue=` (default off), which bounds those
  sub-searches by the deterministic per-net node-expansion cap instead;
  board 06 opts in. **Residual:** with
  the wall clock removed, runs still serialized ~2300 differing lines
  from *identical* copper multisets, because `kicad-cli` (invoked by
  every `kct zones fill` round) re-saves the board with tracks ordered
  by UUID and the stitch/pour-repair emitters minted random
  `uuid.uuid4()` values. Fixed: `kct stitch` now mints content-derived
  uuid5 values for its vias/stub segments, and board-06's pour-repair
  `_generate_uuid` mints sequence-derived uuid5 values — all copper
  UUIDs are deterministic, so file order is stable under kicad-cli
  refills. A gated determinism smoke test
  (`tests/test_board06_determinism.py`, enabled via
  `KCT_BOARD06_DETERMINISM=1`; two ~12-minute regens) asserts two
  consecutive same-seed runs produce identical uuid-normalized
  artifacts; the normalization + scope-guard convention is documented in
  `boards/06-diffpair-test/README.md`. A residual load-correlated
  sensitivity (a stagnation-recovery reroute can flip under extreme
  concurrent host load) is tracked separately as #4724.

- **Connectivity `validate(reconcile_native=True)` no longer discards the
  internal classification** (#4551) — the native reconciliation path used to
  wholesale-replace `result.issues` with kicad-cli relationships hardcoded as
  `zone_island` with empty `unconnected_pads`, so `unrouted`/`partial`/
  `isolated` always read 0 on machines with kicad-cli (board-03 unrouted:
  internal 16 `partial`/68 unconnected pads collapsed to 37 `zone_island`/0).
  Native relationships now keep their authoritative *count* (#4498 fleet
  parity: board-03 = 13, board-05 = 57) but are classified by endpoint kind:
  zone-only relationships stay `zone_island`, while pad-bearing relationships
  inherit the internal classification for their net and carry the pad
  endpoint descriptions in `unconnected_pads`. Two rendering/diagnostic
  defects fixed alongside: kicad-cli's generic "Missing connection between
  items" message is enriched at the source with the real endpoint refs
  ("Pad 3 [GND] of U1 on B.Cu"), fixing the default table, JSON, and
  `summary()` in one place; and an *unexpected* kicad-cli exit code during
  reconciliation now emits a `RuntimeWarning` naming the exit code and the
  internal issue count being silently substituted (the expected KiCad-less
  degradations — no board path, kicad-cli not installed — stay silent).
  `validate(reconcile_native=False)` behavior is unchanged.

- **Boards 03/06/07: committed routed artifacts refreshed with corrected 3D
  model transforms** (#4585) — the kicad-tools.org gallery cards for
  `diffpair_test_routed` (board 06, mini-PCIe socket 90° off its pad column)
  and `matchgroup_test_routed` (board 07, 1x9 header pins unseated + HDMI
  receptacle 180° flipped) showed misoriented component bodies because the
  committed artifacts' `(model ...)` refs predated the #4583 rotation-sign
  fix (#4617) and the #4584 LCSC per-part rotate/offset overrides (#4631).
  All three artifacts are re-baked via the new `kct pcb add-3d-models
  --refresh` (#4586/#4660): board 03's four SW_SPST_TL3342 buttons go
  `rz 90 → -90`, board 06's `PCIE_Mini_Edge` (LCSC C444929) picks up its
  per-part `-90` override, and board 07's 1x9 header / HDMI receptacle get
  sign-corrected rotate + offset re-bakes. Metadata-only: stripping the
  `(model ...)` blocks pre/post leaves byte-identical files (copper,
  placement, zones, nets untouched) and DRC output is identical pre/post.
  Renders remain git-ignored; the gallery picks up the fix at the next
  operator redeploy.
- **`kct check` LVS: net-less PCBs no longer emit one pseudo-mismatch per
  schematic pin, and `detail` names both comparator legs** (#4681) — on a
  board whose pads carry no `(net ...)` bindings, the label-based (netlist)
  leg used to emit one all-`pcb_net: null` record per bound schematic pin
  (264 on the filed board) while the `detail` prose reported only the
  copper leg's count, so consumers naturally paired `detail` with the
  wrong array. The label leg now has a vacuity guard mirroring the copper
  leg's #4005 guard: zero PCB net bindings (with pads present and >=1
  bound schematic pin) yields a single synthetic record
  (`ref="<vacuous>"`, `pcb_net="<no-pcb-evidence>"`, evidence counters in
  the other fields) and an additive `netlist_vacuous: true` flag in both
  `lvs.json` and `meta_checks.lvs`; the result is still dirty (no
  evidence is not a pass). Explicit no-connect sentinels count as raw
  evidence, so all-NC boards and genuine partial mismatches keep their
  per-pad records. Whenever either leg is dirty, `kct check`'s LVS
  `detail` now names both legs' outcomes (e.g.
  `copper: 2 mismatch(es): ...; label: 264 mismatch(es)` or
  `label: vacuous (no PCB pad carries a net binding); copper: 0
  mismatch(es)`), and `build_lvs_payload`'s docstring states the leg
  mapping explicitly (`mismatches` = label leg, `copper_mismatches` =
  copper leg). v1 `lvs.json` keys/shapes are unchanged; additions only.

- **Softstart lattice proofs: re-pin to the current fixture + content-hash
  drift guard** (#4670) — the local-only softstart routing proof
  (`tests/router/lattice/test_softstart_routing.py`) failed at its topology
  pre-assertion because the external fixture moved (softstart PR #26 NRST
  star-break rework grew the anchor-star topology 287 → 295 connections).
  The proof is re-pinned to the current artifact (softstart commit
  `7800b04`) with re-measured completion floors, and both softstart lattice
  test modules now pin the board file by sha256 via a shared
  `tests/router/lattice/softstart_fixture.py` helper: fixture absent → skip
  (unchanged CI semantics), fixture present but hash-mismatched → skip with
  a message naming the pinned and observed hashes plus the re-pin
  procedure, so future external drift reads as a self-explanatory skip
  instead of a confusing assert failure. Test-only; CI never runs these
  modules (the fixture symlink dangles there by design).

- **`kct route --layers 2` no longer rejects a net-class-map whose
  `avoid_layers` names an inner layer** (#4685) — a well-formed KiCad copper
  layer name (`F.Cu`, `B.Cu`, `In<k>.Cu`) absent from the ACTIVE stack is now
  tolerated instead of fatal: `avoid_layers` entries are dropped silently
  (vacuously satisfied — the grid has no such layer to route on) and
  `preferred_layers` entries are dropped with a one-line stderr warning naming
  the net class and token (the preference cannot be honored). This restores
  layer-subset composition (the HV-outer recipe: route the HV backbone at
  `--layers 2`, then everything else at `--layers 4 --preserve-existing`) with
  ONE shared sidecar describing the physical board. The semantics live in the
  shared resolver (`_resolve_layer_index_list`, threaded per-field from
  `NetClassRouting.from_dict`), so every consumer — `kct route`'s `--layers`
  preload and the canonical `net_class_map_from_path` loader used by
  `check`/`creepage`/`zones`/`audit` — behaves identically. Malformed tokens
  (`"Bogus.Cu"`), integer indices, and the stack-less resolution path are
  unchanged (the #4587 typo guard stays loud, integer maps stay
  byte-identical, and absent names are judged only against a *supplied* stack,
  never guessed).
- **LVS copper leg no longer reports a false `open` when a trace endpoint
  lands inside pad copper but off the pad center** (#4678) — the copper
  partition extractor (`ConnectivityValidator.extract_pad_partition`, the
  primitive behind `kct check`'s LVS copper leg, `lvs/recipe.py`, and
  tapeout Gate 1) only bonded a track endpoint to a pad whose *center* was
  within the legacy 0.01 mm proximity tolerance, so a trace legally
  terminating inside a pad's copper but away from its center stranded the
  pad into its own island and surfaced as an unoverridable `open` —
  contradicting `kct net-status` (strict), `kct route --complete`, and
  `kicad-cli` on the same artifact, and wedging tapeout with a finding no
  flag could waive. A new geometric bonding step (2a3) now ties an endpoint
  to any pad whose eroded copper outline (`POUR_PAD_ERODE` 0.1 mm inset, the
  same guard as the via-in-pad and pour bonds) contains it — or whose
  outline the trace's rounded end-cap penetrates by > 1 µm — on a shared
  copper layer, and carries the bond across the whole segment chain. That
  outline is **shape-aware** (circle → disk, oval → stadium, rect/roundrect
  → size box): the plain size box over-reaches a round pad's real copper on
  the diagonals by more than the minimum clearance (0.211 mm on a 1.7 mm
  header pad), which would let a 45° trace-bend vertex bond across a
  DRC-legal gap — masking a real open on a same-net trace and minting a
  phantom short on a foreign one (both directions regression-tested). The
  model stays label-free (no `net_name` reads), an endpoint across a real
  clearance moat does not fuse, and shapely-absent core installs keep the
  previous behavior. All four connectivity consumers now agree by
  construction, with no flag discipline required.

- **Board-04: C16 field overlap on the committed schematic** (#4675) — the
  fleet's only genuine `sch_field_overlap` advisory (`C16.Value` text
  overlapping U2's body in `stm32_devboard.kicad_sch`) is resolved by a
  cosmetic-only two-line edit shifting C16's `Reference`/`Value` fields one
  2.54 mm grid step up. The originally suggested `kct sch tidy --refs C16`
  was measured to *regress* the finding (1 warning → 2) because tidy's
  canonical below-body placement has no collision avoidance and pushed both
  fields deeper into U2's body — manual placement was required. Netlist/BOM
  identity preserved; no generator or lint-rule changes.

- **`--net-class-map` sidecars with layer NAMES (`"B.Cu"`) now load in every
  consumer, not just `kct route`** (#4683) — the #4587 hardening made an
  unresolvable layer token a hard error inside `net_class_map_from_dict` but
  only `route_cmd` was taught to pass the board's layer stack, so the very
  sidecar `kct route` accepted made `kct creepage` / `kct check` /
  `kct zones hv-keepout` exit 1 without doing any work (blocking the HV
  creepage safety gate; regression vs `45744d0a`), and — worse — made
  `kct audit` **silently** drop the sidecar in both `_check_drc` and the HV
  `_check_isolation` safety gate (warning-only degradation). A new canonical
  loader, `net_class_map_from_path(path, *, pcb_text=, pcb_path=,
  layer_stack=)` in `router/rules.py`, pairs sidecar parsing with best-effort
  board-stack detection; all five consumer call sites now use it, resolving
  `"B.Cu"` against the board's actual copper count (grid index 3 on a 4-layer
  board, 1 on a 2-layer board) exactly as `kct route` does. Integer-index
  sidecars and stack-less library callers are unchanged, and a structural
  guard test now fails if any future consumer calls `net_class_map_from_dict`
  directly — the invariant is *any sidecar accepted by one consumer is
  accepted by all of them*.
- **`kct stitch` now honors the strict connectivity model `kct net-status`
  prints its remedy from** (#4679) — net-status could name a plane-net pad
  "needs via to plane" while running the printed
  `kct stitch <file> --net <NET>` command reported "No unconnected pads
  found on target nets". Three defects fixed. (1) *Model divergence*:
  stitch's "already connected" gate was a 0.5mm endpoint-proximity heuristic
  with no notion of "reaches the plane" — an SMD pad tied to a stub trace
  going nowhere counted as connected. For target nets with real fill copper,
  the strict copper-contact model (`NetStatusAnalyzer`, the engine behind
  net-status, via a new scoped `analyze_nets()`) now VETOES a
  proximity-"connected" skip for any pad it reports unconnected, so the
  net-status remedy applies by construction; the veto is one-directional
  (the strict model never *suppresses* stitching — its "largest island"
  connected set tie-breaks arbitrarily when the fill bonds no pads) and
  nets without fill copper keep the legacy gate unchanged. (2) *Name-only
  net references*: stitch's pad/via/track/drill extractors silently dropped
  KiCad-10 name-only `(net "NAME")` inline references (the `kicad-cli pcb
  drc --save-board` dialect, which also deletes the header net table), so
  on such boards stitch found NO pads at all — the exact "No unconnected
  pads found" no-op the issue reproduced. A shared `resolve_net_num()`
  resolver plus header-table synthesis in `get_net_map()` (mirroring
  `PCB._synthesize_net_table`, #4416) makes all stitch extractors
  dialect-aware. (3) *Output accounting*: stitch now distinguishes "No pads
  found on target net(s): X" (naming the nets, with a cross-check hint)
  from "All N pads already connected", and surfaces a stderr warning when
  the strict model could not run — a stitch/net-status disagreement can no
  longer be silent. Bonus: `NetStatus.suggested_fix` embeds the real
  analyzed filename instead of a hardcoded `board.kicad_pcb`, so the
  printed remedy is copy-pasteable.

- **`kct net-status --net X` output scoping** (#4682) — two `--net` defects
  that made the filter untrustworthy. (1) `--net X --why` ignored the filter
  entirely (`--why` short-circuited before net validation and
  `output_why()` had no net parameter), so asking about one net printed
  every OTHER stuck net's diagnosis; the whole-board classification is now
  post-filtered to the selected net, with an explicit
  `Net 'X' is not stuck (...)` statement (exit 0) when the selected net has
  no diagnosis, and the same filtering in `--format json` (additive
  `net_filter` key; unfiltered `--why` JSON unchanged). `--net NONEXISTENT
  --why` now errors with the available-nets listing (exit 1) instead of
  running unfiltered. (2) plain `--net X` rendered the board-wide summary
  (e.g. `Incomplete: 1`) directly above an `All nets are fully connected!`
  banner computed from the filtered set — a direct contradiction; the
  summary and banner are now both scoped to the selection (`Summary: 1 net
  selected (of N on board)` / `Selected net 'X' is fully connected.`). The
  misleading `(100% connected)` literal on the Complete summary line is
  reworded to `(all pads connected)`. Exit codes for the normal status path
  are deliberately unchanged (board-wide even under `--net`, documented in
  the `--net` help text); `--incomplete` keeps its board-wide header.

## [0.20.0] - 2026-08-06

### Summary

Development since `v0.19.0`: a targeted `kct route --complete` completion pass,
route-time enforcement of high-voltage isolation across every engine, a general
`.kct_waivers.json` waiver mechanism for `kct check`, and a broad correctness
sweep (KiCad-10 net dialect, export manifests, LVS identity, diff-pair shadow
constructor). No breaking changes.

### Added

- **Board-edge keepout auto-resolved from manufacturer limits** (#4568) —
  `load_pcb_for_routing(edge_clearance=None)` now resolves the copper-to-edge
  clearance from the effective rules' `manufacturer` via the same per-tier
  `MfrLimits.min_edge_clearance` floor that `kct check --mfr` enforces
  (jlcpcb 0.3, oshpark 0.381, ...), and applies the preventive board-edge
  keepout in the routing grid (both backends). Previously only the `kct
  route` CLI auto-filled `--edge-clearance`, so in-process API callers
  (board pipelines, `optim/router_factory`) silently routed with no edge
  constraint and could emit copper hugging the outline
  (`edge_clearance_trace` at 0.187mm on the board-06 shadow re-route).
  Pass `edge_clearance=0` to explicitly opt out; explicit positive values
  and manufacturer-less rules are unchanged. The shared resolver lives in
  `router/mfr_limits.py::resolve_edge_clearance` (CLI now delegates to it),
  and `build_pcb_router_factory` gained a `rules=` passthrough so
  placement-fitness routing can opt into the same keepout.
- **Mypy version-drift guard + worktree env sync** — fresh worktrees perform
  no Python env setup, so a drifted `.venv` (e.g. mypy 1.20.2 vs the locked
  1.19.1) surfaced toolchain diagnostics as phantom "NEW mypy errors beyond
  the baseline". `scripts/ci/check_mypy_baseline.py` now compares the invoked
  PATH `mypy --version` against the `uv.lock` pin and prints a warn-only
  `MYPY VERSION DRIFT` diagnostic (banner when new-vs-baseline errors are
  also present) naming the `uv sync --frozen --extra dev` remedy; exit-code
  semantics are unchanged and the guard skips silently on an unreadable
  lock. A new repo-owned `.loom/hooks/post-worktree.sh` hook runs
  `uv sync --frozen --extra dev` automatically in Loom-created worktrees, and
  the README "Fresh worktree checklist" documents the manual step. (#4558)
- **`kct pcb add-3d-models --refresh` — rewrite existing `(model ...)` refs
  in place** (#4586) — the command was insert-only, so refs generated before
  a resolution fix (#4045 offsets, #4457/#4583 rotation baking, #4584 LCSC
  per-part transforms) were stranded stale with no migration path short of
  hand-stripping the nodes. `--refresh` re-resolves every footprint through
  the current tier chain (exact → same-library variant → cross-library
  substitution → LCSC sidecar) and replaces existing model nodes with the
  freshly computed ones; output is byte-identical to stripping the old nodes
  and re-running the insert path. Footprints the tiers cannot re-resolve keep
  their existing nodes byte-for-byte (reported as `refresh_kept` alongside
  the new `refreshed` field in text/JSON output). Without the flag the
  command is unchanged (insert-only, existing refs never touched); combines
  with `--dry-run`. All edits stay scoped to `(model ...)` metadata — copper,
  placement, zones and nets are untouched.
- **`kct check` schematic field-geometry lint (`sch_fields`)** — new
  warning-severity check category with two rules: `sch_field_offset` flags a
  visible `Reference`/`Value` field farther than a threshold (default 15 mm,
  `--sch-field-threshold`) from its symbol's placed body bbox, and
  `sch_field_overlap` flags a field's estimated text bbox colliding with
  another symbol's body or another symbol's visible field (the superimposed
  `+3.3VA9`-style composites). Runs on the same resolved schematic ERC/LVS
  use (explicit `--schematic` or sibling discovery), recurses into
  hierarchical sub-sheets, and shares the exact `field_geometry` metric that
  `kct sch tidy` fixes — lint reports it, tidy repairs it. Both rules are
  advisory (`advisory-quality` bucket), never raise the error count, and
  never block `kct build`/`kct pipeline` fab gates; under `--strict` they
  become fatal by the pre-existing global warnings contract. Findings are
  deterministically sorted for CI. Skipped entirely under `--drc-only`.
  (#4595)

- **Search-time HV pairwise clearance in the lattice engine** — `--voltage-map`
  + `--route-engine lattice` now *avoids* HV↔LV proximity during the A\* search
  instead of only failing the #4588 post-route gate after committing the
  copper. The lattice consumes `rules.pairwise_clearance` through a new
  net-id-space projection (`router/lattice/pairwise.py`, mirroring the #4510
  C++ grid projection): every committed-copper predicate widens its gap to
  `own_half + stored_half + max(own_clr, stored_clr, pairwise(pair))`, static
  pad keep-outs grow per-pad/per-pair, spatial query windows inflate to the
  widest requirement the querying net participates in (the #4511
  search-radius trap), and #4506 rated attach zones exempt in-search at the
  same closest-gap midpoint the post-route gate probes — so search and gate
  agree by construction. The routing cache now keys the derived pairwise
  matrix so a scalar run's entry is never served to a `--voltage-map` run
  (previously it silently bypassed route-time avoidance on grid too). Without
  `--voltage-map` the lattice path is byte-identical; coupled diff-pair fat
  envelopes are deliberately excluded (same-domain by construction; emitted
  legs remain audited). The mesh engine stays post-route-gated only (#4602).

- **Keepout rule areas honored by the lattice engine, with a per-net-class
  filter** — KiCad `(zone … (keepout …))` rule areas now parse into a
  first-class model (`schema/pcb.py`: `Zone.keepout` flags + multi-layer
  `Zone.layers` + `PCB.rule_areas`; previously the `keepout` child was
  silently dropped) and `--route-engine lattice` enforces them at search
  time: `tracks not_allowed` keeps every segment's copper edge out of the
  area on its declared layers, `vias not_allowed` rejects through-vias whose
  barrel would enter it, and `copperpour not_allowed`-only areas (the
  `kct zones hv-keepout` pour voids) never constrain routing. A new
  `spatial_keepouts` block in the `--net-class-map` sidecar scopes an area
  per net class by zone name (`only_classes` / `except_classes`; no entry =
  KiCad-default all-nets), which makes disjoint HV bank corridors expressible
  as complementary keepouts — declarable spatial segregation by construction,
  composing with (not replacing) the #4602 pairwise search-time avoidance.
  Nets rendered unroutable by an area report keepout-attributed declines
  (`pad-escape-*-keepout`, `no-path-keepout-constrained`); boards with no
  track/via-blocking rule areas route byte-identically; grid/mesh warn once
  that they do not honor rule areas (#4605).

- **`kct sch tidy` — headless Reference/Value field autoplace** — new
  schematic subcommand that resets visible `Reference`/`Value` field
  positions to deterministic bbox-relative defaults (Reference centered
  above the placed symbol body, Value centered below, grid-aligned and
  horizontal), honoring symbol rotation and mirroring — a diff-reviewable,
  CI-runnable stand-in for Eeschema's GUI-only "Autoplace Fields"
  (Eeschema-parity is an explicit non-goal). Supports `--threshold <mm>`
  (only touch fields farther than this from the body), `--refs` scoping,
  `--dry-run` before/after offset reports (`--format text|json`), and
  `--backup`. Strictly cosmetic: only field `(at x y angle)` nodes change —
  structural-diff, BOM, and kicad-cli netlist invariance are asserted in
  tests. Power/virtual symbols and hidden fields are skipped by default;
  unresolvable `lib_id`s warn instead of crashing. The underlying geometry
  (placed body bbox, field-offset metric, default positions) lives in a new
  shared `kicad_tools.schema.field_geometry` module so the companion
  field-geometry lint (#4595) can reuse the exact same metric (#4596).

- **Connector mating / edge-access rule in `kct check`** — new default-on
  `connector_access` category (selectable via `--only`/`--skip`, no new flags)
  with a warning-severity `connector_edge_access` rule that flags rigid-plug
  panel-entry connectors (audio / USB / barrel-jack / RJ / card-edge families,
  using KiCad's own `_Horizontal`/`_Vertical` library naming) whose courtyard
  is more than 3.0 mm from every `Edge.Cuts` segment — catching the
  shipped-incident class of a certified board whose audio jack sat 10.66 mm
  inside the outline, unpluggable. Pin headers and JST/Molex/FFC wire-to-board
  connectors are deliberately excluded (precision-first). An info-severity
  `connector_edge_distance` inventory row reports every `Connector_*`
  footprint's distance-to-edge in JSON/verbose output, and findings are
  suppressible per-reference via `.kct_waivers.json`; both rule ids are
  classified advisory, so plain exit codes are unchanged (#4613).

- **Advisory routing-quality metrics in `kct check`** — a new pure
  `kicad_tools.analysis.routing_quality` module measures segment-length
  distribution (median, sub-0.25 mm fragments), direction classes
  (orthogonal / true-45° / off-axis), H/V staircase steps (< 0.6 mm legs),
  zero-length segments, and per-net segment counts over the board's copper.
  Default `kct check` prints a "Routing quality (advisory)" stanza after the
  meta-check rollup and the JSON/`--output` envelopes gain a top-level
  `routing_quality` object; strictly advisory — never affects the verdict or
  exit code (even under `--strict`), no new flags, and `--drc-only` output is
  unchanged (#4623).

- **`kct route --complete`** — a targeted completion pass: auto-detect the
  still-unconnected signal nets and route only those links, with all other
  copper fixed, on the octilinear lattice engine. Implies `--preserve-existing`
  and is a byte-identical no-op on a connected board. The lattice build is
  localized to the stranded nets' pad bounding boxes and every link carries a
  wall-clock deadline, so a walled SMD pocket that used to grind for over ten
  minutes now terminates and reports. `--complete-report PATH` writes per-link
  records (endpoints, elapsed vs. budget, blocking copper) and the pass exits 8
  when links remain unroutable; batch rescue now shells it instead of the
  coarse grid A*. The companion `--via-in-pad-last-resort` (default off) lets
  the lattice engine stage a same-net via-in-pad attach for a walled SMD pad
  only after an in-layer route and an off-pad via have failed, gated on the
  manufacturer tier floor so an unsupporting tier declines with an explicit
  `via-in-pad-tier-unsupported` reason (#4471, #4472, #4475, #4477, #4478).

- **Route-time high-voltage isolation** — `kct route` gains `--voltage-map`
  (plus `--creepage-standard` / `--pollution-degree` / `--material-group` /
  `--hv-threshold`) and resolves each net pair at `max(DRU, creepage(|ΔV|))`,
  sharing the derivation with `kct creepage` and `kct optimize-placement` so
  the three agree by construction. Enforcement spans the stack: a per-net
  domain matrix in the C++ validator, search-time A* hard-blocking, and
  rated-footprint attach zones that waive the widening but never the DRU floor.
  A post-route board-level audit now gates *every* engine's exit code — the
  lattice engine previously committed HV copper at DRU spacing and exited 0
  (#4431, #4506, #4510, #4511, #4588).

- **`kct creepage-export-rules`** — turns a `--voltage-map` into artifacts
  `kicad-cli pcb drc` enforces: voltage-domain netclasses and net patterns
  merged into `<project>.kicad_pro`, and pairwise clearance `(rule ...)`
  clauses in a sentinel-delimited block in `<project>.kicad_dru`. Both are
  idempotent and preserve user-authored content; domain-bridging footprints
  become refdes-scoped `insideCourtyard` exclusions (#4508).

- **Richer creepage model** — a `--voltage-map` entry now accepts
  `{"min": v0, "max": v1}` as well as a scalar and the census derives the
  worst-case endpoint stress, closing a same-potential false PASS where two
  nets sharing a dominant state derived ΔV = 0 despite mains-class transient
  stress (an all-scalar map still serializes byte-identically). Pairs also
  carry a `relationship` (`board` / `same_footprint`), so component-internal
  gaps — functional insulation governed by the part's own rating — are
  distinguishable; `--waive-same-footprint` (default off) drops them from the
  exit gate but still lists them `WAIVED`, and `kct audit` still blocks
  (#4403, #4411).

- **`.kct_waivers.json` — a general waiver mechanism for `kct check`** — a
  version-2 sidecar waives findings for **any** `rule_id` by matching a
  violation's items (and optional nets) as an exact set, via `--waivers PATH` or
  auto-discovery; stale entries raise a `waiver_unused` advisory. Waived findings
  leave `kct check`'s exit gate but keep `severity: "error"` in the JSON, so
  `kct audit` stays blocking (#4417).

- **Fab-profile auto-discovery for `kct check`** — `kct route` writes a
  `fab_profile.json` sidecar recording the resolved `--manufacturer`, and
  `kct check` resolves the effective `--mfr` by precedence (explicit `--mfr` >
  sidecar > `project.kct` `target_fab` > `jlcpcb`), so a board routed with
  tier-gated geometry such as via-in-pad no longer reports a false FAILED
  against the base tier. With neither source, a non-blocking advisory names a
  permitting tier (#3920).

- **Placement refinement and edge connectors** — `--seed current` warm-starts
  CMA-ES from the board's on-disk footprint positions (with a much tighter
  sigma), so the optimizer refines a ratified hand floorplan instead of
  re-imagining it from the bounds center. `kct placement check` flags an
  off-board connector standing more than `edge_connector_max_inset` (2 mm)
  inside the outline — one no cable can mate with — and the solver gains a
  torque that rotates such connectors to face off-board (#4405, #4450).

- **`kct pcb move-footprint --drag-endpoints`** — a translation nudge carries
  coincident trace endpoints along with the pads instead of stranding the routed
  copper, with `--drag-tolerance`, per-pad drag counts, `--map` batch
  composition, and UUID-preserving dry-run reporting (#4418).

- **Grid-independent different-net short verifier and repair** — a post-route
  pass geometrically verifies emitted copper for via-via, via-segment, and
  segment-segment different-net overlaps independently of the router's grid
  model, then relocates each offending via onto a clearance-validated site
  (escape-node slide, then an 8-direction ladder, with connectivity stubs).
  This matters under `--allow-unsafe-grid`, where a coarse grid lets two nets
  quantize into a real short that passes grid occupancy (#4470).

- **Manufacturer-driven hole-to-hole via relocation** — a `--mfr`-driven
  post-pass relocates in-pad and plane-stitch vias that violate the drill
  hole-to-hole floor onto a clearance-validated location, re-bonds them, and
  reports boxed-in vias rather than moving them. It never introduces a new
  violation (#4408).

- **Zone-fill island reporting** — KiCad's `unconnected_items` now land in a
  dedicated `DRCReport.unconnected_items` collection, so orphaned zone-fill
  islands surface without redefining `violations`, which keeps its
  geometry-only meaning for every existing consumer (#4498).

- **Kelvin / current-sense star topology** — the router recognizes
  current-sense nets and roots their topology at the shunt pad instead of an
  arbitrary hub, so a sense tap connects *at* the Kelvin point rather than
  merging into the high-current segment and reading the load-current IR drop.
  Applied by the RSMT builder and by the lattice and mesh netset drivers, which
  previously anchored every star at `pads[0]`. Detection needs a net-name
  pattern *and* a resolvable shunt root; other nets are untouched (#4473, #4476).

- **Congestion-aware escape-corridor reservation** —
  `--escape-corridor-reservation` (default off) clusters a dense part's pins by
  face and destination, sizes a corridor per cluster from the congestion
  estimator, assigns clusters to distinct inner layers, and soft-reserves those
  cells before the general negotiation. Reservation is selective and
  length-bounded, so it is a local escape channel rather than a board-spanning
  attractor field — board 05 goes 25 875 cells over four layers → 2 886 over
  two (#4474, #4519).

- **Classifier-driven placement feedback for stuck nets** — a stuck-net
  diagnosis now translates into a concrete placement delta (translate,
  `rotate_180`, `reorder_pins`), and `kct route --placement-delta-feedback`
  applies it, re-routes, and keeps it only on a strict routed-net improvement —
  otherwise placement, router pads, and routes revert atomically. A kept delta
  persists the mutated placement, since the new copper is valid only against
  the moved footprints (#4466, #4467, #4468).

- **Per-net `avoid_layers` as a hard constraint** — `--strict-layers` promotes
  a net class's `avoid_layers` from a soft cost bias (Python-only, and absent
  from the C++ backend) to a hard routable-layer restriction honored by both
  backends, so an ampacity-bearing net can no longer land copper on a thin
  inner plane when escalation reaches the all-signal rung. It also applies
  automatically to any class declaring `target_ampacity` (#4433).

- **Router failure diagnostics** — the coupled diff-pair router emits a
  per-reason rejection histogram from the C++ joint search plus a structured
  per-pair failure taxonomy, and the rescue path replaces the opaque `FAILED
  (no output produced)` line with a concrete reason (blocked-by-non-rippable-
  copper, no-legal-escape, budget-exhausted, clearance-infidelity, CLI-refused)
  plus a grid-fidelity report naming too-narrow lanes (#4459, #4469).

- **Shared pipeline gate for board recipes** —
  `recipes.gate.evaluate_pipeline_gate()` returns one verdict from which every
  board recipe derives **both** its printed SUMMARY and its exit code, so the
  two can no longer disagree. The authoritative DRC leg is `kicad-cli pcb drc
  --refill-zones` (`kct check --drc-only` trusts stale zone fills), unioned
  with a supplemental verdict for the kct-internal rule families (#3912).

- **`project.kct` precondition for mutating recipes** —
  `recipes.precondition.require_spec()` asserts a board carries captured intent
  (a `project.kct` that exists, is non-empty, and parses) before a recipe
  mutates anything. Advisory and fail-soft by default, read-only w.r.t. the
  `.kct`; `KCT_REQUIRE_SPEC=1` escalates to full semantic validation (#4539).

- **`silk_overlap` DRC rule** — `kct check --only silkscreen` now detects
  silkscreen printed on top of other silkscreen, closing a gap where an entire
  `kicad-cli pcb drc` rule class was invisible to a kct-only workflow. Emitted
  at `warning` severity (advisory, non-blocking), one violation per unordered
  (silk item, silk item) pair. Same-footprint pairs count — a reference
  designator over its own courtyard art is a real finding, confirmed against
  `kicad-cli` on a synthetic fixture. Detection is bare geometric intersection
  rather than KiCad's configurable silk clearance, so it under-reports rather
  than over-reports (#4612).

- **Stem-keyed net-class-map sidecar auto-discovery** — `kct check` now probes
  `<board-stem>.net_class_map.json` in addition to the bare
  `net_class_map.json`, so board trees that keep several revisions side by side
  (`board_v24.kicad_pcb` next to `board_v24.net_class_map.json`) no longer
  silently skip the three gated rules (`match_group_length_skew`,
  `diffpair_length_skew`, `diffpair_routing_continuity`). Within a directory the
  stem-keyed name wins; the "rules are INACTIVE" warning now names the paths it
  actually probed instead of an invented `output/` example. New
  `--no-net-class-map` suppresses the probe (#4601).

- **Opt-in crossover legality census** for the diff-pair shadow phase, behind
  `KCT_CROSSTAIL_CENSUS=1`. Reports how many via-site candidates are legal and
  how many distinct `v1` barrels they use, rather than stopping at the first
  legal one. Default-off and byte-identical when unset (#4580).

- **Full LVS mismatch records in machine output** — `kct check` now attaches the
  complete record set from both comparator legs under `meta_checks.lvs`, reusing
  the `lvs.json` shapes verbatim so one consumer parses
  `boards/*/output/lvs.json` and the embedded form with the same code.
  `--verbose` expands the `LVS:` stanza to the full uncapped list. Previously the
  only form anywhere on that path was a truncated prose string (`"50
  mismatch(es): a, b, c (+47 more)"`) — there was no way to see the other 47. The
  default terminal line is unchanged (#4616).

- **Per-part 3D-body transforms for the LCSC/EasyEDA model tier** — the
  fetch-on-demand tier has no source footprint, so its body rotation was
  underivable and defaulted to identity, which is wrong for any part whose STEP
  is authored off-axis. Two override sources now resolve in
  `models3d._resolve_lcsc` (sidecar > packaged table > identity, merged per
  field): an object form in `lcsc_models.json` (`{"lib_id": {"lcsc": "C…",
  "rotate": […], "offset": […]}}`, with the bare-string form still valid), and a
  packaged C-number-keyed table. Every packaged entry must carry render
  provenance, enforced by a test. Parts with no entry keep identity transforms,
  so existing boards emit byte-identical model nodes (#4584).

### Changed

- **`kct net-status` / `NetStatusAnalyzer` default to strict real-geometry
  connectivity** — the default connectivity model is now shapely copper-shape
  intersection (`strict=True`), matching `kicad-cli pcb drc` semantics. The
  legacy 0.01mm endpoint-proximity model diverged from KiCad in both
  directions: it over-connected copper whose reference points were merely near
  (#4176) and false-flagged pads open when a trace endpoint landed inside pad
  copper but away from the pad *center* — 16 false opens on board 06's poured
  nets (`GND`/`+1V2`/`VBUS_USB`), now 0 by default. Board 07's 5 genuine
  #3438 opens are unchanged. `--legacy-proximity` (CLI) / `strict=False`
  (API) opt back into the old model; `--strict` remains accepted as a no-op.
  Output now names the model in use, `--why` respects the selection
  (previously `--strict --why` silently ignored `--strict`), and strict-mode
  segment/pad bonds are now layer-gated so a 2D copper overlap on a different
  layer can no longer fuse copper a via does not electrically span.
  `kct check` connectivity defaults are unchanged (#4557).

- **`kct check` splits its report into manufacturing and advisory buckets** —
  a rule-to-category taxonomy renders a CATEGORY SUMMARY with "Manufacturing
  DRC: N blocking" and "Advisory/quality: M advisory", so routing-intent
  findings (connectivity, diff-pair skew and continuity, `copper_sliver`,
  ampacity, silkscreen) no longer inflate an undifferentiated "N DRC
  violations" count that reads as N fab-blocking defects even when `kicad-cli`
  reports 0. Presentation only: counts, verdict, exit code, and the gating
  advisory-rule set are unchanged (#3803).

- **`copper_sliver` detection now matches KiCad's algorithm** — the native
  component-wise tiny-vertex traversal and sliver thresholds are ported, so a
  sub-0.0008 mm numerical kink beside an acute tip no longer hides a sliver
  `kicad-cli` reports. Two shapely-union artifacts KiCad's integer-nm poly-set
  union never creates are also suppressed (a low-height union seam, and a
  keyhole pinch at a shared ring coordinate). **User-visible: board 06 goes 2
  markers → 0**, matching `kicad-cli`; 00–05 and 07 stay clean (#4497, #4521).

- **Negotiated routing bails out of terminal stalls** — when a rip-up round
  makes zero progress and every remaining stuck net is confidently *not*
  budget-starved, the loop breaks to PARTIAL instead of escalating the rip-up
  radius through the full iteration budget; more iterations cannot help such a
  set by construction (board 07 previously spent ~13 of ~14 minutes on this
  plateau). Default on, with the old behavior bisectable (#4406).

- **`silk_over_copper` now counts one violation per (silk item, mask aperture)
  pair**, matching `kicad-cli pcb drc`. Previously it de-duplicated to one
  violation per silk element. **User-visible: reported `silk_over_copper`
  counts roughly double** (measured on the in-repo fleet: 2→4, 6→12, 4→7, 3→5),
  with no change to which silk elements are detected. This makes kct and
  kicad-cli counts directly comparable — the cross-gate convention that has the
  two engines referee each other at manufacturing sign-off previously compared
  numbers that could not agree by construction. It also fixes a second defect:
  the rule stopped at the first R-tree hit, so a refdes straddling four pads
  named one *arbitrary* member of the collision set (board 05 reported `U10
  pad 28`; the real set is pads 27, 28, 29, 30) (#4612).

### Removed

- **One-off diagnostic scripts for closed issues** —
  `scripts/calibrate_area_estimate.py` (packing-overhead calibration helper
  for #3403) and `scripts/diagnose_b03_diffpair.py` (board-03 diff-pair
  pre-pass diagnostic for #2490) are deleted along with their
  `scripts/README.md` catalogue rows. Both parent issues shipped long ago,
  nothing in the repo invokes either script, and the estimator's coverage
  lives in dedicated tests; recoverable from git history if ever needed
  (#4566).

### Fixed

- **Batch completion and nudge re-route no longer drop newer `RescueConfig`
  fields.** `complete_unfinished_nets` (`router/partial_rescue.py`) and the
  post-nudge re-route (`router/placement_nudge.py`) rebuilt their per-pass
  `RescueConfig` field-by-field, silently resetting any field added after the
  copy was written: `allow_unsafe_grid` (#4528/#4532) was dropped at both
  sites — so an unsafe-grid board's completion subprocess exited 1 at the CLI
  gate before routing anything (bogus `no_output` failures) — and the nudge
  path also lost `deterministic_budget` (#3877 cross-machine
  reproducibility). Both sites now derive the pass config via
  `dataclasses.replace` (helpers `_completion_pass_config` /
  `_reroute_pass_config`), so future fields propagate by construction, and a
  sentinel-based drift-guard test over `dataclasses.fields(RescueConfig)`
  fails if a field is ever enumerated-but-not-propagated again (#4550).
- **Docs: the `enable_shadow_construction` comment block in
  `router/diffpair.py` no longer cites pre-#3988 off-angle measurements.**
  The 2026-07-08 #3921 snapshot (3/9 convergence; off-angle shadow segments
  with a "pending" #3907 dogleg; open residuals #4570/#4574/#4575/#4577) is
  re-framed as dated, resolved history: `_quantize_shadow_segments` (#3988)
  doglegs shadow copper 45-legal at emission (verified 2026-07-31, zero
  `OffAngleSegmentWarning` across all 9 board-06 pairs, #4461), construction
  is 6/9 since #4460 (#4512/#4526), and the residuals are all closed. The
  multiply-stale docstring of `test_shadow_construction_flag_defaults_off`
  now states the current OFF rationale (USB_CC1 corridor contention + shadow-ON
  wall-clock vs the 30-min CI ceiling, per the 2026-08-02 #4463 measurement).
  The stale comment had already spawned near-duplicate work once (#4461 was
  almost built as a new feature). Comment/docstring-only; zero executable-code
  change (#4552).
- **`route_cmd.main()` no longer leaks process-global state to in-process
  callers** (#4559). The route CLI stamps the `KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE`
  / `KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK` (+ `_SIZE`/`_DRILL`) escalation
  env vars, seeds the global `random` module for `--seed`, installs a SIGINT
  partial-save handler, and pins the live `Autorouter` in a module-level
  interrupt dict — and previously restored none of them. Any in-process
  invocation (`kct route` via `commands/routing.py`, or a test calling
  `route_cmd.main()`) permanently poisoned the process: a leaked fallback
  `=1` silently re-enabled micro-via-in-pad rescue for every subsequent
  `EscapeRouter`, producing the selection-dependent pytest-xdist worker
  failures first seen in PR #4556. `main()` is now a thin wrapper whose
  `_process_state_guard` context manager snapshot-restores `os.environ`
  (exact, including removing vars created mid-run), the global RNG state
  (only when `--seed` was applied; unseeded runs still leave the RNG
  untouched), the SIGINT handler (only if still ours on exit), and releases
  the pinned router/grid. Stickiness *within* one invocation — including the
  escalation ladder's per-rung stamp/pop and subprocess env propagation — is
  unchanged: the restore runs strictly at the outermost exit, after all
  copper is written.
- **Docs: board-07 placement-delta probe results corrected to CI ground
  truth.** The board-07 README, the `generate_design.py` "MEASURED VERDICT"
  comment block, and the `run()` docstring in `router/placement_feedback.py`
  presented the U3-translate probe as a universal "+1 net (26/31 -> 27/31)
  reverted for clearance" — that was a host-specific (local macOS arm64)
  measurement. Both independent board-07 CI runs measured the probe as routed
  25 -> 25 (refused for no reach gain; the clearance guard never fires there)
  and probe 0 as 25 -> 14 (host: 25 -> 16). All three sites now carry both
  measurements with host-vs-CI provenance, the README records the divergence
  alongside the board-06 nondeterminism tracked in #4536, and `DQ4` /
  `TMDS_D0_N` / `TMDS_D1_N` are now explicitly named unprobed-by-budget
  rather than reading as unroutable. Text-only; no code, thresholds, or
  routed artifacts changed (#4561).
- **Legacy pre-#4600 generated `.kicad_dru` sidecars duplicated on merge.**
  The marker-guarded fab-floors merge (#4600) treated kct's own
  clobber-written, unmarked sidecars as user-authored and APPENDED a second
  copy of every tier floor instead of replacing the legacy content — rewriting
  committed board artifacts in place (board-05's sidecar tripped the #3580
  committed-artifacts guard, failing the Test job on main). `merge_dru_floors`
  now recognizes the legacy generated shape (a `(version N)` header followed
  exclusively by `generate_dru`-grammar rules with generated rule names) and
  replaces it with the managed block; genuinely user-authored no-marker
  content keeps the never-clobber append semantics. All eight committed
  `boards/*/output/*.kicad_dru` sidecars were migrated to the marked format
  (byte-identical rule floors, now sentinel-delimited), so re-export is
  idempotent on them. (#4667)

- **The `.kicad_dru` sidecar write clobbered pre-existing user content.** The
  tier-floor `.kicad_dru` written beside the board by `kct route`, `kct build`,
  `kct mfr apply-rules`, and `kct check --emit-dru`/`--emit-drc-constraints`
  was a blanket overwrite that silently deleted the marker-delimited HV
  creepage rules merged by `kct creepage export-rules` (#4508) and any
  hand-written custom rules. The fab-tier floors now live in their own
  sentinel-delimited managed block (`# BEGIN kct fab floors ...` /
  `# END kct fab floors`, mirroring the creepage exporter's merge dialect):
  re-emitting replaces only that block, a pre-existing user-owned file is
  preserved with the block merged in alongside its content, the merged file
  keeps exactly one leading `(version 1)` header, and the creepage and
  fab-floors managed blocks coexist in one file. Bare `kct check` (no
  `--emit-*` flags) continues to write nothing beside the board — now locked
  in by a regression test — and the `.kct/CONVENTIONS.md` cross-gate
  convention vendored by `install-kct.sh` documents that `kicad-cli` auto-loads
  these sidecars, sharing rule floors with `kct check` by design (#4600).

- **Docs: `boards/06-diffpair-test/README.md` no longer cites the pre-ratchet
  28-error allowlist.** The CI Gate section, "Interpreting a failure" table,
  and "Tightening the allowlist" section now state the actual pin — 18 errors
  (9x `diffpair_length_skew` + 9x `diffpair_routing_continuity`, #3540-#3544,
  floor ratcheted 24 -> 18 by #4019) — matching
  `.github/routed-drc-tolerance.yml` and `EXPECTED_STRICT_GATE_ERRORS`, and
  stop presenting closed issues #2672/#2677/#2648 as pending prerequisites
  (#4606).

- **An HV pairwise-clearance failure could flow through `kct build` /
  `kct pipeline` as a soft warning.** Neither consumer forwarded
  `--voltage-map`, and both treated route exit 3 as non-fatal with a message
  that misnamed it as "DRC violations remain". Both now thread `--voltage-map`
  (plus `--creepage-standard` / `--pollution-degree` / `--material-group` /
  `--hv-threshold`) through to the route subprocess across every parser hop,
  and with a voltage map in play a clearance-dirty route result (exit 3/4) is
  fatal — the board no longer continues to verification and manufacturing
  export. Because the audit only runs inside the `kct route` subprocess, both
  consumers also refuse loudly instead of silently dropping the audit when the
  route step would not reach it: `kct build` fails fast when the board routes
  via a Tier 1-3 route recipe/script, and `kct pipeline` fails fast instead of
  skipping the route step on an already-routed board (both messages point at
  `kct creepage` / `--force`). Without a voltage map, exit 2-5 handling and
  the skip semantics are byte-identical to before, and the exit-3 message now
  names all three shared meanings (#4607).

- **`kct check` `silk_over_copper` now sees untented-via mask openings.** The
  aperture set was pads-only, so silk over an exposed via was invisible
  (kicad-cli 1, kct 0 — the residue #4612 measured and deferred). The rule now
  resolves per-via tenting per side (per-via `(tenting ...)` override → board
  `setup` default → the measured absent-token default, tented) and yields an
  aperture for each untented side; `Setup` and `Via` parse the setup-level and
  per-via `(tenting (front …) (back …))` nodes, with the per-via override
  round-tripped through `Via.to_sexp` like `via_type`. Tented vias — the
  entire committed fleet — contribute nothing, preserving #4612's exact
  kicad-cli parity; all seven synthetic tenting probes match kicad-cli 10.0.5
  counts exactly (#4624).

- **`/kct:tapeout` certified "upload as-is" on errors alone — assembly-affecting
  warnings rode through.** The tapeout skill contract gains a warning-review
  gate (Gate 4b): a per-rule warning table is derived from Gate 1's `kct check
  --output` JSON report and printed (an aggregate total alone is
  non-compliant), and any nonzero count in the assembly-affecting set
  (`silk_over_copper`, `silkscreen_line_width`, `silk_edge_clearance`,
  `silk_overlap`) forces TAPEOUT REFUSED unless explicitly acknowledged via
  `--ack-warnings` or a per-rule accepted-risk line; Gate 7's README and Gate
  8's manifest now carry the per-rule counts. `/kct:manufacturing-readiness`
  sign-off quotes the per-rule `BY RULE:` warning breakdown, and the installer
  vendors a 4th `.kct/CONVENTIONS.md` convention: manufacturing warning
  baselines are recorded per rule, never as an aggregate (#4614).

- **KiCad-10 name-based net references were invisible to four copper
  scanners.** On a board whose segments and vias name their net (`(net "GND")`)
  instead of numbering it, the `--preserve-existing` parser returned an empty
  set and the dialect-blind strip pass then deleted *all* existing copper — so
  `--preserve-existing` / `--nets` / `--region` silently destroyed a fully
  routed board. The `strip_traces` net filter, the stranded-stub strip, the
  conflicting-via scan, and the 45° quantizer were likewise no-ops. All now
  resolve both dialects through one shared helper, copper kct adds re-emits the
  board's own dialect, and a guard aborts a write retaining under 10% of a
  board's copper (#4413, #4414, #4416, #4529).

- **`kct spec decide` destroyed the rest of the `.kct` file.** It was a
  load → model → dump → overwrite cycle over models with pydantic's default
  `extra="ignore"`, so every key the schema does not define was dropped at load
  and deleted on write. One invocation on a copy of
  `boards/00-simple-led/project.kct` exited 0, printed "Decision recorded", and
  took the file from 88 lines to 74. Unknown keys now survive `model_dump` for
  every `save_spec` caller, and `decide` splices the new entry into the
  existing `decisions:` sequence textually, re-parsing and comparing the result
  before committing it (#4538).

- **LVS reported false shorts from per-pad unnamed-net naming.** An unnamed
  net's auto-generated name was derived from the *queried pin*, so a k-pad
  unnamed net splintered into k identities and was reported as C(k, 2) shorts
  inside the one copper component that legitimately joined them. A single
  10-pad net accounted for 45 of 50 false shorts on copper `kicad-cli pcb drc`
  found clean. Auto-names now pick a deterministic canonical representative per
  connected component (also making them stable across runs), and board LVS
  compares pad partitions, not strings, when both sides carry one (#4615).

- **Export manifests were keyed by bare filename.** Every artifact in a
  subdirectory (`gerbers/`, `images/`, `vN/`) was recorded under a key that
  does not resolve from the manifest's own directory: **70 of 118 keys across
  the eight committed bundles failed the obvious `bundle_dir / key` walk**, and
  same-named artifacts in different subdirectories overwrote each other. Keys
  are now bundle-relative POSIX paths, with the legacy fallback retained so
  committed manifests keep verifying. Separately, `kct export --mfr
  jlcpcb-tier1` recorded a false `bom_cpl_match` FAIL because preflight
  compared the raw tier id against an exact-match `{"jlcpcb"}` set while the
  CPL writer resolved the fab *family*; preflight now resolves the family once,
  and `bom_fields` is reconciled against `--auto-lcsc` (#4590, #4591).

- **3D bodies rendered rotated away from their copper.** The model inserter
  compensated translation only and always wrote an identity rotation, so a
  footprint whose rotation is baked into its pad coordinates rather than its
  placement angle rendered a quarter turn off. Orientation is now derived from
  an anchor-pad vector on both footprints and baked into the model node, with
  the centroid offset composed in the rotated frame. A sign error in that bake
  (`rz - θ`, where KiCad's render-time negation makes `rz + θ` correct)
  additionally threw board 07's 9-pin header 20.32 mm off the board edge.
  Co-oriented footprints compute θ ≈ 0 and are byte-identical (#4448, #4583).

- **A sidecar `rotate` could silently inherit a frame-mismatched packaged
  `offset`.** A packaged LCSC transform's `offset` is a post-rotation
  translation, meaningful only under the `rotate` it was calibrated with — but
  the per-field sidecar/table merge let a board sidecar override `rotate` alone
  and keep that offset, producing a plausibly-placed, quietly-wrong body. The
  merge now rejects the split with an error naming the stale components and
  both remedies; restating `offset` in the sidecar (including an explicit
  `[0, 0, 0]`) wins the merge and clears it (#4636).

- **Routing was nondeterministic from a dangling C++ grid reference.** The
  nanobind pathfinder constructors stored the incoming `Grid3D` as a bare C++
  reference with no keep-alive policy, so in the common
  `Pathfinder(Grid3D(...), rules).route(...)` idiom the grid was freed as soon
  as the factory returned and the A* then read freed heap (AddressSanitizer
  pins it at `grid_.at()`). The symptom was run-to-run variation in segment
  counts on identical input (#4485).

- **Net-class clearances were dropped on three independent paths.** The
  post-route sidecar writer hard-coded `<output_dir>/net_class_map.json` and
  wrote unconditionally, so routing into the directory holding the user's
  hand-authored map silently clobbered it — the derived map now diverts to
  `net_class_map.effective.json` on a collision, and the user's map is merged
  over the classifier output. Hand-authored KiCad layer names (`"In1.Cu"`) in
  `preferred_layers` / `avoid_layers` either killed detailed routing with
  `invalid literal for int()` or silently never matched; they now normalize to
  grid indices against the board's own layer stack. And the lattice pathfinder
  seeded every `--preserve-existing` obstacle at the DRU floor, spacing new
  copper 0.166 mm from an HV net the map put at 2.0–3.2 mm (#4428, #4587, #4597).

- **Creepage safety and table correctness.** A high-|V| net whose routing class
  was not HV (a 150 V gate-drive net classed Digital) never entered the HV
  census — a safety-gate false pass — now closed by unioning voltage-mapped
  nets into census membership behind a new `--census-threshold` (default 30 V).
  The IEC 60664-1 Table F.4 transcription started at the 50 V row, so every
  working voltage ≤ 50 V mapped to 1.2 mm at PD2/IIIa — a floor no dense-board
  adjacent-pad gap can meet, making the gate unpassable; the seven missing
  sub-50 V rows from EN 60664-1:2007 are transcribed, dropping the floor to
  0.40 mm (IEC 62368-1 Table 17 shares the table and is fixed too). And
  `kct optimize-placement --voltage-map` rejected the `_`-prefixed reserved
  keys the creepage loader skips; it now delegates to that one parser
  (#4401, #4402, #4404).

- **Connectivity analysis reported false opens and dangling vias.**
  `kct net-status` split a cross-layer path (`pad → F.Cu → through-via →
  In2.Cu → through-via → F.Cu → pad`) into per-layer components and reported a
  false `incomplete`; pads are now fused across every via that spans the
  relevant layers, with a blind/buried via declining to fuse a layer it cannot
  reach. `kct stitch` decided via placement against the zone *outline* rather
  than the refilled copper, so a via dropped where the pour had retreated had
  nothing to bond to and KiCad reported it dangling; all three modes now gate on
  same-net fill containment (#4429, #4432).

- **Writer and mutation fidelity.** `kct zones hv-keepout` emitted its
  disposition tokens as quoted strings, which KiCad 10 rejects as a hard parse
  error, making the written board unloadable; they now serialize as bare
  symbols. And rotating a footprint through the recovery applicator bumped only
  the footprint angle, leaving every pad's absolute angle stale on
  serialization — the phantom-short / wrong-gerber-aperture defect class for
  chamfered-roundrect, trapezoid, and custom pads. Schema `Pad` objects are now
  linked to their S-exp nodes, so `pad.rotation` round-trips (#4430, #4518).

- **`kct sch fix-annotation` left cross-sheet duplicate power references in
  place.** Canonicality was judged one symbol at a time, so two symbols on
  different sheets both holding a canonical `#PWR40` were each skipped and the
  error survived the repair. Uniqueness is now enforced project-wide across the
  flattened hierarchy, first-occurrence-wins. Power nodes are also excluded from
  the net-neutrality gate's snapshot — their designators are electrically
  meaningless, and including them aborted an otherwise neutral repair (#4415).

- **CLI plumbing defects.** The outer `kct route` parser declared
  `--auto-layers` with `default=True`, collapsing "the user typed it" and "the
  user typed nothing", so an explicit `--auto-layers` never reached the inner
  command — `--complete` disabled auto-layers while printing "pass
  `--auto-layers` to override". The flag is now tri-state. `kct explain` picked
  `spec_references[0]`, so which manufacturer's YAML won a shared `rule_id`
  depended on glob order; references are now merged per rule id and
  `explain(context={"manufacturer": ...})` resolves deterministically
  (#4491, #4502).

- **Three `kct check` flags were declared but unreachable.** `--refill-zones`,
  `--waivers`, and `--courtyard-waivers` existed on the inner check command but
  were never declared on the outer `kct check` subparser, so the documented
  `--refill-zones` was rejected outright and the two waiver-path overrides
  silently fell back to sidecar auto-discovery. All three now reach the inner
  command, and the route-parser drift guard is extended to the check parser
  pair (both allowlists empty) so the class cannot recur (#4633).

- **`kct build-native` could contradict `--check` seconds later.** Post-build
  verification ran in the process that had just done the build, which cannot
  observe a replaced C extension: once `router_cpp.*.so` is dlopen'd, CPython's
  runtime extension cache survives `sys.modules.pop` and `importlib.reload`, so
  the probe described the *pre*-build extension — both a false "not loading
  correctly" and a false "installed successfully!" were reproduced.
  Verification now runs in a fresh interpreter. Separately, nanobind and the
  scikit-build-core/cmake/ninja chain moved into the default `dev` group and
  the `all` extra, so an unrelated `uv sync` cannot prune them (#4412, #4589).

- **Misleading diagnostics.** An LCSC no-match search leaked
  `(J1) ('NoneType' object is not iterable)` into the BOM-enrichment summary,
  because the live JLCPCB API returns `"list": null` and `dict.get(key,
  default)` substitutes only when the key is *absent*; it now reports a clean
  "no matching parts found". And the auto-grid "may produce clearance violations
  at fine-pitch pads" warning fired on a predicate omitting the fine-pitch term
  its own text claims, so DRC-clean boards without fine-pitch pads were told
  they were at risk (#3942, #4407).

- **Rescue and mid-session C++ router state.** A per-net rescue subprocess had
  been exiting 1 before routing anything since the auto-grid safety gate
  landed, because the rescue command never forwarded an `--allow-unsafe-grid`
  equivalent — bogus `no_output` failures for 8 of 8 nets; the refusal is now
  classified `CLI_REFUSED` rather than a fabricated "crash / OOM" diagnosis.
  And the C++ pairwise domain matrix was never re-pushed after a mid-session
  net-map or attach-zone change, because the install was gated on a flag that
  latches true on first install: remapped net ids reported 0.0 required
  clearance while stale ids kept phantom widening (#4528, #4530).

- **Diff-pair shadow constructor — copper legality** (opt-in, default off).
  Constructed copper was validated against the routing grid raster only, whose
  pad halo is deliberately shrunk in fine-pitch corridors on the promise that
  full clearance is checked in post-routing DRC — a promise never kept for
  diff-pair nets, which the nudge backstop skips. Exact, DRC-equivalent pad-
  and via-clearance predicates now apply everywhere the constructor accepts
  copper, tail candidates are ordered by how much of their length runs *coupled*
  to the partner, and crossing-tail via sites are scored by how deeply they
  intrude on other nets' escape channels (#4571, #4572, #4574, #4575).

- **Diff-pair shadow constructor — geometry and length symmetry** (opt-in,
  default off). Inside-bend corners folded the parallel offset across the guide
  centerline, and sub-grid-cell steps between impedance-band rungs left
  polyline breaks invisible to every per-segment gate but fatal to the pair —
  a finished, length-matched pair read "1/2 pads reached" and was ripped
  wholesale. Corner joins now mitre concave bends and bevel sub-cell steps, and
  the assembled chain enforces the no-gap invariant. The guide is compressed
  with a deviation-bounded 45°-legal simplifier before offsetting, the pair is
  meander-matched at construction time, and the legs are gated on a
  thickness-free via signature so a 0.012 mm planar match cannot ship a 3.19 mm
  electrical skew. Rescue tails are partner-aware, and the negotiated loop stops
  at a stranded fixed point instead of re-deriving the same failure to its
  ceiling (#4460, #4462, #4463, #4553, #4570, #4577).

- **`--net-class-map` was silently inert on every composition step.** On a
  filtered routing pass (`--nets` / `--skip-nets`, and the `--region` /
  `--complete` forms that imply `--preserve-existing`), sidecar keys were
  resolved only against the nets being routed, so keys naming *preserved* nets
  matched nothing and their clearances were dropped. `kct route` now resolves
  against preserved nets as well, merging the two domains per field. Boards with
  no preserved copper are byte-identical to before (#4622).

- **LVS label-leg mismatches were discarded, not truncated, on copper-dirty
  boards.** `_lvs_subcheck` computed the label-leg result and then early-returned
  on a copper-dirty board before formatting it, so those mismatches were absent
  from the terminal, `--format json`, and `-o report.json` alike. The
  copper-dirty branch is now a detail-selection branch rather than a return. Exit
  codes are unaffected — a copper-dirty board already failed (#4616).

- **The crossover legality census spent the budget it was measuring.**
  `KCT_CROSSTAIL_CENSUS=1` (#4580) is state-neutral but was not budget-neutral:
  its lattice sweep runs inside the shadow phase's per-pair wall-clock window,
  so census seconds were deducted from all four downstream deadlines and a
  census-on run could differ from a census-off run through budget pressure
  alone. The census now credits its **incremental** cost (the sweep after the
  first legal candidate; zero when nothing is legal) back to that window and
  reports it as a `census_s=` field appended to the `[crosstail-census]` header.
  Default-off arithmetic is bit-identical — the credit is exactly `0.0` when the
  census is unset (#4635).

- **`kct check` and the CI merge gates could load different net-class-map
  sidecars.** Stem-keyed sidecar discovery (#4601) reached `kct check` only, so
  in a directory holding both `<board-stem>.net_class_map.json` and
  `net_class_map.json` the routed-DRC gate, the match-group gate and the
  `kct export` report surface all loaded the *other* file — a different rule set
  for the three gated rules locally than in the gate that guards merge, with no
  warning on either side. All four consumers now share one stdlib-only probe
  (`kicad_tools.sidecars`); each keeps its own directory scope, so boards
  carrying only the bare name (every board in this repo) resolve exactly what
  they resolved before (#4634).

### Known limitations

- `silk_over_copper`'s aperture set is **pad-only**: untented via mask openings
  are not indexed, so a silk stroke crossing an untented via is reported by
  `kicad-cli` but not by kct. Measured on a synthetic probe board; the in-repo
  fleet tents all vias, so the gap is invisible there (#4612).

## [0.19.0] - 2026-07-20

### Summary

Feature release centered on a **high-voltage / isolation design loop** and
**via-in-pad manufacturability**, plus a family of correctness fixes to the
creepage, netlist-sync, router, and writer subsystems. New capabilities let
agent-driven boards define per-net voltage domains, void inner pours around HV
copper, place HV parts with creepage-aware segregation, relocate via-in-pad
vias off-pad while preserving connectivity, and lint LED/capacitor electrical
ratings — all deterministic and zero-GUI. No breaking changes; existing
commands are unaffected.

### Added

- **HV-isolation design loop** — a coordinated set of high-voltage tools:
  - `kct creepage --voltage-map`: per-net voltage model that derives each
    conductor pair's required creepage/clearance from its own `|ΔV|`, replacing
    the single group working-voltage assumption (#4371).
  - `kct zones hv-keepout`: generate geometric plane voids/keepouts so inner
    GND/power pours clear HV nets by a required clearance (#4372).
  - `kct optimize-placement --voltage-map` / `--hv-domains`: HV-aware placement
    with a hard creepage-keepout feasibility term, a soft same-domain cohesion
    term, and automatic derived-tap creepage exemption (#4373).
  - `/kct:hv-isolation-loop` orchestration skill sequencing the loop from
    voltage-domain capture through creepage + refill-zones fab sign-off (#4374).
- **`kct analyze electrical-rating`** — deterministic, advisory LED-overcurrent
  and capacitor voltage-derating checks sourced from schematic-field
  conventions; parts missing ratings are skipped, never failed (#4381).
- **`kct check --emit-dru` / `--emit-drc-constraints`** — emit `.kicad_dru` /
  `.kicad_pro` sidecars from the checker's already-resolved `--mfr` floors so
  `kicad-cli pcb drc` and `kct check` reason over identical rules by
  construction (#4375).
- **`kct fix-vias` off-pad relocation** — relocate via-in-pad and plane-stitch
  vias off-pad while preserving connectivity, with THT hole-to-hole clearance
  checking and multi-branch relocation (#4363, #4376, #4377).
- **`kct doctor` version-record drift check** — flags stale KiCad version
  records (#4349).

### Fixed

- **creepage/audit HV false-pass guards** — block silent HV false-PASS on mains
  boards, and drop bare `LINE`/`HOT`/`PRIMARY` from the mains-name regex to
  curb over-matching (#4362, #4370).
- **writer format hygiene** — centralize KiCad format-version stamps into
  per-stream constants, and guarantee the writer never emits a crash-form pad
  chamfer corner that segfaults `kicad-cli` (#4390, #4393).
- **DRC net attribution** — populate violation nets from the `[NetName]` token
  in `kicad-cli` JSON output (previously always empty), and rename the
  drill-clearance rule id to `hole_to_hole_clearance` (#4392, #4357).
- **netlist-sync correctness** — block value/footprint mismatches by default,
  and compare component values by parsed magnitude rather than raw strings
  (#4356, #4360).
- **router obstacle handling** — treat non-listed copper as a fixed obstacle
  for `route --nets` and seed it as a hard obstacle in the mesh router (#4361,
  #4369).
- **LVS gate** — resolve schematics for versioned board stems and warn on a
  skipped LVS gate rather than passing silently (#4358).
- **native backend** — stop a nanobind `PadBounds` ref-leak on
  `kct build-native` (#4346).

### Testing

- Golden/fidelity guards pinning the `kicad-cli pcb drc --format json` schema
  and KiCad-10 new-token round-trip fidelity, plus a deterministic replacement
  for a flaky slide-off timing test (#4388, #4389, #4368).

## [0.18.0] - 2026-07-20

### Summary

Feature release focused on **high-voltage / analog manufacturing gates** for
zero-GUI, agent-driven boards. Adds two new inspection capabilities — a `kct
creepage` audit that measures physical over-surface **creepage** distance
(slot-aware) against **IEC 60664-1 / 62368-1** table requirements, and a `kct
analyze current-sense` analog-layout lint (sense↔high-current parallel-run,
sense-loop area, Kelvin-tap integrity) — plus a real `--nets` route filter and
`pcb reinforce` multi-branch anchoring. Also closes a family of safety-relevant
`kct check` false-PASS bugs on ampacity / copper-weight sourcing. No breaking
changes; existing commands are unaffected.

### Added

- **`kct creepage` — HV surface-path (creepage) audit** (#4327):
  - Per-pair creepage **census** between HV-class nets and every other conductor
    plus the board edge, honoring milled Edge.Cuts slots (a slot lengthens the
    over-surface path), with clearance (through-air) and creepage (over-surface)
    reported as **distinct** values; `--net-class` / `--min` / `--format json`
    (#4334).
  - Required creepage/clearance **derived from IEC 60664-1 / 62368-1 tables**
    given `--working-voltage`, `--pollution-degree`, `--material-group`,
    `--standard` (conservative step-up lookup, loud on out-of-range; an
    engineering aid, not a certification) (#4332/#4338).
  - **HV/isolation section in `kct audit`** + manufacturing-readiness gate — a
    below-standard HV pair fails the audit gate (#4333/#4341).
- **`kct analyze current-sense` — analog layout lint** (#4328):
  - Per sense net: max parallel-run length + min gap to the nearest
    high-current/switching net (same layer), flagged against thresholds, census
    + `--format json` (#4335).
  - Copper **sense-loop area** metric, `--max-loop-area` (#4337).
  - **Kelvin-tap integrity** — flags a sense tap that connects mid-trace instead
    of at the force pad's metallization (#4331).
- **Real `--nets NET[,NET...]` on `route` / `route-auto`** (#4325) — route only
  the listed nets (inverse of `--skip-nets`); mutually exclusive with
  `--skip-nets`; corrects the stale `--region` help text.
- **`pcb reinforce` multi-branch anchoring** (#4323) — `--all-runs` /
  `--min-run-length` anchor every current-carrying branch of a net (not just the
  single longest run), a bounded nudge-fallback before refusing an anchor, and a
  collinear-segment-merge per-run report.
- **`route --layers auto` inner-layer advisory** (#4315) — warn when automatic
  layer assignment strands pour-net / high-ampacity nets on inner layers.

### Fixed

- **`kct check` ampacity false-PASS (safety-relevant)** — `--net-class-map` now
  resolves its keys onto board net names so `target_ampacity` is actually
  enforced (a 15 A / 0.2 mm mains trace no longer silently passes) (#4324);
  copper weight is now sourced from the board's declared **stackup** when
  present, not just `--copper`, with a cross-check warning on disagreement
  (#4326).
- **`kct analyze current-sense`** — evaluate the FAIL rule against **all**
  high-current blockers, not just the nearest-by-gap one (closes a false-PASS
  where a longer-parallel-but-farther net was the real coupling risk) (#4339).
- **`kct creepage` provenance** — correct the clearance-table field-condition
  label to **Case A (inhomogeneous field)** per IEC 60664-1 (#4343).
- **Lattice engine** — reject 0.000 mm segment-to-via copper; opt-in cleanup +
  net split (#4320).
- **`route`** — guard an empty congestion grid and export partial nets under
  `--export-failed-nets` (#4317).

## [0.17.0] - 2026-07-17

### Summary

Feature release. Introduces an **experimental alternative routing substrate** —
an adaptive octilinear **lattice** engine (`--route-engine lattice`) and a
constrained-Delaunay **navmesh/mesh** engine (`--route-engine mesh`), both
**default-OFF** — that routes large mixed-pitch boards the uniform-grid router
cannot fit in memory (softstart rev-C: 74/77 signal nets DRC-clean at ~3% of the
grid's memory footprint). Adds new route flags (`--max-cells`, analytical
`--dry-run`), settable schematic BOM/DNP fields, and a `net-status --why` fix
recommender. Also lands a batch of parts-catalog, placement, and serialization
fixes, and refreshes the vendored Loom / Repo-Skills tooling. `--route-engine
grid` (the default) is byte-identical to 0.16.0.

### Added

- **Experimental octilinear lattice routing engine** (`--route-engine lattice`,
  default OFF) (#4278) — quadtree octilinear lattice substrate where A* paths are
  45°-legal copper by construction; couples diff-pairs via a fat-agent centerline
  (#4288); honors per-net-class widths/clearances (#4289); tapered neck-down
  escape for oversize net classes (#4293). Routes softstart rev-C at 74/77 nets,
  0 DRC errors.
- **Experimental navmesh mesh routing engine** (`--route-engine mesh`, default
  OFF) (#4269, #4274, #4276) — constrained-Delaunay navmesh + funnel with
  multi-net negotiation, in-corridor lane assignment, and 2.5D via injection.
- **`--route-engine {grid,mesh,lattice}`** (#4279) — select the routing
  substrate; default `grid`.
- **`--max-cells`** (#4249) — override the auto-grid memory budget on large boards.
- **Analytical `route --dry-run`** (#4266) — report grid/cell/budget without
  allocating the grid (no OOM on large boards).
- **`--via-drill` / `--via-diameter` on `route-auto`** (#4250) — honor board via
  geometry.
- **Settable `in_bom` / `dnp` on schematic symbol generation** (#4303) — emit
  exclude-from-BOM / DNP symbols (test points, fiducials, mounting holes).
- **`net-status --why` ranked fix recommendations** (#4261) — with pin-order
  (inversion)-verified reversed-bundle detection (#4286).
- **Trunk-first region-refine large-board routing recipe** (docs) (#4264).

### Fixed

- **Parts catalog** — non-circular missing-`requests` advice + MPN-column search
  (#4295, #4296); delimited price-tier parsing so prices surface (#4297); quiet
  offline-catalog fallback on live-API failure instead of per-part noise (#4299).
- **`net-status --why`** — correct `PLACEMENT_BOUND` misattribution (removed
  bogus "analog/codec" label; split same-group vs foreign obstructions) (#4261);
  no false `DE_REVERSE_BUNDLE` recommendation on co-oriented bundles (#4286).
- **Placement** — courtyard-artwork edge overhang no longer invalidates a
  placement when all pads are on-board (ESP32 antenna keep-outs) (#4290).
- **Lattice engine** — hole-to-hole floor for cross-net vias and large-drill
  pads (#4291); tier-gated same-net via-in-pad (#4285); optimize/nudge
  post-passes gated off for non-grid engines (#4281); hard-error on incompatible
  `--route-engine` / `--strategy` combinations instead of silent grid copper
  (#4280).
- **`NetClassRouting` serialization** — `neck_trace_width` round-trip drift
  (#4309); nonexistent `min_trace_width` reference in the routing rationale
  (#4310).
- **`net_class_map_from_dict`** — skip underscore-prefixed comment keys (#4251).

### Changed

- **CLI-tail memory** — free routing-engine state before the DRC tail; attribute
  the large-board process peak to the external `kicad-cli` DRC child rather than
  kicad-tools Python (#4292).
- **board-07 Track A** — coupled diff-pair atomic rip-up/relief transaction +
  discrete `BundlePlan` HARD-lane allocator; measured verdict: the residual opens
  are placement-bound (#4256, #4257, #4258).
- **Tooling** — Loom 0.10.7 → 0.10.9, Repo Skills v0.4.0 → v0.4.1 (#4312).

## [0.16.0] - 2026-07-15

### Summary

Feature release. Adds a large batch of user-facing commands and flags: a
region-bounded routing pipeline (with boundary-stub reconnection), an
ampacity-aware net-class / DRC path, copper deduplication, anchor-PTH
reinforcement, new schematic/PCB inspection commands, and several new
strict-validation modes. Also hardens datasheet fetching, zone-editing
defaults, S-expression quoting, and the router's different-net short
guards, and reworks the vendored skills / conventions install layout.

### Added

- **`kct pcb padmap`** (#4155) — inspect per-footprint pad-to-net bindings
- **`kct sch fix-annotation`** (#4142, closes #4135) — hierarchy-aware
  power/flag-symbol annotation repair gated on net neutrality
- **`pcb strip --region`** (#4147, Phase 1 of #4136) — spatial bound on
  trace/via stripping
- **Region-bounded routing** (#4169, #4179, #4180) — `--region` spatial
  routing bound on `route`/`route-auto`, plus a boundary stub-terminal
  detector (#4174) and stub reconnection on both the router and
  orchestrator paths
- **`kct pcb dedupe` + emission-time copper dedup** (#4204, #4175) — remove
  coincident duplicate copper, and dedupe automatically at emission
- **`pcb reinforce`** (#4224) — add an anchor-PTH row along a named net
- **`/kct:help`** (#4214) — introspective meta-skill, vendored unconditionally
- **`net-status --strict`** (#4201, #4176) — real-geometry connectivity mode
- **`check --strict-connectivity` / `route --strict-drc`** (#4205) —
  require coupling evidence before skipping diff-pair clearance
- **Ampacity-aware net class + DRC** (#4221, #4223) — derive net-class
  minimum width from a target current via IPC-2221, and add an ampacity
  DRC check for routed copper width
- **Courtyard-overlap check with waivers** (#4150) — new sub-check with a
  pair-level waiver file
- **Off-board preflight** (#4187) — flag off-board footprints in the
  placement check and route preflight
- **Zones batch auto-prioritize / carve** (#4199) — auto-prioritize and
  carve overlapping zones in the batch path
- **`--expect-unrouted` copper-LVS mode** (#4164) — in the
  `check_copper_lvs` asserter
- **`BUDGET_STARVED` stuck-net verdict** (#4188) — split out of
  `PLACEMENT_BOUND` in the `--why` classifier
- **`add_stub_label` schematic helper** (#4171) — collision-aware stub-label
  placement
- **`render` accepts a direct `.kicad_pcb` path** with `-o/--output` (#4163)
- **Vendored `.kct/CONVENTIONS.md` + slimmed CLAUDE.md block** (#4209)

### Changed

- **Hierarchy-aware annotation repair** now recurses `(sheet ...)`
  references so LVS/connectivity see nested sheets (#4162, #4157)
- **Test baselines re-tuned** for board-02 segment count (#4198) and
  board-07 diff-pair/match-group gate count 9 → 8 (#4211)

### Fixed

- **`--net-class-map` `/`-prefix matching** (#4181) — match bare keys
  against `/`-prefixed board nets
- **`--placement-feedback` silent no-op** (#4184) — wire it into the
  rule-relaxation and combined-escalation paths
- **Courtyard / placement-check polygons** (#4194) — use real courtyard
  polygons for overlap detection
- **Datasheet fetching** (#4153, #4154, #4191) — validate the PDF payload
  and fall through to the next source, surface install guidance when
  optional deps are missing, and sanitize the resolved part number before
  using it as a filename
- **Zone-editing defaults** (#4193, #4197) — default `zones add`/`batch`
  and `pcb add-zone` to in-place output, matching `zones fill`
- **Stitch via occupancy** (#4200) — reject stitch vias coincident with
  different-net drills
- **`route-auto` false success** (#4203) — report partial completion
  instead of a false success
- **Different-net short hardening** (#4192, #4206, #4222) — rescue
  budget-starved stranded nets after negotiation, widen the finalize-demote
  gate to the full DRC SHORT threshold, and unify the seg-seg finalize gate
  with a post-optimize backstop
- **`create-pcb` multi-pad nets** (#4210) — net every same-numbered pad in
  `assign_net_to_footprint_pad`
- **S-expression quoting** (#4219, #4212) — treat backslash as structurally
  requiring quotes, and preserve bare atoms symmetrically so keepout enums
  round-trip unquoted
- **Sub-kΩ resistor value tokens** (#4141) — parse R-notation values
  (`330R`/`4R7`) so sub-kΩ parts rank correctly
- **Junction-dot-gated wire union** (#4226) — only union touching/overlapping
  wire segments where a junction dot is present, fixing the #4157 over-merge
  that produced false copper-LVS shorts on stub-label schematics (board-05)
- **Pad-bbox-fallback courtyard annotation** (#4227) — label courtyard
  overlaps derived from a pad bounding box (no `F.CrtYd` artwork) so they are
  distinguishable from real-polygon overlaps
- **Zone-pour plane-pad connectivity** (#4229) — count pour copper contact
  (cross-layer via chains and pad-adjacent stitching vias) so a pour-bonded
  plane pad is no longer a false-incomplete, without unioning disjoint fills

## [0.15.1] - 2026-07-13

### Summary

Patch release. Restores the `uv`-based install path (broken upstream by the
removal of `uv add --git` flags), hardens the JLCPCB offline-catalog parts flow,
and fixes two check/routing net-classification bugs.

### Added

- **Offline-catalog fallback in `LCSCClient.search()`** (#4131) — fall back to
  the local jlcparts catalog when the official API is unavailable

### Fixed

- **Installation via `uv`** (#4122) — use a `git+URL` direct reference instead
  of the removed `uv add --git` flags
- **Split jlcparts archive extraction** (#4125) — stream-extract multi-part
  archives that Python's `zipfile` rejects
- **Cross-type LCSC value matching** (#4130) — reject resistor-vs-capacitor
  value mismatches
- **Floating-pin hole-pair classification** (#4129) — classify floating-pin
  hole pairs as different-net, not same-net

## [0.15.0] - 2026-07-13

### Added

#### Router — Feasibility, Corridors & Coupled Routing

- **Monotonic feasibility certificate + constructive escape ordering** (#4089) — Certify escape feasibility and derive a constructive net ordering from it
- **Corridor attractor in the coupled joint-state cost loop** (#4088) — Bias the coupled diff-pair A* toward a shared corridor
- **Soft corridor reservation for cross-package diff pairs at escape** (#4090), plus **slack-budget corridor widening + slack-aware serpentine tuner** (#4092, Phase 1, flag-gated)
- **C++ port of the coupled diff-pair joint-state A\* loop** (#4069)
- **Scoped bundle river planner v1** (#4053, #4070) — Plan facing-column bus reversals
- **Reactive escape-freedom byte-lane reorder** (#4060, opt-in, off by default)
- **Congestion/escape-driven placement nudge** (#3872) and **joint region re-solve** (#3871) — Escape the 1:1-trade congestion minimum for placement-bound nets
- **Read-only stuck-net classifier** (#3867) — `kct net-status --why`; POUR_DISCONTINUOUS classification for pour-carried nets (#3905)
- **Deterministic iteration budgets** (#3879, #3882) — Load-independent `--deterministic-budget` with tuned per-net iteration cap, adopted in the chorus recipe
- **`--order-method` to wire RoutingOptimizer into `kct route`** (#3898)
- **45-degree geometry enforced by construction** at the copper-emission choke point (#3975)

#### Diff-Pair Routing

- **Coupled-pair budget-exit warnings** (#4106) — Warn when coupled pairs budget-exit to single-ended; compose escape routing with the diff-pair pre-pass (#3983)
- **Variable-gap parallel offset within the impedance band** for the shadow constructor (#3991), with **quantized shadow copper by construction + hard per-pair shadow budget** (#3988)

#### Checks, DRC & Validate

- **Stale zone-fill advisory + opt-in `--refill-zones` flag on `kct check`** (#4113) — Warn when copper is measured against possibly-stale stored fills; optionally shell out to `kicad-cli pcb drc --refill-zones --save-board` first
- **Net-relationship labels on hole-to-hole drill-clearance findings** (#4114) — Same-net/different-net tags, unconditional net display, and a BY RULE sub-count breakdown
- **Thin-copper sliver detection** via per-layer morphological open (#3853)
- **`silk_over_copper` / `silk_edge_clearance` DRC rules** (#3849)
- **Net-0 stray-copper bridge detection** between assigned nets (#3819)
- **Length-match measurements on default `kct check` output** + via-inclusive diff-pair skew (#3955)
- **Fail-loud missing-footprint preflight** + heuristic auto-assign (#3868)

#### Copper-LVS Gate (boards 01–07)

- **Label-free zone-pour copper extraction** via hole-aware shapely overlap (#3769); shared `write_lvs_report` helper (#3782)
- **Copper-LVS gates wired across boards 01–07** (#3782, #3784, #3788, #3798), with a `--lvs-only` e2e asserter and board-03 e2e CI job
- **Real LVS evidence** — wire boards 06/07 fixture schematics (#4013); emit `lvs.json` for boards 01+02 (#4001)
- **Hierarchical-schematic LVS** (#4112) — `_schematic_pin_to_net` now recurses `(sheet ...)` references, so LVS is no longer vacuous on multi-sheet designs

#### Parts / LCSC & 3D Models

- **LCSC/EasyEDA fetch-on-demand model resolver tier** (#4072, #4075) and **cross-library substitution tier** for generic footprint model refs (#4033)
- **LCSC tier applied to fleet-waived parts** (#4076); 3D model refs added to fleet footprints so gallery renders show components (#4012)
- **Offline JLCPCB parts catalog** (#4117) — `kct parts sync-catalog` downloads the yaqwsx/jlcparts SQLite dataset into the cache dir; `LCSCClient` falls back to it when the live API is unavailable
- **Official JLCPCB open-platform API backend, BYO access key** (#4119) — HMAC-SHA256 signed client; set `JLCPCB_ACCESS_KEY`/`JLCPCB_SECRET_KEY` to make the official API the preferred parts tier (official → anonymous → offline catalog); keyless behavior unchanged

#### Zones, CLI & Build

- **`zones add --bbox/--region` island-pour flags** (#3813)
- **`pcb move-footprint --absolute` coordinate mode** (#3809)
- **`kct route` routing flags** — monotone-certificate / cross-package / slack flags exposed on the CLI (#4103)
- **Per-step build timings + heartbeat** for silent subprocess steps (#3967)

#### Consumer Tooling / Install

- **Consumer-generic `.claude/commands/kct/` skills** (#4057, #4064) and **vendored portable CI gates into consumer `.kct/ci/`** (#4063)
- **`/kct:tapeout` skill** (#4115) — Guided workflow producing a complete fab-ready export bundle

### Fixed

- **Router correctness** — C++ grid corridor-reservation keep-out + attractor port (#4071, #4078); lateral-trace keep-out + per-reservation soft/hard flags (#4087); through-via measurement in match-group skew (#4030); min-hole-to-hole drill spacing at via sites (#3857); memory-forced auto-grid refuses to go coarser than clearance/2 (#3945); KiCad-canonical uuid/net ordering + seeded DRC-reroute UUIDs (#3957); budget-bounded negotiated rip-up reroutes (#3992); native-backend guard on the slow DDR reach test (#4093)
- **Diff-pair** — stop the impedance-coupling gap from freezing the coupled search (#4052, #4061); restore single-ended back-reference on mid-copy failure (#4082); early-abort the collapsed coupled pass so an all-pairs budget-exit ships the single-ended-equivalent result (#4107); via-length-aware match-group tuner (#3985); iteration-vs-wall-clock exit diagnostics (#3954)
- **Validate / DRC** — rotate pad offsets for solder-mask (#4083) and THT drill-clearance (#4067) violation locations; report DRC locations in sheet-absolute coordinates (#4048); model pads as true roundrect/oval geometry (#3835); dedicated `min_hole_to_hole` drill-to-drill spec (#3846); fail-loud when geometric DRC did not run (#3820, #3832)
- **Copper-LVS / connectivity** — layer-aware segment chainer drops phantom crossover shorts (#3792); via-into-pour / via-in-pad bonding (#3796); detect via-to-foreign-pour shorts (#3946); model pour nets per fill-island (#3947); union pour pads across all fill islands (#3777)
- **Zones / stitch** — carve foreign-net track segments out of zone fills (#3778); de-duplicate pour zones for deterministic board-07 fills (#3821); reject connectivity-fallback vias that graze foreign pour (#3930)
- **Build pipeline** — export before verify, stop misreporting the manifest gate as DRC failure (#3974); reject stale `*_routed.kicad_pcb` via mtime capture (#3982); delete the manufacturing bundle when VERIFY fails after EXPORT (#3980); block shorted-board bundles with a DRC safety floor (#3929)
- **Parser / schema / schematic** — synthesize the net table for KiCad 10 `--save-board` output (#4039); preserve multi-unit symbol structure on from-scratch save (#3885); multi-unit-aware net-query connectivity (#4038); emit companion sym-lib-table for synthesized power symbols (#3965)
- **PCB / report / site** — emit absolute pad angles when placing rotated footprints (#3903); cwd-independent pandoc PDF image resolution + dropped-figure detection (#4040); frame the interactive viewer on the board, not the drawing sheet (#4016); center every board on its drawing sheet + `kct pcb center-on-sheet` (#4015)
- **ERC** — suppress cross-sheet global-label false positives in `kct erc` (#3814)
- **Placement** — point the cmaes `ImportError` at the placement extra (#4111); score the current layout in `--dry-run` (#3964); contain components within the real Edge.Cuts outline (#3812)
- **Deps** — make `shapely` a core dependency and fail loud when absent (#3833)
- **Manufacturing / mfr** — write sibling `.kicad_pro` + Default netclass on `apply-rules` (#4109); read `gr_poly/gr_curve` outlines and warn on off-board placement in sync-netlist (#4110)
- **Export / BOM** — hard-fail `--auto-lcsc` with an actionable install hint when the `parts` extra is missing, instead of shipping an empty LCSC column at exit 0 (#4116)

### Changed

- **Board fixtures (00–07) hardened for the gallery** — Reconciled schematic↔PCB net drift to single net models (#3771, #3774), re-routed boards to 0 blocking DRC / clean copper-LVS (#4002, #4004, #4005), and added fail-fast route/verify gates (#3854, #4066, #4068). Board-05 phase tree and ISENSE Kelvin sense nets completed artifact-first (#3908, #3997, #3998)
- **CI gates** — unified the blocking-error counter across the two routed-DRC gates (#4029); activated diff-pair/match-group DRC rules via net-class sidecar in board-06 (#3834); deterministic CI-terminating budgets for board-05/06 re-routes (#3886, #3892)

## [0.14.0] - 2026-06-16

### Added

#### Demo Gallery Website (kicad-tools.org)

- **Astro demo gallery** (#3682, #3683, #3684) — Build-time board data loader, gallery index with one card per board, and per-board detail pages with renders, metrics, and downloads
- **Interactive PCB viewer** (#3692, #3693, #3706, #3708) — Embedded KiCanvas viewer on board detail pages with loading/error states, mobile CSS, and a bulletproof loading overlay
- **Gallery structure & chrome** (#3698, #3703) — Separate "Demo boards" vs "Project" sections (excludes `chorus-test`) and shared Header/Footer across gallery pages
- **LVS status chip in gallery** (#3754) — Surface schematic-vs-layout LVS status on board cards
- **DRC-aware "Ready" badge** (#3718) — Never show a "Ready" badge for boards with DRC violations
- **Cloudflare Pages deploy** (#3687, #3689, #3697) — Token-guarded manual deploy script and workflow (renders before board-metrics, with a Cloudflare account guard)
- **Live demo link** (#3710) — README now links to the live gallery at kicad-tools.org

#### Rendering & Board Metrics

- **`kct render`** (#3677) — Per-board 2D layer plots and 3D PNG renders
- **`kct board-metrics`** (#3678) — Emit a normalized `board.json` per board
- **2D layer plots as SVG** (#3701) — Emit 2D plots as SVG for kicad-cli 10 compatibility
- **Oblique 3D views** (#3704) — Map 3D front/back to oblique top/bottom views

#### PCB & Layout

- **`page_fit`** (#3715) — Auto-size the drawing sheet to the board and center it
- **Auto-size schematic sheet to content** (#3536) — Plus footprint census and off-board supercap docs

#### Checks & Verification

- **ERC/LVS/Manifest meta sub-checks for `kct check`** (#3755) — Roll up ERC, LVS, and manifest verification into `kct check`
- **Independent copper-extracted netlist LVS gate** (#3757)
- **Schematic ↔ routed-PCB LVS guard in board-00 recipe** (#3748, #3753)
- **Board 00 end-to-end regeneration CI gate** (#3751, #3756)
- **`clearance_segment_zone` / via-and-pad-vs-zone-fill DRC rules** (#3527, #3558, #3636) — Detect traces and vias/pads violating clearance against foreign zone fills
- **Connectivity DRC rule** (#3041, #3060) — Flag unrouted multi-pad nets
- **`kct fleet status` / `fleet ship-ready`** (#2832, #2843, #2932, #2939, #3099, #3113) — Survey routing and manufacturing readiness with a warn-only ship-ready gate

#### Routing & CLI

- **`kct route --preserve-existing`** (#3155, #3169) — Incremental routing mode
- **`kct route --net-class-map`** (#2996, #3000) and **`--length-match-groups`** (#2736) — Rich per-net-class routing and length-match tuning
- **`kct pcb lock-footprints` / `unlock-footprints`** (#2978)
- **`kct sch assign-footprints` + footprint suggestion** (#3158, #3173, #3175, #3182, #3196) — Bulk and ref-only footprint suggestion with pin-count validation
- **Auto PCB sizing** (#3352, #3359, #3404) — `--auto-pcb-size` with a sum-of-clearances area heuristic and edge-cut grow
- **Fine-pitch escape routing** (#3374, #3378) — Adaptive-radius escape detector with per-net-class clearance threading

### Fixed

- **Zone-fill foreign-pad clearance** (#3712) — Carve foreign-net antipads out of zone fills
- **Foreign-pad-metal traversal** (#3225, #3226, #3227, #3545, #3565) — Reject A* routes through static foreign-pad halos and sync the `pad_blocked` bitmap into the C++ grid
- **Euclidean clearance kernels** (#3232, #3248) — Switch trace- and via-clearance kernels from Chebyshev to Euclidean discs
- **Multi-layer via-barrel clearance** (#3487, #3517, #3522, #3578) — `clearance_segment_via` checks every layer a barrel spans; certify global-min clearance in R-tree queries
- **Board zone-fill refreshes** (#3552, #3576, #3584, #3725) — Refill stale zones on boards 02/04/05 to clear segment-vs-foreign-fill findings
- **Manufacturing manifest hashing** (#3529, #3572) — Write BOM/CPL CSVs with LF endings so manifest hashes match committed content
- **PCB viewer overlay** (#3706, #3708) — Emit the viewer overlay script as raw JS so it actually runs

### Changed

- **C++ router parity** (#864, #3657, #3654, #3659) — Align C++ standard-mode via/pad-clearance behavior with the Python reference path
- **Hybrid placement objective** (#3186, #3189) — Hard-constraint gate plus 10-term soft objectives for `kct placement optimize`

## [0.13.0] - 2026-04-28

### Added

#### Schematic Editing

New atomic schematic editing commands for fully programmatic circuit construction.

- **`add-component`** (#1872) — Place symbols from library into schematics with junction and wire awareness
- **`add-wire` / `remove-wire`** (#1883, #1881) — Place and delete wire segments by coordinate
- **`add-label`** (#1885) — Place net labels on schematic wires
- **`add-no-connect` / `disconnect`** (#1858) — Place no-connect flags and disconnect pins
- **`add-bypass-cap`** (#1940) — Composite command to insert decoupling capacitors with automatic wire routing
- **`add-pull-resistor`** (#1939) — Composite command to add pull-up/pull-down resistors with wire rerouting around crossings
- **`insert-inline`** (#2111) — Break a wire and insert a component inline
- **`remove-component`** (#2133) — Delete symbols with cleanup of exclusive wire segments
- **`set-value`** (#1871) — Update symbol `Value` property
- **`set-footprint`** (#1855) — Assign footprint to a schematic symbol
- **`set-reference`** (#2180) — Rename reference designators
- **`set-symbol-property`** (#2181) — Set boolean flags on symbols
- **`move-component`** (#2186) — Reposition symbols on the schematic
- **`reconnect-pin`** (#2183) — Atomic pin-to-net reassignment
- **`set-label-direction`** (#1882) — Change global/hierarchical label shape/direction
- **`re-annotate`** (#1897) — Batch reference designator annotation with `--unannotated-only` and `--include-power` flags
- **`repair-instances`** (#1971) — Repair or create missing project instances blocks in symbol definitions
- **`cleanup-wires`** (#2179, #1858) — Detect and remove sub-mm dangling wire stubs and duplicate wire segments

#### Schematic Inspection

- **`pin-map`** (#1903, #2201, #2237) — Resolved pin-to-net assignments with coordinates, traversal across full hierarchy, connected field, and synthetic `_local_N` nets for floating pins
- **`show-pins`** (#2200) — Enriched pin output with name, type, net, and position fields
- **`sch preflight`** (#1856) — Pre-layout schematic validation including pin/pad count checks and wire connectivity
- **`sch summary`** — Connectivity counts now aggregated correctly across all sheets (#1895)
- **`sch labels --type global`** (#1896) — Now scans all sheets in the hierarchy

#### Schematic Validation (`sch validate`)

- **I2C pull-up resistor check** (#2050) — Detect missing pull-ups on SDA/SCL nets
- **Connector pinout verification** (#1936) — Validate connector pins against known interface standards
- **Symbol-to-footprint pin/pad count mismatch** (#2113) — Check for footprint pad count mismatches
- **BOOT0 pull-down detection** (#2107) — STM32 BOOT0 pin configuration check
- **Missing NRST filter capacitor** (#2102) — STM32 NRST RC filter detection
- **SWD debug pin routing check** (#2112) — STM32 SWD pin connectivity audit
- **Matched channel symmetry check** (#2105) — Detect asymmetry in differential/parallel output filter channels
- **Power pin polarity errors** (#2078) — Detect VDD/GND swap errors
- **Unconnected component detection** (#2082) — Flag components with all pins floating
- **Zone fill check** (#2109) — Detect unfilled copper zones
- **Duplicate reference designator detection** (#1968, #1582) — Per-sheet and cross-sheet duplicate ref des checks
- **Missing instances blocks check** (#1933) — Detect symbols without project instances
- **Global label driver/receiver direction mismatch** (#1909) — Warn when label shapes are inconsistent
- **No-connect on input pins warning** (#1912) — Flag `NC` flags placed on driven input pins
- **Inconsistent global label shapes** (#2049) — Detect mismatched shapes across sheets
- **Pin assignment audit and power-short detection** (#2031) — Validate pin assignments and flag power shorts
- **`value_consistency` check** (#2234) — Flag mixed capacitor voltage rating formatting
- **Unnecessary footprint variety warning** (#2185) — Flag same-value passives with mixed footprint sizes
- **`net_undeclared` rule** (#2106) — Detect PCB pads referencing undeclared nets
- **Solder mask clearance, pad size, and PTH annular ring checks** (#1690)
- **Package-size constraint in LVS** (#1583) — Pass 3/4 LVS validation now checks package dimensions
- **LVS with hierarchical schematic support** (#1568) — Multi-pass matching

#### PCB Commands

- **`pcb sync-netlist`** (#1973, #1983) — Netlist-driven PCB synchronization with collision-safe renames, ambiguous warnings, and `--auto-rename`; `--remove-orphans` flag and standalone `remove-footprint` command (#1982)
- **`create-pcb`** (#1744) — New CLI command and MCP tool to create a PCB from a schematic
- **`pcb move-footprint`** (#2076) — Relocate components on the PCB
- **`pcb add-zone`** (#2079) — Create copper pours
- **`pcb edit-outline`** (#2081) — Manage Edge.Cuts contour
- **`pcb snap-rotation`** (#2080) — Normalize component rotation angles
- **`pcb zones`** (#2077) — Inspect zones and copper pours
- **`pcb reannotate`** (#1569) — Collision-safe batch reference renaming
- **`kct sync`** (#1562) — Reconcile schematic/PCB reference designators
- **`pcb summary`** — Board dimensions added to output (#2178); zone count always displayed (#2084)

#### Routing

- **AdaptiveGridRouter as default** (#1772) — Wired as default for `--grid auto`
- **Sub-grid escape routing** (#1610, #1831) — Integrated into default `route_all` pipeline; fine-zone resolution for dense IC pads
- **BlockRouter** (#1612) — Per-block detail routing with sub-Pathfinder
- **Block-aware routing** (#1616, #1599) — Inter-block net classification; `register_block` for protected-zone routing
- **GCD-based grid candidate generation** (#1762) — Off-grid pad alignment
- **Edge clearance constraint** (#2140) — Avoid board-edge DRC violations
- **Post-optimization DRC verify-and-nudge pass** (#1793)
- **Per-net-class trace widths** (#1546, #1691) — Applied during segment creation and A* search
- **Per-net-class clearance in pre-save validation** (#1664)
- **`--best-effort` flag** (#1759) — Continue pipeline past routing failures
- **`--no-cache` / `--clear-cache` flags** (#1826, #1630) — Route cache control exposed in CLI
- **Seeed/Seeed Fusion manufacturer aliases** (#2027) — Closest-match suggestions for manufacturer names
- **Exit code epilog and SIGINT disambiguation** (#2030)

#### Via Stitching (`stitch`)

- **Extended escape routing for dense IC power pins** (#1818)
- **`--blanket` mode** (#1789) — Grid-based via stitching across zones
- **Stitch step in build pipeline** (#1814) — Added between route and optimize
- **Micro-via retry and structured skip diagnostics** (#2139)
- **`fix-vias`** (#2134) — Detect and repair same-layer vias

#### Placement

- **`place-unplaced`** (#1994) — Grid placement for unplaced components
- **Fast targeted pad clearance nudge** (#1974)
- **Block-aware placement constraints** (#1598) — Reduced-dimensionality encoding for block placement
- **C++ force-directed placement engine** (#1722) — Batch interface with Python fallback
- **C++ AABB cost evaluator** (#1718) — With nanobind bindings
- **C++ evolutionary fitness evaluator** (#1723)
- **Auto-scale boundary forces by component density** (#2032)
- **`place-route` DRC fix-retry loop** (#2002) — `ClearanceRepairer` wired in

#### DRC & Validation

- **`ViolationCategory` enum** (#1975) — Fine-pitch solder mask bridge filtering
- **Footprint nudge for pad-pad clearance violations** (#2218)
- **`repair-clearance`** — Improved post-route clearance repair (#1683)
- **Net names in clearance violation output** (#2150)
- **`--verify` flag in `fix-drc`** (#2083)
- **C++ accelerated pad-to-pad clearance checking** (#1719)
- **Footprint-outside-board placement rule** (#2104)
- **Silkscreen text height auto-fix** (#1522)
- **`--suppress-library` flag** (#1985) — Suppress silkscreen warnings for library components

#### ERC

- **Cross-sheet power pin driver suppression** (#2015) — No more false-positive `power_pin_not_driven` for cross-sheet drivers
- **Re-attribution of violations to correct sheets** (#2236) — Hierarchical designs now show violations on the correct child sheet
- **Label/net names in ERC warning messages** (#1934)
- **`fix-erc`** (#2230) — Handle `unconnected_wire_endpoint` and `wire_dangling` violation types

#### Export & Manufacturing

- **Seeed manufacturer profile** (#2231) — Added to export command
- **Pandoc+TeX PDF rendering** — Manufacturing packages now include PDF reports
- **`--latest-only` flag** (#1679) — Flatten report into a single output directory
- **`--bom-source` flag** (#1561) — PCB-only BOM generation
- **`--keep-build-artifacts` flag** (#2022) — Preserve intermediate build files
- **Spec-aware BOM enrichment** (#1510) — LCSC part numbers from `.kct` project files
- **CPL output filters** (#1511) — Exclude THT and DNP components
- **LCSC API 403 fallback** (#1760) — Fall back to cached enrichment when JLCPCB API is unavailable
- **`-o/--output` flag** (#1632) — Output path for `kct build`
- **Auto-discovery of PCB files** (#1551) — `export` accepts directory paths
- **Design reports with narrative and assembly notes** (#1995)
- **DRC violation-type breakdown table in reports** (#2163)
- **Report stackup section and figure generation** (#1698)

#### Build Pipeline

- **`zones` step** (#1688) — Auto-create power/ground zones before routing
- **Silkscreen step** (#1689) — Ref des visibility and board markings
- **Export step** (#1686) — Added to `kct build` pipeline
- **Placement optimization step** (#1687) — Added to `kct build` pipeline
- **Edge.Cuts outline generation step** (#1505)
- **Zone fill re-enabled as default step** (#1749)

#### CLI & MCP

- **`kct stitch` subcommand** (#1613)
- **`kct check --output`** (#1537) — Persist DRC reports as JSON
- **`/review-schematic` and `/repair-schematic` skills** (#1868)
- **`/review-pcb` and `/repair-pcb` skills**
- **`/export-manufacturing` skill**
- **`report-review` and `report-revise` skills**
- **Prominent C++ backend warnings and CLI hints** (#1970)

### Changed

#### Schematic Infrastructure

- **Embedded `lib_symbols` used by default** (#2202, #2235) — `sch pins`, connection checks, and hierarchy traversal now use symbols embedded in the schematic file rather than requiring `--lib`
- **Wire-graph BFS for pin connectivity** (#2238, #2216) — All pin connectivity checks now use BFS over the wire graph instead of coordinate proximity
- **NetTie net-name tracing** (#2213) — `pin-map` traces net names through `Device:NetTie` symbols
- **Hierarchical label net merging** (#2114) — Improved net merging and component-count validation in netlist builder
- **S-expression tab indentation** (#2026) — `SExp.to_string()` now uses tab indentation for KiCad compatibility

#### PCB / Schema

- **`sync-netlist` uses PCB API for value updates** (#2215) — Routes changes through the API; orphan removal added
- **Footprint position/rotation/layer setters** (#1998) — Synced to S-expression on write
- **Board summary counts** (#1958) — Derived from S-expression tree instead of cache lists

#### Routing

- **`--grid` default changed from `0.25` to `auto`** (#1680) — Prevents DRC clearance violations on mixed-pitch boards
- **KiCad 9 name-only net format** (#1821, #1779, #1780) — Supported throughout router, zones, and DRC repair modules

#### Refactoring

- **`optimize/` consolidated into `optim/`** (#1861)
- **`find_kicad_cli()` consolidated** (#1743) — Single implementation in `cli/runner.py`
- **`SessionManager` consolidated** (#1740) — Single implementation in `tools/session.py`
- **`PatternAdapter` converted to module-level functions** (#1737)

### Fixed

#### Schematic

- **`cleanup-wires`** — Mid-segment stub detection, collinear overlap detection (#2194); uses actual pin positions instead of symbol centers (#2014); strict electrical connectivity for stub detection (#2203)
- **`add-component`** — Junction detection and standalone wire/junction commands (#1884); double-snap replaced with round and connection-aware targeting (#2052); emit `instances` block (#2061); snap rotated pin offsets to 1.27mm grid (#2062)
- **`add-pull-resistor`** — Wire crossing detection and L-shape reroute (#2063)
- **`add-bypass-cap` / `add-pull-resistor`** — Emit `instances` block (#2110)
- **`re-annotate`** — Handles unannotated components and multi-project instances; supports space-indented files (#1920); detects and annotates symbols missing instances blocks (#1976)
- **`sch-replace`** — Updates `lib_symbols` entry and instance pins on replacement (#2051); handles derived symbols with `extends` chains (#2154)
- **`sch-preflight`** — Resolves `extends` chains in pin/pad count check (#2153)
- **`set-label-direction`** — `--sheet` filter made case-insensitive (#1910)
- **Pin position calculation** (#2016, #2129) — Negated Y offset and post-rotation Y in `get_pin_position()`
- **Power symbol junction** (#2126) — Creates junction when power pin lands on wire midpoint
- **`lib_symbols` embedding and lookup** — Fixed for `add-component`
- **`LibraryManager.load_embedded`** (#1911) — Inline schematic symbol loading
- **`repair-instances`** (#2132) — Detects and replaces wrong project names in existing instances blocks
- **`sch connections`** (#2008) — Loads embedded `lib_symbols`
- **Derived symbol pin resolution** (#2160) — Resolves inherited pins using `extends` chains

#### ERC

- **False-positive `power_pin_not_driven`** (#2015) — Suppressed for cross-sheet drivers
- **False-positive `single_global_label`** (#1922) — Filtered for cross-sheet globals
- **False-positive `isolated_pin_label`** (#1937) — Filtered on sheets with no labels
- **Phantom `wire_dangling` violations** (#2217) — Filtered when no matching schematic coordinates
- **KiCad 10 `items` array label extraction** (#1938)
- **`wire_dangling` re-attribution coverage** (#2192, #2151) — Expanded to cover more hierarchy cases
- **`pin_assignment` suppression for MCU GPIO pins** (#2149) — On protocol nets

#### PCB / Schema

- **`sync-netlist`** — Uses `on_board` flag (not `is_virtual`) to filter components (#2232, #2212)
- **`create-pcb`** — Grid layout calculated from board dimensions to prevent overflow (#1996)
- **Board outline detection** (#1509) — Extended to handle `gr_rect` on Edge.Cuts
- **DRC board outline coordinates** (#2130) — Transformed to board-relative space
- **Net name resolution in DRC** (#2193) — Net numbers resolved to names in violation output
- **`fix-drc`** — All violation categories detected; non-repairable violations reported (#2059); aligned categories with `check` (#2083)
- **Same-component pad-to-pad false positives** (#2075) — Suppressed in DRC
- **`.kicad_pro` selection disambiguation** (#2127) — Auto-assigns power symbol references

#### Router

- **Sub-grid escape through neighbor clearance zones** (#1712)
- **C++ backend clearance rules in A* pathfinding** (#1711)
- **Inner-layer segment clearance violations** (#1801)
- **Via-to-via, via-to-pad, via-to-segment clearance** (#1800, #1650, #1797)
- **Segment-to-segment clearance from grid quantization** (#1682)
- **Same-net via merging on save** (#1800)
- **Net-0 orphan trace cleanup** (#1981)
- **Oscillation detection relaxed; full-reorder escape strategy** (#1827)
- **Origin-offset search in grid auto-selection** (#2038) — Mixed-pitch boards
- **Pad approach radius derived from pad geometry** (#1622)
- **Off-grid net skipping and per-net A* timeout** (#1609)
- **Escape candidates that violate inter-pad clearance rejected** (#1836)
- **SSOP odd-pad escape vias routed inward** (#1844)
- **SSOP fine-pitch lateral fan-out offset** (#1791)
- **Post-route clearance correction for all routing strategies** (#1790)

#### Stitch / Zones

- **`stitch`** — Infer inner plane layers from stackup when zones are missing (#2044); check zone fill polygons in via clearance (#1816); include through-hole pads (#1955); post-write verification (#1957); guard `shutil.copy` for same-file input/output (#1950)
- **`zones`** — UUID-based net restore; proximity threshold widened to 0.5mm (#1857); overlap detection and 4-layer stackup-aware zone assignment (#2042); KiCad 9 name-only net format (#1780)
- **Net-status zone fill connectivity accounting** (#2036)

#### Placement

- **Board origin subtracted from Edge.Cuts in optimize** (#2060)
- **Post-convergence slide-off pass for residual overlaps** (#2103)
- **Courtyard-aware clamping in placement optimizer** (#1956)
- **Iterative multi-pass conflict resolution in `cmd_fix`** (#1953)
- **Force-directed divergence prevention** (#1769) — 1/r² falloff and force clamping
- **Intermediate placement state saved on SIGINT/SIGTERM** (#2037)
- **Pad positions preserved when writing optimized placements** (#2207)

#### Export / Report

- **Markdown report preserved alongside PDF during flattening** (#2162)
- **Blank schematic sheets excluded from report figures** (#1992)
- **Blank schematic detection switched from PNG size to SVG size** (#2029)
- **ERC rendered as SKIPPED when not run** (#1503)
- **Routing status classifies zone-filled and single-pad nets separately** (#1838)
- **Only exported PCB included in project ZIP**
- **Report generator uses `ReportGenerator` class** (#1521)

#### CLI / Misc

- **`.kicad_pro` resolved to `.kicad_pcb`** (#1506) — In `validate --connectivity` and `report generate`
- **Spec path resolution for subdirectory `.kct` files** (#1631)
- **`--min-completion` controls exit code threshold** (#1951)
- **BOM LCSC part numbers preserved across regeneration** (#1606)
- **JLCPCB API 403 circuit breaker** (#1508)
- **JLCPCB 2oz copper min trace/clearance corrected to 6mil** (#1502)
- **`copper_weight` string values like `'2oz'` parsed correctly** (#1596)
- **Progress output flushed during long routing runs** (#1662)
- **`review-schematic` uses pre-computed pin-map data** (#2233) — Instead of LLM coordinate math

### Removed

- **atopile submodule** — No longer referenced by any source code
- **Dead GPU signal integrity kernel** (#1873)
- **Dead `layout/` module** (#1742)
- **4 dead standalone CLI scripts** (#1904)
- **6 dead GPU detection symbols** (#1721)
- **Dead `optimize/` package** (#1861) — Consolidated into `optim/`
- **4 dead functions** (#1741) — Zero callers
- **Dead `SessionExpiredError` and error-mapping code** (#1725)
- **Dead `detect_signal_type` and `assign_layer_preferences` router functions** (#1730)

## [0.12.0] - 2026-04-15

### Summary

Manufacturing-export release: a complete `kct export` package generator with pre-flight
validation, JLCPCB LCSC auto-matching, and an EXPORT pipeline stage — plus substantial
routing and DRC/ERC auto-repair improvements and KiCad 10 compatibility fixes.

### Added

#### Manufacturing Export

- **`kct export`** (#1469) — Manufacturing package generation (Gerber, drill, BOM, placement)
- **Pre-flight validation checklist** (#1475) — Validate a design before manufacturing export
- **BOM–CPL cross-reference check** (#1487) — Catch BOM/placement mismatches before export
- **LCSC auto-match** (#1473) — Auto-match LCSC part numbers during JLCPCB BOM export, with `--auto-lcsc` / `--no-auto-lcsc` flags (#1484)

#### Pipeline

- **EXPORT step** (#1472) — Final pipeline step after REPORT
- **REPORT step** (#1377) — Final pipeline step after AUDIT
- **FIX_ERC step** (#1378) — Automatic ERC remediation, with `fix-erc` command (#1379)
- **fix-silkscreen step** (#1376) — Inserted between ERC and fix-vias
- **DRC/ERC/verdict summary** (#1416) — Printed after pipeline completion
- Raised default `--max-passes` 3 → 20 (#1437) and `--max-displacement` 0.5mm → 2.0mm (#1429)
- ERC errors partitioned into blocking and non-blocking (#1406)

#### Routing & DRC Repair

- **All copper layers routable** including PLANE-type layers (#1474)
- **`--layers 4-all`** — 4-layer all-signal routing (#1466)
- **Local A\* rerouting** for infeasible clearance violations (#1399), with multi-segment cluster rerouting for grouped via violations (#1408)
- **Post-pass connectivity check** with automatic rollback (#1439)
- Handle `clearance_pad_segment` / `clearance_pad_via` violations in fix-drc (#1470)
- Partial-routing UX improvements with actionable suggestions (#1387, #1467)

#### Audit & Report

- **Analog component detection** with advisory audit action item (#1489)
- **Orphaned-footprint detection** — footprints on PCB but not in schematic (#1488)
- Zone connectivity treated as advisory when core checks pass (#1465, #1431)
- Five missing ERC violation types and suggestion handlers (#1385); non-electrical ERC violations classified as warnings (#1386)

### Fixed

- **KiCad 10 compatibility**: version-aware drill origin (#1486); recover `net_number` from PCB header for name-only net format (#1432)
- **fix-drc**: preserve trace terminal endpoints during corner chamfering (#1438); use audit step result for summary verdict instead of DRC-only (#1464)
- **zones**: restore per-element net assignments after zone fill (#1389); stable keys for net restoration under KiCad 10 net format (#1395)
- **export**: capitalize JLCPCB CPL layer values and wire aux-origin auto-detection (#1463)
- **report**: prevent double-nesting of figures path in `_embed_images` (#1384)

## [0.11.0] - 2026-04-12

### Added

#### Placement Optimization (`placement/`)

A complete placement optimization system with multiple strategies, cost evaluation,
and CLI/MCP integration for AI-driven component placement.

- **PlacementVector & Encoding** (#1223) - Define placement search space
  - Encode/decode component positions and rotations as optimization vectors
  - Board-relative coordinate system with boundary constraints

- **Cost Functions** (#1224, #1225, #1219) - Multi-objective placement evaluation
  - HPWL wirelength estimator using transformed pad coordinates
  - Overlap and boundary violation geometry detectors
  - Weighted cost function aggregator for combining objectives

- **Placement Strategies**
  - Force-directed and random seed heuristics (#1226)
  - CMA-ES evolutionary optimizer with `PlacementStrategy` ABC (#1227)
  - Bayesian Optimization strategy using Ax/BoTorch (#1235)

- **DRC Integration** (#1228) - Courtyard and pad spacing clearance checker

- **Evaluation Pipeline** (#1233) - Multi-fidelity evaluation
  - Fast/cheap evaluations for early filtering, full DRC for promising candidates

- **Netlist Graph Analysis** (#1231) - Connectivity-aware placement priors

- **Visualization** (#1230) - Optimization progress plots and convergence tracking

- **Benchmark Boards** (#1229) - Test boards for optimizer validation

- **CLI** (#1234) - `kct optimize-placement` command for CMA-ES optimization

- **MCP Tools** (#1236) - `optimize_placement` and `evaluate_placement` tools

#### Routing Orchestration

Unified multi-strategy routing with hierarchical planning and fine-pitch support.

- **Routing Orchestrator** (#1140, #1194, #1195, #1218) - Coordinate multiple routing strategies
  - Full pipeline strategy with via conflict resolution and clearance repair
  - Wire real router strategies into orchestrator

- **Hierarchical Router** (#1127) - Global routing foundation with channel assignment

- **Adaptive Grid** (#1157) - Fine grid near pads, coarse grid in open channels

- **Fine-Pitch Support**
  - Sub-grid routing for fine-pitch components (#1120)
  - Dog-leg routing for fine-pitch components (#1150)
  - Escape routing for SSOP/TSSOP packages

- **GPU-Accelerated Pathfinding** - Batch pathfinding for fine-grid routing

- **Via Conflict Management** (#1124) - Handle blocked pad access points

- **Automatic Power Plane Stitching** - Via stitching for power planes

- **CLI & MCP** (#1220) - `route-auto` command and MCP tool for orchestrator-based routing

#### DRC & Validation

- **Trace Clearance Repair** (#1121) - Nudge traces to fix DRC violations
- **Fab-Aware Severity** (#1187) - Reclassify DRC severity based on manufacturer capabilities
- **Post-Stitch DRC** (#1188) - `--drc` flag for zone fill and DRC validation after stitching

#### Type System

- **Interval Types** (#1170) - Parametric constraint system with unit-aware arithmetic
- **Typed Interface Ports** (#1184) - Type-checked circuit connections

#### Library & Tooling

- **Unused Symbol Detection** (#1222) - Find unused symbols/footprints in project libraries
- **MCP Client Setup** (#1186) - `kct mcp setup` auto-configures MCP clients
- **CLI Flags** - `--routing-aware` and `--check-routability` in unified CLI
- **C++ Backend Warnings** (#1125) - Improved discoverability and performance guidance
- **CI Pipeline** (#1165) - GitHub Actions CI with pytest, ruff, and mypy

### Changed

- **Command Protocol** (#1126) - Add command protocol for CLI migration
- **Type Consolidation** - Merge duplicate `Severity` and `Layer` enum types

### Removed

- **Dead Code Cleanup**
  - Remove stalled command protocol migration infrastructure (#1240)
  - Remove 4 dead methods from `router/core.py` and `spec/parser.py` (#1237)
  - Remove unused classes and functions from `exceptions.py` (~540 LOC) (#1167)
  - Remove unused `generate_grid_stress_test` function (#1215)
  - Remove 8 unused exports from `__all__` declarations (#1189)

### Fixed

- **Stitch Reliability** - Multiple fixes for via stitching correctness
  - Pad clearance checking to prevent shorts (#1143)
  - Copy `.kicad_pro` alongside PCB output for DRC compatibility (#1144)
  - Trace path clearance to prevent shorts from pad-to-via connections (#1142)
  - Clearance against other-net copper before placing vias (#1119)
  - Pad-to-via trace segments for electrical connectivity (#1116)
  - Remove invalid rotation parameter from via format (#1115)

- **Router Fixes**
  - Correct single-paren removal to fix invalid PCB output (#1118)
  - Cross-check annular ring requirement when sizing vias (#1117)
  - Add `start`/`end` properties to `Segment` class

- **S-Expression Parser** (#1163) - Fix bugs in `SExp.to_string()`
- **Coordinate System** (#1172, #1197) - Resolve test failures from API and coordinate changes
- **Placement Loader** - Convert footprint positions to board-relative on load

## [0.10.3] - 2026-01-24

### Added

#### GPU Acceleration (`acceleration/`)

Automatic GPU acceleration for computationally intensive operations with cross-platform support.

- **Backend Abstraction** (#1023) - Unified interface for GPU operations
  - `BackendType` enum: CUDA, Metal, CPU
  - Automatic backend detection and selection (CUDA > Metal > CPU)
  - Graceful fallback when GPU unavailable

- **GPU Configuration** (#1024) - Integration with PerformanceConfig
  - `[gpu]` section in `~/.config/kicad-tools/performance.toml`
  - Configurable backend, device ID, memory limits
  - Problem-size thresholds for GPU vs CPU selection

- **GPU Benchmarking** (#1025) - Calibrate command GPU support
  - `kicad-tools calibrate --show-gpu` displays GPU capabilities
  - Automatic GPU performance calibration

- **Optional Dependencies** (#1026) - GPU packages as optional extras
  - `uv pip install kicad-tools[cuda]` for NVIDIA GPU support
  - `uv pip install kicad-tools[metal]` for Apple Silicon support

- **GPU-Accelerated Operations**
  - Force-directed placement (#1027) - O(n²) pairwise force calculations
  - Evolutionary fitness evaluation (#1028) - Parallel population evaluation
  - Grid operations (#1029) - Large 3D array operations for routing
  - Signal integrity calculations (#1030) - Parallel trace pair analysis

### Changed

- **MCP Tool Registry** (#1083) - Unified tool registration between stdio and HTTP transports
  - New `tools/registry.py` with `ToolSpec` dataclass
  - Single source of truth for all 28 MCP tools
  - `server.py` reduced from ~1,700 to ~310 lines (82% reduction)

### Fixed

- **Acceleration**: Consolidate duplicate `GPUBackend`/`BackendType` enums (#1080)
  - Single `BackendType` enum as source of truth
  - Removed redundant `GPUBackend` from detection module

## [0.10.2] - 2026-01-18

### Summary

Programmatic PCB-construction release: a full board-editing API surface — trace routing,
copper pours/zones, silkscreen management, footprint import from schematic, and
placement optimization — alongside new schematic netlist-query and ERC capabilities.

### Added

#### PCB API

- **Trace routing API** for programmatic PCB routing (#926)
- **Copper pour / zone API** for ground planes (#927)
- **Silkscreen management APIs** for reference-designator visibility and placement (#931)
- **Manufacturing export API** — Gerber, drill, BOM, placement (#928)
- **Collision detection and DRC-aware placement validation** (#925)
- **`import_from_schematic()`** — programmatically import footprints (#919)
- **Zone-based placement optimization API** (#921)
- **Node insertion methods** for position control (#917)

#### Schematic API

- **Netlist query API** for verifying connectivity (#903)
- **Wire endpoint collision detection** (#904)
- **`run_erc()`** — invoke KiCad ERC programmatically (#906)
- **Power net connectivity validation** (#900)

### Fixed

- **PCB**: `update_footprint_position()` rotation now persists (#922)
- **PCB**: insert at-node after layer node in `add_footprint_from_file`
- **netlist**: parse reference from `(ref ...)` child node in `NetNode.from_sexp()`
- **schematic**: flatten symbols with `extends` for embedding in `lib_symbols` (#896); fix missing `lib_symbols` for complex symbol types (#892, #893)

## [0.10.1] - 2026-01-17

### Fixed

- **Schematic**: Fix `pin_position()` returning inverted Y coordinates (#889)
  - Pins were being vertically swapped due to incorrect Y-axis negation
  - This caused KiCad to crash when opening schematics with wires or no_connect flags at affected pin positions
  - Both symbol definitions and schematics use Y-down coordinates (the code incorrectly assumed symbol defs used Y-up)

## [0.10.0] - 2026-01-16

### Added

#### Design Pattern Library (`patterns/`)

Encode expert PCB design knowledge for agent use. Agents can instantiate validated patterns instead of solving common layouts from first principles.

- **Pattern Schema** (#823) - Foundational schema for PCB patterns with placement rules
  - `PCBPattern` base class for placement-based patterns
  - `IntentPattern` base class for constraint-based patterns
  - `Placement`, `PlacementRule`, `RoutingConstraint` schema types
  - Validation and spec compliance checking

- **Core Patterns** (#824) - Comprehensive library of validated design patterns
  - **Power**: `LDOPattern`, `BuckPattern` with thermal and decoupling placement
  - **Timing**: `CrystalPattern`, `OscillatorPattern` with load capacitor placement
  - **Interface**: `USBPattern`, `I2CPattern`, `SPIPattern`, `UARTPattern`, `EthernetPattern`
  - **Analog**: `ADCInputFilter`, `DACOutputFilter`, `OpAmpCircuit`, `SensorInterface`
  - **Protection**: `ESDProtection`, `OvercurrentProtection`, `OvervoltageProtection`, `ReversePolarityProtection`, `ThermalShutdown`

- **Pattern Validation & Adaptation** (#825)
  - `PatternValidator` checks instantiated patterns meet spec requirements
  - `PatternAdapter` generates pattern parameters for specific components
  - Component requirements database for automatic parameter lookup
  - Validation checks: distance, presence, trace length, value matching

- **User-Defined Patterns** (#826)
  - YAML pattern definitions with `PatternLoader`
  - Pattern definition DSL: `define_pattern()`, `placement_rule()`, `routing_constraint()`
  - `PatternRegistry` for registering and discovering patterns
  - Custom validation checks via `register_check()`

#### Explanation System (`explain/`)

Queryable explanations for design rules with spec references and fix suggestions.

- **Queryable Explanations** (#827)
  - `explain(rule_id, context)` returns contextualized rule explanations
  - `explain_violations(violations)` attaches explanations to DRC results
  - `explain_net_constraints(net)` explains why nets have certain constraints
  - Spec references with document name, section, and URL
  - Auto-generated fix suggestions with calculated deltas

- **Common Mistake Detection** (#828)
  - `MistakeDetector` identifies common PCB design mistakes
  - `detect_mistakes(pcb)` scans design for typical errors
  - Categories: decoupling, thermal, signal integrity, power distribution
  - Each mistake includes explanation and fix suggestions

- **Design Decision Rationale** (#829)
  - `DecisionStore` tracks design decisions with rationale
  - `record_decision()` captures why choices were made
  - `PlacementRationale`, `RoutingRationale` for structured tracking
  - `explain_placement()`, `explain_route()` retrieve decision context
  - Persistent storage for decision history

#### Multi-Resolution Abstraction (`design/`)

High-level operations that decompose into low-level commands automatically.

- **Multi-Resolution API** (#830)
  - **High-level**: `design.add_subsystem("power_supply", components=[...], near_edge="left")`
  - **Medium-level**: `optimizer.group_components(refs, strategy="power_supply")`
  - **Low-level**: `session.apply_move("U_REG", x=10, y=50)` (existing API)
  - Automatic decomposition of high-level commands
  - Consistent results across abstraction levels

- **Subsystem Types** (`design/subsystems.py`)
  - `POWER_SUPPLY`, `MCU_CORE`, `CONNECTOR`, `TIMING`, `ANALOG_INPUT`, `INTERFACE`
  - Optimization goals: thermal, routing, compact, signal integrity, mechanical
  - Built-in placement hints and typical component lists

- **Command Decomposition** (`design/decomposition.py`)
  - Breaks high-level operations into atomic moves
  - Pattern-aware placement strategies
  - Constraint propagation from subsystem to component level

### Changed

- **MCP Types Refactored** (#857) - Split `mcp/types.py` into domain-specific modules
  - `types/assembly.py`, `types/board.py`, `types/clearance.py`, `types/drc.py`
  - `types/gerber.py`, `types/intent.py`, `types/placement.py`, `types/routing.py`
  - `types/session.py`, `types/warnings.py`, `types/drc_delta.py`
  - Improved maintainability and reduced file size

## [0.9.3] - 2026-01-06

### Fixed

- **Pad/Via/Obstacle Clearance** (#587) - Include `trace_width/2` in clearance zone calculations
  - Pathfinder checks trace centers, but grid marking must account for trace edges
  - Fixes DRC violations where traces were placed too close to pads/vias
  - Affects `_add_pad_unsafe()`, `_add_pad_vectorized_unsafe()`, `_mark_via()`, `add_obstacle()`

- **build-native CMake Path** (#586) - Use `cpp/` directory CMakeLists.txt for pip-installed packages
  - Fixes `kct build-native` failing when installed via pip
  - Now correctly locates CMakeLists.txt in package installation directory

## [0.9.2] - 2026-01-06

### Added

#### Parallel Processing Infrastructure

- **Thread-Safe Grid** (`router/grid.py`) (#584)
  - Optional `thread_safe=True` parameter for `RoutingGrid`
  - RLock-based synchronization for concurrent grid access
  - `locked()` context manager for atomic multi-operation sequences
  - Zero overhead when disabled (default)

- **Parallel Routing Operations**
  - Parallelize Monte Carlo routing trials (#576)
  - Parallel fitness evaluation in evolutionary optimizer (#578)
  - Parallelize placement conflict detection (#577)
  - Parallelize congestion grid processing (#582)

- **C++ Backend Integration** (`router/core.py`) (#581)
  - Autorouter class now automatically uses C++ backend when available
  - Seamless fallback to Python implementation

#### New Commands

- **`kct build-native`** (`cli/__init__.py`) (#580)
  - Install C++ backend for ~100x performance boost
  - Automatic compilation with nanobind
  - `kct build-native --check` to verify installation

#### Configurable Units

- **Unit System** (`units.py`) (#574)
  - Configurable output units: millimeters (default) or mils
  - `kct config units mm|mils` to set preference
  - Affects all CLI output (congestion, spacing, dimensions)

### Fixed

- **mfr compare --layers** (#568) - Fix `--layers` flag being ignored in manufacturer comparison

## [0.9.1] - 2026-01-06

### Added

#### Router Performance Optimization (Phase 4)

- **C++/nanobind Core** (`router/cpp/`)
  - High-performance C++ implementation of core routing operations
  - nanobind bindings for Python integration
  - ~100x speedup potential for A* neighbor evaluation
  - ~100x speedup potential for route marking operations
  - Graceful fallback when C++ module is not built

- **Algorithm Improvements** (`router/pathfinder.py`)
  - Optimized grid operations for JLCPCB's 0.0635mm grid constraints
  - NumPy-based grid for improved performance
  - Benchmarking infrastructure with `--profile` flag (#554)

#### New Commands

- **`kct init` Command** (`cli/init_cmd.py`)
  - Initialize projects with manufacturer-specific design rules
  - Support for all registered manufacturers

- **Parts Suggest CLI** - Exposed parts suggestion feature as command-line tool

#### Manufacturers

- **FlashPCB** (`manufacturers/flashpcb.py`, `manufacturers/data/flashpcb.yaml`)
  - USA-based PCB fabrication and assembly house
  - 2 and 4 layer boards with 1oz/2oz copper
  - 5 mil trace/space, 8 mil minimum drill
  - 10" × 10" max board size
  - 2-sided assembly (down to 0201)
  - 3, 5, and 10 day lead times

### Fixed

- **Trace Blocking Radius** (#553) - Include clearance in trace blocking radius calculation for DRC compliance
- **fp_text Reference Format** (#565, #547) - Support `fp_text` reference format in placement fix apply
- **mfr export-dru FileNotFoundError** (#550) - Fix FileNotFoundError when running outside project
- **Trace Deletion** (#555) - Delete traces from sexp.children instead of sexp.values
- **Test Cleanup** (#557) - Remove unused pytest import in cpp_backend tests

### Changed

- **Version Management** - `__version__` now automatically reads from `pyproject.toml` via `importlib.metadata`

## [0.9.0] - 2026-01-06

### Added

#### Design Intent System (`kicad_tools.intent`)

Declare high-level design intent and automatically derive constraints:

- **Interface Declarations** (`intent/types.py`, `intent/constraints.py`)
  - `IntentDeclaration` - Declare design intent for net groups
  - `Constraint` - Auto-derived constraints from interface specs
  - `create_intent_declaration()` - Create declarations with automatic constraint derivation
  - `validate_intent()` - Validate declarations against design

- **Built-in Interface Specifications** (`intent/interfaces/`)
  - `USB2HighSpeedSpec`, `USB2FullSpeedSpec`, `USB3Spec` - USB with impedance/length matching
  - `SPISpec` - SPI bus with clock/data timing constraints
  - `I2CSpec` - I2C with pull-up and capacitance requirements
  - `PowerRailSpec` - Power rails with decoupling and current requirements

- **Interface Registry** (`intent/registry.py`)
  - `REGISTRY` - Global registry of interface types
  - Extensible for custom interface definitions
  - Auto-registration of built-in specs

- **MCP Integration** (`mcp/tools/intent.py`)
  - `declare_interface` - Declare interface intent via MCP
  - `list_interfaces` - List available interface types
  - `get_intent_status` - Check constraint satisfaction

#### Continuous Validation (`kicad_tools.drc`)

Real-time DRC during placement sessions:

- **Incremental DRC Engine** (`drc/incremental.py`)
  - `IncrementalDRC` - Efficient DRC with cached state
  - `SpatialIndex` - R-tree spatial indexing for O(log n) region queries
  - `check_move()` - Preview DRC impact without applying
  - `apply_move()` - Apply move and update cached state
  - Performance: <10ms incremental checks for 200+ components

- **DRC Delta in Responses** (`drc/incremental.py`)
  - `DRCDelta` - New vs resolved violations after changes
  - `Violation` - Rich violation details with location and items
  - Integrated into MCP `query_move` and `apply_move` responses

- **Predictive Warnings** (`drc/predictive.py`)
  - `PredictiveAnalyzer` - Anticipate problems before they occur
  - Routing difficulty estimation based on placement
  - Congestion analysis for component density
  - Intent risk checking for declared interfaces
  - Confidence-scored warnings with suggestions

#### Intelligent Failure Recovery (`kicad_tools.router`)

Root cause analysis and resolution strategies for failures:

- **Failure Analysis** (`router/failure_analysis.py`)
  - `RootCauseAnalyzer` - Determine why operations failed
  - `FailureCause` enum - CONGESTION, BLOCKED_PATH, CLEARANCE, etc.
  - `BlockingElement` - Identify what's blocking desired operations
  - `CongestionMap` - Grid-based congestion heatmap

- **Resolution Strategies** (`router/resolution.py`)
  - `ResolutionStrategy` - Actionable fix with difficulty rating
  - Multiple strategies per failure with trade-off analysis
  - Strategy types: MOVE_COMPONENT, ADD_VIA, REROUTE_NET, USE_LAYER
  - Difficulty estimation: EASY, MEDIUM, HARD

#### Context Persistence (`kicad_tools.mcp.context`)

Maintain design context across MCP sessions:

- **Decision Tracking** (`mcp/context.py`)
  - `Decision` - Record design decisions with rationale
  - `DecisionOutcome` - Track success/failure of decisions
  - Decision history for learning and explanation

- **Session Context** (`mcp/context.py`)
  - `SessionContext` - Extended session state
  - `AgentPreferences` - Learned preferences from behavior
  - `StateSnapshot` - Efficient state checkpoints

- **State Summaries** (`mcp/context.py`)
  - Compact state encoding for reduced token overhead
  - Incremental updates instead of full state
  - Queryable decision history

### Changed

- MCP session tools now return DRC delta in `query_move` and `apply_move` responses
- Placement sessions integrate with incremental DRC for real-time validation

### Dependencies

- `rtree>=1.0` - R-tree spatial indexing (optional, falls back to linear scan)

## [0.8.0] - 2026-01-05

### Added

#### MCP Server for AI Agent Integration (`kicad_tools.mcp`)

FastMCP-based server enabling AI agents like Claude to interact with KiCad designs:

- **Core Infrastructure** (`mcp/server.py`)
  - FastMCP server implementation with stdio and HTTP transports
  - Comprehensive error handling with actionable MCP responses
  - Session management for stateful operations
  - CLI: `kct mcp serve` (stdio), `kct mcp serve --http` (HTTP transport)

- **Analysis Tools** (`mcp/tools/analysis.py`)
  - `analyze_board` - Get board summary (layers, components, nets, dimensions)
  - `get_drc_violations` - Run DRC and return violations with locations
  - `measure_clearance` - Check clearance between components/nets

- **Export Tools** (`mcp/tools/export.py`)
  - `export_gerbers` - Generate Gerber files for manufacturing
  - `export_bom` - Generate bill of materials in various formats
  - `export_assembly` - Generate complete manufacturing package (BOM + pick-and-place)

- **Placement Tools** (`mcp/tools/placement.py`)
  - `placement_analyze` - Analyze current placement quality with metrics
  - `placement_suggestions` - Get AI-friendly placement recommendations

- **Session Tools** (`mcp/tools/session.py`)
  - `start_session` - Begin a placement refinement session
  - `query_move` - Preview effect of moving a component
  - `apply_move` - Apply a component move within session
  - `commit` - Commit session changes to file
  - `rollback` - Discard session changes

- **Routing Tools** (`mcp/tools/routing.py`)
  - `route_net` - Route a specific net with configurable strategy
  - `get_unrouted_nets` - List nets that need routing

#### Layout Preservation System (`kicad_tools.layout`)

Preserve component placement and routing when regenerating PCB from schematic:

- **Hierarchical Address Matching** - Match components by hierarchical path (e.g., `power.ldo.C1`)
- **Anchor-Based Positioning** - Calculate subcircuit offsets from anchor components
- **Net Remapping** - Handle net name changes during regeneration
- **Incremental Updates** - Only touch changed components, preserve manual adjustments

#### BOM Command Enhancements

- **Availability Checking** (`--check-availability` flag)
  - Check LCSC/JLCPCB stock availability for BOM parts
  - `--quantity` flag to specify board count (multiplies quantities)
  - Exit code 2 when parts are unavailable
  - CLI: `kicad-bom design.kicad_sch --check-availability --quantity 5`

- **JLCPCB Assembly Validation** (#510)
  - Validate BOM compatibility with JLCPCB assembly service
  - Check for missing LCSC part numbers
  - Verify part availability and assembly category
  - CLI: `kicad-bom design.kicad_sch --validate-jlcpcb`

#### Documentation

- **MCP Server Setup Guide** - Configuration for Claude Desktop and other MCP clients
- **Example Workflows** - End-to-end agent-driven PCB design examples

### Dependencies

- `fastmcp>=2.0,<3` - MCP server framework (optional, in `[mcp]` extra)
- `pydantic>=2.0` - Request/response validation (optional, in `[mcp]` extra)

## [0.7.2] - 2026-01-04

### Added

- `--format json` flag for `kct placement optimize` command (#449)

### Fixed

- Handle empty reference in BOM `grouped()` to prevent IndexError (#452)
- Net-status now correctly detects pad-to-zone connectivity (#451)
- Estimate cost reads components from PCB footprints (#448)

## [0.7.1] - 2026-01-04

### Added

- `--layers` option to autorouter for multi-layer board support (#426)
- Positional argument support to `validate --sync` command (#422)
- Auto-detect target layer from zones in stitch command (#417)
- `name` property to PadState for state export (#430)
- `graphic_items` property to PCB schema (#427)
- pyyaml added to required dependencies (#419)

### Fixed

- Reason agent reporting incorrect board size and layer count (#425)
- Parts availability command silent exit (#424)
- Net-status via-to-zone connectivity detection (#418)
- Router `--grid` and `--clearance` parameters being ignored (#423)

## [0.7.0] - 2026-01-04

### Added

#### Rich Error Diagnostics (`kicad_tools.exceptions`)

Compiler-style error reporting with actionable context:

- **Source Position Tracking** (`SourcePosition`)
  - File, line, and column tracking for all errors
  - Element type and board coordinates for PCB errors
  - Layer information for multi-layer issues

- **S-expression Snippet Extraction** (`SExpSnippetExtractor`)
  - Extract code context around error locations
  - Line numbers and visual markers
  - Complete element extraction by reference

- **Error Accumulation** (`ErrorAccumulator`)
  - Collect multiple errors instead of failing on first
  - Batch validation for comprehensive feedback
  - `ValidationErrorGroup` for aggregated reporting

- **Rich Terminal Rendering**
  - Syntax-highlighted error output
  - Color-coded severity levels
  - Visual separators for multi-error reports

#### Actionable Feedback (`kicad_tools.drc`, `kicad_tools.analysis`, `kicad_tools.constraints`)

Transform error codes into specific fix suggestions:

- **DRC Fix Suggestions** (`drc/suggestions.py`)
  - "Move C1 0.5mm left to clear U1 pad"
  - Specific component and direction recommendations
  - Clearance violation resolution strategies

- **ERC Root Cause Analysis** (`cli/erc_explain_cmd.py`)
  - Deep analysis of electrical rule violations
  - Root cause identification
  - Step-by-step fix instructions
  - CLI: `kct erc explain <schematic>`

- **Routing Congestion Analysis** (`analysis/congestion.py`)
  - Grid-based density hotspot detection
  - Track length and via count per area
  - Unrouted connection identification
  - Severity classification (LOW → CRITICAL)
  - CLI: `kct analyze congestion <pcb>`

- **Constraint Conflict Detection** (`constraints/conflict.py`)
  - Detects keepout/grouping/region conflicts
  - Conflict types: OVERLAP, CONTRADICTION, IMPOSSIBLE
  - Multiple resolution options with trade-off analysis
  - CLI: `kct constraints check <pcb>`

#### Design Quality Metrics (`kicad_tools.analysis`)

Proactive design quality analysis:

- **Trace Length Reports** (`analysis/trace_length.py`)
  - Per-net and per-segment length calculation
  - Automatic timing-critical net detection (CLK, USB, DDR, LVDS)
  - Differential pair skew calculation
  - Layer change tracking
  - Tolerance checking (actual vs target)
  - CLI: `kct analyze trace-lengths <pcb>`

- **Thermal Analysis** (`analysis/thermal.py`)
  - Heat source identification (regulators, MOSFETs, drivers)
  - Power dissipation estimation by package type
  - Thermal resistance calculations
  - Nearby heat source clustering
  - Copper area and via effectiveness estimation
  - Temperature rise prediction
  - CLI: `kct analyze thermal <pcb>`

- **Signal Integrity Estimates** (`analysis/signal_integrity.py`)
  - Crosstalk risk detection between adjacent traces
  - Impedance discontinuity analysis (width changes, vias, layer transitions)
  - High-speed net identification (USB, LVDS, MIPI, HDMI, DDR, Ethernet)
  - Coupling coefficient calculation
  - Risk level classification (LOW → HIGH)
  - CLI: `kct analyze signal-integrity <pcb>`

#### Cross-Domain Validation (`kicad_tools.validate`)

Consistency checks across design artifacts:

- **Schematic↔PCB Consistency** (`validate/consistency.py`)
  - Component matching between schematic and PCB
  - Net consistency verification
  - Reference designator, value, and footprint sync
  - CLI: `kct validate --consistency`

- **Net Connectivity Validation** (`validate/connectivity.py`)
  - Unrouted net detection
  - Partial connection (island) detection
  - Isolated pad identification
  - Actionable fix suggestions
  - CLI: `kct validate --connectivity`, `kct net-status <pcb>`

- **BOM↔Placement Verification**
  - Component count verification
  - Placement status for all BOM items
  - Missing component detection

#### Cost Awareness (`kicad_tools.cost`)

Manufacturing cost visibility:

- **Manufacturing Cost Estimation** (`cost/estimator.py`)
  - PCB fabrication cost breakdown
  - Component and assembly costs
  - Quantity-based pricing tiers
  - Manufacturer-specific costs (JLCPCB, PCBWay, OSHPark, Seeed)
  - Surface finish and color adjustments
  - Layer and thickness premiums
  - CLI: `kct estimate cost <pcb>`

- **Part Availability Checking** (`cost/availability.py`)
  - LCSC stock level queries
  - Availability status (AVAILABLE, LOW_STOCK, OUT_OF_STOCK, DISCONTINUED)
  - Lead time reporting
  - Minimum order quantity handling
  - Price break calculations
  - CLI: `kct parts availability <schematic>`

- **Alternative Part Suggestions** (`cost/alternatives.py`)
  - Suggest replacements for unavailable parts
  - Price difference comparison
  - Pin-compatible alternatives
  - Basic part preferences for JLCPCB assembly
  - CLI: `kct suggest alternatives <schematic>`

#### CLI Commands

New commands for v0.7.0 features:

- `kct erc explain <file>` - ERC root cause analysis with fix suggestions
- `kct analyze congestion <pcb>` - Routing congestion hotspots
- `kct analyze trace-lengths <pcb>` - Timing-critical trace analysis
- `kct analyze thermal <pcb>` - Thermal hotspot detection
- `kct analyze signal-integrity <pcb>` - Crosstalk and impedance analysis
- `kct constraints check <pcb>` - Constraint conflict detection
- `kct validate --consistency` - Schematic↔PCB sync check
- `kct validate --connectivity` - Net connectivity validation
- `kct estimate cost <pcb>` - Manufacturing cost estimation
- `kct parts availability <schematic>` - LCSC stock checking
- `kct suggest alternatives <schematic>` - Alternative part suggestions

## [0.6.0] - 2026-01-03

### Added

#### Intelligent Placement Engine (`kicad_tools.optim`)

Comprehensive placement optimization for PCB component positioning:

- **Functional Clustering** (`optim/clustering.py`)
  - `ClusterDetector` - Detects related component groups
  - `detect_functional_clusters()` - Find MCU+bypass caps, timing circuits, etc.
  - `ClusterType` enum: POWER, TIMING, INTERFACE, ANALOG

- **Thermal-Aware Placement** (`optim/thermal.py`)
  - `ThermalClass` - Heat source, heat sensitive, neutral classification
  - `classify_thermal_properties()` - Auto-detect thermal components
  - `detect_thermal_constraints()` - Generate separation constraints
  - Heat sources pushed to edges, separated from sensitive components

- **Signal Integrity Hints** (`optim/signal_integrity.py`)
  - `SignalClass` - CLOCK, HIGH_SPEED, DIFFERENTIAL, ANALOG, POWER, GENERAL
  - `classify_nets()` - Auto-classify nets by name patterns
  - `analyze_placement_for_si()` - Get SI warnings
  - `get_si_score()` - Placement quality metric

- **Edge Placement** (`optim/edge_placement.py`)
  - `detect_edge_components()` - Find connectors, mounting holes
  - `EdgeConstraint` - Keep components at board edges
  - `BoardEdges` - Edge detection and constraint generation

- **Keep-out Zones** (`optim/keepout.py`)
  - `KeepoutZone` - Define no-go areas
  - `create_keepout_from_component()` - Auto-generate from components
  - `load_keepout_zones_from_yaml()` - Load from config file
  - `validate_keepout_violations()` - Check placement against zones

#### Placement Constraints (`kicad_tools.optim`)

Declarative constraint system for component placement:

- **Component Grouping** (`optim/constraints.py`)
  - `GroupingConstraint` - Keep components together
  - `SpatialConstraint` - Position constraints
  - `validate_grouping_constraints()` - Check constraint satisfaction

- **Alignment** (`optim/alignment.py`)
  - `snap_to_grid()` - Grid alignment
  - `align_components()` - Row/column alignment
  - `distribute_components()` - Even spacing
  - `AlignmentConstraint` - Declarative alignment rules

#### Agent Integration (`kicad_tools.optim`)

AI-friendly APIs for placement optimization:

- **Placement Suggestions** (`optim/suggestions.py`)
  - `PlacementSuggestion` - Suggested position with rationale
  - `generate_placement_suggestions()` - Get improvement ideas
  - `explain_placement()` - Why is component here?
  - `suggest_improvement()` - Specific move suggestions

- **Iterative Refinement** (`optim/session.py`, `optim/query.py`)
  - `PlacementSession` - Stateful refinement session
  - `query_position()` - "What if I move X here?"
  - `query_swap()` - "What if I swap X and Y?"
  - `find_best_position()` - Optimal position search
  - `process_json_request()` - JSON API for agents

#### CLI Commands

New placement optimization commands:

- `kicad-tools placement optimize --cluster` - Enable clustering
- `kicad-tools placement optimize --thermal` - Enable thermal awareness
- `kicad-tools placement optimize --edge-detect` - Edge component detection
- `kicad-tools placement optimize --keepout FILE` - Load keepout zones
- `kicad-tools placement suggest` - Get suggestions
- `kicad-tools placement refine` - Interactive refinement

## [0.5.0] - 2026-01-02

### Added

#### Circuit Blocks (`kicad_tools.schematic.blocks`)

Reusable, tested circuit blocks for common schematic patterns:

- **MCUBlock** - Microcontroller with configurable bypass capacitors
- **CrystalOscillator** - Crystal/oscillator with load capacitors
- **USBConnector** - USB-B/Mini/Micro/Type-C with optional ESD protection
- **DebugHeader** - SWD, JTAG, and Tag-Connect programming headers
- **I2CPullups** - I2C bus pull-up resistors with optional filtering capacitors
- **ResetButton** - Reset switch with debounce capacitor and optional ESD protection
- **BarrelJackInput** - DC barrel jack with reverse polarity protection
- **USBPowerInput** - USB power input with fuse and ESD protection
- **BatteryInput** - Battery connector with protection circuitry
- **LDOBlock** - Linear regulator with input/output capacitors
- **LEDIndicator** - Status LED with current-limiting resistor
- **DecouplingCaps** - Decoupling capacitor placement helper

All blocks feature:
- Ports for inter-block wiring
- Configurable component values
- Optional protection circuits
- Factory functions for common configurations

#### Schematic Enhancements

- **Auto-layout** (`schematic.layout`)
  - Automatic symbol placement to avoid overlaps
  - Configurable spacing and alignment options

- **Netlist Sync Validation** (`validate.netlist`)
  - Compare schematic netlist to PCB netlist
  - Detect missing/extra components and nets
  - CLI: `kct validate-sync schematic.kicad_sch pcb.kicad_pcb`

#### API Refinements

- **Unified Project Class** (`kicad_tools.Project`)
  - Load complete KiCad projects (`.kicad_pro`)
  - Cross-reference schematics to PCBs
  - Find unplaced components
  - Export manufacturing packages

- **Actionable Error Messages** (`kicad_tools.exceptions`)
  - All exceptions include `error_code` field
  - Structured `to_dict()` for JSON serialization
  - Fix suggestions included in error messages

- **Progress Callbacks** (`kicad_tools.progress`)
  - `ProgressCallback` protocol for monitoring long operations
  - `ProgressContext` context manager for scoped progress
  - `create_json_callback()` for automation
  - Cancelable operations via callback return value
  - Integration with router, DRC, and export operations

#### Examples & Documentation

- **End-to-End Example** (`examples/05-end-to-end/`)
  - Complete workflow from schematic to manufacturing files
  - Demonstrates circuit blocks, routing, and export

- **Agent Integration Examples** (`examples/agent-integration/`)
  - Claude integration with tool definitions and prompts
  - OpenAI integration with function calling
  - Common utilities for error handling and API wrapping

### Changed

- Improved exception hierarchy with structured error information
- Router operations now support progress callbacks
- DRC checker supports progress reporting

## [0.4.0] - 2025-12-31

### Added

#### Library Management

- **Symbol Library Tools** (`kicad_tools.library`)
  - Create and save KiCad symbol libraries programmatically
  - Symbol creation with pin and property editing
  - Round-trip editing preserves existing content

- **Footprint Library Tools** (`kicad_tools.library`)
  - `FootprintLibrary` class for loading `.pretty` directories
  - Footprint creation and save support
  - Parametric footprint generators for common package types:
    - SOIC (8, 14, 16 pins)
    - QFP (32, 44, 48, 64, 100 pins)
    - QFN (16, 24, 32, 48 pins)
    - DFN (6, 8, 10, 12 pins)
    - BGA (grid-based ball patterns)
    - Chip resistors/capacitors (0402, 0603, 0805, 1206)
    - SOT variants (SOT-23, SOT-223, SOT-89)
    - Through-hole (DIP, SIP, pin headers)
  - CLI: `kct footprint generate <type>` for parametric generation

#### Pure Python DRC (no kicad-cli required)

- **DRCChecker** (`kicad_tools.validate`) - standalone design rule checking
  - Clearance checks: trace-to-trace, trace-to-pad, pad-to-pad, via-to-trace
  - Dimension checks: trace width, via drill, annular ring
  - Edge clearance checks: copper-to-board-edge minimum
  - Silkscreen checks: line width, text height, over-pad detection
  - Manufacturer rule presets for JLCPCB, PCBWay, OSHPark, Seeed
  - Support for 2-layer, 4-layer, and 6-layer configurations
  - CLI: `kct check board.kicad_pcb --mfr jlcpcb`

#### Datasheet Tools

- **Datasheet Infrastructure** (`kicad_tools.datasheet`)
  - Datasheet search across multiple sources
  - PDF download with local caching
  - PDF to markdown conversion using MarkItDown
  - Image extraction from PDF datasheets
  - Pin table extraction from datasheets
  - Package dimension extraction and footprint matching
  - Symbol generation from datasheet pin tables
  - End-to-end part import workflow
  - CLI: `kct datasheet search/download/convert/extract-images/extract-tables`

#### CLI Enhancements

- **Netlist analysis commands**: `kct netlist analyze/list/show/check/compare/export`
- **Footprint generation**: `kct footprint generate --list` shows available types

### Changed

- Manufacturer rule presets expanded to support 2, 4, and 6-layer configurations
- DRC checking can now run without kicad-cli installed

### Fixed

- Test suite compatibility with pure Python DRC checker

## [0.3.0] - 2025-12-31

### Added

#### Documentation & Examples

- **Comprehensive API documentation** with type hints for mypy --strict compliance
- **User guide and tutorials** in `docs/` directory
- **Example projects** demonstrating common workflows:
  - Schematic analysis
  - BOM generation
  - DRC checking
  - Autorouting
  - LLM-driven routing

#### CLI Enhancements

- **Progress indicators** for long-running operations with real-time feedback
- **Configuration file support** (`.kicad-tools.yaml`) for CLI default options
- **Interactive REPL mode** (`kct repl`) for multi-step workflows
- **Interactive routing preview** for visualizing routing decisions

#### Router Enhancements

- **Differential pair routing** with length matching support
- **Bus routing** for grouped signal routing
- **45° diagonal routing** for shorter, cleaner traces
- **Zone-aware routing** with flood fill and thermal relief generation
- **Staircase pattern compression** - optimizes alternating horizontal/diagonal micro-segments into clean paths

#### LLM Integration

- **PCB Reasoning Agent** (`kicad_tools.reasoning`) for LLM-driven layout decisions
  - State representation suitable for LLM prompts
  - Command vocabulary for routing actions
  - Feedback/diagnosis for failed operations
  - CLI: `kct reason` with `--analyze`, `--export-state`, `--interactive` modes

#### File Format Support

- **`.kicad_mod`** footprint library files
- **`.kicad_dru`** design rules files
- **KiCad 7+** net class handling compatibility

#### Footprint Validation

- **Standard library comparison** - validate footprints against KiCad's official library
  - Auto-detects KiCad library path (macOS, Linux, Windows)
  - Compares pad positions, sizes, and shapes with configurable tolerance
  - CLI: `kct footprint compare-standard`

#### Testing & Quality

- **Edge case test coverage** for parser and schema modules
- **Integration tests** with real KiCad project files
- **Performance benchmarks** for large board handling

### Changed

- **S-expression parser optimized** for 50%+ performance improvement on large files
- **README updated** to highlight agent-focused development goal
- **Custom exception hierarchy** with context and actionable suggestions

### Fixed

- Pad obstacle clearance calculation for PTH routing
- All linting and formatting issues

### Removed

- Unused numpy dependency (router now uses pure Python)
- Hardcoded project paths from CLI modules
- Orphaned CLI modules consolidated into unified CLI
- Duplicate S-expression implementations

## [0.2.0] - 2025-12-30

### Added

#### Manufacturing Readiness (Planned Features)

- **LCSC Parts Integration** (`kicad_tools.parts`)
  - `LCSCClient` for direct part lookups from JLCPCB's LCSC database
  - Part search with filtering by stock, category, and specifications
  - Local caching for offline use and reduced API calls
  - CLI: `kct parts lookup`, `kct parts search`, `kct parts cache`

- **Assembly Package Export** (`kicad_tools.export`)
  - Complete manufacturing packages (Gerbers + BOM + CPL) for fabrication
  - Multi-manufacturer format support (JLCPCB, PCBWay, OSHPark, Seeed)
  - CLI: `kct export assembly`, `kct export gerbers`, `kct export pnp`

- **Fluent Query API** (`kicad_tools.query`)
  - Django-ORM style filtering for symbols and footprints
  - `sch.symbols.filter(value="100nF")`, `pcb.footprints.by_reference("U1")`
  - Chainable filters with field lookups

- **Project Class** (`kicad_tools.project`)
  - Cross-reference schematics to PCBs
  - Unified project-level queries for finding unplaced components

#### Bonus Features (Beyond Roadmap)

- **A* Autorouter with Obstacle Awareness** (`kicad_tools.router`)
  - Intelligent obstacle detection and avoidance
  - Net-class aware routing strategies (power, clock, audio, digital)
  - Multi-layer support with automatic via placement
  - Negotiated and greedy routing strategies
  - `CommandInterpreter` for high-level routing commands
  - CLI: `kct route`

- **Trace Optimizer** (`kicad_tools.optim`)
  - Post-routing trace optimization
  - Length matching support for differential pairs

- **Manufacturer DRC Configuration**
  - Configurable design rules per manufacturer
  - Rule comparison tools between manufacturers
  - CLI: `kct mfr compare`

- **Footprint Validation & Repair** (`kicad_tools.footprints`)
  - Detect pad spacing issues, overlaps, courtyard violations
  - Automatic repair with configurable minimum gaps
  - CLI: `kct validate-footprints`, `kct fix-footprints`

- **Placement Conflict Detection** (`kicad_tools.optim`)
  - Detect component overlaps and courtyard violations
  - Conflict resolution suggestions

### Changed

- CLI version now dynamically reads from package metadata instead of hardcoded value

### Fixed

- Test for `LCSCClient.close()` method now patches at correct module level

## [0.1.0] - 2025-12-29

### Added

- **Core S-expression parser** with round-trip editing support
- **Schematic parsing** - symbols, wires, labels, hierarchy traversal
- **PCB parsing** - footprints, nets, traces, vias, zones
- **Symbol library parsing** - read and query KiCad symbol libraries
- **ERC report parsing** - parse KiCad Electrical Rules Check reports
- **DRC report parsing** - parse KiCad Design Rules Check reports
- **Manufacturer profiles** - design rules for JLCPCB, OSHPark, PCBWay, Seeed
- **PCB autorouter** - A* pathfinding with pluggable heuristics
  - Net class awareness (power, clock, audio, digital)
  - Multi-layer support with via management
  - Congestion-aware routing
- **Unified CLI** (`kct` or `kicad-tools`) with subcommands:
  - `kct symbols` - list symbols in schematics
  - `kct nets` - trace and analyze nets
  - `kct bom` - generate bill of materials
  - `kct erc` - run/parse ERC reports
  - `kct drc` - run/parse DRC reports with manufacturer rules
- **PCB tools** - `kicad-pcb-query` and `kicad-pcb-modify`
- **Library tools** - `kicad-lib-symbols`
- JSON output for all CLI commands

### Dependencies

- Python 3.10+
- numpy >= 1.20

[0.20.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.20.0
[0.19.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.19.0
[0.18.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.18.0
[0.17.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.17.0
[0.16.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.16.0
[0.15.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.15.0
[0.14.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.14.0
[0.9.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.9.0
[0.8.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.8.0
[0.7.2]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.7.2
[0.7.1]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.7.1
[0.7.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.7.0
[0.6.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.6.0
[0.5.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.5.0
[0.4.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.4.0
[0.3.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.3.0
[0.2.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.2.0
[0.1.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.1.0

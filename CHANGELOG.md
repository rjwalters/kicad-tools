# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.23.0] - 2026-10-15

### Summary

A large release: 451 entries from about 1,600 commits since 0.22.0.

**Read the upgrade notes first.** There are 84 of them. `kct route` and `kct check` can now fail boards they used to pass:

- Pads left unconnected on a pour now fail `kct route`.
- Signal routing stays off declared plane layers by default.
- Netclass defaults below the manufacturer's floor are now flagged.
- The waiver evidence hash format changed, so existing evidence-bound waivers must be re-recorded once (`ev1:` → `ev2:`).

Most of the router work targets the lattice engine and the bar for a board to be manufacturable: every net routed, and 0 errors from both `kct check` and kicad-cli DRC.

- **Diff pairs and match groups on lattice:** skew and match-group length are tuned automatically, against the checker's own limits.
- **Pour links:** a KiCad-checked completion loop closes pads left unconnected on pours.
- **Inner power plane:** it now goes to the power net that feeds the most parts.
- **BGA keep-outs:** BGA power balls on the same net get keep-outs.
- **Pad geometry:** the clearance code now models roundrect pads exactly.

The demo boards 04, 05 and 06b now route to manufacturable on lattice. 06rf now routes every net and closes every pour link. Its last 3 errors, and those on 07rf, are diff-pair skew that is still being worked on.

**New commands and flags:**
- `kct ecosystem`, `kct agent-guide`, `kct skills install` and `kct mcp setup --client codex|opencode`;
- `kct board-view` overlays and `kct pcb diff`;
- `kct route --checkpoint` / `--resume`;
- `kct route --lint-gate`;
- `kct net-status --crossings`;
- evidence-bound waivers and `kct check --diff`;
- `kct bench external --suite pcbworld`.

Several experimental, default-off routing modes also ship: crossing-aware net order, sequential N+1 rip-up, a no-good cache, min-cost-flow escape corridors and bidirectional A*.

### Upgrade notes / behaviour changes

- **Stranded pour pads now fail `kct route` (exit 3, or 4 below `--min-completion`)** (Issue #5785). If pads remain unconnected on a GND/VCC pour after the oracle completion loop, the SUCCESS banner is replaced by a failure banner and the exit code is non-zero, matching `kicad-cli pcb drc` and `kct net-status`. To keep the old advisory behaviour pass `--allow-stranded-pour-pads` to `kct route` / `kct net-status`, or set `KCT_ALLOW_STRANDED_POUR_PADS=1`. `--oracle-rounds 0` (or `KCT_ORACLE_COMPLETION=0`) disables the loop and the new gate entirely.

- **`--preserve-existing` now routes strictly fewer nets than before**
  (Issue #5788). A pass over a board whose nets are all already connected is now
  a near no-op instead of a full re-route, and `Nets routed: N/M` counts only
  the nets that still had open connections. If you were relying on
  `--preserve-existing` to *rebuild* copper for nets that are already complete
  (for example to re-apply a new `--net-class-map` trace width to them), name
  them explicitly with `--nets NET[,NET...]` or bound the pass with `--region`
  -- both still re-route what they select. A one-line notice on stdout lists the
  nets that were left untouched.

- **`kct route` now hard-restricts signal routing off declared reference-plane
  layers by default** (Issue #5789). `--reserve-plane-layers` was previously
  off by default, so a board whose layer stack declares `PLANE` layers (e.g.
  `--layers 4`'s In1.Cu/In2.Cu GND/PWR references) could have ordinary signal
  routed straight across them -- measured on board 06 as 94 segments (22.8 mm)
  on In1.Cu, breaking the plane's continuous return path. The restriction is
  now the default whenever the resolved stack declares one or more `PLANE`
  layers; pass `--no-reserve-plane-layers` to opt back into the previous
  behavior (e.g. a recipe that intentionally relies on all copper layers for
  routing capacity). A fleet-wide before/after measurement (boards 00-07)
  found no other board regresses under the new default except
  `05-bldc-motor-controller`, whose recipe now passes the explicit opt-out
  (its historical routing capacity depends on all 4 copper layers,
  independently tracked by #3425/#3822/#4548).

- **MCP tool calls now require the `mcp` extra** (Issue #5804). `jsonschema`
  moved from the core dependencies to the `mcp` extra, and it is now imported
  lazily on the one path that uses it -- the `tools/call` argument validation in
  `kicad_tools/mcp/server.py`. On a core-only install `kct mcp serve` still
  starts and still answers `initialize` / `tools/list`, but every `tools/call`
  comes back as JSON-RPC error `-32603 No module named 'jsonschema'`;
  previously the `stdio` transport happened to work on a bare
  `pip install kicad-tools` (only `http` needed the extra, for `fastmcp`).
  Install `kicad-tools[mcp]` (or `kicad-tools[all]`) if you serve MCP -- which
  is what `kct mcp serve` already tells you to do for HTTP. No other command is
  affected: `kct route-auto`, `kct screenshot`, `kct report` and the
  manufacturing-bundle figures import `kicad_tools.mcp` submodules lazily at run
  time and keep working on a core-only install, now that the `jsonschema` import
  is out of module scope.

- **`kct readiness --hv-requirement` is removed** (Issue #5807). It could
  never prove an HV board's isolation was actually met, since it accepted any
  nonempty free-text description as a pass. Use the same structured flags
  `kct audit` already accepts: `--hv-standard {iec60664,iec62368}` with
  `--hv-working-voltage`/`--hv-pollution-degree` (optionally
  `--hv-material-group`), or a manual `--hv-min <mm>` override. `--hv-net-class`
  is unchanged.

- **LVS net names for locally-labelled nets gain their sheet prefix** (Issue
  #5809). `kct check --format json`'s `meta_checks.lvs.mismatches[].schematic_net`
  (and `lvs.json`'s equivalent field) now report `/SENSE` rather than `SENSE`
  for a net driven by a plain schematic label, matching KiCad. Consumers that
  match those strings exactly need to expect the sheet-qualified spelling;
  global-label and power-symbol nets (`GND`, `+3V3`) are unchanged. A design
  that labels *different* nets with the same bare name on two sheets, paired
  with a board that binds both to one unqualified net, can now newly fail LVS:
  those were two nets conflated into one identity before, and the board really
  does short them.

- **`kct check --copper 1` is now honoured** (Issue #5810). The unified
  parser used to drop an explicit `--copper 1` (it equalled the old hardcoded
  default), so a board declaring a heavier stackup was silently evaluated at
  the stackup weight. It is now forwarded like any other value: against a
  2 oz stackup it evaluates ampacity at 1 oz, emits the stackup-disagreement
  WARNING, and is fatal under `--strict`. Omit `--copper` to use the board
  stackup.

- **`kct check` can newly fail on boards whose silkscreen uses polygon
  markers** (Issue #5811). Polygon silk was previously invisible to the silk
  clearance checks, so a `fp_poly` / `gr_poly` pin-1 or polarity marker that
  sits over pad copper — or inside the manufacturer's silk-to-pad floor —
  produced no finding at all. Those boards now report the
  `silk_pad_clearance` **errors** (and `silk_over_copper` /
  `silk_overlap` / `silk_edge_clearance` warnings) that native KiCad DRC
  already reported for them. This is a previously-missing detection, not a
  new rule: no flag turns it off, and the fix is to move the marker (or waive
  the specific finding). `silk_pad_clearance` findings now carry the silk
  element's UUID in `items[0]` (`"U1 poly {…}"` rather than `"U1 poly"`), so
  a consumer that matches that string exactly — including a waiver file keyed
  on it — needs to expect the UUID suffix, which is what makes a per-marker
  waiver possible.

- **`engine.commit` can now be absent from a readiness report** (Issue #5812).
  It was previously always present whenever *any* git repository contained the
  installation — including a consumer repository that merely hosted kct's
  `.venv`, whose HEAD was wrong. Reports produced by a wheel install (no VCS
  pin, no owning checkout) now omit the field rather than populate it with an
  unrelated commit. `engine.commit` was already documented as optional and the
  readiness validator does not require it, so no configuration change is
  needed; but tooling that indexes reports by `engine.commit` should treat a
  missing value as "engine revision unknown" and fall back to
  `engine.source_digest`, which is still always recorded. Previously-written
  reports are unaffected. Re-running readiness from a VCS-pinned or editable
  install restores a correct `engine.commit`.

- **`kct check` can newly fail a board that previously passed, including an unrouted one** (Issue #5875). The new `netclass_floor` category raises fab-blocking errors from the `.kicad_pro` netclass declaration alone, so a project whose `net_settings.classes` sit below the active `--mfr` floor now exits non-zero even with no segments or vias on the board. Fix the netclass (raise `track_width` / `clearance` / `via_diameter` / `via_drill` to the profile's floor — `kct mfr apply-rules` writes conforming values), pass the `--mfr` tier your board is actually fabricated at, or skip the category with `--skip netclass_floor`. A board whose process legitimately permits a tighter netclass reports nothing, because the floors come from the same resolved `DesignRules` the rest of the check uses, fabrication overrides included.

- **`kct route` may now move components on a board whose routing plan is
  infeasible** (Issue #5890). Previously the classifier-driven placement-delta
  loop only ran under an explicit `--placement-delta-feedback`; it is now
  **on by default** when the Epic #5510 plan stage reports
  `overflow_report.feasible: false` **and** nets remain unrouted. Boards whose
  plan is feasible are unaffected and byte-identical.

  Every move is still bounded by `--placement-feedback-max-movement` (5.0 mm
  default), kept only on a strict routed-net increase with no clearance,
  pairwise-creepage or keepout regression, and recorded in
  `<output>_placement_delta.json` next to the routed board -- the saved PCB
  carries the moved placement, so a recipe that diffs its artifact will see it.

  To restore the previous behaviour on a given run, pass the new
  `--no-placement-delta-feedback`.

- **Routing behaviour change: the pad-access invariant is on by default**
  (Issue #5891). The grid engine now refuses a negotiated candidate route whose
  commit would strand another unrouted pad. On a board where that trade cannot
  be avoided, a net that previously routed may now be reported unrouted instead
  — the pad it would have stranded routes in its place. Two things to know:

  - Pass `--no-pad-access-invariant` (both `kct route` and `kct route-auto`) to
    restore the previous behaviour byte-for-byte; with the flag set the gate is
    never constructed and the commit path is identical to pre-#5891.
  - A test or recipe that *asserts* a pad gets stranded (for example to
    exercise stranding diagnostics) must set
    `Autorouter.enable_pad_access_invariant = False`, as
    `tests/router/test_access_witness_replay_5508.py` now does.

- **Diff-pair routing can now produce coupled pairs where it used to fall back
  to independent legs** (Issue #5895). The pose-centerline search is on by
  default, so `kct route --differential-pairs` output changes on boards whose
  pairs the joint-state search could not couple. Set `KCT_POSE_CENTERLINE=0` to
  get the previous routing back.

- **`kct route --order-method greedy|critical_first|congestion|hybrid` now exits 2 on escalation paths** (Issue #5908). Previously the flag was silently ignored there. If a script passes one of these methods together with the default `--auto-layers` (or `--adaptive-rules`, `--auto-pcb-size`, `--auto-mfr-tier`), either add `--layers N` or `--no-auto-layers` (and drop the other escalation flags) to route a single attempt that honours the order, switch to `--order-method crossing`, or drop `--order-method` to keep the routing you were actually getting.

- **A hung `kicad-cli` now costs up to 30 s on every lookup, not 5 s once**
  (Issue #5932). A timed-out probe is no longer cached: each call waits 10 s,
  retries once at 20 s, and is probed again on the next call. On a host where
  `kicad-cli` never answers (#5877), fix the host or set
  `KCT_KICAD_CLI_PROBE_TIMEOUT` to a small value. Expect a stderr `WARNING:`
  wherever a zone fill or refill is skipped.

- **`kct validate --sync` can now fail on pad-net drift** (Issue #5937). Boards
  whose pads disagree with the schematic's connectivity now get `net_mismatch`
  errors, `in_sync: false` and exit 1 where they previously passed; this also
  affects `Project.check_sync()` and the sync note in `kct build`'s verify step.
  Fix by updating the PCB from the schematic (`kct pcb sync-netlist`). The
  committed fleet boards (00-07, 09) produce no new findings. With `--strict`, a
  schematic that wires none of its pins now exits 2 on the new "pad-net check
  skipped" warning.

- **Multi-net `kct route-auto --nets ... -o` now rolls back regressing net
  passes by default** (Issue #5945). A net pass that lowers the number of
  complete nets on the board is undone, the net is reported as failed, and the
  run exits 1 where it used to exit 0. The same applies to any
  `--checkpoint`/`--resume` run. Recognise it by `"success": false,
  "rolled_back": true` on the net's `--format json` entry, or the
  `rollback: ... restored pass N` line on stderr. Pass `--no-rollback` to keep
  such passes (the previous behaviour). The `-o` file is now also seeded with
  the input board (or the `--resume` checkpoint) before the first net routes,
  so it is always written, even when no net adds copper.

- **`kct readiness` (and `/kct:tapeout`) now refuse sign-off on unknown
  coverage or stale waivers** (Issue #5946). The `kct_check` gate reports NOT
  RUN when `kct check` marks any blocking check `unknown`. Two common causes:
  copper zones with no committed fill (refill the zones), and diff-pair-named
  nets with no `--net-class-map` (supply the sidecar). The gate FAILS when an
  evidence-bound waiver is stale; re-review the finding and re-waive it with
  `kct check --waive` only if it is still intentional. Writing a keyed waiver
  upgrades the waivers file to schema version 3, which builds older than
  this release reject (they report the waivers file as malformed). The
  `kct check` exit code is unchanged.

- **`kct validate --sync` can now fail on swapped net names** (Issue #5980).
  A PCB whose nets carry another schematic net's name, for example a
  `GND`/`+3.3V` or `SWDIO`/`SWCLK` swap with every pad moved, now gets a
  `net_mismatch` error, `in_sync: false` and exit 1 where it previously
  passed. The same applies to `Project.check_sync()` and the sync note in
  `kct build`. It is an error, not a warning, because net classes, diff pairs
  and zones apply to the wrong copper. Fix by exchanging the PCB net names, or
  by updating the PCB from the schematic (`kct pcb sync-netlist`). The
  committed fleet boards (00-07, 09) produce no new findings: all 358 of
  their paired nets keep their own names.

- **`kct route-auto` can now fail nets it used to report as routed** (Issue
  #6001). If a net's only route crosses another net's existing copper, the
  net now fails with `shorts net(s) '...' already on the board` and exits 1.
  Before, route-auto wrote the short and exited 0. Route such nets with
  `kct route`, which routes around existing copper. The new `unrouted` entry
  names the net in the way.

- **Schematic checks can report different results on mirrored symbols**
  (Issue #6005). Checks that compare pin positions against wires, labels or
  no-connect flags now see the corrected pins on symbols with `(mirror x)` or
  `(mirror y)`. These include the model's `validate()`, `extract_netlist()`,
  the `sch_wire_stub` and `sch_orphan_label` validators, and `kct sch
  check-connections` / `pin-positions` / `tidy`. Findings that came from the
  old mirror-twin positions go away, and real problems they hid can now
  appear. Symbols without a mirror are unaffected. No flag restores the old
  positions, because they did not match KiCad.

- **Grid routes on boards with keepout rule areas change** (Issue #6008).
  Boards whose routes previously crossed a `tracks not_allowed` or
  `vias not_allowed` rule area will now detour around it, change layers, or
  leave the connection unrouted when the area is the only path. Pour-void-only
  rule areas (`kct zones hv-keepout` output) are unaffected. The native
  router's build version moves to 47: run `kct build-native` after upgrading.

- **Re-waive evidence-bound `kct check` waivers once after upgrading**
  (Issue #6011). Evidence hashes moved from `ev1:` to `ev2:`, so every keyed
  waiver recorded with an `ev1:` hash goes stale once, even on an unchanged
  board, and its finding is active again. These are reported as
  `STALE waiver (outdated evidence version)`, and the finding's JSON carries
  `"stale_waiver_cause": "outdated_evidence_version"`. Review each one and
  re-run `kct check --waive KEY --waive-reason ... --waive-reviewer ...` to
  record the `ev2:` hash.

- **Silkscreen waivers that name a line as `fp_line` must be re-waived**
  (Issue #6015). `silkscreen_line_width` (since #5946) and now
  `silk_edge_clearance` name each offending silk line by its own
  footprint-local geometry, e.g. `fp_line@-2.11/-2.11~-1.635/-2.11` or
  `J1 (fp_line@-1.62/6~-1.62/8.73)`, instead of the bare `fp_line` /
  `J1 (fp_line)`. A waiver written against the old name matches nothing.
  This applies to a legacy `rule`/`items` entry such as
  `{"rule": "silkscreen_line_width", "items": ["J1", "fp_line"]}` and to a
  keyed entry whose key contains `(fp_line)`. The waiver is reported as
  `waiver_unused`, the silkscreen warning it covered comes back, and
  `kct check --strict` fails. To fix it, run `kct check BOARD --format json`
  to get the new per-line `key`s. Re-review each finding, then run
  `kct check BOARD --waive KEY --waive-reason ... --waive-reviewer ...`
  for each one you still accept, and delete the old entry. The
  `waiver_unused` advisory for such an entry now lists the matching
  per-line keys.

- **`kct optimize-placement` places boards with decoupling caps differently**
  (Issue #6020). The decoupling-affinity term and snap pass are on by
  default, and rotated parts are now scored at their true orientation, so
  a re-run on an unchanged board can land caps (and rotated parts)
  elsewhere. Pass `--weights '{"decoupling": 0}'` to turn the new term and
  snap pass off; the rotation and board-side fixes have no switch.

- **`kct optim fom-debug` scores move with the new default weights** (Issue #6021).
  Soft sums and composite scores shift by up to five orders of magnitude,
  most on small boards. On board 01, for example, the score goes from
  0.17 to 5.9e-4. Re-baseline any stored `fom-debug` numbers. To reproduce
  the old scores, pass `--weights` a YAML file with the old values:
  `net_congestion_variance: 0.2226`, `trace_length_excess: 1.6126`,
  `compactness: 0.0968`, and the seven other weights unchanged.

- **Boards with keepout rule areas can newly fail native DRC** (Issue #6039).
  After the next `kct route` / `kct check --emit-drc-constraints` / export
  re-writes the `.kicad_dru`, `kicad-cli pcb drc` reports
  `items_not_allowed` for any track, via, pad, footprint or filled pour
  copper inside a rule area that forbids it. Previously these were silently
  passed. A pour fill that KiCad carved around a `copperpour not_allowed`
  area is not flagged. Boards without rule areas get a byte-identical
  `.kicad_dru`; boards with them get a new `# BEGIN kct keepout rules` block,
  which changes the sidecar's hash in readiness manifests.

- **`kct route-auto` output can change on boards with track/via keepout rule
  areas** (Issue #6059). A net whose old route crossed a keepout now detours
  through `hierarchical` or fails. A forced corridor strategy
  (`--strategy global|escape|subgrid|via_resolution`) now exits non-zero
  instead of writing that copper. Use `--strategy hierarchical` or `auto` on
  such boards. Boards with no `(tracks not_allowed)` / `(vias not_allowed)`
  rule area are unaffected; pour-void-only areas (`kct zones hv-keepout`)
  still never constrain routing.
- **`kct route-auto --strategy hierarchical` stays within the board's copper
  layers** (Issue #6059). A 2-layer board's net that only converged by
  escalating to a 4- or 6-layer stack used to be written with `In*.Cu` tracks
  the board does not have. It is now reported as not converged.

- **Board-recipe gates and Gerber export can now fail on a split saved zone
  fill** (Issue #6078). The pipeline gate's DRC leg also fails, and
  `GerberExporter` raises `ExportError` (`kicad-export-gerbers` exits 1), when
  `kicad-cli pcb drc` on the fill saved in the board finds more unconnected
  items, `isolated_copper` or `copper_sliver` than the same board refilled.
  Fix it by re-running `kct zones fill` on the board. To export a deliberately
  fixed fill, set `GerberConfig(verify_zone_fill=False)`; to skip the gate leg,
  pass `evaluate_pipeline_gate(..., check_saved_fill_leg=False)`. When
  kicad-cli DRC cannot run, export still proceeds with a warning; under
  `require_drc=True` the gate fails.
- **`kct route` pour copper can differ from earlier runs** (Issue #6078).
  Zones are now filled under the routed board's emitted `.kicad_pro` and
  `.kicad_dru` rather than the source project. The KiCad-oracle completion
  loop now also compares the source project against those emitted rules, as
  its documentation describes. Re-routing an unchanged board can therefore
  produce different fill polygons and pour-completion copper. Regenerate
  committed routed boards before diffing them.

- **Boards with footprint-embedded keepouts can newly fail native DRC or
  route differently** (Issue #6087). After the next `kct route` /
  `kct check --emit-drc-constraints` / export rewrites the `.kicad_dru`,
  `kicad-cli pcb drc` reports `items_not_allowed` for tracks, vias, pads,
  footprints or pour copper inside a keepout defined in a footprint (e.g.
  an antenna keepout). The owning footprint's own items are exempt. The
  router now also avoids these areas. No board in the repository fleet has
  footprint-embedded zones, so fleet DRC results are unchanged.

- **Waivers that name a track or via by UUID must be re-waived** (Issue
  #6088). Copper findings now name tracks, arcs and vias by geometry
  (`Trace@F.Cu:w0.25:24.2/15~24.7/15.5`, `Via@25.2/14.9:F.Cu-B.Cu:d0.3/s0.6`)
  instead of `Trace-1a2b3c4d` / `Via-1a2b3c4d`, and `width_consistency` names
  a run as `Run@...` instead of listing track UUIDs. A waiver written against
  the old names matches nothing. That covers a keyed entry whose key contains
  `Trace-...` / `Via-...` and a legacy `rule`/`items` entry such as
  `{"rule": "via_under_body", "items": ["Via-1a2b3c4d", "U3"]}`. The waiver is
  reported as `waiver_unused`, the finding it covered comes back, and
  `kct check` (or `--strict`, for warnings) fails. The `waiver_unused`
  advisory lists the current keys of the same rule, nets, layer and item kinds
  to re-waive. Re-review each one, run
  `kct check BOARD --waive KEY --waive-reason ... --waive-reviewer ...` for each
  you still accept, and delete the old entry. The key format and the evidence
  hash version (`ev2`) are unchanged, so no other waiver is affected. SARIF
  alerts for copper findings get new `kctFindingKey/v1` fingerprints once, so
  code scanning closes the old alerts and opens new ones.

- **`physical_copper_gap` waivers that name a track, arc or via by UUID must be
  re-waived** (Issue #6106). Findings now name that copper by geometry
  (`Trace@F.Cu:w0.2:0/0~4/0`, `Via@3/0:F.Cu-B.Cu:d0.3/s0.6`) instead of its
  UUID, so a waiver written against the old names (a keyed entry whose key
  lists UUIDs, or a `rule`/`items` entry such as
  `{"rule": "physical_copper_gap", "items": ["<uuid>", "<uuid>"]}`) matches
  nothing. It is reported as `waiver_unused`, the finding it covered comes
  back, and `kct check` fails. The `waiver_unused` advisory lists the current
  keys of the same rule, nets and layer to re-waive. Re-review each, run
  `kct check BOARD --waive KEY --waive-reason ... --waive-reviewer ...` for
  each you still accept, and delete the old entry. Only boards that opt in to
  `physical_copper_gap` are affected.

- **`kct route-auto` refuses copper closer than the board's clearance to
  another net** (Issue #6107). The clearance is the strictest unconditional
  value in the board's `.kicad_dru` and `.kicad_pro` (board minimum and
  `Default` netclass). A board that declares none is checked at KiCad's
  default of 0.2 mm, which is what `kicad-cli pcb drc` applies. A corridor
  strategy (`global`, `escape`, ...) whose copper touches or crowds another
  net's pad, track, arc or via now fails that net and exits 1 instead of
  writing it. Under `--strategy auto` such a net is retried with
  `hierarchical`, which routes around other nets' copper. Net-0 tracks,
  vias, arcs and zones are not checked (KiCad reassigns floating copper on
  load, and a zone refill clears around new copper). Net-0 pads, such as
  no-connect pins and plated mounting holes, are checked; bare NPTH holes
  with no copper are not.

- **`kct route-auto` refuses more routes** (Issues #6139, #6122). Copper
  within the board's hole clearance of a drilled hole (0.25 mm by default), or
  within its edge clearance of an NPTH slot (0.5 mm by default), is now
  refused and not written. So is copper closer to a net in a larger netclass,
  such as `HV`, than that class requires. Such a net now fails and exits 1
  instead of producing a board that KiCad's DRC rejects. Under
  `--strategy auto` it is retried with `hierarchical`. A `.kicad_dru` rule
  whose condition the gate cannot evaluate (such as `insideArea()`) is
  assumed to apply wherever it might, at the larger of its value and the
  default, so on boards with such rules the gate may also refuse copper that
  KiCad's DRC would accept. Boards with an unconditional `.kicad_dru`
  clearance below their `Default` netclass are now gated at the rule's value,
  as KiCad's own DRC is.

- **`kct panel` V-score lines moved from Edge.Cuts to `Cmts.User`** (Issue #6143).
  An open score line on Edge.Cuts is a malformed outline, so `--cut vcut`
  now draws its score lines on the `Cmts.User` documentation layer (added to
  the panel's layer table when the source lacks it); give your fab that layer
  as the V-score drawing. `Panel.make_vcuts()` gains a `layer=` argument and
  `VCutConfig.layer` defaults to `"Cmts.User"`. Frameless panels also change
  shape: instead of one rectangle around every copy, the outline follows the
  copies and tabs, so the gaps between tabs are routed out.

- **`kct route-auto` refuses vias drilled too close to another hole**
  (Issue #6150). A new via's drill closer than the board's `min_hole_to_hole`
  (0.25 mm by default) to any other drilled hole is now refused. This includes
  holes of the routed net itself and the route's other new vias. So is copper
  that breaks a `.kicad_dru` `physical_clearance` or `physical_hole_clearance`
  rule. KiCad also applies the two physical rules between items of the same
  net; the gate does not. A board whose `.kicad_dru` kicad-cli cannot parse
  now has its custom rules ignored by the gate, with a warning, as kicad-cli
  ignores them.

- **`kct route` without `--manufacturer` may now route under a different fab
  tier** (Issue #6155). If the board's `project.kct` declares a `target_fab`
  other than `jlcpcb` (for example `jlcpcb-tier1`), an unflagged `kct route`
  now uses that tier's clearances, trace widths, via and drill limits instead
  of base `jlcpcb`. The `fab_profile.json` it writes changes to match, and so
  does the tier a later bare `kct check` judges against. To keep the old
  behaviour, pass `--manufacturer jlcpcb`. In this repo, boards 03, 04 and
  05/legacy_drv8301 already pass `--manufacturer jlcpcb-tier1`, so their
  outputs do not change. Board 06's `assembled-demo/build.py` now pins
  `--manufacturer jlcpcb` so its outputs do not change either.
  `kct route-auto` has no `--manufacturer` flag and is unchanged.

- **`GerberManufacturerPreset` no longer accepts `layer_rename`** (Issue
  #6163). The field never had any effect. Drop the argument when you build a
  custom preset. Exported Gerber file names do not change.

- **V-cut panel geometry changed** (Issue #6164). `kct panel --cut vcut`
  (and `PanelConfig(cut_method=CutMethod.VCUT)`) now defaults to 0 mm
  spacing and, with `--frame`, 0 mm frame space. The panel is smaller,
  has no tabs, and its score lines move from gap midpoints onto the board
  edges. Pass `--spacing 2` to keep a gap; such seams are now tab-routed
  with mousebites and are not V-scored. `Panel.make_vcuts()` no longer
  scores gapped seams. `PanelConfig.spacing` and `FrameConfig.space`
  default to `None` (resolved per cut method), and `kct panel --format
  json` adds `spacing_x_mm`, `spacing_y_mm`, `frame_space_mm`,
  `vscore_clearance_mm` and `warnings`.

- **JLCPCB, PCBWay and Seeed Gerber packages now carry separate
  `<board>-PTH.drl` and `<board>-NPTH.drl`** instead of one merged
  `<board>.drl` (Issue #6167). These presets always declared separate drill
  files; kicad-cli's merged default shipped only because the flag was
  dropped. Separate plated/non-plated files are what each fab's KiCad export
  guide shows (KiCad's "PTH and NPTH in single file" left unchecked), and
  they stop a fab from plating mounting holes. The OSH Park preset keeps one
  merged `<board>.drl`, as OSH Park asks, and now exports instead of
  failing. Anything that looked for `<board>.drl` by name in a JLCPCB, PCBWay
  or Seeed package should glob `*.drl`; pass
  `GerberConfig(merge_pth_npth=True)` to get the old single file.

- **Unflagged `export`, `fix-vias`, `fix-silkscreen`, `fix-drc`, `audit`, `pipeline`,
  `reason` and `estimate cost` may now use a different fab tier** (Issue #6169). If the
  board's `project.kct` declares a `target_fab` other than `jlcpcb` (for example
  `jlcpcb-tier1`), these commands now use that tier's rules instead of base `jlcpcb`, so
  they agree with `kct check` and `kct route`. To keep the old behaviour pass
  `--mfr jlcpcb`. In this repo every committed invocation on a tier1-declared board
  already passes `--mfr jlcpcb` except `boards/05-bldc-motor-controller/legacy_drv8301`'s
  `fix-drc`, which now pins it, so no committed output changes. Board 06 (declares tier1,
  builds at jlcpcb) is unaffected because its export is pinned; its tier is left to #6172.

- **`kct panel --cut vcut` clearance now follows the resolved fab** (Issue
  #6177). Boards resolved to `oshpark` or `seeed` now get a 0.5 mm V-score
  clearance (was 0.4 mm), and `flashpcb` gets 1.0 mm. Those boards may see
  new copper-near-score warnings and a wider pour keepout. Pass
  `--vscore-clearance 0.4` to keep the old value.

- **Board minima stricter than the fab floor are no longer relaxed**
  (Issue #6191). If a board's `.kicad_pro` declares a clearance, track width
  or via size above the manufacturer profile and does not set
  `KCT_PRESERVE_BOARD_RULES`, the next `kct route`, `kct check --emit-dru`,
  `kct mfr apply-rules` or manufacturing export keeps that stricter value
  instead of replacing it with the profile's. DRC, zone fill and routing can
  change as a result. The `.kicad_dru` now carries the stricter clearance
  floor, plus a `Reviewed clearance - <class>` rule for each netclass whose
  clearance is above it. Set the project text variable
  `KCT_PRESERVE_BOARD_RULES=0` (or `false`, `no`, `off`) to get the old
  behaviour back. A `Default` netclass left at a template value is still
  relaxed to the profile: kct's own `(0.15, 0.25, 0.6, 0.3)` and pre-#5654
  `(0.2, 0.25, 0.6, 0.3)`, KiCad 5/6 stock `(0.2, 0.25, 0.8, 0.4)`, and
  KiCad 7+ stock `(0.2, 0.2, 0.6, 0.3)`, given as clearance, track width,
  via diameter and via drill in mm.
  This is a ratchet: values an earlier kct pass wrote into the project count
  as authored, so neither a looser fab tier nor a `fabrication_overrides.json`
  that lowers a floor can loosen them on a later pass. Set
  `KCT_PRESERVE_BOARD_RULES=0` (or pass `preserve_board_rules="0"` from a
  script) for that pass. A generator that writes its own `.kicad_pro` minima
  must write the values its copper actually meets, because those minima are
  now enforced.

- `pip install kicad-tools` now also installs `rtree` (about 0.5 MB wheel,
  libspatialindex bundled). No action needed unless you install with
  `--no-deps` or on a platform without an rtree wheel.

- Routing with placement-excluded copper now reports a zone-fill failure (instead of empty multilayer pours) when the only KiCad found is older than 10.0.2 and the board has multilayer zones. Upgrade KiCad to 10.0.2 or newer.

- **`kct route --preserve-existing` can newly fail on boards whose input copper
  already shorts a pad** (Issue #6229). A kept trace or via overlapping another
  net's pad now fails the run like a routing short (exit 3, or 4 below
  `--min-completion`) instead of printing SUCCESS, because the written board is
  unmanufacturable whoever drew the copper. The report labels these shorts
  "pre-existing short in input board"; fix or remove that copper in the input
  board. Kept-copper near-misses (a positive but sub-clearance gap) are not
  reported, only physical overlap.

- **`kct route --preserve-existing` can newly fail on input copper that
  violates clearance or shorts other kept copper** (Issue #6237). Kept copper
  of different nets that overlaps now fails the run like any short (exit 3, or
  4 below `--min-completion`). Kept copper closer than the required clearance
  to another net's pad or kept copper fails the same way when the routed
  board's project gives KiCad's `clearance` rule `error` severity, which is
  KiCad's default. These are labelled "pre-existing ... in input board": fix
  that copper in the input board. To accept them as KiCad would, set
  `board.design_settings.rule_severities.clearance` to `"warning"` (reported,
  run passes) or `"ignore"` (not reported) in the input board's `.kicad_pro`;
  `kct route` carries it to the routed board's project. Shorts always fail,
  whatever `shorting_items` is set to.

- **Routes can now fail or detour on boards whose `.kicad_pro` declares a netclass stricter than the base clearance** (Issue #6243). Those classes are now mandatory, so a net that only fit below its class (and passed DRC because the generated `.kicad_dru` used to hide the class) is now routed around the gap or left unrouted with an "authored netclass minimum" message. A project with a named class this resolver cannot model now stops the route with `ProjectClearanceError` instead of routing below the class; fix the netclass declaration (unsupported pattern, unknown class, or `net_settings` schema outside 3-5). Rebuild the native router with `kct build-native` (`BUILD_VERSION` 48). Projects with only a `Default` class are unaffected.

- **`kct check` can newly fail on boards with named netclasses** (Issue #6249). Copper closer than a
  project netclass clearance (stricter than the fab floor) is now an error in the
  `netclass_clearance_copper` category, and a netclass declaration the resolver cannot model is a
  `netclass_clearance_unsupported` error. Re-route the board (`kct route` enforces the class since
  #6243), fix the gap, or lower the class in `.kicad_pro`. Skip with `--skip netclass_clearance_copper`.

- **`kct optimize-placement` keeps `locked` footprints where they are** (Issue #6262).
  If you relied on the optimizer moving a locked part, unlock it first
  (`kct pcb unlock-footprints`).

- **`kct route --timeout N` routes for less than N seconds, and can exit 10**
  (Issue #6273). `--timeout` is still the hard total, but the routing loops now
  stop at `N - max(60 s, 15% of N)` (at most 25% of N), so optimize / save /
  zone fill / DRC can finish inside N. The `--auto-fix` reserve is added on
  top, and the two together are capped at half of N. A run whose routing
  already finished early is unaffected. A recipe that relied on routing using
  all of N should raise `--timeout` by the reserve, or set `--search-timeout`
  for its routing stage. When the time left still cannot cover the post-route
  DRC, or the pour-net stranded-pad check (#5785) before its first KiCad run,
  that check is skipped and the board is saved **unverified**: the banner reads
  `UNVERIFIED: ...` (never "SUCCESS" or "DRC passed"), `--format json` reports
  `summary.verdict: "unverified"`, and the run exits **10** instead of 0. A
  run with a known failure keeps its own exit code. Scripts that treat any
  non-zero exit as failure need no change; to accept an unverified board, run
  `kct check` on it.

- **Routes change on boards whose own clearance rule is below 0.20 mm**
  (Issue #6280). `kct route` now uses the board's declared clearance (from the
  `.kicad_pro`, `.kicad_dru` or netclass, floored at the manufacturer minimum)
  for vias as well as traces. On boards that declare less than 0.20 mm, such
  as the 0.15 mm netclass `kct` writes by default, traces may pass closer to
  foreign vias, so via counts and copper length change on re-route. The
  copper still meets the board's own rules. A recipe that depends on exact
  route geometry (for example a reviewed fix-up at fixed via coordinates) can
  keep the old spacing with `--via-clearance 0.2`. Board 04's recipe does this.

- **`--strategy monte-carlo` / `evolutionary` trials run the negotiated router
  by default** (Issue #6345). Each trial now costs about one default
  `kct route` run instead of one A* pass, so expect roughly
  `ceil(trials / cores)` times a default route's wall time (still bounded by
  `--timeout`, which is split evenly across trials). Pass
  `--trial-router basic` for the old single-pass trials.
  Note the evolutionary strategy runs `pop-size x generations` trials (default
  20 x 10 = 200 negotiated routes) and `--timeout` has no default, so an
  unbounded evolutionary run on a large board can take hours.

- **Lattice routes on 4-layer boards may change** (Issue #6350). With the
  default `--layers 4`, `--route-engine lattice` no longer routes signals on
  the In1.Cu/In2.Cu reference planes, so a board that previously completed by
  using the planes may now leave nets unrouted. To restore the old behaviour
  pass `--no-reserve-plane-layers` or `--layers 4-all`.

- **Mesh routes on 4-layer boards may change** (Issue #6360). With the
  default `--layers 4`, `--route-engine mesh` no longer routes signals on the
  In1.Cu/In2.Cu reference planes, and it now treats hard net-class
  `avoid_layers` as forbidden. A board that previously completed by routing on
  those layers may now leave nets unrouted or use F.Cu to B.Cu through-vias
  instead. To restore the old behaviour pass `--no-reserve-plane-layers` or
  `--layers 4-all`.

- **`--early-stop-patience 0` no longer stops on overflow stagnation**
  (Issue #6361). Runs that pass `--early-stop-patience 0` (or call
  `route_all_negotiated(best_stall_patience=None)`) previously still ended
  when `should_terminate_early` saw no overflow progress; they now keep
  iterating until `--iterations` or `--timeout`, so they can take much
  longer. Drop the flag (default patience 2) to restore early stopping.

- **`kct route` exits 6 when the written board leaves open a net the router
  counted as connected** (Issue #6371). Previously exit 6 for post-save
  connectivity failures required `--strict`, and the run could still print
  `SUCCESS`. Now, when post-save verification finds a net open that the
  router counted as connected, the run prints
  `ROUTING FAILED`, reports `Nets connected` from the verified count, and
  exits 6 without `--strict`. `--min-completion` does not cover it. Nets
  the router already reported as partial keep their existing threshold
  exit codes. Scripts that treat any non-zero exit as failure need no
  change.
  `Nets routed` (and `get_statistics()["nets_routed"]`) can now be lower
  than an earlier run reported for the same copper. The difference is nets
  joined only by a nearby endpoint or by a layer change without a via, and
  those nets were open.

- **Lattice diff pairs that cannot couple now get uncoupled copper instead of
  none** (Issue #6373). Under `--route-engine lattice`, a declined coupled
  pair used to emit nothing and leave both nets open. It now emits two
  ordinary single-ended traces, which is how the grid engine's coupled
  pre-pass already falls back. Those traces do not guarantee the intra-pair
  gap or length matching. If the pair needs controlled impedance, look for
  `uncoupled-fallback:` in the route log and fix the cause (usually a part
  sitting in the pair's path) rather than shipping the board. The lattice
  `Lattice negotiation: X/Y connections` totals count such a pair as two
  connections.

- **The default `kct route` flow can now exit 6** (Issue #6376). The #6371
  post-save connectivity exit code now applies to the default auto-layers
  flow, `--adaptive-rules` and `--auto-layers --adaptive-rules`, not only
  `--no-auto-layers`. A run whose written board leaves open a net the router
  counted as connected now exits 6 where it used to exit 0 or 2, and
  `--strict` with any disconnected pad in the written file exits 6 in every
  flow. Under `--auto-mfr-tier` an inner exit 6 moves on to the next tier,
  like any other failed tier. Scripts that treat any non-zero exit as failure
  need no change.

- **New lattice diff-pair outcome `partially-coupled:<k>/<n>`** (Issue #6378).
  `kct route --route-engine lattice` can now report a diff pair as
  `partially-coupled:1/2`, followed by one line per segment such as
  `USB_D[1] U2.4->R4.1: coupled`, and a `WARNING: diff pair ... only
  partially coupled` line. Log scrapers that only matched `coupled` and
  `uncoupled-fallback:` need to handle the new outcome.

- **Routed boards can newly fail net-class clearance in both DRC engines** (Issue #6390).
  After re-routing, or after re-emitting the sidecars, copper that sits
  closer than its router class allows is now reported: `kct check` reports
  `netclass_clearance_copper` and `kicad-cli pcb drc` reports `clearance`.
  Zone fills saved under the old fab-floor-only rules can also violate the
  new class clearances under `kicad-cli pcb drc` without `--refill-zones`
  until you refill them. Refill the zones (for example
  `kicad-cli pcb drc --refill-zones --save-board`) and re-run both checks. Fix
  any remaining findings in routing; do not loosen the class. Three demo
  boards are affected: 03 (13 findings, #6394), 05 (27, #6395) and 07 (42,
  #6396). A fresh route of board 02 also produces one Power-class clearance
  finding (#6427), although its committed copper is clean. Boards 03 and 05 carry explicit, issue-cited
  `.github/routed-drc-tolerance.yml` entries until they are re-routed. Board
  07's active release takes no allowance, so its strict gate fails on the
  next change to that PCB. The demo boards' saved fills and release evidence
  are refreshed separately (#6404). Use `kicad-cli pcb drc --all-track-errors`
  when you compare counts, because the default reports one error per track,
  and which one it reports varies between runs.
- **Floor edits in a renamed output's sidecars are now overwritten, not
  reported** (Issue #6390). When `kct route` writes to a renamed output, it
  now regenerates that output's `.kicad_pro` from the source if the only
  edits since the last kct run are to kct's own floor keys. Those keys are:
  - the `rules.min_*` keys;
  - the `defaults` keys (`track_min_width`, `clearance_min`,
    `via_min_diameter`, `via_min_drill`);
  - the two non-blocking rule severities (`lib_footprint_mismatch`,
    `isolated_copper`);
  - `Default`'s clearance, track and via sizes.

  Previously such an edit raised `DRC sidecar conflict`. Now it is silently
  replaced from the source, even an edit that tightened a floor. Make those
  changes in the source project instead. Hand edits anywhere else still
  conflict.

- **`kct route` reserves pour-pad via sites by default** (Issue #6401). Routed
  copper on boards with auto-skipped pour nets can change, because signal
  routes now avoid the reserved sites. Pass `--no-plane-via-reservation` to
  restore the previous behaviour.

- **Re-run `kct mcp setup --client claude-code`** (Issue #6418). If you ran it
  with an earlier kct, the `kicad-tools` server went to `~/.claude/mcp.json`,
  which Claude Code never reads. Re-running registers it in `~/.claude.json`;
  check with `claude mcp list`. The old `~/.claude/mcp.json` is left untouched
  (setup prints a note while it still has a `kicad-tools` entry) and can be
  deleted. Setup now exits 1 instead of overwriting a `~/.claude.json` or Claude
  Desktop config that is not valid JSON.

- **Routes can change on boards whose nets get a stricter router class**
  (Issue #6427). Foreign copper now keeps the larger of both nets' class
  clearances (e.g. 0.2 mm from `Power` nets) during search and optimization,
  not only in the final DRC. A net that cannot meet it is reported unrouted
  instead of shipping with a clearance violation. Re-route and re-review any
  board-local fixups that depend on exact via or track coordinates.

- **`kct fix-drc` can now exit 2 where it used to exit 0** (Issue #6431).
  Exit 0 now requires a post-repair re-check (kct check's rule set, plus
  kicad-cli when it produced the violations) to find no remaining targeted
  error. Previously the exit code was computed from the repairer's own
  count, which could report success while both DRC engines still failed.
  Scripts that treated exit 0 as "manufacturable" are now correct; scripts
  that expected exit 0 after any repair attempt should read the new
  `recheck` block of `--format json`. `kct repair-clearance` likewise exits 1
  when a pair it reported as repaired before is now, on re-measurement,
  reported as unrepaired.
  The re-check adds time: `kct fix-drc`, and the auto-fix stage of
  `kct route --auto-fix`, now run `kct check`'s rule set on the output and,
  for kicad-cli-detected violations, a second kicad-cli DRC with zone refill
  (seconds on a real board) within the same time budget. A board the last
  pass left unchanged reuses the previous kicad-cli result instead of running
  it again.

- **Differential-pair skew limits and their severity changed** (Issue #6443).
  - A `diffpair_length_skew` finding is now a **warning**, not an error, when
    its limit is an `uncited` value from the protocol defaults table. Cited
    limits, limits set with `skew_tolerance_mm`, and the generic 0.5 mm default
    remain errors. Only classes that declare `pair_protocol` can be affected.
  - Board 03's `USB_D` and `USB_MCU_D` pairs are declared `USB2_FS`, so their
    skew limit moves from 0.5 mm to 2.5 mm, in `kct check` and in the board's
    own `routing_plan.usb_geometry` bound. 0.5 mm stays the stretch target.
  - Board 06's regression-fixture `USB2` class drops its explicit 3.0 mm and
    takes the `USB2_HS` default, 1.27 mm. If you copied the USB 2.0 recipe
    with `skew_tolerance_mm=3.0`, that value still applies to your class as an
    explicit limit; remove it and set `pair_protocol` to take the cited one.
  - `DifferentialPairRules.for_type` emits a `DeprecationWarning`. Two of its
    values changed when it became a shim over the table: `USB3` length delta
    0.5 mm -> 0.1 mm and `CUSTOM` 1.0 mm -> 0.5 mm. The grid router reads these
    for name-detected pairs, so it now attempts length matching sooner on
    such pairs.
  - A net-class sidecar whose `pair_protocol` names no known protocol is
    rejected instead of falling back to the generic limits.
- **Routed boards carry native `diff_pair_gap` rules** (Issue #6443). After a
  re-route, the `.kicad_dru` has one `diff_pair_gap (min ..)` rule per coupled
  pair KiCad recognises by name, so `kicad-cli pcb drc` can newly report
  `diff_pair_gap_out_of_range` for copper below the gap the router used (for
  example after hand edits). Pairs whose class asked for less than the
  manufacturer or authored board minimum now route wider; nothing is needed
  unless a design relied on the narrower gap, in which case lower its
  `min_clearance` deliberately rather than expecting the router to undercut it.
- **Routing continuity is info-only; a new pair check can fail** (Issue #6443).
  - `diffpair_routing_continuity` now reports `info` severity and never fails a
    board. `coupled_continuity_threshold` still parses but is **deprecated** and
    emits a `DeprecationWarning`; board 03's 0.25 override is removed.
  - New `diffpair_uncoupled_length` can newly fail a board: an engaged pair of a
    class that declares `pair_protocol` (USB2: 6.35 mm per component on the net)
    or `max_uncoupled_total_mm` now has its uncoupled length and via count
    checked, as an error when the limit is cited or set on the class and a
    warning when it is an `uncited` table value. Classes with no protocol and no
    explicit limit get no uncoupled-length or via-count limit; an engaged routed
    pair with unequal P/N via counts still gets a warning. The generic 0.5 mm
    skew default stays an error.
  - `diffpair_clearance_intra`, `diffpair_length_skew` and the new rule move
    from the advisory-quality to the manufacturing bucket of the `kct check`
    category summary, because their findings block `manufacturable`.
  - A re-routed board's `.kicad_dru` carries native `diff_pair_uncoupled`,
    `skew` and `via_count` rules, so kicad-cli can newly report
    `diff_pair_uncoupled_length_too_long`, `skew_out_of_range` and
    `too_many_vias` on a coupled pair that breaches its limit.

- **Connectivity checks can newly report opens and shorts** (Issues #6452,
  #6453, #6470). `validate()` / `kct validate --connectivity` no longer treats
  a pad inside a zone's outline as connected unless its copper touches the
  fill. `NetStatus.open_connections` and `validate()` now count a detached
  track or via, and a fill island that has only a track or via on it, as
  kicad-cli does; the new `NetStatus.padless_components` holds that count. A
  lone fill island (fill copper that nothing touches) is not an open, because
  kicad-cli does not report one: `validate()` now lists it as a `zone_island`
  warning, where it used to be an error. `NetStatus.status`, `incomplete`,
  `island_count` and `routed_connections` stay pad-based, so a net whose pads
  are all joined is still `complete` when a detached fragment gives it
  `open_connections == 1`; gate on `open_connections` to catch those. A blind
  via no longer bonds a pad on a layer it does not span. Copper LVS can report
  pad overlaps and track-on-pad contacts as shorts that the eroded pad box
  used to miss. Apart from the lone-fill warning, every new finding is one
  kicad-cli also reports.

- **A partial-span via with no type is now written `blind` or `buried`, not as a keyword-less node KiCad drilled through** (Issue #6485). `PCB.add_via`, `via_node` and the block exporter used to write `(via ... (layers "F.Cu" "In1.Cu"))` for a partial copper pair, which KiCad built as an ordinary through via. The same call now writes `(via blind ...)` (one outer end) or `(via buried ...)` (two inner ends) and KiCad drills it that way, so an unchanged script can produce a board that needs a blind/buried fab process. To keep the old fabricated result pass `via_type="through"` (`PCB.add_via`, `via_node`), which writes the via on F.Cu/B.Cu. Neither `kct check --mfr` nor kicad-cli DRC reports a blind or buried via on a fab profile that cannot build one (tracked in #6518); the log warning each of the three writers emits when it derives the keyword is the only signal. `via_node` and `PCB.add_via` now raise `ValueError` on an unknown `via_type`; `via_node` used to ignore anything other than `"micro"`. On boards with more than two routable layers, non-micro router vias are written on F.Cu/B.Cu and every via clearance gate (Python and C++) checks the whole barrel, so a re-route can place vias differently.

- **`kct net-status` can newly exit 2 on a board whose pads are all connected**
  (Issue #6491). A track or via that reaches no pad on a net that has pads now
  fails the exit code. `--allow-stranded-pour-pads` does not relax it.
  `scripts/ci/check_net_status.py` fails the same way, and `--known-open-nets`
  does not excuse it. Delete the stray copper or connect it; `kct net-status`
  names each item. This replaces the #6452 advice to gate on
  `open_connections`: gate on `NetStatusResult.stranded_count` (JSON:
  `summary.stranded`) together with the incomplete/unrouted counts. A track on
  a net with no pads is not counted, since kicad-cli does not report it.

- **Keyword-less partial-span vias now count as through vias** (Issue #6493).
  If a board from another generator (or a hand-written fixture) has
  `(via ...)` nodes with a partial `(layers ...)` pair and no
  `blind`/`buried`/`micro` keyword, `kct check` can now report shorts
  where the through barrel crosses other-net copper, and opens that came
  from reading the via as blind (a barrel KiCad actually drills to
  F.Cu/B.Cu) disappear. This matches kicad-cli. If you meant a
  blind or buried via, add the keyword, e.g. `(via blind (at ...) ...)`.
  Loading such a board logs one warning naming how many vias were affected.

- **`kct validate --connectivity --format json`: `summary.zone_island_count`
  now counts only unconnected (error) zone relationships** (Issue #6495). Lone
  fill island warnings are counted in the new `summary.lone_fill_island_count`.
  `--strict` no longer exits 2 on a board whose only warnings are lone fill
  islands (it previously did so only when kicad-cli was absent); other warnings
  still fail `--strict`.

- **`kct check --mfr` can newly fail on boards with blind, buried or micro
  vias** (Issue #6518). No shipped profile claims these capabilities yet (the
  new `blind_buried_vias_supported` / `micro_vias_supported` flags default to
  `false` until a fab's published capability is sourced), so every
  `(via blind|buried|micro ...)` is now a manufacturing error. Replace such vias
  with through vias, or run `kct check --skip via_type` if your fab order
  includes HDI/sequential-lamination processing.

- **`kct route --route-engine mesh --differential-pairs` now exits 2**
  (Issue #6551). The mesh engine has no coupled router, so the flag could never
  couple a pair under it. Use `--route-engine lattice --strategy basic
  --differential-pairs` for coupled pairs on a whole-netset engine, or
  `--route-engine grid --differential-pairs`. `--route-engine mesh --strategy
  basic` without the flag is unchanged.

- **The manufacturer clearance floor now depends on the board's stackup**
  (Issue #6561). The floor is now the fab profile's per-stackup
  `min_clearance_mm`, the value `kct check --mfr` already enforces, so it can
  rise or fall by board. Examples:
  - jlcpcb 2-layer 2oz rises to 0.1524mm.
  - seeed 2/4-layer rises to 0.1524mm (0.2mm on 2oz).
  - oshpark 2-layer rises from 0.152mm to 0.1524mm.
  - pcbway 2-layer 2oz rises from 0.127mm to 0.2mm, so the default 0.15mm
    route clearance on those boards becomes 0.2mm.
  - flashpcb previously had no route clearance floor at all; it now gets
    0.13mm (0.2032mm on 2/4-layer 2oz), so the default 0.15mm is raised to
    0.2032mm on 2/4-layer 2oz boards.

  The default 0.15mm route clearance on these boards is now raised to the fab
  floor, with the usual banner. Pass `--clearance 0.15` to keep the old value;
  `kct check --mfr` will flag it, as it already did.

  The floor *loosens* on jlcpcb and pcbway 4-layer (0.1016mm) and 6-layer
  (0.0889mm), and on oshpark 4-layer (0.127mm). Declared clearances and
  diff-pair gaps between the new and old floors are no longer raised. The
  relaxation ladder's floor never drops below the flat `MfrLimits` value. Runs
  whose clearance sits above every floor, like the 0.15mm default on jlcpcb
  1oz 2/4/6-layer boards, are unchanged.

- **`kct route --dry-run` no longer routes, so it is not a "route but don't save" shortcut** (Issue #6563). On the default `--auto-layers`, `--auto-pcb-size`, `--auto-mfr-tier` and `--adaptive-rules` flows it used to route and print routing statistics (and, with `--format json`, emit the routing result). It now prints only the analytical grid plan and writes no files; the JSON document is just `{"exit_code": 0, "verdict": "success"}` and carries no plan. Scripts that relied on the old behaviour should route into a scratch path instead, e.g. `kct route board.kicad_pcb -o /tmp/scratch.kicad_pcb`.

- **`diffpair_clearance_intra` can newly fail** (Issue #6575). Pairs laid
  tighter than the board's authored `min_clearance` or the class pair gap are
  now flagged (for example `X_POS`/`X_NEG` copper at 0.10 mm on a board with a
  0.15 mm minimum). The check is manufacturing-category since #6443, so these
  findings block `manufacturable`. Re-route the pair at the floored gap.

- **`kct pcb import-ses` can now exit 1 after writing its output** (Issue #6593).
  It exits non-zero when the session imports no wires or vias, or when any
  wire or via was skipped (unknown net, non-board layer, unsupported padstack
  or wire shape, `was_is` pin swaps); the reasons are printed, and `--format
  json` carries them in a new `skipped` list with `success: false`. JSON
  `wires`/`vias` now count what was written to the board; the parsed totals
  are in `ses_wires`/`ses_vias`. The merged board is still written in every
  case, so scripts that only need the file can ignore the exit status.

- **A `--net-class-map` can now stop `kct route` from pouring a net** (Issue
  #6599). If your sidecar puts a power-named net (`VBUS`, `+5V`, `VIN`, ...)
  in a class with `is_pour_net: false` and `route_via: "pathfinder"` or
  `"manual"` (which is also what an entry gets when it omits both fields),
  auto-pour no longer creates a zone for it, and the router traces it
  instead. To keep the pour, set `route_via: "pour"` on that class or pass
  the net in `--skip-nets`. Of the demo boards, only board 07's recipe
  changes: its sidecar's AUX entries omit both fields, so `+5V` is now traced
  rather than poured (matching its committed routed board). Board 03's recipe
  passes `--no-auto-pour` and is unaffected; only a manual `kct route
  --net-class-map` run on board 03 now traces VBUS.

- **Coupled pairs with a per-terminal uncoupled limit can newly fail `kct
  check`** (Issue #6615). A pair whose class or protocol sets
  `max_uncoupled_per_terminal_mm` (USB2: 6.35 mm) now fails when more than that
  much uncoupled copper outside the courtyards is charged to one terminal, even
  if the KiCad total passes. Set `gather_ignore` to a radius on the class to
  widen the ignored region; every committed board passes unchanged.

- **`kct route --route-engine lattice --differential-pairs` now changes pair
  copper after routing** (Issue #6664). A pair whose skew exceeds an
  error-severity limit (a class `skew_tolerance_mm`, a cited protocol default,
  or the generic default) gets serpentine bumps on its shorter half, even when
  its class is `length_critical: false`. Pairs already inside the limit, and
  pairs whose limit is only an uncited protocol default (a `kct check`
  warning), are left byte-identical. There is no opt-out flag; to keep a
  pair's copper untouched, loosen its `skew_tolerance_mm` in the net-class
  map, or route with `--route-engine grid`.

- **`kct route --oracle-rounds` now defaults to 12 (was 3)** (Issue #6671).
  Rounds still stop as soon as one fails to lower the open pour-link count,
  and `--timeout` still bounds them, but a board whose pour links close over
  several rounds can now spend longer in the KiCad-oracle stage. Pass
  `--oracle-rounds 3` for the old budget.

- **Lattice routes can now include serpentines without `--length-match-groups`**
  (Issue #6674). With `--route-engine lattice`, a net-class map that declares
  `length_match_group` now triggers match-group length tuning by default, and
  `kct route` prints a `--- Length-Match Groups (auto ...) ---` report. The
  tuner engages on `kct check`'s own limit, regardless of the class's
  `length_critical` flag, and only for groups over that limit. There is no
  flag to switch it off; to route without it, drop the `length_match_group`
  declarations or use `--route-engine grid`. Routes on boards without
  `length_match_group` declarations are unchanged.

- **Auto-pour may put a different power net on `In2.Cu`** (Issue #6714).
  On 4+ layer, single-ground boards where `kct route` or `kct build`
  auto-pours two or more power nets, the inner power plane now goes to the
  power net that feeds the most footprints. It used to go to the first power
  net in net-number order. Boards where that first net already fed the most
  parts, or tied for it, are unchanged (boards 03, 04, 05 and 07rf), and
  hand-authored zones are never moved. To keep a
  particular layout, author that zone yourself.

- **Rebuild the native router after upgrading** (Issue #6718). The C++
  authored-clearance gate now takes each roundrect pad's corner ratio, so
  `router_cpp` is bumped to build version 56 and a build-55 extension is
  rejected. Run `uv run kct build-native` once.

### Fixed

- **Commit-time pad-access gate now protects pending Kelvin sense pins** (Issue #5398).
  A recognised Kelvin sense net that has landed its first branch no longer
  exempts its not-yet-reached terminals, so a later foreign route that would
  consume a pending pin's last exit is refused. Ordinary nets are unchanged.
  Part of the phase; dense-board qualification remains outstanding.

- **The router's clearance halo is now the exact disc the geometry requires,
  not a Chebyshev square** (Issue #5660). Both the Python grid and the C++
  `Grid3D` mark a via's and a segment's keep-out with the shared clearance
  kernel's dilation, so the halo no longer reaches `sqrt(2)` times too far at
  its diagonals. A legal candidate near an existing via is no longer refused
  during search (#5410). The halo's reach is unchanged, so the newly marked
  cells are a strict subset of the old ones. The square's diagonal excess had
  also been hiding a gap in the via-drop check, which the router now closes:
  a **same-net** via is held to the fab's hole-to-hole minimum
  (`min_hole_to_hole`), not only to the same-net merge threshold, matching
  KiCad's `hole_to_hole_clearance` rule (#5673).

- **Search no longer refuses a trace-vs-via gap the commit gates accept**
  (Issue #5661). The search-time halo refinement — `RouteHaloGeometry.clear`
  in the Python A* and `Grid3D::route_trace_geometry_clear` /
  `route_via_geometry_clear` / `trace_stored_vias_clear` behind the C++ A* —
  now asks the shared clearance kernel for every copper verdict instead of
  subtracting both half-extents from its own distance. A trace candidate
  measured against a stored via is held to the clearance that pair actually
  resolves, not to `max(required, via_clearance)`: that extra floor made the
  search **stricter** than the validator the same run applies at commit time,
  so A* could burn its expansion budget refusing a 0.18 mm gap it would then
  have accepted once routed (#5410). Hole-to-hole and drill floors are
  untouched — they are a different rule value — and no clearance rule value or
  A* cost term changed.

- **Board 03 footprint silkscreen now meets the jlcpcb-tier1 0.15 mm stroke floor** (Issue #5762). The 139 stock-library 0.12 mm silk graphics (137 `fp_line` + 2 `fp_poly`) are widened before the silk-to-pad clip re-runs, and `kct fix-silkscreen` now also widens undersized `fp_poly` strokes.

- **Board 00 LED `D1` now carries a cathode silkscreen mark** (Issue #5764).
  `generate_led()` draws the stock `LED_D5.0mm` outline, flat edge on the
  pad-1 (cathode) side at the 0.15 mm silk floor, as an `fp_line` polyline
  that kct's silk clearance model checks, so `pin1_marker_missing` no longer
  fires on `boards/00-simple-led`. The shipped manufacturing package and
  `output/readiness.json` were rebuilt through `kct readiness --generate`
  (new `boards/00-simple-led/package_release.py`) and verify as `ready`.

- **`via_under_body` precision fixes** (Issue #5767). The default footprint pattern no longer selects word-interior `LGA` (e.g. `Vulgaris`), and an intact exposed pad no longer absorbs same-net, slightly-larger-than-median pins into a phantom thermal-pad cluster.

- **`kct route`, `kct net-status` and kicad-cli now give one verdict on stranded pour pads** (Issue #5785). `kct route` used to exit 0 on a board whose pads were left stranded on a pour (kicad-cli `unconnected_items`) while `kct net-status` exited 2. Stranded pour pads are now a completion failure everywhere. `kct stitch`'s `--avoid-pad-overlap` filter used by the completion loop also rejects vias whose drill overlaps (not only sits inside) a same-net SMD pad, and net-less copper pads are now stitch obstacles.

- **`kct route --preserve-existing` no longer re-routes nets that are already
  fully connected** (Issue #5788). The flag loaded existing copper as an
  obstacle but never narrowed the route set, so both engines re-routed complete
  nets and dropped their copper -- board 06 lost all 64 of its pre-routed,
  coupled LVDS segments (0/64 survived; now 64/64). A net that `kct check`
  already reports as connected (traces, vias and same-net filled zones all
  count) is now held out of the route set, its copper kept as a hard clearance
  obstacle and re-emitted verbatim. An explicit route set is unchanged:
  `--nets`, `--region` and `--complete` still re-route exactly what they name.

- **`kct route` no longer routes signal traces across declared reference-plane
  layers by default** (Issue #5789). `--reserve-plane-layers` now defaults to
  on whenever the resolved layer stack declares one or more `PLANE` layers, so
  the router keeps GND/PWR reference planes (e.g. `--layers 4`'s In1.Cu/In2.Cu)
  free of signal copper instead of silently cutting the plane's return path.
  Pass `--no-reserve-plane-layers` to opt back into the previous behavior.

- **`kct readiness`'s HV/isolation gate now runs a real creepage/clearance
  audit instead of trusting free text** (Issue #5807). `_gate_hv_isolation`
  used to mark the gate `passed` whenever `--hv-requirement` held any nonempty
  description — no measurement, no audit, no binding to the checked PCB. The
  gate now runs the same audit `kct audit --hv-standard`/`--hv-min` uses
  (`kicad_tools.audit.check_isolation`) against the checked PCB and
  net-class-map sidecar on every run, so a description alone can never
  establish `passed`; absent, malformed, or mismatched (wrong-board)
  evidence blocks the gate instead.

- **`kct create-pcb` resolves footprints through the input schematic's
  `fp-lib-table` and no longer reports false success when components are
  dropped** (Issue #5808). Custom/project-local footprint libraries are now
  resolved the same way `suggest-footprint` / `assign-footprints` already do
  -- the project's `fp-lib-table` (walked up from the input schematic, so
  `${KIPRJMOD}` still resolves correctly even when `--output` writes the
  board elsewhere) is consulted before the standard KiCad library search. A
  component whose footprint still can't be resolved now makes `create-pcb`
  exit non-zero and report `"success": false`, instead of silently omitting
  the part and exiting 0 with `"success": true`. `--no-place` is unaffected
  -- its explicit skip-placement mode still exits 0.

- **Label LVS no longer reports every locally-labelled net as a mismatch on a
  KiCad-created board** (Issue #5809). A plain schematic label is sheet-scoped,
  and KiCad names the net it drives `/SENSE` on the root sheet or
  `/SubMcu/SENSE` inside a sheet named `SubMcu`. `compare_netlists` compared
  those against the bare label text (`SENSE`), so every named local net on a
  board whose netlist came from `kicad-cli` failed — 291 mismatches such as
  `NRST` versus `/NRST` on the reporting board, against a netlist KiCad itself
  considered identical. The schematic side now reports the sheet-qualified
  identity, verified pin-for-pin against `kicad-cli sch export netlist`. Global
  labels, power-symbol nets (`GND`, `+3V3`, `PWR_FLAG`) and hierarchical
  sheet-pin nets stay unqualified, matching KiCad; boards that spell a local
  net bare (the pure-Python netlist fallback's convention, used by the
  committed `boards/00-simple-led` artifacts) still compare clean, but only
  where the bare name unambiguously names one net — sibling sheets that each
  label a net `SENSE` keep three distinct identities and a board that collapses
  them still fails.

- **`kct check --copper` accepts the keyed `outer=X,inner=Y` form** (Issue
  #5810). The unified `kct check` parser declared `--copper` as a float with a
  hardcoded `1.0` default, so `kct check board.kicad_pcb --copper
  outer=2,inner=0.5` -- the form the mixed-copper diagnostics recommend --
  died with `invalid float value`, while `python -m kicad_tools.cli.check_cmd`
  accepted it. Both parsers now share one `--copper` definition (raw string,
  no default), the value is forwarded verbatim, and an omitted `--copper`
  still resolves from the board's declared stackup, then the profile default.
  A new parser-drift guard compares argparse `type=`/`default=` for every flag
  declared on both `check` parsers, not just flag presence.

- **Polygon silkscreen is checked for clearance instead of silently skipped**
  (Issue #5811). `_stroke_geometry` returned `None` for every graphic type
  other than `line`/`rect`, so `fp_poly` / `gr_poly` silk — the shape KiCad
  footprint libraries use for pin-1 and polarity markers — contributed no
  geometry at all to `silk_over_copper`, `silk_overlap`,
  `silk_edge_clearance` or `silk_pad_clearance`. A filled pin-1 triangle
  0.1052 mm from a pad read as clean while native KiCad DRC reported a real
  sub-floor violation. Polygons are now modelled in both fill states, which
  print different shapes and are measured to differ in native DRC too: a
  filled polygon prints its interior (so a pad inside it is covered) even
  with `(stroke (width 0))`, while an unfilled one prints only its buffered
  outline. Footprint rotation and side selection apply exactly as they do to
  line/rect strokes, and `gr_poly` is now parsed into `PCB.graphics` (it was
  previously not parsed anywhere). `silk_pad_clearance` findings name the
  offending element's UUID, so a specific marker can be located and waived.
- **Silk primitives kct cannot model are reported instead of passing
  silently** (Issue #5811). A new `silk_geometry_unmodeled` **info** finding
  names each silkscreen `fp_circle` / `fp_arc` / `gr_circle` / `gr_arc` (with
  its UUID) that the clearance geometry does not cover, so incomplete
  coverage is a stated fact rather than an absence of findings. It is
  advisory-category and info-severity, so it never affects the gate; native
  DRC remains authoritative for those shapes.
- **Pad mask apertures in the silk checks are oriented by the pad's angle**
  (Issue #5811). `_pad_aperture_geometry`'s axis-aligned box ignored
  `pad.rotation`, which was harmless while every silk element was a text box
  or a thin stroke but produced a false `silk_over_copper` pair once real
  polygon markers were modelled (a `1.475 x 0.55` TQFP pad on a footprint
  rotated -90° spilled ~0.5 mm sideways into its own pin-1 triangle). The box
  is now rotated by the pad's absolute angle; the measured `kicad-cli` pair
  parity on boards 03/05/06/07 (4/12/7/5) is unchanged.

- **The readiness report's engine fingerprint no longer records the consumer
  repository's commit** (Issue #5812). `build_fingerprint` handed
  `_git_describe` the installed package's grandparent directory and trusted
  whatever git's upward `.git` search found there. For kct installed as a
  pinned VCS dependency inside another project's `.venv`, that is the
  *consumer's* checkout: `engine.commit` read `7800b046…` (the consumer's
  HEAD) where the installed `direct_url.json` said `14d52b1e…`, and the
  consumer's uncommitted board edits were reported as a dirty engine. Engine
  provenance is now taken only from sources that demonstrably belong to the
  engine — first the installed distribution's PEP 610 `direct_url.json`
  (`vcs_info.commit_id`), then a git checkout that `git ls-files` confirms
  actually tracks the imported `kicad_tools` package. An editable install or a
  run straight out of a clone still reports its own HEAD and dirty state
  exactly as before. An ordinary wheel install, which has neither, now
  **omits** `commit` (with `dirty: false`) instead of borrowing an unrelated
  repository's identity. `source_digest` is unchanged and remains available in
  every case, so an omitted `commit` still leaves the engine's sources
  identifiable.

- **Mask-to-copper no longer rejects a freshly loaded KiCad 10 name-only-net
  board as "edited"** (Issue #5814). The `mask_to_copper` check's
  immutable-source guard compared the loaded `PCB` object against a plain
  re-parse of the saved bytes. Loading a board whose nets are written in
  KiCad 10's name-only dialect (`(net "SWCLK")` inline references with no
  top-level `(net N "name")` table, as `kicad-cli pcb drc --save-board`
  writes) synthesizes that table into the in-memory tree, so the two sides
  could never match -- 0 net declarations on the file side versus one per net
  on the object side (3 on a two-pad net tie, 80 on a real board). Every such
  board was reported `coverage="incomplete"` with `PCB object differs from
  current source bytes` before native mask geometry inspection ever ran. Both
  sides of the comparison now go through the same load-time normalization
  (`kicad_tools.validate.checker.canonical_source_text`), so an untouched
  board is analyzed. The guard itself is unchanged in strength: an unsaved
  in-memory board, pad, mask, net or copper edit, a source changed on disk
  after load, an unreadable source, and the sha256 source binding on the
  native result all still block the check.

- **Label LVS handles a sheet placed more than once** (Issue #5815). A
  hierarchy that reuses one `.kicad_sch` file under two sheet symbols used to
  be walked only once, so every pin of the second placement vanished from the
  schematic-side netlist and `kct check` judged those pads against nothing.
  The two placements are now separate sheets, their identically-spelled local
  labels stay distinct nets (`/MCU_A/DBG_LED` vs `/MCU_B/DBG_LED`), and each
  placement's pins take the reference designators KiCad records in the
  symbol's `(instances ...)` block — matching `kicad-cli sch export netlist`
  exactly. Circular sheet references are still refused; global labels and
  power nets still span placements unqualified.

- **`kct readiness` now accepts `--generate`/`--verify` like the standalone
  runner** (Issue #5816). The unified CLI maintained a separate, stale
  argument list that never declared this mutually-exclusive operation flag
  pair, so `kct readiness <board> --generate` failed argument parsing
  ("unrecognized arguments") and a bare `kct readiness <board>` silently
  verified instead of erroring. `--verify` remains the default; passing both
  flags together is rejected, matching `python -m
  kicad_tools.cli.readiness_cmd`.

- **`--physical-copper-gap` no longer gives up on boards that use a standard
  net tie** (Issue #5817). Filled and stroked footprint copper polygons
  (`fp_poly`) are now modeled -- layer, stroke width and footprint
  rotation/mirroring applied -- and unioned into the per-layer copper with
  their source UUID kept for provenance. A `NetTie-2_SMD_Pad0.5mm`'s
  pad-joining copper previously came back as
  `physical_copper_gap_incomplete: unsupported copper graphic: footprint/fp_poly`
  instead of an actual gap assessment; it now checks clean, while a genuinely
  separate boundary closer than the floor is still reported. Polygons this rule
  cannot model exactly (malformed points, an unresolved copper layer, a
  negative stroke) still report incomplete coverage rather than reading as
  clean.

- **Native mask/copper attribution accepts ordinary factory `silk_clearance`
  rules** (Issue #5818). A board whose same-stem `.kicad_dru` carried the
  silkscreen clearance minimums fabricators routinely ship was reported
  `incomplete` with `Native object rule context unsupported: silk_clearance`,
  even though that constraint is measured against already-plotted material and
  never changes plotted mask or copper geometry. `silk_clearance` is now part
  of the geometry-neutral custom-rule set, so such a board attributes exactly
  as the same board without the sidecar. The addition is narrow: genuinely
  fill-shaping constraints (`zone_connection`, `thermal_relief_gap`,
  `min_resolved_spokes`, ...) and every other unrecognized constraint still
  fail closed, as do native extraction errors and invalid contours or polygons.

- **`kct mcp serve` names the `mcp` extra when the server module fails to import** (Issue #5820).
  The `kicad_tools.mcp.server` import now sits inside `_run_serve`'s `try`, so a core-only
  install prints `pip install 'kicad-tools[mcp]'` for both stdio and http transports instead
  of a bare `ModuleNotFoundError`.

- **Label LVS keeps a sheet-crossing net whole and stops excusing split or
  mis-sheeted spellings** (Issue #5826, reconciling the #5809 fix with PR
  #5824 / Issue #5815). A root-sheet local label wired to a sheet pin (`BUS`)
  now resolves to KiCad's `/BUS` on *both* halves of the net: the child
  sheet's hierarchical label inherits the identity its parent resolved,
  instead of staying bare `BUS` and splitting one physical net in two, so a
  KiCad-spelled board no longer reports the child-side pads as mismatches
  (`schematic_net` for those pins now reads `/BUS`). Whether a label is
  sheet-qualified is decided by its type *on its own sheet*, so a global
  `SENSE` on the root never merges with a child's unrelated local `SENSE`.
  `compare_netlists` additionally accepts a board that drops the leading slash
  (`MCU/DBG_LED`), but only when the schematic net is spelled one consistent
  way across all of its pads on the board; a board-side name starting with
  `/` must match exactly, so a root-level `/SENSE` is never taken for
  `/ChildA/SENSE`.

- **`kct ecosystem list` no longer silently omits projects in an unmapped
  category** (Issue #5843). The section-heading table it prints from is now
  the single source shared with the README's generated block (previously two
  independently-maintained copies), and the listing now fails loudly instead
  of under-printing if a category is ever missing a heading. The related
  `scripts/ecosystem_refresh.py` drift check also now maps GitLab license
  keys to their correct SPDX casing (e.g. `apache-2.0` -> `Apache-2.0`,
  `bsd-3-clause` -> `BSD-3-Clause`) instead of a `.upper()` guess, so a
  GitLab-hosted project no longer risks a recurring false-positive
  `license-changed` finding.

- **The post-route trace optimizer now measures pad clearance exactly instead
  of off a dilated raster** (Issue #5854). `GridCollisionChecker` and
  `VectorCollisionChecker` (used by `merge_collinear`, `eliminate_zigzags`,
  `compress_staircase`, `convert_45_corners` and `pull_tight`) previously
  judged a candidate shortcut against pad copper by comparing two already-grown
  raster halos — the pad's own clearance halo and the candidate's own
  `width / 2 + trace_clearance` dilation — which rejected shortcuts up to
  roughly twice the real clearance requirement away from a pad, especially at
  a rotated roundrect's corner. Both checkers now ask the shared clearance
  kernel for the exact edge-to-edge gap (the same kernel search and commit-time
  validation already use), so the optimizer takes shortcuts that are legal by
  the project's own clearance rule instead of leaving them on the table.
  Routed copper and via clearance already used this exact model (#5625); no
  rule value changed — a pad's requirement is still the per-component one the
  grid resolved for it (`component_clearances` overrides and fine-pitch
  relaxations included), and a plane-net pad still keeps the stitch-via space
  `kct stitch` needs (#2842).

- **Match-group length/phase tuning's post-insertion DRC self-check now
  measures pad clearance exactly, not via a bounding-circle approximation**
  (Issue #5855). `_post_insertion_clearance_detail_group` and
  `_post_insertion_clearance_detail_pair_group` (`router/match_group_tuning.py`)
  measure every pass — segment-vs-segment, segment-vs-via, and segment-vs-pad —
  through the shared clearance kernel instead of private arithmetic. The
  segment-vs-pad pass previously treated every foreign pad as a circle of
  radius `max(width, height) / 2`, which could over-reject a legal trombone
  insert near a rotated or non-square pad; it now reads the pad's real
  roundrect/rotation/custom geometry. No rule value or heuristic changed.

- **`kct check` no longer under-models rotated or non-square pads against
  trace and board-edge clearance** (Issue #5857). `ClearanceRule` (and its
  `_calculate_clearance` dispatch) and `EdgeClearanceRule` now ask the shared
  clearance kernel (`router/clearance_kernel.py`) for pad/segment/via pair
  distances, through a new `validate/clearance_shapes.py` board-file
  translation, instead of composing a rect-pad-vs-segment check against the
  pad's axis-aligned bounding box. A rotated or non-square `rect` pad near a
  trace or the board edge no longer over-reports copper at its corners (the
  `roundrect-corner-gap` failure mode). `kct check`'s own exact-polygon pad
  model is unchanged and remains the kernel's own reference model (ported
  from this module in an earlier phase of Epic #5509), so this is a dedup
  with no rule-value change, not a new clearance policy.

- **`kct check` now distinguishes missing KiCad library-table initialization from genuine footprint/symbol defects** (Issue #5860). The reviewed paid-drill gate's strict-ERC leg previously failed identically for both; it now emits a loud, distinct diagnostic pointing at `scripts/ci/init_kicad_libraries.py` when the host's global library tables were never initialized.

- **`kct route`'s post-route self-check now catches copper shorts it used to
  excuse** (Issue #5862). A foreign-net pad or via violation on a component
  with a fine-pitch / same-component clearance relaxation was classified
  `component_inherent` -- excluded from the printed summary, the
  `drc_verify_and_nudge` repair pass and the exit code -- with no check of
  the violation's sign, so an actual copper overlap (a hard short, not a
  sub-clearance near-miss) was excused exactly like a 0.1mm gap. The
  segment-vs-segment check also only compared new segments against each
  other, never against copper preserved by `--preserve-existing` /
  `--nets`. All three gaps are closed: only a *positive* sub-clearance gap
  can be `component_inherent`; a new segment is now checked against
  preserved foreign-net copper too; and any short found anywhere in the
  pre-save validation (default `--auto-layers` path included) now prints an
  explicit `SHORT` finding and forces the router's existing "routing
  succeeded but the copper is dirty" non-zero exit contract instead of exit
  0. On the normalized Arduino Nano benchmark, the router's self-check now
  reports the same 10 `shorting_items` `kicad-cli pcb drc --refill-zones`
  independently finds, instead of reporting a clean board.

- **`kct route` no longer refuses a whole board over one custom-shaped pad**
  (Issue #5863). A pad whose copper the router cannot represent exactly
  (`custom` with unsupported primitives, or `trapezoid` with a non-zero
  `rect_delta`) now degrades instead of aborting the load: it is kept as
  obstacle copper (exact when derivable, otherwise a conservative bound that
  is guaranteed to contain the real copper) and only its own net is excluded
  from routing, with a warning naming the pad and net. A pad with no copper
  layer at all (a paste-/mask-only mechanical pad) is dropped with no net
  impact. A `trapezoid` pad whose `rect_delta` is zero or absent is exact as
  a plain `rect` and stays fully routable. This recovers five of the six
  affected real `dataset-srj18` boards (`gmsl_serializer`,
  `hdmi_edid_debug_board`, `oculink_pcie_adapter`, `ov9281_camera_board`,
  `usb_c_power_adapter`); the sixth now reaches a separate duplicate-pad-number
  guard, tracked as #5873.

- **`kct route`'s auto-pour no longer aborts the whole run when one net's pour
  cannot be partitioned** (Issue #5864). On a board that already carries
  authored zones — the normalized Arduino Leonardo, whose `GND` pour fully
  covers the `AGND` pad region on `B.Cu` — the outline allocator raised
  `ZonePartitionError` out of the default `--auto-pour`, and the route exited
  with a traceback after ~28 s having written no board at all. That net's pour
  is now **skipped with a warning** naming the net, the layer and the covering
  zones; the remaining pour nets still get their zones, the skipped net is left
  for the router to route as traces, and routing continues. `kct zones` and
  `kct build` are unchanged — they still fail with the actionable
  `ZonePartitionError` (#3240), since those callers are asserting an
  authoritative pour where a silent skip would be the wrong answer.

- **`kct route` no longer refuses a footprint whose same-numbered pads carry
  distinct no-connect nets** (Issue #5873). KiCad gives each unconnected land
  of a duplicated pad number (e.g. a connector's two `MP` mounting pads) its
  own autogenerated `unconnected-(...)` net; the loader used to abort with
  `Physical terminal J5.MP has conflicting nets`. Such lands now load as
  independent terminals. Two same-numbered pads on different signal nets, a
  signal net colliding with a no-connect net, or distinct nets on lands the
  footprint declares jumpered are still rejected.

- **`kct route --nets` no longer blames a "loader bug" when the pour-net
  auto-skip is the real cause** (Issue #5876). `--nets VCC,GND` on a board
  where VCC/GND are real, correctly-bound pour nets aborted with "...this is
  a loader bug, not an empty board" -- even though the preceding `Auto-skip:
  GND, VCC (pour nets — use zone fill)` line showed the auto-skip, not the
  loader, had removed every requested net. The `--nets` preflight guard now
  checks `_auto_skipped_net_names` before choosing wording: when every
  requested net was deliberately excluded by the pour-net/manual auto-skip,
  it reports that conflict by name and points at `--no-auto-pour` instead of
  claiming a loader defect; the genuine lost-pad-binding case (issue #4983)
  still reports the loader bug. Both paths still exit non-zero.

- **Pose-centerline trunks can yield a sealed corridor** (Issue #5895). With the
  pose search (#5786), a pose-coupled trunk that seals
  a net the main strategy then strands is now a #4463 corridor-yield candidate,
  the pose search honours the per-pair wall-clock budget, and the corridor-yield
  re-run no longer inherits the lifted copper's negotiated usage counts (on
  board 06 that phantom congestion drove the re-run to its 300 s cap and the
  resulting copper split the `+3V3` pour).

- **Placement-delta keepout guard no longer fails open on duplicate-reference
  boards** (Issue #5902). The #5890 keepout-intrusion check matched router
  pads by authored reference while the delta targets the physical component
  key, so on boards with repeated references it counted no pads and kept moves
  that parked a footprint inside a track-blocking rule area. It now matches
  pads by `component_id or ref`, like the rest of the feedback loop.

- **`kct route --order-method greedy|critical_first|congestion|hybrid` no longer silently does nothing** (Issue #5908). These four methods are only wired on the single-attempt path (`--layers N` or `--no-auto-layers`); on every escalation path -- the default `--auto-layers` recipe, `--adaptive-rules`, `--auto-pcb-size` and `--auto-mfr-tier` -- they were discarded and the board was routed with the default order. Those combinations now fail before any board is loaded or routed, with an error on stderr naming the supported alternatives. `--order-method crossing` and the flag-absent path are unchanged.

- **Rip-up can now target the copper that sealed a pad's last exit**
  (Issue #5913, Epic #5508 Phase 3a). When a net the negotiated loop cannot
  route has a terminal whose access set is empty, the pad-access witness's
  named closing nets are added to the existing targeted rip-up's
  `blocking_nets` — a shape neither overuse-based rip-up (the enclosing copper
  is legal and unshared, so it emits no overflow) nor the Bresenham
  direct-line blocker scan could ever name. Only route copper of nets the loop
  owns is eligible, so escape stubs, `--preserve-existing` copper, coupled
  diff-pair bodies, foreign pads, fills, keepouts and Kelvin sibling branches
  are reported as *held* rather than ripped. The rip-up, the per-net
  `max_ripups_per_net` budget and the all-or-nothing rollback are the existing
  transaction, unchanged. `kct route` prints the new
  `Access-loss rip-up targeting:` summary beside the pad-access invariant's,
  and `--no-pad-access-invariant` turns both off.

- **A slow or stalled `kicad-cli` no longer reads as "not installed"**
  (Issue #5932). `find_kicad_cli()` used to memoize a probe timeout for the
  whole process, so one stalled `kicad-cli version` made `kct route` and
  `kct pipeline` skip the zone refill and print `kicad-cli not installed`.
  Now a probe that times out is retried once with double the budget, and a
  timeout is never cached. It is reported as `kicad-cli probe failed`. The
  default probe timeout rose from 5 s to 10 s, and `KCT_KICAD_CLI_PROBE_TIMEOUT`
  overrides it. A skipped zone fill or refill now always prints a `WARNING:`
  on stderr, even with `--quiet`. The new `locate_kicad_cli()` returns a
  `found` / `not_found` / `probe_failed` status.

- **`kct route`'s pour-oracle completion lands the same copper on every run**
  (Issue #5934). `kicad-cli pcb drc` names a byte-identical board's
  `unconnected_items` differently from run to run: the per-net count is
  stable, but which spanning tree and which of several items at a shared
  anchor it names are not, even with KiCad capped to one thread. The closer
  acted on those names, so three oracle replays of one board 03 landed 561,
  547 and 563 copper nodes. It now acts on the board's own copper clusters
  whenever their count matches KiCad's, orders KiCad's links canonically
  otherwise, and unions the two projects' reports per net. Separately,
  `kicad-cli pcb drc --refill-zones --save-board` now gets the board with its
  stale `filled_polygon`s removed. A via added since the last fill that sat
  inside another net's stale plane fill was saved under the wrong net in 2 of
  8 KiCad refills of one file. On board 03, three replays now give
  byte-identical boards (26 -> 5 -> 1 links, 434 copper nodes, down from
  547-563).

- **`kct validate --sync` now checks that every pad is on the right net**
  (Issue #5937). The per-pad check was a placeholder, so a PCB with swapped,
  shorted or disconnected pads still reported `in_sync: true` and exit 0. Each
  pad's PCB net is now compared with its schematic pin's net (full sheet
  hierarchy) by connectivity, not by name -- `VCC` vs `/VCC` or differently
  named unnamed `Net-(...)` nets are not errors, but a pad on the wrong net is
  reported as `REF.PAD: schematic net X, PCB net Y`. A schematic with no wiring
  at all yields one "pad-net check skipped" warning instead of a silent pass.

- **`--format json` stdout is now a single JSON document for every command** (Issue #5938).
  - `drc`, `erc`, `detect-mistakes` and about 15 other commands used to print progress lines such as "Running DRC on: …" to stdout ahead of the JSON, so agents and MCP callers parsing stdout failed. Progress and diagnostics now go to stderr.
  - `kct route --format json` now always emits its routing-diagnostics document, including on the default layer-escalation path, which previously printed no JSON at all.
  - A new contract test runs every command that offers `--format json` (195 cases) and requires `json.loads(stdout)` to succeed.

- **`kct detect-mistakes` no longer treats `USB_D+`, `PG_3V3` or `unconnected-(...)` nets as power rails** (Issue #5939).
  The power/ground net classifier matched substrings, so any `+` or voltage
  fragment in a signal name made it a "rail". On board 03 that caused false
  "Missing decoupling capacitor" errors and "Power trace too narrow" warnings
  on the USB data nets. Classification now uses pad pin types (`power_in` /
  `power_out`) when the board has them. Otherwise it falls back to a shared
  whole-token name heuristic (`router.net_class.is_power_rail_name`) that
  strips the hierarchical sheet path first and recognises the common rail
  conventions: `+3V3`, `3V3_MCU`, `P3V3`, `V5V0`, `V3P3`, `VDD_CORE`,
  `VBUS_FUSED`, `VOUT_PRE`, `VAA` and `VUSB`. A qualifier such as `_SENSE`,
  `_DET`, `_EN`, `_FB` or `_PG` marks a derived signal (`VBUS_DET`), not a
  rail. `VBUS`, `VM`, `V3P3` and qualified `VBUS_*`/`VOUT_*` rails were not
  recognised before, so their trace-width and bypass checks may report new
  findings; on board 05 most of the new bypass-distance findings pair a cap
  with another cap or a diode, which is the pre-existing Issue #5970.
  `KELVIN_P`/`KELVIN_N` are no longer rails (the old `VIN` substring matched
  inside "KELVIN"), and `VCAP`-style regulator compensation pins are not
  rails. The missing-decoupling check now accepts any capacitor of 10 nF
  or more (for example 470 nF or 2.2 uF) and no longer treats `Q`
  transistors as ICs. The optimizer's FOM decoupling term uses the same
  classifier, so `kct optim fom-debug` values change (board 04: 5.99 to
  70.24; board 05: 31.51 to 67.87). Placement output does not change, but
  the default `decoupling_proximity` weight was calibrated against the old
  classifier and may need re-calibration (Issue #5984).

- **`kct detect-mistakes` recognises shared LED series resistors** (Issue #5940).
  The "LED missing series resistor" check now uses a path rule: an LED passes
  when every path from its anode or cathode side to a rail or driver pin goes
  through a resistor. Charlieplex line resistors and one resistor feeding
  several LEDs no longer warn (board 02 drops from 9 findings to 0); LEDs wired
  straight to a rail or GPIO, or with a branch that bypasses the resistor, are
  still flagged.

- **`kct mcp serve` keeps stdout clean for the stdio transport** (Issue #5961).
  Tool handlers that reach code reporting progress with `print()` (the router,
  the placement optimizer) no longer leak that text onto stdout, where Claude
  Code, Codex CLI and opencode would parse it as a broken JSON-RPC frame; it
  goes to stderr instead, as do `kct mcp serve`'s own startup errors. A new
  stdio smoke test drives the launch command `kct mcp setup` registers for
  each client through handshake, `tools/list` and a tool call.

- **`kct mcp serve` stdio transport is immune to fd-level stdout writes** (Issue #5965).
  While serving over stdio, the JSON-RPC frames now go out through a private
  duplicate of the real stdout, and file descriptor 1 is pointed at stderr. Output
  that bypasses Python's `sys.stdout` therefore lands on stderr and can no longer
  corrupt the stream for Claude Code, Codex CLI or opencode. That covers C/C++
  `printf`/`std::cout`, `os.write(1, ...)` and child processes such as
  `kicad-cli` that inherit stdout. fd 1 is restored when the loop exits or
  raises. The HTTP transport is unchanged.

- **`kct agent-guide` commands now work as written** (Issue #5966). The sync
  step now reads `kct sync <pro> --analyze`, then `--apply --dry-run` to
  preview, then `--apply --confirm`; before, it showed a bare `--apply`, which
  refuses to run. The sign-off checklist now passes the board to
  `kct check <pcb> --mfr <fab>` and `kct net-status <pcb>`, which both require
  it. A new test parses every `kct` command line in the guide, rendered for each
  harness, with the real CLI parser and checks known guards such as `--apply`
  needing `--confirm` or `--dry-run`.

- **`kct detect-mistakes` bypass-cap findings name only real ICs** (Issue #5970).
  `BypassCapDistanceCheck` used to treat any part with a power-net pad as an
  "IC", so it reported bypass caps as "too far" from resistors, other caps,
  connectors and fuses. A new shared `is_ic_footprint` classifier
  (`U`/`IC` prefix, or an IC package footprint) restricts the check to ICs.
  Findings across fleet boards 02-09 fell from 1775 to 727; every remaining
  finding names a `U*` part, and the `U*` findings are unchanged.

- **`kct audit` accepts the HV/isolation flags** (Issue #5979).
  `--hv-standard`, `--hv-working-voltage`, `--hv-pollution-degree`,
  `--hv-material-group`, `--hv-min` and `--hv-net-class` were defined only on
  the standalone audit parser, so `kct audit --hv-standard ...` (Gate 4 of the
  `/kct:manufacturing-readiness` skill) failed with "unrecognized arguments".
  The `/kct:hv-isolation-loop` skill's reinforce step now names the real
  command, `kct pcb reinforce <pcb> --net <net> --all-runs`, instead of the
  nonexistent `kct reinforce`.

- **`kct validate --sync` catches net names swapped wholesale** (Issue #5980).
  When every pad of two nets is exchanged (all `SWDIO` pads on PCB net `SWCLK`
  and vice versa), the copper still joins the right pads, so the
  connectivity-based pad check reported `in_sync: true`. The names drive net
  classes, diff pairs, match groups and zone nets, so this is now a
  `net_mismatch` error: `Net names swapped: schematic net X is PCB net Y and
  schematic net Y is PCB net X`. A PCB net carrying the name of a different
  schematic net without a mutual swap is reported as `Net named after another
  net`. Tool-generated `Net-(...)` names never count, and same-named nets on
  different sheets (`/A/CLK`, `/B/CLK`) are not names of each other. A split
  net is also
  worded better. When most of a net's pads moved to another PCB net, the one
  pad still on the right name used to be reported as wrong (`U2.5: schematic
  net '/OSC_IN', PCB net 'OSC_IN' (expected PCB net 'Net-X')`). It now reads
  `schematic net '/OSC_IN' is split on the PCB: this pad is on 'OSC_IN', but
  2 of the net's 3 pads (C10.1, Y1.1) are on 'Net-X'`.
  The split wording is used only when the pad's PCB net carries no other
  schematic net. A pad left on another net's copper is still
  reported as a mismatch.

- **Unflushed native `printf` output can no longer corrupt `kct mcp serve` stdio frames** (Issue #5982). Before restoring stdout, the server now flushes the C library's stdio buffers as well as Python's, so bytes a native library `printf`s without flushing go to stderr rather than onto the JSON-RPC stream at exit.

- **`kct explain` bypass-cap check recognises non-`U` active parts**
  (Issue #5995). `is_ic_footprint` now combines reference prefix, footprint
  library/package, pad count and the symbol-derived pad `pintype`, so
  oscillators (`X`/`Y`/`OSC` in `Oscillator:`), regulators (`VR`/`REG`/`LDO`/
  `PS` in SOT-223, TO-252/DPAK, SOT-89 ...), `Sensor*:` and `Converter_*:`
  parts and large project-local modules (`MOD`/`A`/`M`/`MCU`/`SOM`/`CM`
  prefixes, 16+ pads) are paired with their bypass caps again.
  `Button_Switch*:` DIP switches, connectors (including board-to-board and
  bare `PinHeader_*` footprints), shield cans, magnetics, mounting hardware,
  crystals, trimmers and discrete transistors stay excluded. Footprint-library
  matching is now case-insensitive.

- **`kct detect-mistakes` no longer treats a buck converter's switch node as a
  power rail** (Issue #5998). KiCad's libraries type a switching regulator's
  `SW`/`LX` pin as `power_out`. Once a board carries pin types, the pin-type
  rail evidence (#5939) counted the switch node as a rail. On board 09 this
  produced a "Missing decoupling capacitor" error asking for a capacitor from
  `SW` to ground. Switch-node pins (`SW`, `SW1`, `LX`, `PH`, `PHASE`) and
  bootstrap pins (`BOOT`, `BST`) are no longer rail evidence, and a net that
  carries a switch-node pin is never reported as a rail. The checks read the
  pad's `pinfunction`, which `Pad` now parses. If a pad has no
  `pinfunction`, a switch-node net name (`SW`, `BUCK_SW`) disqualifies its
  `power_out` pins instead. MCU pins such as `PH0` and `BOOT0` still count.
  `classify_net` now classifies a switch node as `HIGH_CURRENT_SIGNAL`
  instead of `POWER`. Board 09's generator now annotates pad pin types, and
  its committed PCB carries them.

- **`kct validate --sync` reports name swaps between same-named nets on
  different sheets** (Issue #5999). A wholesale exchange such as
  `/MCU_A/DBG_LED` <-> `/MCU_B/DBG_LED` was silently accepted because the
  self-name test matched on the leaf name alone; it is now reported as
  "Net names swapped" (error), while legitimate `X` / `/X` / `/Sheet/X`
  spellings and unclaimed sheet-path differences stay clean. An unqualified
  PCB name shared by several sheets is now attributed to the swap partner,
  or to all candidate nets, instead of the alphabetically first sheet.

- **`kct route-auto` no longer writes copper through another net's existing
  tracks and vias** (Issue #6001). None of its strategies sees other nets'
  copper, so every one drew straight through an already-routed net and
  reported success, which wrote a short. Such copper is now refused, the same
  way copper inside a keepout rule area is (#6059).

- **Pin-type annotation covers every placement of a reused sheet** (Issue #6004).
  `kct pcb annotate-pintypes`, `kct pcb sync-netlist` and the board generators
  now resolve each placement's own reference designators from the symbols'
  `(instances ...)` blocks, so when one `.kicad_sch` is placed twice the
  second copy's pads (e.g. `R11`/`R12`) get `pinfunction`/`pintype` too,
  instead of being left blank. `build_pin_to_pad_map` shares the same
  per-placement resolution.

- **Mirrored schematic symbols now place their pins where KiCad does**
  (Issue #6005). The schematic model kept no `(mirror x|y)`, so
  `SymbolInstance.pin_position()` gave every mirrored symbol its unmirrored
  pin positions, and pin-connection checks built on the model misattributed
  pins. Loading and saving a schematic through the model also dropped the
  mirror. The model now reads and writes the mirror axis on symbols and power
  symbols. Pin positions, stub directions, `LibrarySymbol.get_pin_position`
  and the `kct sch tidy` / field-lint body bounding boxes now share one
  rotate-then-mirror transform, which matches `kicad-cli` netlist
  connectivity for all 12 rotation x mirror combinations.
  `get_pin_position` had previously negated the wrong axis, which put
  mirrored symbols at 0 and 180 degrees on their mirror-twin positions.

- **`kct route`'s default grid engine now honours board-file keepout rule
  areas** (Issue #6008). `(zone ... (keepout (tracks not_allowed) (vias
  not_allowed)))` areas were read only by the lattice engine (#4605); grid
  routes could run straight through them. The grid engine now enforces them on
  both the Python and C++ backends -- tracks per layer, vias across the stack,
  each per the area's own flags -- from the same parse the lattice engine
  uses, including the `spatial_keepouts` per-class filters. This holds for
  every grid driver: `route_all*`, `--differential-pairs`, bus-aware and
  block-aware routing and direct `route_net` calls, plus the escape-stub
  generator (#6061). The unrouted-cause report names a rule area that closes off a connection as a `keepout` blocker
  with its zone name, and the #4605 "does not honor" warning now fires only
  for `--route-engine mesh`.

- **Rip-up's relaxed blocker search now finds the nets that are in the way**
  (Issue #6009). Neighbourhood rip-up (#2274) and
  `kct route --ripup-strategy sequential-n1` pick what to rip by re-running a
  failed net's search with every routed trace lifted. That lift used to clear
  only the Python occupancy planes. The paired C++ grid and the route R-trees
  still held every trace, so on both backends the search failed and named no
  blockers, and rip-up never had anything to rip. The lift now goes through the
  same route-resync path as the unrouted-cause diagnosis (#5944), which updates
  both grids and the R-trees. Blockers are attributed only to cells the lift
  actually freed, and the grid is restored exactly afterwards.

- **`kct optimize-placement` models rotated and back-side parts the way it
  writes them** (Issue #6020). The pad transform used the y-up rotation, so
  every 90/270-degree footprint's pads were mirrored relative to the board
  KiCad opens. A back-side footprint's pads, which the board file already
  stores flipped, were mirrored a second time. The overlap, boundary and
  clearance terms ignored rotation, so a turned part was checked as if it lay
  flat, the opposite of the slide-off pass, which could then turn a feasible
  optimum infeasible. The optimizer could also pick a board side the writer
  never applies. Pads now follow KiCad's orientation, bounding boxes swap at
  90/270 degrees, side flags stay as on the board, and `--seed current` never
  returns a placement that scores worse than the one it started from
  (`--max-iterations 0` keeps it outright). Pad-anchored wirelength on rotated
  and back-side parts changes accordingly.

- **Zone fill never leaves a board without fills** (Issue #6023). The
  stale fills stripped before a KiCad refill are now restored on every
  failure path, including `PermissionError`, `KeyboardInterrupt`, and a
  `kicad-cli` run that wrote the DRC report but did not save the board.
  That last case was reported as a successful fill and is now reported as
  a failure. When a write fails while the pour-oracle closer is saving its
  committed routes after an earlier error, the original error now
  propagates instead of the write error.

- **Board 03 (USB joystick) carries schematic pad pin types** (Issue #6025).
  `boards/03-usb-joystick/generate_design.py` now copies each schematic pin's
  name and electrical type onto its pad (`(pinfunction ...)`, `(pintype ...)`)
  right after it writes the unrouted PCB. The committed unrouted and routed
  PCBs, the readiness fill-witness copies, the manufacturing bundle and
  `readiness.json` were regenerated by the board's own producer; the committed
  copper is unchanged and `kct detect-mistakes` findings are identical.

- **Board 09 readiness no longer lists the closed #5061 GND blocker** (Issue #6027).
  `check_design.py` now computes the strict-vs-native connectivity blocker from
  the `net-status` result instead of a hardcoded string; strict net-status is
  32/32 complete, so the blocker is gone from `readiness.json`.

- **`kct detect-mistakes` power-trace width findings are deterministic and
  skip no-load regulator pins** (Issue #6028). Each net now reports its
  *narrowest* segment below 0.3 mm plus how many segments fall below it,
  instead of whichever narrow segment came first in the file; findings are
  sorted by net name. Nets driven only by a `power_out` pin that feed
  nothing but capacitors (an IC's internal-regulator decoupling pins, such
  as the STUSB4500's `VREG_1V2`/`VREG_2V7`) are no longer flagged.

- **`is_switch_node_name` no longer matches `SWD`, `*_SW` load-switched rails or button nets** (Issue #6029). `SWD`/`SWA`/`SWC` and `3V3_SW`, `+5V_SW`, `VBUS_SW`, `BTN_SW`, `KEY_SW` are no longer treated as switching-regulator switch nodes; the trailing `_SW` rule now requires a regulator prefix (`BUCK_`, `BOOST_`, `VREG_`, `Ux_`).

- **`kct validate --sync` reports swaps between a root-sheet net and a
  same-named sub-sheet net** (Issue #6032). A wholesale exchange such as
  `/SENSE` <-> `/ChildA/SENSE` was reported as in sync because the
  root-sheet name `/SENSE` was treated as unqualified. A leading-`/` name
  with no further sheet path is now root-qualified, so the exchange is
  reported as "Net names swapped" (error). Unqualified names (`SENSE`) and
  sheet-path differences that no other net claims are still accepted.

- **`kicad-cli pcb drc` now catches copper inside keepout rule areas**
  (Issue #6039). Headless `kicad-cli` 10.0.1 does not enforce rule areas on
  its own, so a track or via routed through a `(tracks not_allowed)` area
  passed native DRC cleanly. Every `.kicad_dru` kct writes for a board
  (`kct route`, `kct check --emit-drc-constraints`, `kct mfr apply-rules`,
  manufacturing export) now carries one `A.intersectsArea('<zone uuid>')`
  disallow rule per keepout rule area, covering exactly the kinds the area
  forbids (track, via, pad, copper pour, footprint), in its own managed block
  that re-runs replace and that leaves user rules untouched.

- **Schematic load/save no longer produces files KiCad refuses to open**
  (Issue #6048). Labels, hierarchical labels, global labels and text notes
  whose text looks numeric (the Arduino templates' `4`, `13`, ...) were
  written as bare atoms such as `(label 4 ...)`, which `kicad-cli` rejects
  with "Failed to load schematic"; they are now always quoted. Power symbols
  also keep their placed `Value` on a round trip instead of re-deriving it
  from the lib_id, so a `power:+3.3V` placed as `+3V3` no longer renames its
  net. Every KiCad 10 template and every fleet schematic now round-trips with
  an unchanged `kicad-cli` netlist.

- **Loading and saving a schematic no longer rewrites what you did not edit**
  (Issue #6051). `Schematic.load(...).write(...)` on a KiCad-saved sheet
  (format version 20231120 or newer) now reproduces the original
  s-expression tree: label justification, text-note font size and UUID,
  symbol and power-symbol field positions, `Description` properties, wire
  stroke types, junction diameters, `polyline`/`sheet`/`bus` items,
  `embedded_fonts` and the file's format `version` all survive. Edited
  elements are regenerated in place; added ones are inserted before
  `sheet_instances`. Older-format files are still rewritten wholesale at
  kct's format version.

- **`kct route` saves the same pour fill every time it routes the same copper**
  (Issue #6052). Board 03 runs with identical routed copper still ended with
  different pour fills. KiCad's zone fill depends on the order it loads
  tracks in, and `kicad-cli --save-board` writes tracks sorted by UUID. Router
  segment/via UUIDs came from the process-wide random stream, so after the
  first KiCad save every later refill (thermal remediation, each oracle-closer
  round) could load the same copper in a different order and emit slightly
  different polygons. Pads and footprint graphics without a `(uuid ...)` also
  got random ones from KiCad on every run (#5591). `kct route` now gives the
  routed board content-derived UUIDs and a UUID-sorted track order before
  KiCad first loads it, and replaces the UUIDs KiCad invents once the oracle
  stage ends. Three board-03 routes now save byte-identical boards, fill
  included; before, they saved three different fills. UUIDs that came from
  the input board are kept (KiCad 6/7 `tstamp` ids included, and also when
  routing in place with `-o` equal to the input), so only router-minted
  copper and previously UUID-less items get new UUIDs. Expect a one-time change on the next regeneration of each routed board:
  its UUIDs and track order will differ from the committed `.kicad_pcb`, so
  regenerate the board's readiness artifacts along with it. Later
  regenerations of the same copper are stable.

- **`kct check` local net evidence now sees nearby pads on boards whose
  origin is not (0, 0)** (Issue #6055). Unreleased fix to the #6011
  behaviour: the `ev2:` nearby-pad evidence compared finding locations (sheet
  coordinates, as `kct check` reports them) with pad positions relative to the
  board origin, so on most real boards a located finding's net evidence was
  empty and adding or moving a pad right next to a waived finding did not make
  the waiver stale. Pads are now placed in sheet coordinates. The hash stays
  `ev2:` with no version bump: no release has shipped `ev1:` or `ev2:` hashes
  (both postdate v0.22.0), so there is nothing to migrate.

- **Editing a loaded schematic keeps the edited element's KiCad styling**
  (Issue #6057). Moving a label, renaming it, changing a symbol's value or
  reference, retyping a text note, or moving a wire, junction or no-connect
  now changes only the edited atoms in that element's node. Label `justify`
  and font size, text-note font and UUID, symbol field positions and
  effects, `Description` properties, pin UUIDs, the instances `project`
  name, wire stroke type and junction diameter all survive the edit. Moving
  a symbol now moves its fields by the same offset instead of snapping them
  to builder defaults. Elements added to the model (including a deleted
  element re-added as a new object or as a different type) are still built
  from defaults, and files older than format version 20231120 are still
  rewritten wholesale.

- **`--physical-copper-gap` now measures arcs and reports findings in the sheet frame** (Issue #6058). Track arcs were read in sheet coordinates while all other copper was board-relative, so on boards with a non-zero origin arc gaps were mis-measured, findings (`location`, `closest_locations`, message) were board-relative, and ev2 nearby-pad evidence was empty. The rule now measures in one frame and goes through `_absolutize`, which also shifts `closest_locations`.

- **`kct route-auto` honours board keepout rule areas** (Issue #6059).
  `(tracks not_allowed)` / `(vias not_allowed)` rule areas used to constrain
  only `kct route`; every `route-auto` strategy ran copper straight through
  them, with no warning. The `hierarchical` strategy now routes around them,
  using the same parse and grid enforcement as `kct route` (#6008). The
  coarse corridor strategies (`global`, `escape`, `subgrid`,
  `via_resolution`, `multi_resolution`) cannot plan around an area. They warn
  once, and refuse any corridor that enters one instead of writing it.
  `--strategy auto` then retries `hierarchical`. The `hierarchical` strategy
  also no longer escalates past the board's copper layer count, so a 2-layer
  board no longer gets `In1.Cu`/`In2.Cu` tracks.

- **Router bus detection recognises sheet-qualified numeric names** (Issue #6060).
  `/DATA0` and `/sub/ADDR12` now parse as buses (prefix kept in the bus name), so
  `--bus-routing` no longer silently misses real KiCad `/`-prefixed nets.

- **Escape stubs and escape vias now stay out of keepout rule areas**
  (Issue #6061). The BGA / QFN / fine-pitch escape generator and the sub-grid
  escape pre-pass placed copper without consulting `(tracks not_allowed)` /
  `(vias not_allowed)` rule areas, so fan-out stubs and dogbone vias could
  land inside them even though #6008 kept the A* routes out. Escape vias are
  now rejected inside a via keepout (or an all-nets track keepout), and stubs
  inside a track keepout on their layer, using the same grid predicates the
  pathfinder uses, `spatial_keepouts` filters included, on both backends. An
  escape that cannot avoid an area is left to the main router.
  `route_with_subgrid()` and `route_with_adaptive_grid()` now install the
  areas before their escape pre-pass.

- **`Autorouter.route_all_hierarchical()` now honours keepout rule areas**
  (Issue #6062). With the default `use_negotiated=True` the corridor driver
  skipped the install added in #6008, so routes crossed `tracks not_allowed`
  areas on the Python API (not `kct route --hierarchical`).

- **Symbol `extends` flattening is now deterministic** (Issue #6070). `SymbolDef._flatten_with_parent` emitted properties in set-hash order, so schematics using ATtiny85, LM2596, AMS1117 or STM32G431 varied with `PYTHONHASHSEED`. Properties now follow parent order, then child-only keys in child order; netlist point grouping also uses insertion order.

- **Removed elements no longer resurrect when saving a deepcopied or unpickled loaded schematic** (Issue #6071). The lossless-save bookkeeping now holds source nodes instead of `id()` integers, so copies stay independent of the original.

- **Boards 01 and 02 regenerated with deterministic UUIDs and rebuilt by their
  own release scripts** (Issue #6076). New `boards/01-voltage-divider/package_release.py` and
  `boards/02-charlieplex-led/package_release.py` (shared helper
  `boards/release_package.py`) rebuild each shipped package through
  `kct readiness --generate`. They carry hand-curated files forward only while
  they are still valid, and they refuse to drop any file the current package
  ships. Board 02 also re-runs its `firmware`, `smt_drill_clearance` and
  `native_erc` checks. Board 02's CPL now applies the JLCPCB `LED_0805*` 180°
  rotation correction to D1–D9, which the September package missed. Its
  generator again writes full `Library:Footprint` IDs and a pinned title-block
  date. `kct readiness` gains board-recipe hooks (`Engines.package_extras`,
  `extra_checks`, `extra_inputs`). It now writes `manufacturing.zip`
  byte-reproducibly and records the checked PCB in `kct-check.json` relative
  to the board, not under its temporary candidate path.

- **Saved zone fills are no longer split; gates and Gerber export now check the
  saved fill** (Issue #6078). Two causes made the fill saved in a routed board
  differ from a fresh KiCad fill. First, the foreign-pad clearance carve run
  after KiCad's fill cut a slit from every hole to the nearest outline point;
  on pours with many holes these slits crossed other holes, and the island
  filter then dropped the cut-off copper. On board 03 the F.Cu GND pour went
  from 4230 mm² to about 2470 mm² in 16 pieces. Second, `kct route` filled
  zones before writing the routed board's manufacturer `.kicad_pro`/`.kicad_dru`,
  so the fill used the source project's clearances and came out in 6 pieces
  under the rules it shipped with. Every gate refilled before checking
  (`--refill-zones`), so none of them saw this, but `kicad-cli pcb export
  gerbers` plots the saved fill. The carve now encodes holes the way KiCad does
  (one ring with zero-width bridges, so no copper is removed) and refuses any
  carve that would split a pour's anchored copper. `kct route` now writes the
  sidecars before filling. The board-recipe pipeline gate
  (`evaluate_pipeline_gate`) adds a saved-fill leg (DRC without refill,
  compared with the refilled run), and Gerber export (`GerberExporter`,
  `kicad-export-gerbers`) verifies the saved fill before plotting. All
  committed `boards/*/output/*_routed.kicad_pcb` pass the new check.

- **Rotating a loaded symbol now rotates its fields with it** (Issue #6085).
  Reference/Value field positions turn about the symbol origin (after any
  move) and their text flips between horizontal and vertical, as in KiCad.
  Also fixes the dead pin-UUID preservation guard in `_patch_node`, and a
  retyped text note now keeps its own font and UUID when another deleted note
  carries the new text.
  Mirrored symbols (`(mirror x|y)`) rotate their fields the opposite way, as
  their symbol does, and an edit that changes mirror and angle together moves
  fields per the new mirror state.

- **Footprint-embedded keepouts are now enforced by `kicad-cli pcb drc` and
  the router** (Issue #6087). The #6039 `.kicad_dru` keepout rules only
  covered board-level rule areas, so a track through an RF module's antenna
  keepout (a rule area defined inside the footprint) still passed native DRC
  and the router could route through it. kct now emits an
  `A.intersectsArea('<zone uuid>') && !A.memberOfFootprint('<ref>')` rule for
  each footprint keepout, so the footprint's own pads stay exempt as they are
  in KiCad. The grid and lattice routers also treat these areas as obstacles.
  Pre-KiCad-7 zones that carry `(tstamp ...)` instead of `(uuid ...)` are keyed
  on the tstamp. Zone names containing a backslash no longer produce a rule
  that never matches.

- **`kct route --lint-gate` no longer leaves an unjudged board at `--output` when killed** (Issue #6090). Routing now writes a staging file in a hidden `.<output>.lint-gate-*` directory next to `--output`, and the board is moved onto `--output` atomically only after the gate passes. A `timeout`/CI SIGTERM keeps `--output` at its pre-run bytes (or absent), moves the routed board to `<output-stem>.lint-rejected.kicad_pcb`, prints a `[lint-gate]` message and exits 143. A SIGTERM during promotion is held until `--output` and its sidecars are all in their final state. After SIGKILL, `--output` is also untouched and the unjudged work stays in the staging directory until the next gated run to that `--output` removes it. The same applies to `kct route-auto --lint-gate`. `--checkpoint` may no longer name the `--output` path under `--lint-gate`.

- **`kct route` no longer fails a board on pour links only the source project
  strands** (Issue #6095). The KiCad-oracle pour completion now judges the
  routed board under the `.kicad_pro` / `.kicad_dru` it ships with. Since
  #6078 that is the emitted manufacturer project, and its fill is the one
  saved in the board. On board 03, the source project's wider 0.15 mm
  clearance stranded C8.2 and C10.2 on the F.Cu GND pour, so the oracle
  reported 2 links that the shipped rules connect and that the closer could
  not close (`no_closer`). The route exited 3 (`ROUTING FAILED: pour pads left
  unconnected`); it now converges with 0 links. The source project is still
  merged per net, but only when no project ships next to the routed board.

- **8+-layer boards are routed on their real copper stack** (Issue #6099).
  `detect_layer_stack` used to treat every board that was not 2, 4 or 6
  layers as a 2-layer board, without warning. As a result, `kct route` routed
  an 8-layer board on F.Cu/B.Cu only, a keepout on `In3.Cu` constrained
  nothing, and the `route-auto --strategy hierarchical` layer cap (#6059)
  dropped to 2. Any copper count up to KiCad's 32 is now built from the
  board's declared layers, in physical order and with the KiCad 10 format's
  non-contiguous layer ordinals. An inner layer counts as a plane when KiCad
  declares it `power` or a zone sits on it; otherwise it is a signal layer.
  The router's layer enum now reaches `In30.Cu`. The hierarchical ladder and
  `kct route --auto-layers` (`--max-layers` now accepts 8–32) end on the
  board's own stack. Detection for 2-, 4- and 6-layer boards is unchanged.

- **Native-fill multilayer tests skip with a version reason on KiCad < 10.0.2** (Issue #6101). Local `multilayer=True` native-fill cases no longer fail with an empty fill on older pcbnew; new `multilayer_fill_supported()` helper gates them.

- **Preserved copper now reaches the C++ routing grid** (Issue #6103).
  `load_pcb_for_routing(load_existing_routes=True)` (behind `kct route --nets`,
  `--preserve-existing` and `--resume`) marked existing tracks and vias on the
  Python grid only, so C++ A* routed through them and fell back to the slow
  Python pathfinder. Both grids now start in agreement.

- **`kct route-auto` no longer writes copper that shorts or violates clearance against other nets** (Issue #6107). Hierarchical routing now treats other nets' pads, arcs, tracks and vias as obstacles, and a new output gate refuses routes that touch or come within the board's DRC clearance of another net, naming the conflicting item.

- **Pure-Python router fallback no longer commits vias too close to
  foreign pads** (Issue #6117). The Python A* post-route validator checked
  vias against traces, vias, drills and holes, but never against pads. When
  the C++ search rejected a via for via-to-pad clearance and fell back, the
  Python path could accept the same via. On board 03, a `BTN1` via landed
  0.125 mm from `BTN2`'s `U1.19`, where 0.15 mm is required. The fallback
  now applies the same via-to-pad check as the C++ validator, with the same
  same-component carve-outs. It either finds a legal via site or reports
  the net as failed.

- **Footprint keepouts now move with their footprint** (Issue #6119). KiCad
  stores zones embedded in a footprint, such as an RF module's antenna keepout,
  at their final board position. Moving, rotating or flipping a footprint
  through kct used to leave those zones where they were, so the router and
  `.kicad_dru` blocked the old spot and left the real antenna area open. Every
  kct move path now transforms them (outline and fill) exactly as pcbnew does:
  `PCB.update_footprint_position`, direct `position`/`rotation`/`layer`
  assignment, `kct pcb modify move|rotate|flip`, the mirror strategy,
  `optimize-placement` and its MCP tool, the placement fixer, and DRC
  clearance repair. `update_footprint_position` and `pcb modify rotate` also
  turn pad angles with the footprint, since KiCad stores those board-absolute.
  `pcb modify rotate` no longer silently drops the angle on a footprint that
  was at 0°. The `optimize-placement` writers (CLI and MCP) and
  `update_pcb_placements` no longer rewrite pad and property positions on
  KiCad 8+ boards in place of the footprint's own position.

- **`kct route-auto` resolves clearance per net pair, as KiCad does** (Issue #6122).
  The foreign-copper gate used one board-wide clearance. Now each pair gets
  the larger of the two nets' netclass clearances, so an `HV` class is
  enforced. The last matching `.kicad_dru` rule overrides that value, so an
  unconditional custom rule below `Default` is honoured instead of refused.
  Conditions on `NetName`, `NetClass`, `Type`, `Pad_Type`, `hasNetclass()`
  and `isPlated()` are evaluated, with KiCad's case-insensitive wildcard
  matching. Any other condition is applied at its larger value. The `hierarchical` strategy routes each net at the largest pair
  clearance it needs.

- **`kct panel` copies are now rigid translations of the source board**
  (Issue #6125). Footprint-local pad and text positions are no longer
  shifted a second time; footprint-embedded keepouts/pours and board-level
  `zone` / `gr_poly` outlines (`xy` vertices), arc tracks and `gr_circle`
  centres now move with their copy; and content lands inside its own
  Edge.Cuts cell instead of being displaced by the board origin. Pad and
  zone net names get the same `B<n>/` prefix as the panel net table, so pads
  no longer fall onto `<no net>`. Per-board rotation is applied (multiples
  of 90 degrees; other angles are rejected).

- **Footprint text now turns with its footprint** (Issue #6126). KiCad
  stores the angle of a footprint's `property` and `fp_text` nodes
  board-absolute, like pad angles. Rotating or flipping a footprint through
  kct used to leave those angles stale, so silkscreen and fab text was drawn
  at the old orientation. Every rotate path now matches pcbnew 10:
  - Absolute rotation writes add the rotation delta, as `SetOrientation`
    does. These are `rotation` assignment, `PCB.update_footprint_position`,
    placement writeback, `kct pcb move-footprint` and `kct pcb snap-rotation`.
  - Relative rotates also turn "keep upright" text back upright, as
    pcbnew's rotate does. These are `kct pcb modify rotate` and the recovery
    rotate strategy.
  - A left/right flip maps text angles `a -> -a` and then applies the same
    keep-upright step. These are `kct pcb modify flip` and the mirror
    strategy.
  The keep-upright step turns text that sits at 180° or more by 180° and
  swaps its left/right and top/bottom justification. Text marked
  `(unlocked yes)`, or the legacy `unlocked` token, is left alone. Before
  this fix, the mirror strategy wrote `180 - a`, which put unlocked text and
  some keep-upright text 180° off. `kct pcb snap-rotation` now also turns
  pad angles by the snap amount.

- **Router proven-unreachable memo fingerprint now covers corridor reservations and fill/keepout contents** (Issue #6133). The #6098 memo hashed fixed fills and keepouts by identity only and ignored reservations, the net-name map and attach zones; a mid-run change to any of them now invalidates a stale proof.

- **`kct route-auto` no longer writes tracks through drilled holes** (Issue #6139).
  Corridor strategies such as `global` could draw a track straight through a
  bare NPTH mounting hole or slot, which KiCad flags as `hole_clearance` (and,
  for a slot, `copper_edge_clearance`). The output gate now measures new copper
  against every drilled hole (NPTH holes and oval slots, plated pad drills,
  via drills) at the board's hole clearance, and against a non-plated slot at
  its edge clearance. A new via's drill is checked against other nets' copper
  at the hole clearance too.

- **`kct panel` writes a valid, closed Edge.Cuts outline** (Issue #6143).
  Tabs used to be drawn as two loose side lines that never opened the board
  rectangles they joined, so `kicad-cli pcb drc` reported 18 `invalid_outline`
  errors on a default 2x2 panel. The outline is now the boundary of the union
  of board copies, tabs and frame rail, so every tab splices into the board
  edge and the neighbouring board or rail. Tabs between side-by-side boards
  (and to the left/right rail) no longer reach 0.5 mm into each board, and
  `--frame` no longer adds rail tabs that ran straight through neighbouring
  copies. Boards 01 and 02 now panelise 2x2 (default, `--frame`, `--cut vcut`)
  with 0 `invalid_outline` and 0 `copper_edge_clearance` after `--refill-zones`.

- **`kct route -o <path>` honours the input board's `fabrication_overrides.json`** (Issue #6147). The sidecar was probed only relative to the output path, so routing to a scratch directory silently reverted a reviewed floor (e.g. board 03's 0.45 mm hole-to-hole) to the profile default and produced phantom `hole_to_hole` DRC errors. The output location is still probed first (nearest wins); the input board's location is the fallback. The override stays scoped to its declared manufacturer, so pass `--manufacturer jlcpcb-tier1` for board 03.

- **`kct route-auto` no longer marks preserved copper on the C++ grid twice** (Issue #6148).
  The hierarchical board-grid loader re-mirrored every track and via the shared
  loader already mirrored (#6103); it now mirrors only the arcs it adds itself.

- **`kct route-auto`'s clearance gate follows more of KiCad's DRC rules**
  (Issue #6150). A `.kicad_dru` rule with `(severity ignore)` now switches the
  check off for the pairs it matches, as KiCad does, instead of being
  enforced. A `.kicad_dru` that kicad-cli cannot parse (a syntax error, or a
  constraint or severity it does not know, such as `solder_mask_margin`) is
  now reported with a warning; kicad-cli silently ignores every rule in such a
  file, and the gate now does the same instead of enforcing them. Conditions
  on `A.Layer` / `B.Layer` are evaluated (a track's layer; a via's or pad's
  `Layer` is KiCad's null). The gate also checks hole-to-hole
  spacing of new vias (`hole_to_hole`, `min_hole_to_hole`), and the
  `physical_clearance` and `physical_hole_clearance` rules. Each was checked
  against `kicad-cli pcb drc` 10.0.1. The board 06 and 07 `regression-fixture`
  `.kicad_dru` files were regenerated; their stale 0.4 mm `hole_clearance`
  rule made the gate refuse 16 of board 06's 26 routed nets.

- **A relief rescue no longer trades a stranded net for a half-routed one**
  (Issue #6151). When the router rips nets out of the way to rescue a
  stranded net, it puts each displaced net back afterwards. A displaced net
  now counts as put back only if every one of its connections landed.
  Previously, a net that ran out of its reduced 10 s budget partway through
  was accepted with pads still stranded, so the rescue committed and lost a
  net. On a loaded machine this left board 03 at 23/24, with `USB_D+`
  stranding 3 of its 5 pads.
  A displaced net that runs out of time is now retried once under the budget
  it was originally routed with: `--per-net-timeout`, or the node-expansion
  cap under `--deterministic-budget`.

- **`kct export` / `kct gerber` ship V-score lines for V-cut panels** (Issue #6156).
  Since #6143 moved `kct panel --cut vcut` score lines to `Cmts.User`, the
  Gerber exporter never plotted that layer, so a V-cut panel reached the fab
  with no score data at all. The exporter now adds any user drawing layer
  (`Cmts.User`, `Dwgs.User`, `Eco1/2.User`, `User.N`) that carries straight
  lines spanning the board outline -- written by kicad-cli as
  `<board>-User_Comments.gbr` (`FileFunction Other,Comment`) and listed in the
  `.gbrjob`. Ordinary notes on those layers do not trigger it; set
  `GerberConfig.include_vscore=False` to opt out. `kct panel` gains
  `--vcut-layer {Cmts.User,Dwgs.User,Eco1.User,Eco2.User}`, and `-o` may now
  point into a directory that does not exist yet.
- **Mousebite end holes no longer straddle the routed slot** (Issue #6156).
  The first and last hole of each tab were centred on the tab's side edges, so
  half of each sat in the slot. They are now inset by the hole radius
  (`MousebiteConfig.offset` is the extra gap beyond that).

- **`kct panel` keeps rounded and shaped board outlines** (Issue #6158).
  Each panel copy used to be outlined with its bounding-box rectangle, so
  a board with rounded corners was panelised, and routed by the fab, with
  square ones. The panel Edge.Cuts is now built from each copy's real
  outline, including chamfers and interior cutouts. Corner arcs come out
  as real `gr_arc`s, not chord polygons. A tab that would land on a curved
  corner slides onto the nearest straight run of that edge, or is dropped
  with a warning when no straight run is wide enough. A V-cut seam (or a
  `--frame-space 0` rail) between sides that are not straight end to end
  cannot be butted without leaving unroutable cusps, so it gets the
  default 2 mm gap and is tab-routed with mousebites, with a warning.

- **Gerber manufacturer presets no longer carry a file-rename map that was
  never applied** (Issue #6163). `GerberManufacturerPreset.layer_rename`, which
  the JLCPCB and Seeed presets populated, was never read, so every preset
  already shipped kicad-cli's own names (`board-F_Cu.gtl`,
  `board-Edge_Cuts.gm1`, `board-job.gbrjob`). The map and the field were
  removed, not wired up. The JLCPCB map was identical to kicad-cli's
  output. JLCPCB, PCBWay and OSH Park document taking KiCad's default names,
  and Seeed's own KiCad tooling exports them unrenamed. Exported file names are
  unchanged. A new test fails if any `GerberConfig` or
  `GerberManufacturerPreset` field is not read by the exporter.

- **`kct panel --cut vcut` butts the boards instead of gapping and tabbing
  them** (Issue #6164). V-cut panels used the mousebite layout: copies 2 mm
  apart with breakaway tabs, and each score line down the middle of the
  empty routed slot. Copies now sit edge to edge (and against the rails
  with `--frame`), with no tabs. The Edge.Cuts outline is one rectangle per
  butted block, and each score line lies exactly on the shared edge. A seam
  that keeps a gap (`--spacing`, `--frame-space`, or the new
  `--spacing-x`/`--spacing-y` for a mixed panel that scores one axis and
  tabs the other) is tab-routed with mousebites and reported. New
  `--vscore-clearance` (default 0.4 mm) warns about source-board copper
  that close to a scored edge, and flanks each score with a copper-pour
  keepout so a zone refill stops short of the blade.

- **V-score layer detection no longer misses KiKit or old-format panels** (Issue #6165).
  `kct panel` now tags its score lines so the Gerber exporter finds them
  explicitly; the geometric fallback for third-party panels accepts lines that
  overshoot the outline (KiKit's ~3 mm) and unquoted layer tokens, and ignores
  untagged lines lying on an outline edge (e.g. a drawing border). Partial or
  jump scores of third-party panels are still not auto-detected.

- **Gerber drill export passes only flags kicad-cli accepts** (Issue #6167).
  `GerberExporter` sent `--merge-npth` and `--minimal-header`, which
  `kicad-cli pcb export drill` does not have, so the OSH Park preset always
  failed with "Drill export failed" and every other preset silently ignored
  its separate-PTH/NPTH setting. `merge_pth_npth=False` now maps to
  `--excellon-separate-th`, `minimal_header` to `--excellon-min-header`,
  `drill_format="gerber_x2"` to `--format gerber`, and the new
  `drill_units` / `drill_zeros_format` fields to `--excellon-units` /
  `--excellon-zeros-format` (`PCB.export_drill(units=...)` was previously
  ignored). A drill export that exits cleanly without writing its drill
  files now raises `ExportError`, and the MCP `export_gerbers` tool no longer
  mutates the shared manufacturer preset.

- **`generate_dru` no longer crashes on 2-layer profiles with `target_ampacity`** (Issue #6171). The
  internal-layer ampacity rule (and the matching internal check in the ampacity validator) is now
  skipped when the stackup has no inner copper; only the external-layer width rule is emitted.

- **Routed duplicate-reference footprints keep their physical identity; fill
  worker errors are readable; in-process runs stop leaking signal handlers**
  (Issue #6175). The #6052 UUID canonicalization gives each UUID-less
  footprint a UUID derived from its index. That switched the footprint's
  internal physical key from `index:N` to `uuid:...`, so a placement
  disposition computed from the source board no longer named the routed
  board's repeated/empty-reference footprints. Those canonical UUIDs now keep
  the `index:N` key. When the KiCad-Python selective zone-fill worker fails,
  the error now includes its stderr instead of a bare "exit status 1" (#6101).
  The worker also runs on KiCad 10.0.1, whose `ZONE.Duplicate()` takes no
  argument. `kct optimize-placement` no longer leaves its SIGINT/SIGTERM
  handler installed after an early return, and `kct route` restores SIGTERM
  when an escalation flow raises. Before, a later SIGTERM in the same Python
  process (MCP server, library caller) exited 130.

- **`PCB.export_drill()` now writes drill files only** (Issue #6180). It
  previously also plotted copper Gerbers and returned `gerbers.zip`; it now
  writes just the `.drl` files and returns the output directory, as documented.
  `GerberConfig` gains `generate_gerbers` (default `True`) to skip the Gerber plot.

- **The trace optimizer no longer creates cross-net shorts when `rtree` is
  not installed** (Issue #6184). `rtree` is an optional extra, and without it
  the optimizer falls back to a grid collision checker. That checker ran its
  exact clearance measurement only when its raster walk met a cell owned by
  another net. A raster cell has one owner, though, so where two nets'
  clearance halos overlap the neighbour's copper could be invisible. On board
  03, `compress_staircase` moved USB_D+ to 0.105 mm from USB_D-, the
  post-optimize backstop demoted USB_D+, and the board finished 12/13. The
  fallback now measures every candidate against routed copper with the same
  clearance kernel as the rtree checker. A small spatial index keeps the
  check cheap. With `rtree` installed nothing changes.

- **The pad-access invariant no longer treats a through-hole pin as stranded
  while its other layers are open** (Issue #6185). Access sets enumerated exit
  stubs on the pad's own layer only, but the router starts and ends a search on
  a through-hole pad on every routable layer. When a commit closed a THT pin's
  last exit on that one layer, the gate refused it. On board 03 this refused
  USB_CC1's relief probe for "stranding" J1.A6 (USB_D+), whose In1, In2 and
  B.Cu exits were all still legal, and plain `kct route` finished 23/24. A
  through-hole terminal's stubs, and the via sites at their ends, are now
  enumerated on every copper layer. SMD terminals are unchanged. The access
  witness's stub counts for THT pins grow to match.

- **`kct export` detects partial and jump V-scoring on third-party panels** (Issue #6193).
  An untagged score line now counts as a V-score when its pieces cover every
  stretch of board material along the line and its gaps lie only in slots,
  cutouts or the space between boards. Its layer is then plotted. Examples
  are a score that hands off to a routed slot, or a seam that jumps the gap
  between boards. Section dividers, fold lines, dashed lines and lines that
  end on a mounting hole or connector slot with board beyond it are still
  ignored. For layouts the geometry cannot prove, the new `--vscore-layer
  LAYER` option on `kct export` and `python -m kicad_tools.cli.export_gerbers`
  (`GerberConfig.vscore_layers`) plots the named layer. It warns when the
  board does not define that layer.

- **`kct route-auto`'s clearance gate reads a pad's `A.Layer` the way current
  KiCad does** (Issue #6196). The gate treated a pad's `Layer` as KiCad's null.
  That is true only for kicad-cli 10.0.1. From 10.0.2 (CI pins 10.0.6) it is
  the layer of the pad's footprint (see #6201), so a rule such as
  `A.Type == 'pad' && A.Layer != 'Edge.Cuts'` applies there. The gate let
  through copper that kicad-cli 10.0.6 then flagged as `hole_clearance`.
  A pad-layer comparison is now decided only when both KiCad releases agree on
  it (`B.Layer == 'Edge.Cuts'` is never true for a pad); otherwise the gate
  assumes the rule may apply.

- **`kct route-auto`'s clearance gate reads a pad's `A.Layer` as its
  footprint's layer** (Issue #6201). kicad-cli 10.0.2 and later report the
  layer of the pad's footprint, not the pad's own copper. The gate used the
  pad's copper layer, so a `B.Cu` pad in an `F.Cu` footprint (card-edge
  fingers, bottom-side test points) passed a rule such as
  `A.Type == 'pad' && A.Layer == 'F.Cu'` that kicad-cli 10.0.6 then flagged as
  `clearance`. The gate now uses the footprint's layer, and every copper
  layer when it does not know it.

- **Native zone fill no longer silently ships empty multilayer pours on old KiCad** (Issue #6213). `fill_around_fixed_copper` now raises an error naming the required (>= 10.0.2) and found pcbnew versions when a multilayer zone would be filled by an older pcbnew, and `find_kicad_python` tolerates cold-start probe timeouts (20 s, one retry).

- **`kct route --auto-mfr-tier` escalates to a via-in-pad tier again, and
  only when that tier can build via-in-pad at the board's layer count**
  (Issue #6217). Since #5189, a lateral off-pad via that rescued a fine-pitch
  pin hid the missed via-in-pad rescue, so plain `jlcpcb` reported none and
  the ladder never reached `jlcpcb-tier1`. Those pins are counted again. The
  escalation guard now checks the layer counts the run can reach
  (`--max-layers`, `--starting-layers`, the board's stackup) against each
  tier's real via-in-pad process. JLCPCB's POFV needs 4+ layers, so a 2-layer
  run no longer escalates on that signal. Instead it prints why
  (`No via-in-pad gain from jlcpcb-tier1: ... offers via-in-pad only at >= 4
  copper layers ... raise --max-layers to 4`; the hint names the layer
  ladder instead when `--max-layers` is not the limit). The fine-pitch
  fallback rung is also skipped at layer counts where the tier has no
  via-in-pad process.
- **`--per-net-iterations` / `--deterministic-budget` now bound the
  `--backend python` A\*** (Issue #6217). The per-net node-expansion cap
  (#3881) only reached the Python search when it ran as the C++ backend's
  fallback, so a pure-Python run silently stayed wall-clock-bound (and had no
  per-net bound at all under `--deterministic-budget`, which disables
  `--per-net-timeout`). The escape summary line also names how many escapes
  used an in-pad via (`Escape routes: U2 (QFP) - 8 pins escaped (3
  via-in-pad)`) when any did.

- **Symbol registry no longer returns the wrong symbol for KiCad 7 libraries** (Issue #6218). Symbol extraction now uses a paren-depth-aware scan instead of tab-indentation regexes, so space-indented `.kicad_sym` files resolve `Device:C_Small` etc. exactly. Unresolvable symbols raise `SymbolNotFoundError`, unparseable libraries raise `ValueError`, and a missing library raises `LibraryNotFoundError` (a `FileNotFoundError` subclass, so `SymbolInstance.from_sexp`'s placeholder fallback now catches it).

- **`kct route --preserve-existing` no longer fails on same-net power fanout
  vias** (Issue #6225). The post-route short check compared pad and copper net
  ids only, but pads on skipped pour nets (GND, +3V3) carry net id 0, so a
  preserved +3V3 via overlapping its own +3V3 pad was reported as a 0.075 mm
  cross-net short and the run exited 3 (boards 06a/06b) while kicad-cli and
  `kct check` were clean. Skipped-net pads now match by net name; genuine
  cross-net overlaps are still flagged.

- **`kct route --preserve-existing` now catches kept copper that shorts a
  foreign pad** (Issue #6229). The post-route short check compared kept input
  traces only against newly routed copper, so a kept +3V3 trace laid across a
  GND pad went unreported and the run printed SUCCESS while kicad-cli DRC
  reported `shorting_items`. Kept traces and vias are now checked against every
  other net's pads (same-net matching as in #6225). Such a short is reported as
  "pre-existing short in input board (kept by --preserve-existing)", with the
  pad named (`J3.1`), and the summary counts input defects and routing failures
  separately.

- **`kct route --preserve-existing` now checks kept copper against kept copper,
  and for clearance** (Issue #6237). Copper kept from the input board was
  checked only against routed copper and (since #6229) for shorts against other
  nets' pads, so two kept traces of different nets crossing, a kept trace
  through a kept via, or two overlapping kept vias printed SUCCESS while
  kicad-cli DRC reported `tracks_crossing` / `shorting_items`, and a kept trace
  0.075 mm from a foreign pad passed while kicad-cli reported `clearance`.
  Kept-vs-kept overlaps are now reported as "pre-existing short in input board
  (kept by --preserve-existing)", and kept copper closer than the required
  clearance to another net's pad or kept copper is reported as "pre-existing
  clearance violation in input board (kept by --preserve-existing)" with the
  gap and the requirement. Gaps use the shared clearance kernel (real pad
  shapes) and the requirement and severity are those of the written board's
  `.kicad_pro` / `.kicad_dru`, the rules kicad-cli applies to it.
  Both checks, and the #6229 kept-vs-pad short check, now also cover
  KiCad 10 name-only boards (`(net "NAME")`), whose kept copper they
  previously skipped entirely.

- **`kct route` no longer prints `Status: SUCCESS` before a failed verdict** (Issue #6239). The
  per-attempt `Status:` line of the layer/strategy escalation loops, printed before the post-route
  short / stranded-pour / zone-fill gates run, now reads `ATTEMPT COMPLETE (provisional - final
  verdict follows)`; only the final banner says `SUCCESS`. `--format json` now carries the same single
  verdict as `summary.verdict` (`success` / `failed`, matching exit code 0 vs non-zero).

- **`kct route` enforces the project's stricter netclass clearances** (Issue #6243). A net in a `.kicad_pro` netclass stricter than the base clearance is kept `max(clearance[a], clearance[b])` from every foreign pad, trace, via and preserved fill, which is KiCad's netclass rule. Before, the router read only `Default` and could route a 0.5 mm `HV` class at 0.45 mm. The check runs in search on both backends (Python and C++, `BUILD_VERSION` 48), on the lattice and mesh engines, in the coupled diff-pair search, at commit, in the optimizer and escape stubs, and in a final pass that demotes any net still below its class. Diff-pair gaps, component relief and HV attach zones cannot lower it. Netclass resolution matches KiCad 10.0.6 on 36 `kicad-cli` oracle cases. With #6191's default DRU, `kicad-cli pcb drc` checks the same classes on the routed board. Projects with only a `Default` class route exactly as before.

- **`kct check` enforces stricter `.kicad_pro` netclass clearances on routed copper** (Issue #6249). New
  `netclass_clearance_copper` category uses the router's resolver
  (`authored_netclass_clearances`) and pair rule, requiring
  `max(fab floor, class[a], class[b])` between foreign coppers, so a 0.45 mm gap
  to a 0.5 mm `HV` class now fails as `kicad-cli pcb drc` already did. An
  unsupported netclass declaration is reported as `netclass_clearance_unsupported`
  rather than skipped. Projects with only `Default` are unchanged. It also errors
  under `KCT_PRESERVE_BOARD_RULES=0`, where `kicad-cli` is silent; the message says so.

- **`kct optimize-placement --seed current` no longer nudges a legal hand
  floorplan** (Issue #6250). Slide-off pushed apart any pair of parts closer
  than its 0.5 mm margin, even when the pair broke no rule, and its moves
  were not on any grid. On board 04's DRC-clean floorplan it moved the
  LQFP-48 U2 by 0.0275 mm (U2 sits 0.45 mm from C11), and the fine-pitch
  escapes no longer routed: 6/9 signal nets instead of 9/9. On a warm start
  slide-off now only moves parts that overlap or sit closer than the
  objective's 0.2 mm clearance rule, still pushes those out to the full
  margin, and rounds each move to the 0.05 mm placement grid. A feasible
  floorplan comes back unchanged, so `--no-slide-off` is no longer needed to
  keep it. Generated seeds (`force-directed`, `random`) are unchanged.

- **MCP `optimize_placement` now pulls decoupling caps onto their supply pins** (Issue #6253).
  The tool shares the CLI's code path with `kct optimize-placement`, so it applies the
  `decoupling` affinity term (default weight 2.0) and the post-optimize snap pass, and
  accepts the same opt-out (`weights={"decoupling": 0}`). Same board and seed now give the
  same placement through either front-end. The result gains `decoupling` and
  `decoupling_snap_moves`, and scoring uses the CLI's lexicographic cost mode.

- **`kct route-auto` runs the authored-netclass final pass** (Issue #6254). A result is now
  judged by the same `demote_authored_floor_violation_nets()` census `kct route` uses, and the
  foreign-copper gate resolves a multi-class net through the shared KiCad 10 priority resolver,
  so both commands agree on a net's minimum clearance. `Default`-only projects are unchanged.

- **`kct optimize-placement` re-anchors reference text the decoupling snap
  disturbs** (Issue #6256). The #6020 snap moves a cap by its footprint origin
  only, so the cap's reference could land on a neighbour's pad or silk, and the
  cap's pads could land under a neighbour's reference, leaving
  `silk_over_copper` / `silk_overlap` warnings. After writing, the
  `place-silk-refs` solver now moves just those references (the snapped caps
  and the neighbours they collide with); every other reference is left
  byte-identical, and nothing runs when the snap moves nothing. A reference
  with no clear spot is left in place and named in the summary and in the new
  `snap_silk_repair` JSON field, never hidden or resized. The MCP
  `optimize_placement` tool does the same.

- **`kct route --preserve-existing` no longer optimizes tracks through kept copper** (Issue #6258). Before the trace optimizer ran, the router rebuilt the routing grid from the newly routed nets only. That dropped every via and track kept from the input board, so the optimizer could not see them. On the board 06 LVDS demo it straightened OUT4 through a GND stitching via on B.Cu, a 0.312 mm short, and still reported `Nets routed: 8/8`. Kept copper is now registered again before the post-route passes. A routed net whose copper still shorts another net after those passes, including against vias and kept copper, is demoted to unrouted instead of counted as routed. The assembled demo's generator no longer writes its own `diffpair_test_routed.kicad_pro`; `kct route` refused that file as a DRC sidecar conflict. The demo now builds with 8/8 nets routed and 0 errors from both `kicad-cli pcb drc --refill-zones` and `kct check`.

- **`kct route`, `kct route-auto` and `kct check` accept projects with no `net_settings` schema stamp again** (Issue #6262).
  The authored-netclass clearance resolver added for #6243 rejected a
  `.kicad_pro` whose `net_settings` had no `meta.version`
  (`Unsupported net_settings schema: None`) and any netclass pattern
  containing `/`. A missing or `null` `net_settings` now means "no authored
  netclasses", an unversioned block is read as the current schema exactly as
  KiCad loads it, and hierarchical patterns such as `/Power/*` are matched
  literally. A stated but unsupported schema still fails explicitly.
- **`kct optimize-placement` no longer moves locked footprints** (Issue #6262).
  Since its writer began persisting real moves (#6119), a footprint carrying
  KiCad's `locked` flag -- the perimeter anchor the `--anchor-weight` recipe
  relies on -- was optimized and written like any other part. Locked
  footprints now keep their board position, rotation and side through every
  score, the slide-off passes and the decoupling snap, and are never
  rewritten; the MCP `optimize_placement` tool behaves the same.

- **`kct route --pop-size` / `--generations` are no longer silently dropped** (Issue #6267).
  The outer `kct route` parser accepted both flags but never forwarded them to the inner
  route command, so `--strategy evolutionary` always ran the default 20 x 10 GA. They now
  reach the evolutionary optimizer.

- **Board 04's readiness report is re-issued by a gated producer and no
  longer claims a clean native DRC it did not have** (Issue #6269). U2's
  reference text moved clear of pads 41-44, removing KiCad's four
  `silk_over_copper` warnings, and `scripts/boards/refresh_readiness.py`
  gained a board-local check hook so board 04 is gated by its own
  paid-drill `check_manufacturing.py` instead of `kct check --mfr
  jlcpcb-tier1`.

- **Router enforces the via clearance floor in both insertion orders** (Issue #6272).
  When `trace_clearance` and `via_clearance` differed, a trace routed past an
  already-placed via was held only to the trace clearance, while a via placed
  next to an existing trace was held to the via clearance -- so the same copper
  pair could pass or fail depending on which net routed first, and a diff-pair
  partner's tighter intra-pair gap could relax a via barrel. Every trace-vs-via
  gate (Python and native search, commit validation and negotiated
  revalidation) now uses one shared floor, `max(trace_clearance, via_clearance)`,
  and a barrel keeps that floor even beside its diff-pair partner. `kct route`
  keeps `via_clearance` at 0.20 mm, so boards routed with a smaller trace
  clearance now keep traces 0.20 mm from foreign vias and may use more vias or
  copper (board 02: 27 -> 52 vias). Requires a native rebuild
  (`kct build-native`; router build version 49).

- **`kct route --timeout` now ends with a saved board instead of a killed run**
  (Issue #6273). Routing used to spend the whole hard budget, so the optimize /
  save / zone-fill / DRC stages were killed and the board was quarantined as
  `*_timeout_unverified_*`. Board 05's legacy Kelvin recipe never finished
  inside its 900 s. Routing now stops early enough for those stages: it keeps
  back a post-route reserve, and each escalation attempt's slice includes its
  board-load time. When the time left can't cover them, these stages are
  skipped, each with a note: the pre-save optimizer and DRC nudge, the zone
  fill's thermal-remediation passes, the KiCad oracle completion rounds, the
  access-witness replay, and the post-route DRC verification. The post-route
  DRC still writes its sidecars. The two-phase rip-up loop no longer keeps an
  iteration that lost nets: a rip-up cut off mid-reroute could beat the
  complete snapshot on overflow alone. On board 05 that turned a 36/36 pass
  into 32/36 and forced a second 4-layer attempt.

- **`kct route --no-auto-layers --format json` now prints the routing summary** (Issue #6277).
  The direct (non-escalation) path only emitted its JSON from inside the text banner
  block, so stdout carried just `{"exit_code", "verdict"}`. Every route path now emits
  the same document with one final `summary.verdict`, and the escalation path reports
  `single_pad_nets` like the direct path. `kct optimize-placement --route-check` uses
  `--no-auto-layers` instead of pinning the layer count.

- **`kct route` resolves via clearance from the board's own rules** (Issue #6280).
  The router used to set only the trace clearance from the board and fab
  profile, leaving via clearance at a flat 0.20 mm. Since #6272 every trace is
  held `max(trace, via)` from every foreign via, so that default cost board 02
  25 extra vias (27 -> 52) and 41 mm of copper for no DRC benefit. Via
  clearance now comes from the strictest declared rule that applies to a via:
  the `.kicad_pro` board minimum, a `.kicad_dru` clearance rule (unconditional,
  or scoped to vias with a condition such as `A.Type == 'Via' || B.Type ==
  'Via'`), or the netclass. It is raised to the manufacturer's minimum
  clearance and to the trace clearance (a via is never held closer to foreign
  copper than a trace is). 0.20 mm is kept only when the board declares
  nothing. A new `--via-clearance MM` flag overrides it. Board 02 is back to
  27 vias and 529 mm, and kicad-cli is still clean.

- **`kct route-auto` and the MCP `route_net` tool honour `.kicad_pro` / `.kicad_dru` clearances** (Issue #6283).
  Both tools took trace and via clearance from the `.kicad_pcb` alone, so a
  project board minimum, netclass or `.kicad_dru` via rule that the board's own
  DRC enforces was ignored. They now resolve both values through the same
  clearance resolver `kct route` uses, floored by the fab declared in the
  board's `project.kct` (no floor when none is declared), and report any
  floor-raise warning in their `warnings`. A board with no sidecars and no
  declared fab routes with exactly the same clearances as before.

- **Board 04's recipe routes at its declared 0.15 mm via clearance** (Issue #6284).
  `generate_design.py` no longer pins `--via-clearance 0.2`; `kct route`
  resolves the clearance from the board's own rule (#6280). The full recipe
  passes (native DRC, copper LVS, `check_manufacturing.py` 0/0), kicad-cli
  10.0.6 reports 0 violations / 0 unconnected on the routed PCB, and a second
  run is byte-identical after UUID normalization.

- **Board 07 routing frame now carries the project rules** (Issue #6285). The recipe copies `.kicad_pro`/`.kicad_dru` beside `matchgroup_test_routing_frame.kicad_pcb`, so `kct route` resolves the board's declared clearance/via rules instead of the nothing-declared fallback.

- **Relief rescue no longer commits vias too close to foreign pads**
  (Issue #6286). When a rescued net's normal-mode re-route failed, the rescue
  fell back to committing its relief probe path, which never went through
  post-route clearance validation; on board 05 this left vias 0.05-0.085 mm
  from foreign pads (rule 0.127 mm) at positions the router's own validator
  had just rejected. The probe path now has to pass the same
  `_validate_route_clearance` gate (C++ and Python backends) before it is
  committed; otherwise the rescue rolls back.

- **`kct stitch` honours the fab profile's hole-to-hole minimum** (Issue #6289).
  Stitch vias are now spaced against the resolved `--mfr` `min_hole_to_hole_mm`
  (the value `kct check --mfr` enforces) from same-net vias, sibling-net vias
  and vias placed earlier in the same run, instead of only copper clearance.
  Fixes board 05's +24V/+3V3 stitch vias landing 0.457-0.496 mm apart against
  the 0.5 mm limit.

- **Net-class-scoped `.kicad_dru` via rules are no longer applied board-wide**
  (Issue #6294). The via-scoped condition recogniser now splits `||` / `&&`
  only at parenthesis depth 0, so a rule such as
  `(A.Type == 'Via' || B.Type == 'Via') && A.NetClass == 'HV'` is ignored
  rather than read as a minimum for every via; unbalanced parentheses are
  never recognised.

- **Access-witness replay judges exit stubs against the replayed copper**
  (Issue #6303). Once a board had 32+ indexed segments, the replay's
  segment-clearance checks were answered from the finished board's segment
  R-tree instead of the copper present at that journal step, so pads could
  read as stranded before any commit (`access_at_escape_end=empty`, no
  `first_closed_at`) on dense boards. `net-status --why` and the stuck-net
  classifier now see the corrected verdicts, which can differ from before.

- **`kct route` auto-grid refusal now names a concrete `--max-cells` value** (Issue #6306).
  When the memory cap forces an unsafe grid, the `Options:` block (and
  `GridSafetyError`) lists `--max-cells <N>`, the budget at which the
  clearance-safe grid fits (passing it clears the gate), warns when `<N>` exceeds
  the 4,000,000-cell auto-grid ceiling, and no longer advises enlarging the board.
  The default budget and bump policy are unchanged: scaling the default with board
  area would move non-fine-pitch boards onto finer, slower grids.

- **`kct route --timeout` sidecar names the copper-bearing board after a routing-stage deadline** (Issue #6308). The supervisor now records this invocation's `<stem>_partial.kicad_pcb` under `snapshot` (with `snapshot_saved: true`) even when the worker is killed before recording it; consumers read `snapshot`, then `unverified_output`. Stale or torn partials are never named.

- **An interrupted `kct route` no longer saves copper that misses its pads**
  (Issue #6314). When the `--timeout` deadline or Ctrl+C landed while
  placement-delta feedback was trying out a footprint move, the
  `_partial.kicad_pcb` paired that trial's copper, routed against the moved
  part and only partly re-routed, with the original footprint positions. On
  board 05 that read as 21 nets with a missing link where the run had 5, with
  every trace into U2 ending beside its pad. The partial save now writes the
  last accepted state: the routes the run had before the trial, on the
  placement they were routed against. If an earlier move had been kept, the
  saved board carries that move too, and so does a partial save in any later
  stage. `<output>.timeout.json` gains `snapshot_state` (`live`,
  `last-accepted`, or `inconsistent` if the restore itself failed),
  `snapshot_placement` (`source` or `moved`), `snapshot_discarded_trial` and
  `snapshot_restore_seconds` (plus `snapshot_restore_error` when the restore hook
  raises, which also records `inconsistent`, as does a moved placement that
  cannot be serialized at save time); no existing key changed. A placement-delta loop
  that fails with an ordinary error now also continues from its last accepted
  state, where it used to carry on with the half-finished trial. And a Ctrl+C
  that saves but does not stop the run (Python discards the handler's exit
  when the signal lands in a destructor, #6324) no longer leaves the loop
  judging a trial the save has just undone: the trial is dropped, never kept,
  and the loop stops with exit reason `pd_interrupted`.

- **`kct route`: a wide net can leave its own fine-pitch pad next to another
  routed net** (Issue #6315). Clearance cells between adjacent pads of two
  different nets went to whichever pad was added first -- including the
  neighbour's own metal row -- so on board 05 the 0.4 mm `PHASE_C` (U2.9) could
  not leave its pad whenever `PHASE_B` (U2.8) was routed in the same run, even
  on an empty board. Pad metal now always belongs to its own pad, a gap shared
  by two nets' pads is owned by the nearer pad whatever the add order, and the
  C++ A* checks pad-derived cells against the exact segment-vs-pad clearance
  instead of treating a neighbour's clearance halo as copper:
  `--nets PHASE_B,PHASE_C` now routes 2/2 with 0 DRC errors on `kct check` and
  `kicad-cli`. A via the router refuses inside the net's own pad is no longer
  reported as "blocked by stored-via geometry (blocking net <own net>)", and
  the stuck-net classifier no longer labels a net `budget_starved` when the
  caller passes a geometric search-failure reason for it
  (`classify_stuck_nets(..., search_failures=...)`). Requires a native rebuild
  (`kct build-native`; router build version 50).
- **`kct stitch` no longer skips a pad island that never reaches the plane**
  (Issue #6315). The strict connectivity model calls the largest pad island
  "connected", so a trace joining two stranded pads (board 04: an
  oracle-completion trace C3.2 -> C10.2, with the B.Cu GND fill bonding only
  J1.6) outranked the plane's own island, and stitch skipped both pads as
  "already connected" -- they shipped as a GND open. When the fill bonds at
  least one pad, every pad on an island the fill does not reach is now
  stitched. `NetStatus.fill_bonded_pads` lists the pads the poured fill
  bonds.

- **`kct board-view` missing links no longer depend on the coordinate frame** (Issue #6319). Links are chosen in the board-relative frame and shifted afterwards for `--absolute`; the text listing now prints each end's coordinates next to its pad name so ambiguous ends like `U2.29` can be located.

- **Interrupted placement-delta trials no longer leak into the access-witness
  sidecar** (Issue #6320). When a `kct route` deadline or Ctrl+C partial save
  discards an in-flight placement-delta trial, the commit journal is now cut
  back to the trial's start (new public `CommitJournal.truncate_to` /
  `CommitJournal.mark`), so `<stem>_partial.access_witness.json` describes the
  copper the saved board contains instead of the discarded trial's re-route.

- **`kct route --placement-feedback` keeps its best routes on a deadline or Ctrl+C**
  (Issue #6321). Each feedback iteration clears every route and re-routes, so an
  interrupt mid-iteration used to save the half-finished re-route as
  `<output>_partial.kicad_pcb` in place of the better result the loop already
  had. The partial save now restores the loop's best routes first (without a
  grid rebuild, inside the save's grace window) and trims the access-witness
  journal to match, and the `.timeout.json` receipt records
  `snapshot_state: "best-trial"`, `snapshot_strategy: "placement-feedback"` and
  the best iteration (`-1` for the pre-loop input).

- **Default `kct route` saves a `_partial` board on a deadline or Ctrl+C**
  (Issue #6322). The layer-escalation flow (`--auto-layers`, on by default)
  never bound the partial-save state, so a `--timeout` deadline wrote no
  `<stem>_partial.kicad_pcb` and Ctrl+C raised a bare `KeyboardInterrupt`.
  It now saves the copper routed so far: the in-flight attempt's until one
  completes, then the best completed attempt, then the chosen result through
  optimize / DRC nudge / finalize (with a kept placement delta's moved
  footprints). The deadline record's `snapshot` names it, Ctrl+C exits 5, and
  `--preserve-existing` copper is kept in the partial. The previous
  SIGINT/SIGTERM handlers are restored when the flow returns.

- **`kct route` now stops when Ctrl+C or the `--timeout` deadline lands inside
  a destructor** (Issue #6324). The handlers save the partial board and then
  raise, but Python throws that raise away when the signal arrives while a
  `__del__` is running. Routing then carried on through optimization, DRC
  nudge and the normal save, and a second Ctrl+C overwrote the first
  `_partial` board. The handler now records its exit code before raising.
  Every route flow (single pass, `--auto-layers`, `--adaptive-rules`, the
  combined ladder and the manufacturer-tier and size wrappers) checks it at
  each stage boundary and between escalation attempts, and the placement
  feedback loop checks it once per iteration (new exit reason
  `pf_interrupted`). The run exits 5, or 130 when nothing was saved. A
  swallowed deadline is raised again at the next boundary, so it still writes
  one `_partial` and exits 124. A repeat Ctrl+C no longer rewrites the partial.

- **An interrupted `kct route` no longer saves flipped silk/fab for a mirror
  trial** (Issue #6325). A Ctrl+C or timeout landing inside the footprint
  mirror's application, or inside the revert that undoes it, could leave the
  partial board with that part's silkscreen and fab layers on the wrong side.
  A mirror trial now snapshots the part's silk/fab before flipping it and the
  undo writes that snapshot back, so the saved board is right whenever the
  signal arrives and whichever thread receives it.

- **Stuck-net classifier no longer calls a zone-less power net a pour
  problem** (Issue #6327). A power-named net with no zone on the board (board
  05's `VM`) used to be labelled `pour_discontinuous` even though only routed
  copper can connect it. It now gets a signal-failure class (`escape_blocked`,
  `congestion_saturated`, `placement_bound` or `budget_starved`) in
  `kct net-status --why`. Nets that have a zone, and nets the pour-net
  classifier tags by name (`VCC`, `GND`, ...), are still `pour_discontinuous`.

- **`kct route --deterministic-budget` now logs the wall clocks that make a
  same-seed re-route differ** (Issue #6329). On board 05 the cause is the
  negotiated loop's per-net wall budget, which is 10% of the remaining stage
  time. The pure-Python A* fallback reaches that budget before the iteration
  cap, so how far it searches depends on machine load. Each such net now logs
  `[deterministic-budget] per-net wall clock bound net ...` and appears in
  `fallback_stats["wall_bound_under_cap"]`. On the default adaptive-grid path
  the negotiated loop now prints the `[deterministic-budget] stage deadline
  fired` line when `--timeout` stops it; before, only the two-phase router
  printed it. The `--deterministic-budget` help names the stages that still
  run on a wall clock.

- **`kct route --timeout` receipt keeps `snapshot_state` when killed mid-save** (Issue #6330). The
  partial-save facts (`snapshot_saved`, `snapshot_state`, `snapshot_placement`,
  `snapshot_discarded_trial`) are now published right after the `_partial` board is written,
  before the slow access-witness sidecar, so a hard kill during serialization no longer leaves a
  `_partial` board beside a `.timeout.json` that says `snapshot_saved: false`.

- **The PCBWorld suite routes the bare D3 guide board, not the routed one**
  (Issue #6332). PCBWorld's `processed_v9_guide_v3.kicad_pcb` carries the
  human copper (PCBWorld strips it at `reset()`), so the #6331 suite refused
  every real board. It now routes `processed_v9_guide_v3_unrouted.kicad_pcb`
  and uses the routed guide only as the human wirelength/via reference. One
  board whose PCBWorld prep fails now yields a `prep_failed` record instead
  of aborting the run.

- **`kct route --route-engine lattice` (and `mesh`) no longer discards a
  finished negotiation when `--timeout` expires** (Issue #6341). Both engines
  negotiate the whole net set on the first net and cache every net's routes;
  when that negotiation used up `route_all`'s outer budget, the between-nets
  timeout check stopped before the cached routes were committed, so board 05's
  56/57-connection lattice result was written as a board with no copper. The
  timeout branch now serves every remaining cached net (a lookup, no extra
  routing) and logs how many it served.

- **`kct route` no longer rips the same blocker for a sealed-in net every
  round** (Issue #6342). Inside the negotiated loop's corridor-reservation
  window a targeted rip-up removed the named blocker from the Python grid
  only, so the C++ search rerouted the stuck net against the copper it had
  just ripped, failed, and rolled back. When the stuck net fails that way,
  the rip-up is now retried once with its own rip-ups applied to the C++ grid
  at once. On board 05, `--nets PWM_B,EN_B,PWM_C,EN_C` (#6340) now routes
  4/4 where it used to finish 3/4. The same `(stuck net, blocker set)` pair
  is ripped at most twice per run: the second attempt may keep the stuck
  net's new route and leave a blocker that cannot re-land for ordinary
  recovery, and a third is refused. Each rip-up logs why it ended
  (`committed`, `traded`, `victim_geometric`, `sibling_failed`,
  `budget_exhausted`), and the access-loss witness line no longer says
  "ripping X" for a net whose rip-up budget was already spent.

- **`kct route --strategy monte-carlo` / `evolutionary` keep their best trial
  when the deadline fires** (Issue #6343). Each new best trial is published on
  the router as it is found, so a `--timeout` (or Ctrl+C) mid-run saves it as
  the partial board (`snapshot_saved: true`, `snapshot_state: "best-trial"` in
  `.timeout.json`) instead of a board with no copper. Both strategies now honour
  the stage budget cooperatively, returning the best completed trial and
  terminating still-running worker processes. Worker trials also now inherit the
  parent's layer stack, `--per-net-iterations` / `--deterministic-budget` cap and
  `--max-search-iterations` backstop; before, every worker routed a 2-layer grid
  with no per-net cap, so results were not comparable with the default strategy.

- **Monte Carlo and evolutionary trials now route in their own net order**
  (Issue #6345). `route_all(order)` re-sorted every caller-supplied order back
  into priority order and negotiated trials were never given one, so every
  `--strategy monte-carlo` trial and every `--strategy evolutionary`
  chromosome routed the same sequence and returned byte-identical copper.
  Each trial now installs its order through the `--order-method` seam
  (`_forced_net_order`); trial 0 is still exactly the default order, a user
  `--order-method` order is honoured as the base, and `--seed` now seeds the
  run. Each trial can also run the negotiated router (new `--trial-router`,
  default `negotiated`) with an equal share of `--timeout`. The evolutionary
  `astar_weights` / `preferred_layers` / `strategy_flags` genes are documented
  as inert; only `net_order` reaches the router.

- **`kct route` no longer drops the access witness on partial exits**
  (Issue #6346). The replay behind `<out>_partial.access_witness.json` (and
  `<out>.access_witness.json`) runs after the router has released its grid
  arrays, and its descriptive raster-labelling step indexed those emptied
  planes, printing `Warning: access-witness replay failed: index 0 is out of
  bounds ...` and writing a journal-only sidecar. A released or out-of-range
  plane now just contributes no marking, so the `witness` block is written
  again with unchanged verdicts and attribution (`closing_markings` may be
  empty after release). Affected every engine, not only `--route-engine lattice`.

- **Evolutionary routing trials honour keepout rule areas** (Issue #6347).
  `--strategy evolutionary` worker processes dropped the parent's resolved
  keepout rule areas (#6008), so a trial could route copper through a
  no-tracks / no-vias area that the Monte Carlo and single-pass routers
  respect. Both trial workers now rebuild the router through one shared
  `build_worker_router` helper, so the two can no longer drift apart.

- **`--route-engine lattice` honours plane-layer reservation** (Issue #6350).
  The lattice engine now reads `DesignRules.allowed_layers`, which
  `--reserve-plane-layers` (the `--layers 4` default) sets, and hard-blocks
  the reserved reference planes the same way the grid engine and C++ backend
  do. Through vias still step over the planes, so F.Cu-to-B.Cu nets keep
  routing. Before this fix, lattice routes put signal copper on In1.Cu and
  In2.Cu, and `--no-reserve-plane-layers` / `--layers 4-all` had no effect.

- **`kct route --nets` no longer refuses to save its own output** (Issue #6351). The
  #4413 copper-loss guard now compares against the copper the input board carried
  rather than the run's own best-so-far checkpoint staged onto the output path.

- **Board 01 generator now reproduces its committed outputs** (Issue #6354). `boards/01-voltage-divider/generate_design.py` emits the four `(model ...)` 3D blocks itself, the routed PCB and release package are regenerated with the current router (14 segments, 4 vias; was 21 / 2), and a design-capture reproducibility test guards the schematic and unrouted PCB.

- **`--route-engine mesh` honours plane-layer reservation and hard `avoid_layers`** (Issue #6360). The mesh layered search no longer lays signal copper on reserved inner layers (default under `--layers 4`) or hard-avoided net-class layers, and its via hops now span blocked layers so an F.Cu to B.Cu through-via stays possible. Boards with no reservation route unchanged.

- **The relief rescue no longer rips the same blockers for the same net
  over and over** (Issue #6363). The #6342 rip-up cycle guard now also covers
  `_relief_rescue`. Relief picks its blockers by probe, so the guard counts
  each `(net, blocker)` pair separately rather than the whole set. After
  rolled-back reliefs have ripped a blocker twice for a net, a later probe
  that needs that blocker is refused (`= relief rip of ... refused (issue
  #6363, #6342)`). Nested depth-1 rescues now count against the per-net relief
  cap. A targeted rip-up that ripped nothing no longer uses up one of its
  pair's two slots. The targeted rip-up's C++ retry now first clears the
  victim's discarded first-attempt copper from the C++ grid. Every negotiated
  run that reaches the rip-up loop prints a `Rip-up cycle guard: ...` summary
  line. On board 05 the relief for EN_C had ripped DRIVER_SLEEP, PWM_C, RUN
  and STATUS_LED in three iterations, rolling back each time and costing
  about 1050 s.

- **Monte Carlo / evolutionary partial saves no longer hide an inconsistent
  restore, and a cancelled parallel Monte Carlo run returns at once with its
  best trial** (Issue #6364). When the partial save's `pre_save_hook` fails,
  the `.timeout.json` receipt now keeps `snapshot_state: "inconsistent"`
  instead of reporting a clean `"best-trial"`; `snapshot_strategy` /
  `snapshot_trial` are still recorded. A progress-callback cancel of
  `--strategy monte-carlo` terminates the running workers instead of waiting
  for every trial to finish, and no longer drops the trial whose completion
  triggered the cancel.

- **Matrix layer preferences no longer change a classless net's width or
  clearance** (Issue #6366). Injecting a layer preference for a net with no
  net class (the #2432 matrix-conflict path and the evolutionary layer gene)
  built a class from the `NetClassRouting` literals (0.2 mm width/clearance,
  0.6 mm via), silently overriding the board's design rules; on a board with
  a larger clearance rule this routed DRC-violating copper. The injected
  class now carries the board's trace width, clearance and via diameter.

- **Traces leave and land on the pad centreline** (Issue #6368). The router
  no longer takes a free dogleg inside a pad to start or end a run on an
  off-centre row, which left fine-pitch landings up to 0.125 mm off-centre and
  ate into the neighbouring pin's entry lane. Both native A* loops and the
  Python fallback now charge a step that crosses a pad's tip line off the
  centreline, on pads in a fine-pitch row (a neighbour at 0.65 mm or less).
  Off-centre landings stay legal when nothing else fits. Rebuild
  the native extension (`kct build-native`): the binding version is now 52.

- **`kct net-status --why` no longer mislabels geometric router give-ups as `budget_starved`** (Issue #6369). `kct route` now records each net's last C++ search-failure reason in a `<stem>.search_failures.json` sidecar next to the routed board, and the stuck-net classifier auto-discovers it. Boards without the sidecar classify as before.

- **`kct route` no longer reports a stranded pad as connected** (Issue #6371).
  The post-save "Output Connectivity Verification" and the router's own
  `Nets routed` / `Nets connected` count both linked a pad to any same-net
  segment endpoint within 2 mm, on any layer, so board 03's open USB_D- pad
  J1.B7 (1.59 mm from its routed sibling J1.A7) was counted as connected.
  Both now use the same layer-aware copper-contact model: copper must
  intersect the pad on a layer the pad occupies, and vias bridge only their
  layer span. The written-file verifier also now reads KiCad 10 via blocks,
  whose `(tenting (front ...))` children it previously skipped.

- **Lattice engine no longer leaves a diff pair open when its coupled run is
  blocked** (Issue #6373). When `--route-engine lattice` cannot find a
  coupled run for an engaged differential pair, it now routes the pair's two
  main-run legs single-ended in the same negotiation pass, so the pair's
  extra pads stop being stranded as islands. Board 03's USB_D pair is the
  example: its J1-to-R3/R4 trunk crosses the pass-through ESD part U2. The
  `Diff pairs (lattice)` block reports such a pair as
  `uncoupled-fallback:<reason>` and adds a `WARNING: diff pair ... routed
  uncoupled` line. The decline reason `pair-no-parity` (no consistent
  polarity, so no search ran) is now separate from `pair-no-path` (the
  search ran and was exhausted).

- **Pour-link completion keeps net-class clearance** (Issue #6374). The
  KiCad-oracle closer (`PourLinkCloser`) routed and welded pour links at the
  single `--clearance` value, so on board 04 a GND link ran 0.150 mm from
  OSC_IN although both nets are in 0.2 mm classes. New pour copper on net P
  now keeps `max(--clearance, class(P), class(N))` from each neighbour net N,
  the grid router's rule. A link with no such route stays open with a
  `no clearance-legal route` note, and any link a closer deliberately closes
  below its class clearance is printed as `RELAXED:`, never silently.

- **`kct route`'s default flow now verifies the board it writes** (Issue #6376).
  The post-save `--- Output Connectivity Verification ---` check ran only
  under `--no-auto-layers`. The default auto-layers flow (and so
  `--auto-mfr-tier` and `--auto-pcb-size`), `--adaptive-rules` and
  `--auto-layers --adaptive-rules` saved, filled zones, ran DRC and printed
  `SUCCESS` without re-reading the written file. Every flow now runs the same
  verifier after the save. A net the router counted as connected but the
  written file leaves open prints `ROUTING FAILED` with the verified
  `Nets connected` count, and the result the `--auto-pcb-size` loop reads
  carries that verified count.

- **Lattice diff pairs that pass through a part now route coupled segment by segment** (Issue #6378).
  A pair whose signal path runs through an interior part (board 03's USB_D
  goes J1 -> U2 ESD -> R3/R4) is no longer forced onto one J1 -> R3/R4 trunk
  across U2's own pads. Each segment between parts gets its own coupled
  attempt, with the per-segment uncoupled fallback from #6373, and the hop
  inside the part routes single-ended. When a segment end's P/N midpoint sits
  on a foreign pad (U2's GND/VBUS pad between D+ and D-), the coupled run now
  starts just clear of the part, also clear of keepouts and reserved pour
  vias. On board 03, U2 -> R3/R4 now couples. USB_D skew drops from 15.5 mm
  to 2.4 mm and continuity rises from 0% to 19%. J1 -> U2 still routes
  uncoupled because the USB-C A row walls it off, so both diff-pair rules
  still fail. Only a part with separate entry and exit couples counts as a
  pass-through: a part that merely taps the pair (board 06's LVDS termination
  resistors), a pair with one test point per net, and nets running through
  different parts all still route as one coupled trunk, unchanged.

- **Lattice router escapes fine-pitch pads whose full-width stubs dead-end** (Issue #6381).
  On a 0.65 mm pitch QFN row, a 0.4 mm trace's legal centreline band can fall
  between lattice rows. The full-width escape then reached only nodes inside the
  pad's own copper, with no legal way out. Those dead ends no longer suppress
  the narrower-neck fallback (#4293). When a search fails because one end's
  escapes are all dead ends, it now declines as `no-egress-start` /
  `no-egress-end` instead of `no-path-layer-constrained`. Lattice negotiation
  without diff pairs now runs up to 12 passes instead of 8, the same cap as
  boards with diff pairs. A board that converges within 8 passes is unchanged.
  Board 05 now routes all 33 signal nets under `--route-engine lattice`.

- **Grid router keeps fine-pitch pins' straight-out lanes clear** (Issue #6382).
  Every long-axis pad of a detected fine-pitch package now carries a soft
  "entry lane" cost (one pin pitch wide, 2 mm past the copper tip, all layers)
  that other nets pay to cross, so wide escapes turn further out instead of
  sealing their neighbours' pocket. The cost also applies on the default
  `--grid auto` path. On board 05 (grid, 4 layers) EN_B and nFAULT now route;
  GND_SENSE and VM's U2.11 pocket remain open. `KCT_DISABLE_PAD_LANES=1` turns
  it off; `KCT_PAD_LANE_LENGTH_MM` / `KCT_PAD_LANE_PENALTY` tune it.

- **Pour welds keep a stricter neighbour's class clearance anywhere a weld
  can actually reach** (Issue #6384). `kct route`'s pour completion picked
  the weld clearance from nets within a fixed 5 mm of each pad, which missed
  a strict class just beyond 5 mm and the longer reach of large pads. The
  neighbourhood is now derived from stitch's own placement geometry
  (`max_via_reach_mm` plus via radius plus the pair's class clearance).

- **The pure-Python router fallback no longer counts a pad's clearance twice**
  (Issue #6389). The Python A* treated a foreign pad's whole clearance halo
  as copper. Where a corridor was narrower than about twice the clearance, it
  detoured through vias that the C++ search does not need. It now checks
  pad-clearance cells against the exact pad geometry, as the C++ search has
  done since #6315, using the post-route validator's segment-vs-pad
  measurement. The same cells as in C++ stay on the old conservative check:
  drilled pads, stitch-via reservations, keep-outs and the board edge,
  routed copper, reserved corridors and zones.
  The Python A* also gains the C++ search's #6375 own-pad step gate. A step
  leaving the routing net's own pad within clearance of a foreign pad is now
  rejected during the search. Before, the validator rejected the finished
  route, and the search could run out of candidates and return nothing.

- **Routed DRC sidecars now carry the router's net-class clearances** (Issue #6390).
  `kct route` routed each net at its router class clearance (`Power` 0.2 mm,
  `Clock` 0.2 mm, `HighCurrentSignal` 0.2 mm, ...), but the routed
  `.kicad_pro` declared only `Default` at the fab floor. Neither
  `kicad-cli pcb drc` nor `kct check` held the copper to those clearances.
  The routed `.kicad_pro` now declares every router class stricter than
  `Default`, assigning nets by exact name in `netclass_assignments` in the
  shape the block's schema reads: strings for schema 3, arrays for an
  unversioned or newer block. The `.kicad_dru` carries matching `NetClass`
  clearance rules, which under `KCT_PRESERVE_BOARD_RULES=0` sit in their own
  managed block. A router class does not govern pad pairs inside one
  footprint, in either engine, because no re-route can widen a connector's
  or package's own pin gaps. Nor does it govern the P/N gap of a coupled
  differential pair (`USB_D+`/`USB_D-`), which the router routes at the
  class's tighter intra-pair clearance. Both engines recognise a pair the
  way KiCad's `isCoupledDiffPair()` does, by complementary `+`/`-` or
  `P`/`N` name suffixes. The fab floor still applies to the pair. Authored classes keep KiCad's full semantics,
  are never replaced, and every router class skipped in their favour is
  reported. Re-routing produces byte-identical sidecars. `kct route` can now
  also regenerate a renamed output's sidecars that an earlier kct run wrote,
  even when its source has changed since: the `.kicad_pro` carries a
  `meta.kct_sidecar_sha256` digest, and an existing `.kicad_dru` that differs
  only in kct's managed blocks is replaced. Hand edits outside kct's floors
  still raise `DRC sidecar conflict`.

- **Access-witness sidecars keep `closing_markings` after `kct route`**
  (Issue #6391). The witness replay now runs while the routing grid is still
  live, just before the engine state is released, and the
  `.access_witness.json` / `_partial.access_witness.json` writes reuse it, so
  each stranded pad again names the raster labels of the copper that closed
  it. Verdicts are unchanged. Under `--timeout` the early replay only spends
  time beyond the post-route finishing reserve and the `--auto-fix` reserve;
  when none is left the sidecars fall back to the previous behaviour. A
  witness cut short by the pad or evaluation cap (not the deadline) is now
  reused instead of being replayed a second time.

- **Lattice router now keeps a pad's own net-class clearance** (Issue #6395).
  Before this, `--route-engine lattice` only knew a pad's class when the input
  `.kicad_pro` authored it. Router-derived classes never reached the pad side
  of a track-vs-pad pair, and skipped pour nets (GND, +5V) lost their `Power`
  class entirely. A 0.15 mm `Digital` track could therefore pass a 0.2 mm
  `Power` pad at 0.175 mm: 6 findings on board 05 in both DRC engines.
  After negotiation, the router now moves each such track segment off the pad
  until it meets `max(track class, pad class)`. A move is kept only if every
  segment it touches stays clear of all other copper, Edge.Cuts and keepouts.
  A shortfall that cannot be cleared is reported, never hidden. The search
  itself is unchanged, so routing completion and run time are unaffected.
  On board 05 this gives 0 findings in both engines with 33/33 nets.

- **Board 07's SDRAM demo now clears its own 0.175 mm net classes in both DRC
  engines** (Issue #6396). After #6390 declared the `AUX` / `SDRAM_*` classes,
  `kct check` found 42 and refilled `kicad-cli` found 40 copper gaps 0.2–25 µm
  short in the hand-reviewed routing. Twenty-four vias move by 10–47 µm (and
  one GND escape by 7 µm), with their stubs kept at 0/45/90°. The committed
  script `real_design/repair_class_clearance_6396.py` reproduces the change.
  The classes are unchanged, both engines now report 0, and LVS, timing,
  impedance and load still pass. The manufacturing bundle and
  `readiness.json` are re-pinned by #6404.

- **`kct route` explains open pour links and joins a stranded pour island to the plane** (Issue #6399).
  Each `still unconnected:` line of the pour-completion stage now lists the
  closer's reasons from the last round beneath it: the per-pad / per-link
  notes, plus any weld or route withdrawn because its copper raised KiCad's
  DRC count (a pad-less `Zone <-> Zone` link shows its net's reasons). A link
  ending on a zone is now routed to the plane-owning copper first, falling
  back to the nearest island only if that fails -- and that fallback is now
  reported -- so a padless sliver nearer than the plane no longer wastes a
  completion round unannounced.

- **Multi-net `kct stitch` no longer drops a via inside a sibling net's fill**
  (Issue #6400). When several pour nets are stitched in one call, each via
  is now gated on its own net's fill only, and sibling target nets' fills
  count as foreign copper. Previously a VCC via could land in the VBUS pour
  and bond only to a sliver of VCC copper (board 03 C1.1). Single-net
  stitching is unchanged.

- **`kct route` keeps a via site free for each pour-net IC pin** (Issue #6401).
  Before signal routing, every SMD pad of an auto-skipped pour net on the
  perimeter of a multi-pad footprint now gets one legal via site plus its stub
  reserved, on both the grid and the lattice engines. This uses the #5700
  planner, which previously ran only in board 06's generator. Signal nets keep
  their normal clearance from the reservation, and the post-route KiCad-oracle
  weld tries the reserved site first. Before this, signal copper could box in
  a fine-pitch pour pin and leave it unconnected (board 04 U2.36 +3.3V, board
  05 U1.21 GND, board 03 U1.44 VCC). Under the lattice protocol, boards 04 and
  05 now exit 0. Board 03 closes VCC U1.44 but still has two open links,
  VBUS U1.7 and VCC R14.1, with or without the reservation (#6409).
  - BGA fields, exposed pads and two-terminal parts are not served.
  - The planner's new exact-geometry mode measures pads as boxes instead of
    circumscribed circles and no longer counts the clearance twice. Board 06's
    default planner is unchanged.

- **Auto-pour no longer covers another pour net's pads with a bounding-box island** (Issue #6402). When `kct route`'s auto-pour gives a net a pad-bbox island on a layer that already hosts another net's zone (board 03's VBUS island on the VCC plane's `In2.Cu`), the island is now carved clear of the incumbent's pad bodies plus a 1 mm via keep-out, and each enclosed pad is opened to the island edge by a channel as wide as that keep-out so the incumbent's fill actually reaches it. Channels take the nearest edge that keeps the island's own pads. Board 03's U1.2/U1.44/C1.1/C3.1/F1.2 VCC pads now stay on their plane and its VCC pour links close under the lattice protocol.

- **Stitching no longer rejects legal pour welds next to fill bridges** (Issue #6409).
  KiCad joins each hole in a zone fill to the outer outline with a
  zero-width "fracture" bridge: two coincident edges running in opposite
  directions. The same-net fill check in `kct stitch` and the pour-link
  oracle counted those bridges as fill edges, so it rejected a via site
  inside solid copper whenever a bridge ran within one via radius. On
  board 03 that left VBUS U1.7 and VCC R14.1 unconnected. The check now
  ignores bridge edges. Real fill and hole edges still enforce the margin.

- **`kct stitch --thermal` / `--blanket` no longer put one plane net's vias
  on another target net's pour** (Issue #6410). In a multi-net call
  (including the default every-plane-net run), each net's thermal vias were
  gated against the fill of *any* target net, and blanket vias could drill
  through a sibling target net's pour on another layer. Each net is now gated
  on its own fill, with sibling pours treated as clearance obstacles, the
  same way `kct stitch` has handled pads since #6400. Single-net output is
  unchanged.

- **Oracle closer tries the nearest-island join when its plane join is
  withdrawn** (Issue #6412). Each routed pour-link variant now has its own
  withdrawal key (direct, `#plane`, `#island`), so a plane join that raised
  KiCad DRC no longer bans the whole link for the round: the retry routes to
  the nearest island and notes `plane join withdrawn for KiCad DRC; joined the
  nearest island instead`. `kct route` prints withdrawn-variant reasons
  against the right link.

- **Plane-via reservation can no longer flank a signal pin, and the
  stitcher accepts reserved sites across layers** (Issue #6413). The
  pre-route site planner never takes a site that would block a signal SMD
  pin's straight outward escape from both sides. It checks neighbouring
  footprints too. If nothing else is legal, that pour pad is left
  unreserved. Sites in front of a pin that could still be flanked are
  tried last. One-sided blocks are reported by `kct route` as
  `signal escape blocked on one side:`. The stitcher now checks the stub
  to a reserved site only against tracks on the stub's own layer. On
  board 04, a B.Cu track had been refusing U2.23's reserved F.Cu site.

- **`kct mcp setup --client claude-code` registers the server where Claude Code
  reads it** (Issue #6418). It used to write `~/.claude/mcp.json`, which Claude
  Code ignores, so `kicad-tools` never appeared. Setup now runs `claude mcp
  add-json --scope user` when `claude` is on PATH; otherwise, or when that call
  fails, it merges the entry into the top-level `mcpServers` of `~/.claude.json`
  (`$CLAUDE_CONFIG_DIR/.claude.json` when set) atomically, keeping every other
  key. New `--scope project` writes `./.mcp.json`. An unparseable
  `~/.claude.json` (or Claude Desktop config) is now reported and left alone
  instead of being replaced with a fresh file. `--format json` gains `scope`,
  `method` (`claude-cli` or `json-merge`), `unchanged`, `legacy_config` and
  `cli_error`; the server entry now carries `"type": "stdio"`.

- **Benchmark DRC helpers can now count what `kct check` and kicad-cli
  count** (Issue #6420). `run_kct_check()` accepts a `net_class_map` (path or
  loaded map), rekeys it onto the board's nets and applies the stackup copper
  weights, so the diff-pair and match-group skew rules and net-class
  clearances engage as they do in `kct check`. Without a map the summary now
  lists those rules in `inactive_rules` instead of reporting them as passed.
  Given a board path, it also runs the CLI-level rules that read the
  sibling `.kicad_pro` (`netclass_clearance_copper`, `netclass_floor`) through
  `kct check`'s own rule dispatch, so its per-rule error counts equal
  `kct check --allow-incomplete` on the same file. `collect_report()` and
  `krt_compare` gain those rules too.
  `run_geometric_drc()` / `run_kicad_cli_drc()` take an opt-in
  `all_track_errors` flag, and the kicad-cli summary records
  `unconnected_count`. Defaults are unchanged. The expand-route research
  harness uses all of this, records a `manufacturable` verdict, and adds
  `--deterministic-rescue` to deterministic grid-engine routes.

- **The pour oracle no longer bans every weld when the declared via is below
  the project's via floors** (Issue #6425). `kct route`'s KiCad-oracle DRC gate
  now ignores `drill_out_of_range` / `via_diameter` / `annular_width` errors
  whose every item is a via the attempt itself placed at the run's declared
  `--via-diameter` / `--via-drill`. Every other error type, and a via of any
  other size, is gated as before, and the final route-time DRC still reports
  those via-size errors. The route-time `Errors:` count also stops
  double-counting a via that the internal checker and kicad-cli both flag
  (`dimension_via_drill` and `drill_out_of_range` count as one). Board 04
  drops its `--no-plane-via-reservation` pin, and its pour converges 18 -> 0.

- **`kct route` holds foreign copper to the router's own net classes**
  (Issue #6427). Since #6390 the routed `.kicad_pro` / `.kicad_dru` declare the
  router's classes (`Power`, `Audio`, ...), so KiCad requires
  `max(class(a), class(b))` between two nets, but the router only applied a
  class to its own net's copper. Board 02's trace optimizer merged a `LINE_A`
  (Audio, 0.15 mm) run into a diagonal 0.165 mm from a `GND` (Power, 0.2 mm)
  via, and `kct route` exited 3. Router classes stricter than the base now
  feed the same per-net floors as authored project classes (#6243), so the
  search, the native validator and the optimizer enforce the pair rule. That
  includes classes from `--net-class-map` and `--analog-nets`. Board 02
  routes with exit 0 and 0 violations in both engines, and its recipe now
  fails on a non-zero `kct route` exit instead of repairing the copper in
  finalize.

- **Pour completion no longer trades one net's links for another's** (Issue #6428).
  `kct route`'s KiCad-oracle completion loop kept a round when the pour
  links summed over all nets fell, so a weld that closed 17 GND links by
  cutting the +3.3V pour (17 GND -> 1 +3.3V) counted as progress. A round may
  now not raise any net's link count. The weld that split the pour (found by
  comparing the pour's fill before and after the round) is withdrawn by
  site and the round retried, so the pad can weld elsewhere; when nothing
  can be blamed the board is restored. The stitcher's pad-to-via stub checks
  (straight, dog-leg and multi-leg) now test only foreign tracks on the
  stub's own layer, and the plane-via reservation's escape-corridor guard
  treats a foreign pad, keep-out or board edge beside a signal pin as a
  blocked side.

- **Coupled diff pairs KiCad does not recognise by name no longer fail DRC
  at their intra-pair gap** (Issue #6429). `kct route` records the pairs it
  coupled whose names KiCad's `isCoupledDiffPair()` misses (`X_POS`/`X_NEG`,
  `tx_p`/`tx_n`, `USB_dp`/`USB_dn`, declared pairs) under
  `meta.kct_router_diff_pairs` in the routed `.kicad_pro`. The router-class
  `.kicad_dru` clearance rule exempts them by exact net name in both orders,
  and `kct check`'s `netclass_clearance_copper` reads the same record, so both
  engines still agree. A route-time warning names each such pair, because
  KiCad's native `diff_pair_gap` / `diff_pair_uncoupled` checks will not see
  it. Boards whose pairs are all KiCad-named get byte-identical sidecars.

- **`kct fix-drc` reports success only after a clean re-check** (Issue #6431).
  After the last pass the output board is re-checked with `kct check`'s own
  rule set: the project's netclasses, the auto-discovered net-class map, and
  the path-dependent rules such as `netclass_clearance_copper`. When the
  violations came from kicad-cli or a report file and kicad-cli is
  installed, `kicad-cli pcb drc --all-track-errors --refill-zones` also runs
  on the output, under the input project's rules. "All violations repaired!"
  and exit 0 require both to find no error in the targeted categories. Any
  remaining pairs are listed (text) or reported under a new `recheck` JSON
  block. Re-detection between passes, `--verify`, and `--output` boards
  checked away from their sidecars now all use the same rule set. Initial
  kicad-cli detection passes `--all-track-errors --refill-zones`.
  `--layers` defaults to the board's copper-layer count. Track/track and
  via/via findings from `kct check` are repaired like kicad-cli's plain
  `clearance`. On the board 05 repro, all 6 class violations are now
  cleared and confirmed by both engines; before, it printed "All violations
  repaired!" while both engines still found all 6.

- **`kct repair-clearance` repairs what it says it repairs** (Issue #6434).
  Each violation's two items are resolved from the report: the kicad-cli item
  uuid, the `kct check` geometry descriptor, or the item type, net and
  position. A via or pad is no longer mistaken for the track beside it, so a
  track is no longer slid along its own axis. The move is perpendicular to
  the gap. Every move is re-measured, and it is kept only if the pair then
  meets the rule without bringing the moved copper under clearance to
  anything else. A pair counts as repaired only when its measured gap meets
  the rule; `new_clearance_mm` is that measurement. Pairs that cannot be
  cleared are listed with the reason. `--prefer move-via` now moves the via,
  and if the preferred item cannot clear the pair the other movable item is
  tried. Locked tracks and vias are never moved, neither as the target nor
  dragged along with it: a track end joined to a locked track stays put, and
  a via carrying a locked track does not move. A track end on a via or on a
  pad of its own net stays put. Same-net track ends joined to a moved end,
  and stubs that meet the moved track part-way along it (T-junctions), are
  dragged with it. A move that would still separate copper of one net that
  touched before is reverted and the pair reported, in `repair-clearance`,
  `fix-drc` and the in-process router callers alike. Moved coordinates are
  written at full precision (they used to be rounded to the micron above
  100 mm), and a dragged track piece that collapses to a point is removed.
  Local reroutes keep the original net (they used to write
  `(net 0)` on KiCad 10 boards without a net table). A report holding only
  track/via pairs no longer reads as "nothing to repair". On the board 07
  repro, 39 of 40 kicad-cli clearance errors are now actually cleared and the
  last one is reported as unrepaired; before, it printed "Repaired 38/40" and
  kicad-cli still found all 40.

- **Strict net status no longer splits GND on exposed-pad footprints** (Issue #6435).
  `NetStatusAnalyzer(strict=True)` now bonds pads whose copper overlaps on a
  shared copper layer (using each pad's absolute rotation and true
  round/oval outline), so an exposed pad's F.Cu and B.Cu rects join the
  `*.Cu` thermal vias inside them, as KiCad does. A fresh board 05 lattice
  route now reports 36/37 nets and 1 open, the same as `kicad-cli pcb drc`
  (previously 35/37 with GND in 5 islands). `kct net-status`, the research
  harness, `kct fleet status` and `kct stitch` all inherit the fix; stitch no
  longer adds needless vias beside exposed pads that are already connected.
  Disjoint pads that share a number still do not bond.

- **Lattice pad-class repair can no longer skew a diff pair** (Issue #6437).
  The #6395 post-negotiation repair now holds every net of a detected diff
  pair (extra-pad stubs and declined pairs routed single-ended) to a 0.01 mm
  skew budget per pair, the epic #6441 P5 threshold: a move that would change
  the pair's skew (the two nets' accumulated length changes, so opposite-sign
  moves cannot add up) by more is refused and reported as "on diff-pair
  nets left in place". Pair legs themselves never move. Via-to-pad class
  shortfalls are now reported (not moved), and a `--voltage-map` board logs
  that the repair was skipped instead of skipping silently.

- **Coupled diff pairs are no longer laid below the board's own minimum
  clearance** (Issue #6443, epic #6441). The router lays a coupled pair at
  `max(class intra-pair gap, manufacturer minimum, authored board
  min_clearance)`, on the lattice and at the grid's coupled-spacing and
  skew/match tuner sites. Before, a board authored with `min_clearance` 0.15 mm
  routed its classifier `HighSpeed` pairs (0.1016 mm) at the 0.127 mm fab
  floor, and kicad-cli failed each coupled segment pair twice
  (`diff_pair_gap_out_of_range` and `clearance`; 48 errors on board 06's
  lattice benchmark row at the #6440 head). `kct route` prints one line per class whose gap a floor
  raised. The routed `.kicad_dru` gains one native `diff_pair_gap (min ..)`
  rule per coupled pair KiCad recognises by name, at the gap the router used,
  in a new managed block.

- **Lattice routes of boards 05 and 04 complete again** (Issues #6449,
  #6451). #6427 fed the router's own net classes into the per-net clearance
  floors, and the lattice search's static pad keep-outs read the same floors,
  so every `Power` / `HighCurrentSignal` / `Clock` pad grew by 0.05 mm. In
  fine-pitch pin fields that closed escapes #6395 had already measured and
  rejected: board 05 lost `GND_SENSE` at `U2.6` (32/33 nets, exit 3), and
  board 04 (`--layers 2`) lost `OSC_IN`, `OSC_OUT` and `SWDIO` (6/9, exit 2).
  The pad keep-outs now grow by designer-authored project classes only. The
  pad side of a router-class pair is still enforced on the routed copper by
  the #6395 class-pad repair, and the #6427 pair checks (committed copper,
  native validator, optimizer, census) are unchanged.

- **`kct net-status`, `kct check`, `ConnectivityValidator.validate()` and copper
  LVS now share one copper-contact model that matches `kicad-cli pcb drc`**
  (Issues #6452, #6453, #6470). A via joining fills of two different zones now
  bridges them, so it no longer reports a false GND open (#6452). Pads are
  tested with their exact outline (rounded `roundrect` corners, circles,
  ovals) and no 0.1 mm erosion, so shallow thermal-spoke contact counts and
  stops showing up as false opens (#6453). `validate()` matches trace ends to
  pad copper instead of pad centres, and it no longer bonds a pad to the
  nearest fill inside a zone outline it does not touch, which had hidden real
  opens (#6470). Open counts now equal kicad-cli's unconnected count per net
  on every committed routed board, including detached tracks and vias (a
  lone fill island is not counted, as in kicad-cli). The partition also reports pad shorts the eroded model hid, such as
  the SENSE_P track on `U3.9` on board 09's routing-investigation boards.

- **Board 07's manufacturing bundle, saved zone fills and readiness evidence
  are current again** (Issue #6455, part of #6404). The saved plane fills
  predated the board's 0.175 mm net classes, so native DRC without `--refill-zones`
  reported 326 via-to-zone clearance findings, and the bundle still bound the
  pre-#6396 routed PCB (`kct check`: `Manifest: STALE`). The reviewed build
  (`real_design/validate.py`) now fills the planes under the published router
  classes. A new staged driver, `scripts/boards/refresh_bundles_6404.py`,
  refills zones (only `(zone ...)` blocks may change), refuses to publish
  unless native DRC on the *saved* fills is clean, re-plots the Gerbers,
  assembly PDFs, images, report and renders, regenerates board 07's reviewed
  evidence and re-pins `readiness.json`. The other boards follow in later
  stages.

- **Strict `kct net-status` and pour bonding orient pads by their absolute
  angle** (Issue #6460). A non-square pad rotated relative to its footprint
  had its width and height swapped, so a trace or pour ending 0.4 mm off the
  real copper counted as connected (hiding an open kicad-cli reports), and
  one landing on the real copper was reported split. Strict trace-to-pad
  contact, pour bonding and the stitch/LVS pad partition now agree with
  kicad-cli; no committed board's result changes.

- **Grid router keeps a route's own vias at the fab hole-to-hole minimum**
  (Issue #6464). A single route could drop its second via 0.461 mm
  drill-to-drill from its first (board 05, EN_A) because every same-net drill
  check only looked at already-committed copper, and the validators used the
  0.102 mm via-merge threshold instead of the 0.5 mm hole-to-hole floor. The
  C++ A* now holds each via against the vias earlier on the same path, and
  both the C++ and Python route validators check a route's vias pairwise and
  apply `max(min_drill_clearance, min_hole_to_hole)` to same-net drills.
  Rebuild the native router (`kct build-native`): `BUILD_VERSION` is now 53.

- **Coupled diff-pair search keeps its own vias at the fab hole-to-hole
  minimum** (Issue #6473). The coupled search only checked a layer change
  against committed copper, so a pair hopping an obstacle could return a rail
  with two vias 0.2 mm drill-to-drill, and a P via 0.4 mm from its N partner,
  against a 0.5 mm floor. Both the C++ search and the Python loop it mirrors
  now reject a layer change whose P or N drill is closer than
  `max(min_drill_clearance, min_hole_to_hole)` to the other, or to any via
  already on either rail of the same search. A pair whose pitch is under
  `via_drill + min_hole_to_hole` (0.8 mm with default rules) can no longer
  change layer side by side; it staggers the two vias where it has room, and
  otherwise falls back to single-ended routing. The Python route validator
  (`validate_same_net_drill_spacing`) also gains the tolerance its C++ twins
  already had, so a via pair sitting exactly on the floor is accepted instead
  of rejected. Rebuild the native router (`kct build-native`): `BUILD_VERSION`
  is now 54.

- **`--deterministic-rescue` now also removes the escape-budget and per-iteration wall clocks** (Issue #6475). With an active expansion cap (`--deterministic-budget` / `--per-net-iterations`) the grid negotiated loop no longer applies the 60 s oscillation-escape budget or derives the per-iteration per-net cap from the remaining `--timeout`, so `--timeout` cannot change which nets are routed. Periodic and completed-attempt checkpoint writes also save and restore the global RNG state, so checkpoint timing no longer shifts the seeded stream. The route banner gains an `Escape budget / per-iteration per-net cap:` line. `diffpair_bench.py run --extra` now reaches `kct route`, `REPLAY_FLAGS` is `--resume` (abbreviations included), and a route that exits 1 or 5 is an invalid row labelled `checkpoint`, not `final`.

- **No writer emits a partial-span via without its type keyword** (Issue
  #6485). KiCad reads a `(via ...)` with no `micro`/`blind`/`buried` token as a
  through via whatever its `(layers ...)` pair says: it rewrites the pair to
  F.Cu/B.Cu on save and drills the hole through the whole board. The grid
  coupled diff-pair router wrote such vias (`(layers "F.Cu" "In1.Cu")`) on
  boards with more than two signal layers, so the file claimed a barrel that
  stopped at an inner layer while the fabricated one reached B.Cu. Router vias
  are now built and written as through vias (F.Cu/B.Cu) unless they are micro
  vias, and the pair-routing clearance gates check the full barrel.
  `PCB.add_via` (new `via_type` argument) and `via_node` write `blind` or
  `buried` for a partial span instead of dropping the keyword, the MCP routing
  tool keeps `micro`, and a constructed `buried` via no longer serialises as
  the quoted `(via "buried" ...)` that KiCad refuses to load.

- **Board 03 USB_D no longer deleted by the authored-floor backstop** (Issue #6489). When the class-pad repair cannot clear a pad shortfall (a slot narrower than `width + 2 x class`, e.g. the J1 shell/GND slot), the lattice now grows exactly those pads' keep-out and negotiates again (bounded, kept only if it routes at least as much with fewer shortfalls), so the net routes around the slot instead of being demoted. The backstop summary now names an authored/class-floor demotion instead of calling it a cross-net short.

- **Abutting pads now agree with kicad-cli** (Issue #6490). Pads that share
  an edge with a same-net pad or fill are no longer reported as an open, and
  different-net pads that share an edge or are tangent (including off-axis
  circles) are now reported as a short by the LVS partition. Exact pad
  outlines bond within a 2.5 um tessellation allowance.

- **`kct net-status` and the CI net-status gate now fail on stranded copper**
  (Issue #6491). A detached track or via, or a fill island with only a track
  or via on it, left every pad joined, so the net stayed `complete` and
  `kct net-status` exited 0 even though `open_connections` was 1 and
  `kicad-cli pcb drc` reported an unconnected item. The new
  `NetStatus.is_stranded` / `stranded_items` and
  `NetStatusResult.stranded` / `stranded_count` carry that signal.
  `suggested_fix` names the stray copper ("Delete or connect stranded copper:
  Segment 12 (F.Cu)") instead of "Route traces to connect 0 pads", and the
  text report lists it under "Stranded Copper" instead of printing "All nets
  are fully connected!". `NetStatus.status` stays pad-based.

- **A keyword-less partial-span via is read as a through via, as KiCad does**
  (Issue #6493). KiCad decides a via's drilled span from its `blind` /
  `buried` / `micro` keyword, not from its `(layers ...)` pair, so a
  `(via ... (layers "In1.Cu" "B.Cu"))` with no keyword is drilled F.Cu to
  B.Cu. kicad-tools took the pair literally, which gave false opens
  (`connectivity`, `via_dangling`, an incomplete `kct net-status`) where the
  barrel really reaches F.Cu, and missed shorts on layers the pair omitted.
  The schema parser and both router via parsers now share one rule
  (`kicad_tools.core.layers.kicad_read_via_layers`); typed vias keep their
  declared span. Each board logs one warning with the count of vias read
  this way. The source file is not rewritten.

- **Board 03's J1 silk no longer crowds the board edge** (Issue #6494). The
  board recipe's silk repair now clips footprint silk lines back to the
  `silk_edge_clearance` floor (`SILK_EDGE_CLEARANCE_MM` plus half the stroke
  width), so `kct check --mfr jlcpcb-tier1` reports 0 `silk_edge_clearance`
  warnings on the committed board 03 artifacts. Silk only; copper is unchanged.

- **Lone fill islands no longer count as unconnected items** (Issue #6495).
  After #6487 made a fill island that touches nothing else a warning, the
  `ConnectivityResult.summary()` "Orphaned zone islands" line, the
  `kct validate --connectivity` table's "UNCONNECTED ZONE/ITEM RELATIONSHIPS"
  section and `kct fix-drc`'s regressed-net rollback still counted it. All of
  them now use one definition (errors only); lone fills are listed under their
  own "Lone fill islands" line/section and stay in the JSON `issues[]` as
  warnings. `kct validate --connectivity --strict` now gives the same exit code
  with and without kicad-cli installed.

- **Coupled diff-pair search no longer ends on the wrong layer at an SMD goal pad** (Issue #6499). Both the Python and C++ coupled searches accepted a goal by x/y alone, so a pair could be returned that stopped on another copper layer under an SMD pad (an open on that leg, reported as routed). SMD goals now need the pad's layer; through-hole goals stay layer-agnostic. `ROUTER_CPP_BUILD_VERSION` is now 55, so a stale extension falls back to Python.

- **Router clearance, connectivity and via-length checks see a standard via's
  full barrel** (Issue #6502). A non-micro router via whose recorded layer
  transition is a partial pair (e.g. `F.Cu`→`In1.Cu`) is drilled through and
  written on `F.Cu`/`B.Cu`, but the segment/via clearance predicates, the
  negotiated router's via-violation hooks and tree-contact cells, the DRC
  nudge, the optimizer collision checker and the diff-pair via-length/skew
  proxy still took that pair as the barrel and missed copper on the layers
  below it. They now read `Via.drilled_layers`; micro vias keep their span.

- **Rip-up no longer leaves a surviving via with a hollow clearance halo** (Issue #6508). A cell has one owner, so a via's halo cells that overlapped another route's halo were recorded under that route and freed when it was ripped up. The pure-Python A* then routed 0.47 mm from the via's centre and `_validate_route_clearance` rejected the candidate (44 rejections on board 05's PWM_C fallback). `unmark_route` now re-stamps the halos of the committed routes it overlaps.

- **Python route validators accept a gap on the clearance rule to float
  noise, like the C++ validator** (Issue #6509). The segment-vs-pad,
  segment-vs-segment, segment-vs-via, via-vs-segment and via-vs-via checks
  in `RoutingGrid` now allow the same 1e-4 mm slack as `grid.cpp`, so a
  step at exactly the clearance (0.14999999999999963 against 0.15) is no
  longer refused by the pure-Python fallback while the native backend
  accepts it.

- **The Python router no longer refuses a via pair sitting exactly on the
  hole-to-hole floor** (Issue #6528). `Router._own_path_vias_clear` now uses
  the same `CLEARANCE_EPSILON_MM` slack as `validate_same_net_drill_spacing`
  and the C++ search. Before, a 0.5 mm drill gap that float rounding measured
  as `0.4999999999999998` was refused on some grid columns, so the Python
  fallback took a pair one cell wider than needed.

- **In-place `kct route` no longer reuses a stale copper-loss baseline**
  (Issue #6531). `_stage_input_for_auto_pour` now records the input copper
  count on in-place runs (input == output) as well, so a long-lived process
  (MCP server, test session) that routes `A -> O` then `O -> O` compares the
  second run's copper-loss guard against `O`'s own copper, not `A`'s.

- **Lattice diff pairs keep real copper on both nets under `--differential-pairs`** (Issue #6535).
  With `--route-engine lattice --differential-pairs`, the grid coupled
  pre-pass laid a P/N trunk for each pair, and the lattice then routed the
  same nets again without seeing that copper. The two overlapping copper sets
  tripped the post-route short check, which removed whole nets: board 06b's
  "coupled" LVDS pairs shipped with 0 mm of N copper, and board 03 lost
  USB_MCU_D+. The grid pre-pass now leaves pairs the lattice routes to the
  lattice, and the lattice warns if a pair net already carries copper from
  an earlier pass. A single coupled trunk that the lattice declines with
  `pair-no-path` because a termination resistor's pads straddle its corridor
  is now retried as an IC -> resistor -> IC chain. The retry is kept only
  when it couples more and leaves nothing more unrouted, so pairs whose
  trunk couples are unchanged. Board 06b (lattice, seed 42) goes from 0/4
  pairs coupled, 16/18 nets and 4 unconnected to 3/4 coupled (LVDS1
  partially), 18/18 nets and 0 unconnected. Board 03 goes from 25/27 to
  26/27 nets, and USB_MCU_D now has coupled copper on both nets.

- **Lattice tap-chain retry no longer trades another pair's coupling for
  its own** (Issue #6550). The #6535 retry of a declined diff-pair trunk is
  now kept only when no other pair couples fewer segments than in the first
  negotiation (as well as the retried pairs improving, the unrouted count
  holding and no deadline hit). A discard caused by such a regression names
  each regressed pair in the retry's log line, e.g. `regressed: LVDS2 1/1 -> 0/1`.

- **Mesh engine no longer double-routes or shorts differential-pair nets**
  (Issue #6551). Under `Autorouter(strategy="mesh")`,
  `route_all_with_diffpairs` ran the grid coupled pre-pass and then the mesh
  negotiated every net without seeing that copper. Pair nets ended up with two
  copper sets, or foreign nets were routed straight through the pre-pass
  trunk. As with the lattice fix in #6535, the mesh now owns its pair nets: the
  grid pre-pass is skipped for them, and a `[diffpair-engage]` line warns that
  the mesh routes them **uncoupled**, because it has no coupled router.

- **`kct route` partial saves keep `--preserve-existing` copper in every flow**
  (Issue #6553). The adaptive (`--adaptive-rules`), combined
  (`--auto-layers --adaptive-rules`) and single-attempt (`--no-auto-layers`)
  flows did not pass the preserved input copper to the deadline / Ctrl+C
  save. Once a periodic checkpoint had overwritten the staged output, the
  copper was lost from `<stem>_partial.kicad_pcb`. All four flows now keep
  it, exactly once, as the layer-escalation flow already did (#6322).

- **Reverted rotate placement deltas no longer leave silk/fab text turned
  180 degrees** (Issue #6557). A `rotate_180` or `rotate_align` trial ends in
  keep-upright text normalization, which the revert did not undo, so a run
  that reverted the rotate and then kept a later delta saved the part's
  reference/value/fab text upside-down with swapped justification. The
  cosmetic snapshot/restore from #6325 now covers both rotation kinds.

- **The rest of the coupled diff-pair path now lays pairs at the floored gap**
  (Issue #6560, follow-up to #6443). The inline serpentine's partner
  clearance, the P/N centreline bound `DiffPairRouter._pair_seg_clearance`,
  and the partner waiver of both route-halo arms (the C++ coupled search and
  its Python mirror `RouteHaloRefiner.trace_clear`) read the pair gap through
  `DesignRules.pair_gap`, i.e. `max(class gap, manufacturer minimum, authored
  board min_clearance)`, instead of the bare class value. A class gap below
  the board's authored minimum could otherwise put pair copper below that
  minimum, which kicad-cli `clearance` fails. Pairs whose class gap already
  clears both floors route exactly as before.

- **`kct route` uses the fab clearance floor for the board's own stackup**
  (Issue #6561). The router's manufacturer clearance floor came from a flat
  `MfrLimits.min_clearance` (jlcpcb 0.127mm for every board), while
  `kct check --mfr` and the emitted `.kicad_dru` use the per-stackup profile
  value. On jlcpcb 2-layer 2oz boards the flat value was *below* the fab's
  0.1524mm, so route could emit copper its own check rejected.
  `resolve_clearance` now takes the board's layer count and copper weight and
  returns the profile's `min_clearance_mm`. That value floors the base and via
  clearance, the diff-pair gap (`DesignRules.mfr_clearance_floor`) and the
  `--complete` / `--adaptive-rules` relaxation ladder. `kct route`,
  `route-auto`/MCP `route_net` and `kct check`'s intra-pair threshold all use
  it, so the router and every jlcpcb `.kicad_dru` agree. The flat value is
  kept only as the fallback when the layer count is unknown.

- **`kct route --dry-run` no longer routes or writes files** (Issue #6563).
  The default `--auto-layers` flow, `--auto-pcb-size`, `--auto-mfr-tier` and
  `--adaptive-rules` routed for real under `--dry-run` and wrote checkpoint,
  partial and final boards; `--layers N --dry-run` still staged a copy of the
  board at `--output`. `--dry-run` now prints the analytical grid plan (one
  per layer count on the escalation ladder) and exits in seconds, and the
  checkpoint and interrupt partial-save writers are no-ops under `--dry-run`.
- **`kct route --analyze --dry-run` no longer deletes stale `_Nlayer` siblings or stores a routing-cache entry** (Issue #6563). It never deletes or writes files.

- **Mesh engine no longer blocks multi-pad nets with their own copper** (Issue #6566).
  `MeshPathfinder.route_netset` now tracks committed copper per net, so later star
  legs of a 3+ pad net are not declined by the capsules of earlier legs sharing the
  anchor pad. Other nets' copper and `fixed_copper` remain hard obstacles.
  Placed vias are now keep-outs for other nets, `_via_allowed_at` enforces a via-body
  standoff, and a net's coincident duplicate vias are suppressed, which keeps the
  exemption from introducing board-02 clearance/short DRC errors.

- **`kct check` holds diff pairs to the gap the router lays** (Issue #6575).
  `diffpair_clearance_intra` now uses `DesignRules.pair_gap()` per pair
  (`max(class intra_pair_clearance, manufacturer min, authored board min)`),
  or `max(manufacturer min, authored board min)` for pairs without a net class,
  instead of the manufacturer minimum alone. `DRCChecker` gains
  `authored_min_clearance_mm`, fed from the `.kicad_pro` `rules.min_clearance`.

- **`kct check` labels via-count findings `vias` in the measurement summary** (Issue #6576). Pair via-count findings from `diffpair_uncoupled_length` no longer print as `uncoupled`, and the managed `.kicad_dru` pair block omits the router-gap header when it carries only limit rules.

- **`kct route` keeps a reserved pour-pad via site on its pour's fill**
  (Issue #6589). The #6401 pre-route via-site reservation kept foreign
  copper only at copper clearance, so on the lattice engine a foreign
  through-via could land legally beside the site yet cut the pour's fill out
  from under it, and the stitcher (correctly) refused the site, leaving an
  open pour link (board 03 `VBUS: U1.7`). Each site now also keeps foreign
  vias, and tracks on the pour's layer, outside the pour's fill pullback
  (its zone clearance plus half its `min_thickness`, read from the board's
  zones). Tracks on other layers keep the old copper-clearance spacing.

- **`kct pcb import-ses` imports standard Specctra sessions** (Issue #6593).
  Wires and vias nested under `(network_out (net NAME ...))`, as Freerouting
  and fastroute write them, were all dropped; they now import. Coordinates and
  widths honour the session's `(resolution UNIT N)` instead of assuming
  1 unit = 1 um, a `(string_quote ")` parser block no longer hides the routes,
  via layer span and size come from the session's `library_out` padstack (or
  the `Via[i-j]` range in its name), and nets on KiCad-10 name-only boards
  resolve and are written as `(net "NAME")`. Elements that cannot be imported
  are listed as `skipped` rather than silently dropped or defaulted to
  `F.Cu`/`B.Cu`, and the output file is always written.

- **`kct pcb export-dsn` now carries the board's own rules, zones and KiCad 10 nets** (Issue #6594).
  Net classes, their widths, clearances and via sizes, and net-to-class
  assignments and patterns now come from the sibling `.kicad_pro` instead of
  hard-coded defaults. Board minima and unconditional `.kicad_dru` clearance,
  track-width and via-diameter floors raise them. Copper pours export as
  `(plane ...)`, and rule areas as `(keepout ...)`, `(wire_keepout ...)` or
  `(via_keepout ...)`. KiCad 10 boards with name-only `(net "NAME")`
  references and `(property "Reference" ...)` now export every net and
  reference. Before this, board 09 exported 0 nets and 47 `?` references.
  Anything Specctra cannot express, such as conditional DRU rules, edge and
  hole clearances, arc zone outlines and footprint zones, is listed in the
  new `omissions` key of `--format json` and under "Not exported" in the
  text output.

- **`kct route` no longer leaves single-layer junction vias** (Issue #6596).
  `Autorouter.cleanup_artifacts` drops a through via whose same-net copper
  all sits on one layer (KiCad's `via_dangling` warning), but only when the
  net's layer-aware copper stays exactly as connected without it; micro,
  in-pad, pad-touching, pour-net and zone-bonded vias are always kept. The
  usual source was an RSMT Steiner branch point pinned to the first pad's
  layer. Board 01's routed PCB and release package are regenerated (VOUT
  loses its redundant via at (149.5, 86.2)).

- **SES import no longer turns a full-span `Via[i-j]` fallback via into a blind/buried via** (Issue #6602). When a session via has no `library_out` entry, the index range is no longer read against every copper layer on the board; only full-span ranges (as the DSN exporter writes) are imported as `F.Cu`-`B.Cu`, anything else is skipped with a reason.

- **The remaining via readers treat a keyword-less partial-span via as a
  through via** (Issue #6614). The zone-fill clearance carve, the Freerouting
  DSN export, the DRC local rerouter, the reasoning-agent board state and
  `strip_traces` (layer filter and orphan-via pass) now read a via's span
  through the same `kicad_read_via_layers` rule as the main parsers (#6493),
  so a `(via ... (layers "In1.Cu" "B.Cu"))` with no `blind`/`buried`/`micro`
  keyword is copper on every layer, as KiCad builds it. The local rerouter
  and the orphan-via pass also now count a through via's barrel on the inner
  layers it passes through, not just the two layers its pair names.

- **`kct route --adaptive-rules` saves a `_partial` on a first-tier deadline or Ctrl+C** (Issue #6625). The adaptive and combined escalation flows now pin the first tier's in-flight router, so a `--timeout` deadline or Ctrl+C before any tier completes writes `<stem>_partial.kicad_pcb` instead of nothing. A completed tier still wins over a worse in-flight one.

- **`kct route` deadline partial and checkpoints no longer double preserved nets** (Issue #6626). `_partial` and periodic checkpoint writes now drop `--preserve-existing` copper that the router re-routed, as the final output does.

- **Remaining router barrel readers use the drilled span** (Issue #6630).
  Terminal connectivity, Kelvin branch isolation, fixed-copper clearance
  validation, lattice fixed-copper occupancy, class-pad repair and escape
  endpoint extent now read `Via.drilled_layers` instead of the logical
  `layers` pair, so a non-micro via recorded as `(F.Cu, In1.Cu)` is seen on
  `B.Cu`. The Kelvin escape layer-transition check intentionally keeps the
  logical pair.

- **In-pad micro-via fallback no longer emits full-stack micro vias** (Issue #6646). On a 4+ layer board with no inner signal layer (planes only), the escape router and sub-grid in-pad rescue fell back to the opposite outer layer and wrote an F.Cu-B.Cu `(via micro ...)`. A micro via now spans stack-adjacent layers only; the micro fallback is declined otherwise.

- **`kct route --analyze --dry-run` prints the analysis and never routes, on every flow** (Issue #6648).
  On the escalation flows (the default `--auto-layers`, `--auto-pcb-size`,
  `--auto-mfr-tier`, `--adaptive-rules`) it used to route the whole ladder in
  memory without printing any analysis. It now prints the complexity and
  routability analysis for the board as loaded (`--layers`, default
  auto-detect) and exits without routing, deleting or writing anything. If
  the analysis itself fails under `--dry-run`, the command exits non-zero
  instead of falling through to a real route.

- **Imported blind/buried vias keep their drilled span in the router**
  (Issue #6662). The router `Via` now carries KiCad's `blind` / `buried`
  keyword (`span_type`), set by both board-file via parsers and kept through
  the routing cache. `Via.drilled_layers` honours it, so a preserved
  `(via buried ... (layers "In1.Cu" "In2.Cu"))` no longer blocks F.Cu and
  B.Cu as a full through barrel, and `--preserve-existing` re-writes it as
  `(via buried ...)` instead of silently turning it into a through via. A
  keyword-less partial pair is still a through via, as KiCad reads it.

- **Lattice diff-pair routes are skew-tuned to `kct check`'s own limit**
  (Issue #6664). With `--route-engine lattice --differential-pairs`, the
  existing diff-pair skew tuner now runs automatically after routing, on every
  pair whose skew `kct check` would fail as an error
  (`resolve_pair_thresholds(...).skew_severity == "error"`), tuning to that
  same limit regardless of the class's `length_critical` flag. On this path
  the tuner measures skew the way the checker does (via barrels included), so
  a pair with unequal via counts is never "tuned" into a worse checker skew,
  and each bump must clear foreign pads, vias and tracks at the larger of the
  two net classes' clearances. Pairs are detected with the net-class map, so
  class-declared (`diffpair_partner`) pairs are included, and one line per
  pair reports the reason, skew before and after, and the limit; a pair left
  over the limit is flagged `STILL OVER LIMIT`. Board 06b routed with the
  lattice engine is now manufacturable: its four LVDS skew errors
  (0.236 / 0.128 mm against a 0.1 mm limit) are gone in both
  `kct check --mfr jlcpcb` and kicad-cli DRC. The grid engine and
  `--length-match-diffpairs` are unchanged.

- **`kct route` falls back to the dense-package escape pre-phase instead of
  aborting when the adaptive grid cannot escape a fine-pitch part** (Issue
  #6666). On an auto-grid board with a multi-resolution plan, the adaptive
  branch used to return before the escape decision was made. Once its Phase 1
  escaped 0 pads of a part, the run hard-failed with "Escape routing failed ...
  rerun with --grid". `kct route` now previews that Phase 1 without changing
  the grid and routes such boards through `route_with_escape`. On chorus-test
  v21, the R2 recipe with `--allow-offboard` went from that exit 1 to 29/51.
  Boards whose adaptive Phase 1 succeeds still take the adaptive branch.
  `--grid-strategy {adaptive,uniform}` is now accepted by `kct route`, not only
  by `python -m kicad_tools.cli.route_cmd`.

- **`kct stitch` no longer routes one pour net's escape through another
  stitched net's existing copper** (Issue #6668). In a multi-net call
  (`--net GND --net +1V2 --net +1V8`), the pre-existing tracks and vias of
  the *other* target nets were not obstacles, so a +1V8 extended escape on
  board 07 was drawn straight through U3.11's GND fanout via (a real
  GND/+1V8 short). Each stitched net now treats every other net's copper,
  sibling stitch nets included, as an obstacle in every placement strategy,
  the connectivity fallback, the off-fill probe and the skip diagnostics;
  a pad's own-net copper still never blocks its own escape. A pad whose only
  escape crossed sibling copper is now skipped (reported) instead of
  shorted. On board 07 this removes all 5 `shorting_items` and 7 of 9
  `clearance` violations; the pour-connectivity audit still passes.

- **`kct route --net-class-map` no longer leaves a map-declared pour net with
  no copper** (Issue #6669). A net the sidecar declares a pour
  (`is_pour_net: true` or `route_via: "pour"`) that the name classifier does
  not read as power/ground (board 06rf `VBUS_USB`, classified high-speed
  because it contains "USB") now gets an auto-pour zone, and auto-skip
  consults the same map, so a pour net the router skips always has a zone;
  one that cannot get a zone is routed as traces instead. The inverse
  (#6599: a map-routed power-named net gets no zone) is unchanged.

- **Lattice pair legs keep pad-array class floors instead of being demoted**
  (Issue #6670). The #6489 grown-keep-out re-negotiation grew only the pads
  a track had just violated, so in a BGA ball field the track moved one row
  over to the next GND/+1V2 ball. On board 06rf this left `USB3_TX2-` and
  `USB3_RX2+` 0.168/0.178 mm from U2's 0.200 mm `Power` balls, and the
  authored-floor backstop deleted both nets. When a shortfall comes back on a
  part whose pads were already grown, every pad of that part with a class
  above the board default now grows. 06rf lattice routes 21/21 nets. The
  backstop log now names the pad (`GND pad U2.G3`) instead of `net 0 pad`, and
  the run log says whether each growth round was kept or rolled back.

- **Pour-oracle completion keeps its clean closures** (Issue #6671). On
  board 06rf `kct route` closed 0 of 128 GND/+3V3 pour links: a few welds
  per attempt raised DRC or split another pour, so each round was restored
  whole. Now a regression removes only the blamed vias/stubs from the
  attempted board and re-checks the rest, a weld site that raised DRC stays
  banned in later rounds, and up to three footprints whose closures keep
  being blamed are withdrawn as a whole, so no one dense part can veto the
  board; a pruned attempt gets one final re-check before a round gives up.
  When the links left are fenced in by copper an earlier round kept for
  another net, that copper is withdrawn and its link closed another way,
  undone unless the count falls and DRC does not rise against the last
  kept round. 06rf lattice s42 now closes 129 of its 131 GND/+3V3/VBUS_USB
  pour links (was 0; 2 stay open, see #6714) and board 03 all 20, with no
  new DRC errors. Separately,
  `kct stitch` measures a `roundrect` pad's exact rounded corners
  (unchamfered, at cardinal angles) on top of the #6683 shape-aware pad
  model, so the plain box no longer refuses legal diagonal weld sites
  (board 03 C5). Via drill spacing keeps 0.015 mm of slack for the board
  writer's 0.01 mm coordinate rounding, enough for two vias placed in the
  same pass (two GND welds planned 0.800 mm apart were written 0.797 mm
  apart, a jlcpcb `hole_to_hole_clearance` error), and the dog-leg / escape
  weld strategies now apply the drill hole-to-hole floor between same-net
  vias. Diff-pair skew / length errors are matched by rule, not by
  kicad-cli's moving anchor.

- **Lattice routing no longer islands a BGA's power-ball field on a signal-layer
  pour** (Issue #6673). When a pour floods an area array's own pad layer (a
  4-layer board's third power net goes on `F.Cu`), the lattice engine now keeps
  foreign tracks from crossing between row- or column-adjacent balls of one
  pour net, and foreign vias off the interstitials beside them, so the cluster's
  fill stays one piece. A foreign ball the cluster would seal in keeps an exit.
  Board 07rf: `+1V8` (U4 C3/C4) no longer strands a fill island the pour oracle
  cannot close. `kct route` prints `Area-array pour continuity (#6673)` when the
  guard engages.

- **`kct route --route-engine lattice` now tunes match groups that `kct check`
  would fail** (Issue #6674). The match-group length tuner used to run only with
  `--length-match-groups`, so lattice output on board 07rf failed
  `match_group_length_skew` on every declared group. It now runs automatically
  on lattice output for each group declared with `length_match_group` whose
  spread exceeds the tolerance `kct check` enforces, and tunes to that same
  tolerance (board 07rf `ADDR_BUS`: 25.1 mm -> within 0.5 mm). Groups inside the
  limit are not touched. Grid runs and the explicit `--length-match-groups`
  flag behave as before.

- **Lattice match-group tuning lengthens diff-pair members** (Issue #6675).
  The auto-engaged group tuner no longer declines every pair member of a
  group on lattice output: a failed host falls through to the next one,
  coupled lattice pairs get a centreline meander that adds the same length to
  both rails and keeps their gap, and uncoupled pairs are tuned leg by leg on
  their uncoupled copper -- never leaving a pair's own skew worse than it
  was, nor pushing a pair out of its skew limit.  A group that is still over
  its limit after tuning is now rolled back whole and reported as
  `STILL OVER LIMIT` instead of keeping serpentines that do not clear the
  error.  On board 07rf this clears `DDR_DATA_BYTE_0`, `MIPI_CSI_LANES` and
  `HDMI_TMDS_LANES` (`match_group_length_skew` 3 -> 0).

- **Via `span_type` survives the access-witness sidecar and merges** (Issue #6677). Blind/buried keywords now round-trip through the sidecar (old sidecars load as `None`), and a merge that widens a via recomputes it: cleared at both outer layers, `blind` at one, `buried` at none.

- **Blind and buried vias keep their span in diff-pair length and the MCP router's
  output** (Issue #6678). Diff-pair and match-group length now count an imported
  `blind` / `buried` via as only the thickness of the layers it is drilled through,
  not the full board, so a pair that crosses one no longer reports extra length or
  skew. The MCP routing tool now writes these vias back as `(via blind ...)` /
  `(via buried ...)` instead of turning them into through vias. Through and micro
  vias are measured and written as before.

- **`kct stitch` clears square and rectangular pads by their real corners**
  (Issue #6683). Every via-vs-pad and leg-vs-pad check (straight, dog-leg,
  extended escape, and the skip diagnostic) now uses the pad's shape: rect,
  roundrect and other polygonal pads by their box, circle pads exactly. The
  old circle model (`max(w, h) / 2`) is the inscribed circle of a square, so
  stitch vias could land inside clearance of a BGA land's corner (board 07
  U4.E3/E5 vs U4.F3/F5). The extended escape also samples the channel
  centre between pad rows (half the grid pitch), so coarse-pitch BGA pads
  are not stranded once the corner check is honest. Round-pad BGA escapes
  are unchanged.

- **`kct stitch` no longer counts a filtered-out via as connecting a later
  pad** (Issue #6684). The `--avoid-pad-overlap` via-in-pad filter and the
  drill hole-to-hole filter used to drop vias only after every pad had been
  processed. By then a dropped via had already let a neighbouring same-net
  pad be counted "already connected" (board 07: U4.E4 through U4.D5's
  dropped via), had pushed later pads of other nets away from copper that
  was never written, and was still counted in the micro-via total. Each via
  is now kept or dropped as it is placed, before anything is recorded, so a
  pad that leaned on a dropped via gets its own via or its own skip entry.
  Runs where neither filter drops anything are unchanged.

- **`kct stitch --thermal` / `--blanket` honour sibling target nets' existing copper** (Issue #6685). With several `--net` values, a via for one net is now checked against the pre-existing tracks and vias of every other target net (never its own), and blanket mode also applies the hole-to-hole check against sibling-net via drills.

- **`kct route` attributes shorts against overlapping input pads to the input**
  (Issue #6693). Before routing, `kct route` now prints an `INPUT DEFECT` block
  naming each footprint whose different-net pads already overlap in the input
  board (e.g. a stale pad angle on a rotated fine-pitch footprint), and points
  at `kct check <pcb> --only clearance`. A post-route short against such a pad
  is now counted as "pre-existing in the input board (input defect)" instead
  of "caused by routing", but only when all of the copper's contact with that
  pad lies inside the overlapping pad of its own net. Copper that crosses the
  foreign pad outside the overlap is still a routing short. Exit codes are
  unchanged; shorts still fail the run.

- **`kct route` deadline/partial-save exit now writes the routing plan** (Issue #6694). The
  `<stem>_partial.kicad_pcb` snapshot gets a `<stem>_partial.routing_plan.json` sidecar,
  written before the access-witness sidecar so it fits inside the 5 s partial-save budget.

- **Lattice diff pairs keep their coupling beside reserved pour-pad via sites and sibling fallbacks** (Issue #6697).
  The fat coupled centerline search skipped the keepout mask, so once negotiation history displaced it
  onto a #6401 plane-via reservation (board 03: the U1.5 GND site beside USB_MCU_D) only the final
  leg check saw it and the pair fell back to `uncoupled-fallback:pair-leg-keepout` (USB_MCU_D skew
  3.228 mm). The search now honours the mask at the pair envelope (mitred vertices included), and the
  pad-attach dogleg tries both orientations around it. Uncoupled-fallback legs of a declined pair are
  also routed only after every pair has had its coupled attempt, so they no longer pre-block a sibling
  pair segment's corridor (board 03 USB_D[1] `pair-leg-blocked`).

- **Lattice diff pairs launch from the USB-C row that faces the chain** (Issue #6698).
  A chained pair now starts at the end part's couple nearest the next chain
  node (board 03 J1.A6/A7, not the walled-in B6/B7 row behind it). The other
  row is tied to it with a generated octilinear stub, checked against exact
  pad copper and necked down to the fab minimum width when the class width
  does not fit. The stub is held as fixed copper while routing. Board 03's
  USB_D uncoupled length drops from about 36 mm to about 10 mm. Stubs are
  used per pair, all or nothing: when either net's stub does not fit (a
  single-layer SMD receptacle), the pair is routed as before.

- **`kct stitch` no longer places dangling vias for nets poured only on the
  pad's own layer** (Issue #6701). When every zone of a net sits on the pad's
  layer, the stackup fallback used to target an inner layer that net has no
  plane on, so each through via touched same-net copper on one layer only and
  kicad-cli flagged it `via_dangling` (all 14 `+1V8` stitch vias on board 07).
  Such pads are now listed under skipped pads with the reason "no same-net
  pour on any layer other than the pad's layer". Nets with a pour on another
  layer, nets with no zones, and an explicit `--target-layer` are unchanged.

- **`kct route` / `kct build` auto-pour gives the inner power plane to the
  board-wide rail** (Issue #6714). On a 4+ layer, single-ground board with
  several power pours, `In2.Cu` used to go to whichever power net came first
  in net-number order. Since #6686 made board 06rf's local `VBUS_USB` (5 pads
  on 2 parts) a pour, it took the plane and pushed the board-wide `+3V3`
  (11 pads on 6 parts) onto a carved `F.Cu` outline. Connector J3's `+3V3`
  pin then sat inside the `+1V2` pour with no plane to drop a via to, and
  the welds for its GND neighbours could only land in a `+1V2` sliver that
  they split. The plane now goes to the power net that feeds the most
  footprints. On a tie the first-listed net keeps it, and the other power
  nets keep their order and priorities on `F.Cu`.
  On 06rf lattice, seeds 42 and 43 now close all 129 GND/`+3V3`/`VBUS_USB`
  pour links (both seeds left 2 open before), with no new DRC errors.

- **`kct route`'s pre-save clearance check measures round, oval and roundrect
  pads as their real shape** (Issue #6718). Track-to-pad and via-to-pad gaps
  now come from the shared clearance kernel that `kct check` and kicad-cli
  agree with, instead of the pad's bounding rectangle. A trace passing a
  round pad's corner no longer reports a phantom `[pad]` violation (board
  03's USB-C `J1` ties read 0.006 mm against a real 0.151 mm), and those
  phantom results no longer drive rip-up/reroute, the short gate or the
  DRC-nudge repair. The lattice class-pad repair drops its rectangle-based
  "strict" pass, which only existed to keep the old check quiet. Roundrect
  pads use the board file's own `roundrect_rratio`, which the router now
  loads, instead of an assumed 0.25. That assumption over-rounded the
  corners of small-ratio pads such as power tabs and inductors, and hid
  real corner near-misses in the kept-copper audit, the authored-netclass
  gate (Python and native), the mesh engine and the other kernel consumers.
  A roundrect with no known ratio is measured as its rectangle.

- **Lattice diff pairs get their tap-chain retry back** (Issue #6743). The
  #6535 retry (IC -> termination resistor -> IC) only fired for a trunk that
  declined with the exact reason `pair-no-path`; since #6697 the coupled
  search honestly reports `pair-no-path-keepout-constrained` on boards with
  plane-via reservations, so the retry silently stopped running and board 06b
  fell from 3/4 to 0/4 coupled LVDS pairs. The gate now accepts every
  exhausted-search reason (`pair-no-path` and its `-keepout-constrained` /
  `-layer-constrained` refinements), derived from the pathfinder's own
  constants so a future rename cannot disable it again; 06b is back to 3/4.

### Added

- **Trapped Kelvin sense pins get a bounded inward via escape** (Issue #5398).
  When a recognised Kelvin sense terminal's escape cannot leave its pad on a
  dense package, the escape pre-phase now tries an ordinary off-pad via on the
  inward side, using the existing lateral-rescue budget and via geometry. Each
  candidate must clear pads, committed and sibling copper, holes, fills, board
  edges and the full drilled barrel. It also must not merge into another
  branch of its own net before the shunt, so it must clear every other
  terminal's pad of that net, SMD or through-hole. A committed recovery is
  never displaced by sibling rip-up, and `Autorouter.kelvin_access_nets`
  reports which nets received one. Two through-via span fixes ship with it.
  Ordinary escape vias now declare their physical `F.Cu`..`B.Cu` barrel
  instead of their landing pair, which removes a class of native
  `shorting_items` where a later inner-layer trace crossed the barrel.
  `RoutingGrid.validate_via_clearance` also now checks every layer a barrel
  spans, not only its two endpoint layers.

- **`scripts/ci/check_pr_checks_present.py` distinguishes "no checks ran" from "checks passed"** (Issue #5701). Stacked PRs (base is a feature branch) never trigger `ci.yml`, so `mergeStateStatus: CLEAN` there means "never ran". The script exits 3 on an absent rollup, which on a non-`main` base includes a rollup with no check from the `CI` workflow (a path-filtered workflow such as `ecosystem-drift.yml` can still run there); 2 failing/pending, 0 passing; documented in `docs/contributing/stacked-prs.md`. `.loom/scripts/merge-pr.sh` is Loom-managed, so the gate is repo-owned.

- **`kct route` closes pads stranded on GND/VCC pours with a KiCad-oracle completion loop** (Issue #5785, Epic #5784 Phase 1). After the zone fill, `kct route` reads `unconnected_items` from `kicad-cli pcb drc --refill-zones` and closes exactly those links on the pour nets (stitch-style via + escape welds for stranded pads, a clearance-legal A* route for the rest, all existing copper held fixed), for up to `--oracle-rounds N` rounds (default 3; `0` disables). A round is kept only when the unconnected count falls and the DRC error count does not rise, otherwise the board is restored byte-for-byte. Design credit: KiCadRoutingTools' `oracle_reconnect` (reimplemented, no code copied). Board 02's three stranded pour links now close in one round.

- **Opt-in pose-based centerline search for coupled diff pairs** (Issue #5786,
  Epic #5784 Phase 2). With `KCT_POSE_CENTERLINE=1`, a pair that the
  joint-state coupled search cannot couple is retried as ONE centerline over
  (x, y, heading) poses (45-degree steps, Dubins-length heuristic) with P and N
  derived as miter-compensated offsets and single-ended end legs chosen by a
  setback scan. Every step goes through the Epic #5509 clearance kernel, and
  the finished pair must pass the exact foreign-pad gate (partner pads
  included) and the intra-pair clearance audit before it is committed. On
  board 06b the four LVDS pairs go from 0 % to 87.0 % minimum coupling, with
  18/18 nets and 0 shared-referee DRC errors. It is off by default because on
  board 06 a pose-coupled MIPI pair blocks MIPI_RST. Single-layer pairs only,
  and C++ backend only: without the backend the search is skipped. The Dubins
  calculator is a port of KiCadRoutingTools' `rust_router/src/dubins.rs` (MIT;
  the notice is in `THIRD_PARTY_NOTICES.md`).

- **`kct route --order-method crossing`** (Issues #5787, #5893). New opt-in net
  ordering that ranks nets by flight-line crossing degree — most-contended
  first — within each net-class priority band, so the nets that must compete
  for a corridor pick one while the grid is still empty. Unlike the other
  `--order-method` heuristics it does not spend an evaluation route. **The
  default ordering is unchanged, and this is not proposed as a default**: on
  boards 00/01/02/03/06a it left completion untouched everywhere and made vias
  slightly worse on board 02 (37 → 38), while improving board 01 (vias 5 → 3);
  board 03's measured row predates a geometry fix and is flagged stale in the
  write-up. Measurement, including the two plumbing
  bugs that made every `--order-method` value a silent no-op on the default
  `--auto-layers` recipe until now, is in
  `docs/research/kct-route-net-order-experiment.md`. `greedy`,
  `critical_first`, `congestion` and `hybrid` remain no-ops on that recipe
  (Issue #5908).

- **`kct optimize-placement --strategy bayesian`** (Issue #5801). The Ax/BoTorch
  `BayesianOptStrategy` is now reachable from the CLI and the MCP
  `optimize_placement` tool (`strategy: "bayesian"`). Requires the optional
  `kicad-tools[bayesian]` extra; a clear error is returned when it is missing.
  A nightly workflow installs the extra and runs `tests/test_bo_strategy.py`.

- **`kct readiness --project-root` collects legitimate dependencies outside the PCB directory** (Issue #5813). A schematic (and sub-sheet hierarchy) or a sibling `${KIPRJMOD}/../symbols/...` library inside the named root is now staged, hashed into `readiness.json` (`external_inputs`) and archived root-relative in `kicad_project.zip`, instead of failing with "not in the subpath" / "outside the package root". Missing files, references beyond the root, symlink escapes and whole-ancestor references are still refused with an actionable message, and sources and existing releases stay untouched on failure.

- **`kct ecosystem` tracks where kicad-tools sits in the KiCad ecosystem**
  (Issue #5839). New packaged registry of 16 neighbouring projects with our
  verdict on each, whether its license permits code reuse, and links to our
  own evaluation notes. `kct ecosystem list|show|where-we-sit` (all with
  `--format json`), matching `ecosystem_list` / `ecosystem_show` MCP tools,
  a generated README section, [`docs/ecosystem.md`](docs/ecosystem.md), and a
  weekly warn-only CI job that reports upstream license changes, renames and
  archives.

- **tscircuit interop gate: its KiCad exports do not pass our bar yet**
  (Issue #5847). New `benchmarks/interop/tscircuit/` harness renders five
  tscircuit designs into KiCad projects at pinned upstream versions and runs
  each through `kct check --mfr`, LVS and `kicad-cli pcb drc --refill-zones`,
  both as emitted and after re-routing with `kct route`. All five fail: the
  `.kicad_sch` and `.kicad_pcb` converters name the same net differently, so no
  export can pass LVS, and vias/netclass/silkscreen defaults sit below every
  mainstream fab floor. Results, pinned SHAs and six upstream defect reports
  are in `docs/research/tscircuit-evaluation.md`; the registry entry moves to
  `verdict = "evaluated-not-adopted"`. Nothing tscircuit generates is
  committed — the harness fetches and emits at run time.

- **`kct route --deterministic-rescue` makes the stall-relief rescue
  load-independent** (Issue #5870). `--deterministic-budget` still leaves the
  relief rescue's probe and victim re-land sub-searches on a 10 s wall clock,
  so a busy machine could land different copper from the same `--seed`. On
  board 03 the re-lands took 10-31 s under CPU load, and two routes fell to
  22/24 nets. The new flag bounds them by the per-net iteration cap instead.
  It is off by default because it costs board 07 nets (#4730). The
  `--deterministic-budget` banner now says that this wall clock remains.

- **`kct check` flags `.kicad_pro` netclass defaults below the manufacturer floor, before any copper exists** (Issue #5875). New `netclass_floor` category, on by default, in the fab-blocking "Manufacturing DRC" bucket: each netclass declared in `net_settings.classes` has its `track_width`, `clearance`, `via_diameter`, `via_drill` and implied annular ring `(via_diameter - via_drill) / 2` compared against the floors the active `--mfr` profile resolved — the same `DesignRules` the post-routing `dimension_*` rules read, board fabrication overrides included. Five rule ids (`netclass_track_width`, `netclass_clearance`, `netclass_via_diameter`, `netclass_via_drill`, `netclass_annular_ring`), each naming the class, the offending field, the declared value and the required one. An unrouted third-party import (e.g. every project `circuit-json-to-kicad` emits, at 0.1mm track/clearance and a 0.05mm implied ring) now fails at import time instead of only after a routing run lays the illegal copper. The three via findings are warnings on a single-copper-layer board, where no via can exist; a project that declares no netclasses, or no `.kicad_pro` at all, reports nothing.

- **Classifier-driven placement feedback now acts on the default `kct route`
  path when the plan is infeasible** (Issue #5890, Epic #5511 Phase 3).
  `--placement-delta-feedback` is tri-state: passing it still forces the loop
  on, the new `--no-placement-delta-feedback` forces it off, and the default
  (neither flag) is **auto** -- the loop runs only when the Epic #5510 routing
  plan reports `overflow_report.feasible: false` and nets remain unrouted. A
  feasible plan, or no plan verdict at all (`--no-routing-plan`, a cache hit),
  leaves the run byte-identical. The loop is now also reachable from the
  `--auto-layers` escalation tails (`route_with_layer_escalation`,
  `route_with_rule_relaxation`, `route_with_combined_escalation`), which is the
  path a default `kct route` actually takes; every applied move is still bounded
  by `--placement-feedback-max-movement` and recorded in
  `<output>_placement_delta.json`.
- **Placement moves are now HV-, creepage- and keepout-aware** (Issue #5890).
  A delta is reverted unless it also leaves the board's **pad-vs-pad** pairwise
  (creepage) violation count unchanged and moves no additional pad of the target
  into a track-blocking `(zone ... (keepout ...))` rule area -- two failures the
  reach and routed-clearance tests structurally cannot see, because both score
  copper the router produced rather than the placement itself. New
  `kicad_tools.router.pairwise_clearance.pad_pairwise_violations()` /
  `pcb_pad_geometry()` expose that scan; both guards are dormant (zero cost, no
  behaviour change) on a board with no `--voltage-map` / signal-spacing table
  and no declared rule areas.

- **Commit-time pad-access invariant, default on for the grid engine**
  (Issue #5891, Epic #5508 Phase 2). A negotiated candidate route is now
  **refused** when committing it would reduce another unrouted pad's *access
  set* — the legal exit stubs on its own layer plus the via sites reachable
  from them, as computed by Phase 1's `router/pad_access.py` — to empty. This
  is the failure mode behind board-05 `ISENSE_A-`, board-06 `U2.B1` and
  board-07 `U4.C2`, where a pad that was reachable at escape-prephase end gets
  enclosed by copper committed for other nets before its own turn; PathFinder
  negotiation cannot see it, because the enclosing copper is legal and
  unshared and never appears in `find_overused_cells`. A refused connection is
  handled exactly like a failed search, so the existing targeted rip-up
  retries it. Each refusal is recorded as a witness naming the protected pad,
  the refused net, the pass/iteration and the copper that closed the pad
  (`Autorouter.pad_access_vetoes`). The gate reports its own cost —
  `PadAccessInvariant.seconds`, measured at **1.0 s of an ~870 s board-06
  route** — and `--no-pad-access-invariant` restores the previous behaviour
  byte-for-byte.

- **`kct route --ripup-strategy sequential-n1`** (Issue #5894, Epic #5784
  Phase 3 step 3). New opt-in prototype that replaces the whole-set negotiated
  loop with a KRT-style sequential router: nets are routed one at a time, and a
  net that fails attributes its blockers by re-running its own search with
  routed copper unblocked (relaxed A\*, #2274), escalates from ripping its top
  blocker to N+1 of them — never a net this pass has not placed, the "slot-0
  guard" — reroutes each displaced sibling at a *soft* congestion cost instead
  of forbidding the vacated corridor, and rolls the whole attempt back if any
  sibling cannot re-land. A whole-pass improvement gate compares the result
  against a no-rip-up baseline pass and reverts if it is not at least as good.
  **The default is unchanged (`negotiated`) and this is explicitly not proposed
  as a default**: on boards 00/01/02/03/06a it wins only on board 01 (vias
  5 → 2, wirelength −12 %, segments 68 → 22, routing-loop 2.79 s → 0.53 s), is
  inert on 00 and 06a, and *regresses* 02 (35/36 → 31/36 connections) and
  badly regresses 03 (103/115 → 69/115). The measurement, including the finding
  that the N+1 escalation never once beat its own no-rip-up baseline on this
  fleet, is in `docs/research/kct-route-ripup-experiment.md`.

- **`find_kicad_cli()` probes before trusting a binary** (Issue #5903). A
  candidate is now run as `kicad-cli version` under a 5 s timeout; a hung or
  failing binary is treated as not found, and the result is memoized for the
  process lifetime (`find_kicad_cli.cache_clear()` to re-probe). Setting
  `KCT_KICAD_DOCKER=1` (image override: `KCT_KICAD_DOCKER_IMAGE`) opts
  `run_pcb_export_svg` into a `kicad/kicad` container when no working native
  `kicad-cli` exists.

- **`docs/guides/routing.md` documents swap groups and recorded deltas**
  (Issue #5904, Epic #5511 Phase 4). A new "Swap Groups and Recorded Deltas"
  section covers declaring a `NetClassRouting.swap_group`, how the
  crossing-minimising pad re-binding is proposed and applied by
  `--placement-delta-feedback`, and how `kicad_tools.router.placement_delta`'s
  `load_placement_deltas` / `pad_map_overrides` / `format_pad_map_report`
  let a recipe replay a reviewed, committed delta artifact as a diffable
  recipe input instead of a silent artifact edit -- using board 07's
  `generate_design.py --placement-delta PATH` as the worked reference.

- **`kct route --format json` says why each connection is unrouted** (Issue #5944).
  A new `unrouted` array labels every unrouted connection `congested` or `blocked`.
  - `congested`: the connection routes once all other signal copper is removed. The entry lists the `contenders`, the nets whose copper lies on that path, as rip-up candidates.
  - `blocked`: there is no path even then. The entry lists the `blockers` (pads, keepouts, board edge, pours) that close off the endpoint.
  - The pass has a time limit, set with `--diagnose-unrouted-budget SEC` (default 20, `0` disables). It is logged as its own `unrouted-diagnosis` stage and phase-profile row.

- **`kct route` / `kct route-auto` gain `--checkpoint PATH` and `--resume PATH`**
  (Issue #5945). `--checkpoint` atomically writes the best board so far on
  every improving pass (nets complete, then DRC/clearance count, overflow,
  wirelength, vias) plus a `.checkpoint.json` sidecar with the score and pass
  number; a worse pass never overwrites it, so a killed run leaves the best
  result on disk. `--resume` seeds a run with a checkpoint's copper, keeping
  its completed nets locked (refused if its footprints/nets differ from the
  input). The negotiated router now reports which iteration produced the
  emitted result, and a multi-net `route-auto` run rolls back a net pass that
  lowers the number of complete nets.

- **Evidence-bound waivers, `kct check --diff`, and explicit coverage states**
  (Issue #5946). Every `kct check` finding now carries a stable `key`
  (`rule|items|nets|layer`) and an `evidence_hash` of its local geometry and
  nets. Waivers schema version 3 adds keyed entries
  `{key, evidence_hash, reason, reviewer, date}` in a committed
  `<board>.kct-waivers.json`. Record them with `kct check BOARD --waive KEY
  --waive-reason ... --waive-reviewer ...`. When the copper or nets under a
  waived finding change, the waiver goes **stale**: the finding returns (marked
  `"waiver_status": "stale"`) and a `waiver_stale` warning names the entry.
  `silkscreen_line_width` findings now name the offending primitive with its
  own geometry (e.g. `fp_line@-2.11/-2.11~-1.635/-2.11`) so that each line
  has its own key. When findings still share a key, their count is part of
  the evidence, so a new look-alike finding makes the waiver stale instead of
  being covered silently. When a per-board sidecar shadows a directory-level
  `.kct_waivers.json`, `kct check` and `kct drc` print a warning.
  `kct check --diff OLD NEW` checks two revisions with the same flags and
  reports findings introduced, resolved and changed, paired by key. Each side
  can be a path or a git `REV:path` spec. It exits 2 when new errors appear.
  The JSON report gains a `coverage` map with one entry per check: `checked`,
  `skipped:<reason>` or `unknown:<reason>`, plus `blocking`. Examples of
  `unknown` are zone-clearance rules on unfilled zones, diff-pair rules
  without a net-class map, and an unreadable `.kicad_pro`.

- **`kct skills install` ships the `/kct:*` agent skills with the wheel**
  (Issue #5950). The skills now live in package data
  (`kicad_tools/agent_skills/kct/`), so `pip install kicad-tools` carries
  them without a clone of this repository. `kct skills install` renders them
  for Claude Code (default, `.claude/commands/kct/`) or Codex
  (`--harness codex`, `.agents/skills/kct-<name>/SKILL.md`) into the project,
  `--user` or `--target DIR`. `--list` shows install state and `--check`
  exits 1 when an installed copy has drifted from the packaged one.

- **`kct skills install --harness opencode`** (Issue #5951). Renders the
  packaged `kct` skills as opencode v2 commands in `.opencode/commands/kct/`,
  invoked as `/kct/<name>`. `--user` writes to opencode's global `commands/`
  dir (`$OPENCODE_CONFIG_DIR`, else `$XDG_CONFIG_HOME/opencode`, else
  `~/.config/opencode`). Only `description` is kept in the frontmatter.
  `docs/agent-surfaces.md` explains why these are commands rather than
  opencode skills.

- **`kct mcp setup --client codex|opencode`** (Issue #5953). Registers
  `kct mcp serve` with Codex CLI (`[mcp_servers.kct]` in
  `$CODEX_HOME/config.toml`, default `~/.codex/config.toml`) and opencode
  (`mcp.servers.kct` in `~/.config/opencode/opencode.json`, or the project
  `opencode.json` with `--project`; `--opencode-schema v1` writes opencode
  v1's `mcp.kct` layout, auto-detected from `opencode --version`). Existing
  servers, settings and TOML comments are preserved, re-running is a no-op,
  an unparseable config is refused rather than overwritten, and `--dry-run`
  / `--format json` work as for the Claude clients.

- **`kct agent-guide`: a harness-neutral primer for agents** (Issue #5960).
  Prints a short Markdown guide shipped in the wheel: the core workflow
  (schematic, ERC, PCB, place, route, zone fill, both DRC engines, LVS/sync,
  export), the `--format json` stdout contract, the two-engine sign-off rule,
  harness setup and common pitfalls. `--harness {claude-code,codex}` shows
  skill invocation examples for one harness only, and `--format json` returns
  `{"version", "harness", "sections"}`. The README gains a "Using kicad-tools
  from Claude Code, Codex and opencode" section.

- **Generated PCBs can carry schematic pin types (`pintype`)** (Issue #5985).
  New `kct pcb annotate-pintypes PCB --schematic SCH` copies each schematic
  pin's name and electrical type onto its pad, as `(pinfunction ...)` and
  `(pintype ...)`, matching KiCad's "Update PCB from Schematic". Pins with a
  schematic no-connect flag get the `+no_connect` suffix, as in KiCad,
  including a flag at the end of a wire stub from the pin and pins on
  rotated, mirrored symbols. Only
  those pad children change; the rest of the file is kept byte-for-byte, and
  re-annotating a KiCad-saved board leaves it byte-identical. `kct pcb
  sync-netlist` now writes the same data. It therefore now rewrites a board
  it previously left alone when only the pad pin types differ, and its JSON
  output gains a `pintype_updated` count. Board 04's generator annotates its
  PCB. As a result, `kct detect-mistakes` pin-type rail evidence (#5939) finds
  `+3.3V` and `+5V` on board 04 from pin types instead of names.

- **`kct route-auto --format json` says why a net is unrouted** (Issue #6001).
  A partial or failed `nets[]` entry now carries the same `unrouted` array and
  `unrouted_diagnosis` summary as `kct route` (#5944): each connection the
  strategy left apart is `congested` (with the nets in the way as
  `contenders`), `blocked` (with the pads, named keepout rule areas or board
  edge closing it off as `blockers`) or `unclassified`. The pass runs on the
  board at the strategies' fine grid resolution. A net that only a corridor
  strategy tried (e.g. `--strategy global`) is `unclassified` with a note
  saying so. New `--diagnose-unrouted-budget SEC` flag (default 20; `0`
  disables).

- **`kct detect-mistakes` takes evidence-bound waivers, and `kct check` /
  `kct detect-mistakes` emit SARIF** (Issue #6006). Mistake findings now carry
  the same stable `key` and `evidence_hash` as `kct check` findings, and
  `kct detect-mistakes --waive KEY --waive-reason R --waive-reviewer ME`
  records keyed v3 waivers in the shared sidecar. A waiver goes stale when the
  footprints, nets or measured values (each check's structured
  `measurements`, not its explanation text or thresholds) under the finding
  change; mistake hashes use their own `ev2.m1:` prefix. New
  `--format sarif` on `kct check`, `kct check --diff` and
  `kct detect-mistakes` writes a SARIF 2.1.0 log: the finding key is the
  partial fingerprint, the evidence hash is the result fingerprint, waived
  findings are suppressed, and board coordinates ride in each location.

- **`kct optimize-placement` pulls decoupling caps onto the IC supply pins
  they serve** (Issue #6020). A new soft cost term finds every capacitor
  between a supply rail and ground and the IC pins on that rail (net names,
  plus schematic pin types when the board carries them: an `EN` pin tied to
  the rail is not a target), pairs caps with pins one-per-pin before any pin
  gets two, and scores the summed cap-to-pin distance. A post-optimizer snap
  pass then moves each cap to the nearest free spot beside its pin without
  adding an overlap or crossing a signal pin's escape lane. On by default
  (`--weights '{"decoupling": 0}'` turns it off); the JSON report lists each
  cap's pin and distance. On board 04's hand floorplan,
  `--seed current --max-iterations 0` moves only the caps and
  takes every STM32/LDO supply pin from 6-14 mm to 1.7-2.3 mm of a cap. `kct placement
  optimize --cluster` springs each cap to its own supply pin the same way
  instead of to the IC's pin 1.

- **`kct route --lint-gate` rolls back a route that introduces new lint findings** (Issue #6054).
  The input board is linted before routing and the routed board after it (`kct check` +
  `kct detect-mistakes`, paired by stable finding key as in `kct check --diff`). A new
  error finding restores `--output` to its pre-run contents, keeps the routed board as
  `<output-stem>.lint-rejected.kicad_pcb`, prints the introduced keys plus a ready-to-run
  `--waive` command, and exits 3; evidence-bound waivers exclude reviewed findings.
  `--lint-gate-strict` (or `--strict`) also blocks new warnings, `--lint-gate-waivers PATH`
  overrides the waivers sidecar. One post-route hook covers every `kct route` path;
  `kct route-auto --lint-gate` gates the whole run. Under `--format json` the single
  document gains a `lint_gate` object. `kct check` / `kct detect-mistakes` `--waive`
  now create a `--waivers PATH` that does not exist yet instead of refusing.
  Copper finding keys are geometry-based, so a copper waiver carries over to any
  re-run that routes the same copper, regardless of `--seed` (Issue #6088).

- **`kct panel --cut vcut` uses the fab's published V-score clearance** (Issue
  #6177). Manufacturer profiles gain `min_copper_to_vscore_mm`, sourced from
  each fab's capability pages: JLCPCB, JLCPCB Capability Plus and PCBWay
  publish 0.4 mm. OSHPark, Seeed and FlashPCB publish no figure. For those,
  a conservative 0.5 mm default (never below the profile's routed
  copper-to-edge minimum) is used and reported as unsourced. The fab comes
  from the shared `--mfr` resolver (new `--mfr` flag, else the
  `fab_profile.json` sidecar, else `project.kct` `target_fab`, else
  `jlcpcb`). JSON output adds `vscore_clearance_source` and `mfr`.

- **`kct panel --cut vcut` warns when the fab has no V-score service** (Issue #6205).
  New profile flag `supports_vscore` (set `false` for OSHPark, sourced to its
  panelization docs); text and JSON `warnings` suggest `--cut mousebite`, and the
  JSON gains an additive `vscore_supported` key.

- **`kct optimize-placement --route-check` confirms a placement with a real
  route** (Issue #6234). Opt-in. The board as read is the incumbent. The
  optimizer result, and then the decoupling snap, each replace it only if a
  bounded `kct route` reaches at least the incumbent's completion with no more
  post-route DRC errors; otherwise the board is written unchanged. At most
  three routes per run, each capped by `--route-check-budget` node expansions
  per net (default 100,000) and seeded, so the verdict is reproducible.
  `--format json` gains a `route_check` object, and the MCP
  `optimize_placement` tool takes `route_check` / `route_check_budget`.

- **`kct board-view` draws board images that answer a routing question**
  (Issue #6316). Five views: `overview`, `net <net>`, `layers <net>`,
  `crop x0 y0 x1 y1` and `list`. Unlike `kct screenshot`, the image carries
  net identity: mm axes, reference designators, pad net labels, one net
  outlined, and the connections still missing as ratsnest lines. The missing
  links come from the connectivity model behind `kct check`, so the picture
  and the check agree; pour nets that check treats as advisory are listed
  under `pour_advisory` with no link, and zone pours are not drawn. Axes are
  board-relative by default (the frame of `kct net-status` and
  `kct pcb move-footprint --to`); `--absolute` uses the sheet coordinates
  `kct check` reports. The title and the JSON name the frame and the board
  origin. Images are at most 1568 px on the long side and need matplotlib
  (`pip install 'kicad-tools[visualization]'`), not `kicad-cli`. `list` needs
  neither and prints the missing pad pairs as JSON. The MCP server gains a
  matching `board_view` tool. `kct agent-guide` says which questions suit an
  image and that a reading is a hypothesis to confirm with the exact tools.

- **`kct board-view` overlays: zone fill, DRC markers, net classes, before/after** (Issue #6317).
  New opt-in flags `--zones` (translucent filled pours), `--drc-report` (violation locations,
  shifted by the board origin in the default frame), `--net-classes` (copper coloured by net
  class) and `--compare AFTER_PCB` (side-by-side pair over one window). Each new colour is
  named in the image title and `color_key`; the `board_view` MCP tool takes the same options.
  Default output is unchanged.

- **`kct bench external --suite pcbworld`** (Issue #6331). Fetches pinned
  PCBWorld and PCBench revisions into the benchmark cache, prepares a
  deterministic `--split d3a|d3b --limit K` smoke subset of D3 `guide` boards
  with PCBWorld's own external prep, routes them without `normalize`, and
  records PCBWorld's Clean Pass in an additive `external_scores.pcbworld`
  report block (schema stays v1) and a separate `PCBWorld CP` column, apart
  from the local `kct check` / `kicad-cli` gates. Requires a KiCad 9 toolchain
  (`PCBWORLD_PYTHON`, `KICAD_CLI`, `PCBNEW_PYTHON`); DeepPCB/SRJ18 runs are unchanged.

- **`kct bench external --suite pcbworld` can run and publish a full D3
  split** (Issue #6332). New `--full-split` (the denominator is the whole
  split, d3a = 99 boards), `--offset`, `--rollouts N`,
  `--deterministic-baseline`, `--route-timeout`, `--max-attempts` and
  `--resume` flags. Every board x rollout gets a compact attempt record --
  a result or an explicit failure -- written to `results.json` and rendered
  to `report.d3.md` with CP@1 / CP@5 (PCBWorld's max-potential-gain
  selection), the stricter local bar, routability, vias, wirelength versus
  the human guide board, wall-time distributions and the fair-95 subset.
  `--resume` reuses an attempt only when its board, seed, input hash, tool
  commit, upstream pins, KiCad version and router options all match.
  `--render-d3 results.json` re-renders a committed report.
  `benchmarks/external/seed_variation.py` measures whether `kct route --seed`
  changes routed geometry.

- **`kct net-status --crossings` reports forced bundle crossings before routing** (Issue #6339). Read-only pre-route report: for each pair of parts joined by three or more two-terminal signal nets it lists both pin orders, the inversion count per entry side (both sides for L-shaped bundles), the inverted pairs at the minimum, and bundles whose conflict graph needs more colours than the signal-layer count (`--signal-layers N` overrides the board's count). It also lists hub pin columns whose exit directions interleave. `--format json` emits the same data; without the flag output is unchanged. See `docs/research/bundle-crossing-report.md` for measured hit and false-alarm counts.

- **Experimental min-cost-flow escape-corridor assignment** (Issue #6352).
  `EscapeCorridorPlanner(assignment="flow")` assigns a dense package's pins
  to `(face, layer)` escape exits jointly by min-cost max-flow, using
  capacities read from free grid cells, and falls back to the greedy plan when
  the flow is empty or infeasible. You select it with the hidden,
  research-only `kct route --escape-corridor-assignment flow`, which also
  turns on `--escape-corridor-reservation`. Default routing is unchanged. The
  A/B result is in `docs/research/kct-route-escape-flow-experiment.md`.

- **`kct route --nogood-cache` experimental opt-in retry cache** (Issue #6353). Bounded cache of failed negotiated route attempts: only failures whose search drained its open set are stored (never timeouts or iteration-cap give-ups), and a hit requires every grid plane the search reads (occupancy, usage, history/present cost, obstacle, zone) to be unchanged. Reports hit/miss/insertion/invalidation/eviction diagnostics; default behaviour unchanged. Research verdict in `docs/research/kct-route-nogood-cache-experiment.md`.

- **`kct route --lattice-iterations N` sets the lattice negotiation round cap**
  (Issue #6362). The whole-netset lattice negotiation was capped at a
  hardcoded 12 rounds that `--iterations` (the grid negotiator, default 15)
  never reached. The new flag applies to `--route-engine lattice` and
  `--complete`; unset keeps 12, so existing runs are unchanged, and `N < 1` is
  rejected. An unconverged negotiation now logs which limit stopped it:
  `stopped by: round cap (N)` (raise `--lattice-iterations`) or
  `stopped by: deadline` (raise `--timeout` / `--per-net-timeout`).

- **`kct pcb diff OLD NEW` shows which copper a change touched** (Issue #6417).
  A geometric board diff over segments, arcs, vias, footprints, pads, zones,
  zone fills and board graphics. Footprints and pads pair by reference, so two
  identical parts that trade places are both reported. Copper, zones and
  graphics with identical normalised content count as unchanged whatever
  their uuids, so a regenerated or reformatted board reports nothing;
  leftovers pair by uuid (same kind and net) as `changed`, and the rest are
  `removed` or `added`. Both sides accept
  `REV:path`, `--net` / `--layer` filter the report, `--format json` is
  key-sorted, and `--svg` writes a colour-coded overlay. Exits `2` when
  anything differs, so CI can assert that a PR leaves a board's copper alone.

- **`kct check` limits the uncoupled length of a coupled diff pair**
  (Issue #6443, epic #6441). New rule `diffpair_uncoupled_length`: per net it
  measures the copper that runs beside no partner copper, the way KiCad's
  `diff_pair_uncoupled` does (the larger of the two nets, not P + N; exactly
  parallel same-layer overlap; via barrels not counted), and holds it to
  `max_uncoupled_total_mm` on the class or the protocol's per-terminal
  allowance times the number of components on the net (6.35 mm x 3 = 19.05 mm
  for board 03's `USB_D`). It also holds each net's via count to
  `max_vias_per_net` and warns on unequal P and N via counts. The routed
  `.kicad_dru` gains the matching native `diff_pair_uncoupled`, `skew (..)
  (within_diff_pairs)` and `via_count` rules per coupled pair, in the managed
  `diff_pair_gap` block; a limit that is an `uncited` protocol value is written
  as `(severity warning)`. A pair KiCad does not recognise by name
  (`X_POS`/`X_NEG`) gets the skew and via-count rules by net name, and
  `kct check` says its uncoupled limit is kct-only. The benchmark harness's
  pair rows carry per-net uncoupled length, the limits and each engine's
  pass/fail per rule. kct's figure is a lower bound of KiCad's on real copper
  (see `docs/guides/diff-pairs/08-uncoupled-length.md`, "Known divergences").

- **`kct check --mfr` flags blind, buried and micro vias the fab cannot build**
  (Issue #6518). New `via_type` category (rule `via_type_unsupported`,
  manufacturing, error severity) backed by two new manufacturer-profile
  capabilities, `blind_buried_vias_supported` and `micro_vias_supported`.
  Each offending via is named with its position, net and layer pair.

- **`kct route --fallback-bidirectional`** (Issue #6527). Experimental, default-off
  flag (`DesignRules.fallback_bidirectional`) that makes the C++ backend's Python
  fallback call `Router.route_auto` (bidirectional A* for long paths) instead of the
  one-way `Router.route`. `route_auto`/`route_bidirectional` now accept
  `per_net_timeout` and `extra_goal_cells`, set `last_route_exhausted` /
  `last_route_deadline_hit`, and a failed bidirectional attempt plus its one-way
  retry share one deadline.

- **`diffpair_uncoupled_length` checks uncoupled copper per terminal** (Issue
  #6615). Besides KiCad's total, each terminal (a footprint with a pad on the
  pair net) is held to `max_uncoupled_per_terminal_mm`, counting only uncoupled
  copper outside every terminal's courtyard and charging it to the terminal
  nearest along the copper. New `NetClassRouting.gather_ignore` field
  (`"courtyard"` default, `"none"`, or a radius in mm); footprints without a
  courtyard fall back to their pads' bounding box plus 0.25 mm. The finding is
  marked kct-only (KiCad has no equivalent); the total figure is unchanged.

### Changed

- **The generated `.kicad_pro` `min_silk_clearance` value is now a named
  constant** (Issue #5704). Before this change the key was derived from the
  profile's `min_solder_mask_clearance_mm`. It is now the fixed constant
  `PROJECT_MIN_SILK_CLEARANCE_MM` (0.05 mm), which is the same value every
  built-in profile produced before. Generated project files are therefore
  byte-identical and no committed board was regenerated. KiCad 10.0.1, 10.0.5
  and 10.0.6 were measured to ignore this key for silk-to-pad gaps. The
  factory silkscreen floor still reaches native DRC through the `Silk to Pad`
  rule in the `.kicad_dru`. A custom `DesignRules` with a different
  mask clearance no longer changes this key.

- **CI cost cut: Diff-Pair Routing Regression moves to main/nightly, board jobs get per-board path filters, benchmarks run more often** (Issue #5743). The Diff-Pair Routing Regression job (kept, assertions unchanged) now runs on main pushes and a nightly schedule instead of every PR. Board jobs run on PRs only when their own `boards/NN-*/` directory or shared code changes (any non-board code change still runs all boards). `Slow / Benchmark Tests` now also runs on main pushes touching code and twice daily, still never as a PR check.

- **`jsonschema` moved out of the core dependencies into the `mcp` extra**
  (Issue #5804). It has exactly one call site, the `tools/call` argument
  validation in `kicad_tools/mcp/server.py`, and is imported function-locally
  there so nothing outside an MCP tool dispatch needs it -- so a plain
  `pip install kicad-tools` no longer pulls in `jsonschema` plus its four
  transitive dependencies (`attrs`, `jsonschema-specifications`, `referencing`
  and the compiled `rpds-py` wheel) for the many users who only run `kct route`,
  `kct check` and friends. `jsonschema` is now declared under the `mcp` extra
  alongside `fastmcp`/`pydantic`, and carried in the `all` and `dev` extras, so
  `kicad-tools[mcp]`, `kicad-tools[all]` and the development environment are
  unchanged.

- **The post-route DRC-nudge repair pass measures clearance with the shared
  kernel** (Issue #5856). `_post_nudge_introduces_foreign_via_violation`,
  `_via_drill_overlaps_bbox` and `_via_edge_sweep_clear`
  (`router/drc_nudge.py`) now take every verdict from the shared clearance
  kernel — the same model search, commit-time validation and `kct check` use —
  instead of their own distance arithmetic, so a repair the nudge pass accepts
  is one the final DRC gate agrees is clear. The gates' verdicts are
  unchanged (pinned verdict-for-verdict against the previous arithmetic in
  `tests/router/test_drc_nudge_kernel_5856.py`), with one deliberate
  exception: the foreign-via gate now compares under the kernel's shared
  `CLEARANCE_EPSILON_MM` (1e-4 mm, KiCad's integer-nanometre band) rather than
  a private 1e-9, so a candidate a tenth of a micrometre short of the
  requirement is admitted as a rounding tie instead of refused. No rule value
  or heuristic changed.

- **`kct route` short gate now shares one predicate and no longer fails open silently** (Issue #5872). Both CLI short gates filter through `router.io.shorting_violations` (the helper `count_shorting_violations` is now defined in terms of it), and if the post-route short audit raises, a `WARNING: post-route short audit failed to run (...); shorts were NOT checked` line is printed instead of silently reporting a clean board.

- **The pose-centerline diff-pair search is on by default** (Issue #5895). A
  pair the joint-state coupled search cannot couple is now retried as one
  centerline trunk (#5786) unless `KCT_POSE_CENTERLINE=0` is set. Before a trunk
  is committed, a new corridor guard probe-routes the unrouted nets with a pad
  within 2.5 mm of the pair's end pads, with and without the trunk. A trunk that
  seals one of them gets one re-route with those corridors reserved, and is
  declined if it still seals one (`[coupled-pose] declined: seals-corridor`);
  `KCT_POSE_CORRIDOR_GUARD=0` switches the guard off. Board 06b's minimum pair
  coupling goes from 0% to 87.0% (18/18 nets, 0 shared-referee DRC errors). On
  board 06 the guard declines the MIPI_D0 trunk that sealed MIPI_RST, so the
  Diff-Pair job still routes 21/21 within its allowlist.

- **`kct` skills and MCP tool descriptions are harness-neutral** (Issue #5954).
  Skill bodies no longer hard-code `/kct:` invocations, `.claude/` paths,
  `CLAUDE.md` or demo-board paths. They use placeholders that
  `scripts/install-kct.sh` (and the new `kicad_tools.agent_surfaces.render`)
  fill in per harness, so Claude Code installs read the same as before and
  Codex installs get `$kct-<name>` and `.agents/skills/` paths. A lint test
  keeps it that way, and `docs/agent-surfaces.md` lists what each skill needs
  from its harness.

- **`kct check` evidence hashes only cover the net pads near a finding**
  (Issues #6011, #6041). A located finding that names a net (for example `GND` in a
  clearance finding) now hashes only that net's pads whose copper outline
  comes within 3 mm (clearance, width and dimension rules) or 5 mm (other
  rules) of the finding, with their positions in sheet (file) coordinates. Adding a `GND` pad elsewhere on the board no longer turns
  its evidence-bound waiver stale; adding, removing or moving a nearby pad
  still does. Whole-net rules (`connectivity`, `single_pad_net`, `ampacity`,
  `path_ampacity`, the diff-pair and match-group skew rules and similar) and
  findings without a location keep the full net membership. The hash prefix
  is now `ev2:`.

- **`silk_edge_clearance` findings name the individual silk line**
  (Issue #6015). Graphic items now carry the primitive's own geometry,
  using the same footprint-local naming as `silkscreen_line_width`
  (`J1 (fp_line@-1.62/6~-1.62/8.73)`, `gr_line@0/5~0/8`). Two strokes of
  one footprint near the board edge now get distinct finding keys and
  evidence hashes, so waiving one no longer covers the other. This removes
  the last key+hash twin on board 03. A `waiver_unused` advisory for an
  entry written against the old coarse `fp_line` name now lists the
  per-line keys to re-waive with `kct check --waive KEY`.

- **FOM default weights re-calibrated on the current fleet boards** (Issue #6021).
  Three of the ten weights in `kicad_tools/optim/weights/default.yaml` moved:
  `net_congestion_variance` 0.2226 -> 3.0319, `trace_length_excess`
  1.6126 -> 0.8591 and `compactness` 0.0968 -> 0.0114. The old values sat
  outside the range of all 40 fits, run across four perturbation samples and
  eight NSGA-II seeds. On four held-out perturbation samples, train-board
  selection score (geo-mean rank consistency minus
  half its spread) improves from 0.77-0.84 to 0.86-0.93. The other seven
  weights are unchanged. Only `kct optim fom-debug` and the
  `kicad_tools.optim.default_weights()` API read these weights; no
  placement or routing command does, so no board's layout changes. Evidence
  is in `docs/research/fom_calibration.md`. Re-running
  `scripts/research/calibrate_fom.py` now keeps the report's hand-written
  `## Addendum` sections, and the `fom-debug --weights` help no longer
  claims the default is uniform 1.0.

- **Demo board 02 carries schematic pad pin types** (Issue #6025).
  `boards/02-charlieplex-led/generate_design.py` now copies each schematic
  pin's name and electrical type onto its pad (`(pinfunction ...)`,
  `(pintype ...)`). The committed unrouted and routed PCBs, the readiness
  evidence copies and the manufacturing package are regenerated by
  `package_release.py`; the committed copper is unchanged.

- **Demo boards 00 and 01 carry schematic pad pin types** (Issue #6025).
  `boards/00-simple-led` and `boards/01-voltage-divider` generators now copy
  each schematic pin's name and electrical type onto its pad
  (`(pinfunction ...)`, `(pintype ...)`), as KiCad's "Update PCB from
  Schematic" does. Their committed unrouted and routed PCBs and their
  readiness packages are regenerated. The copper is unchanged.

- **Generated schematics now get deterministic UUIDs, so regenerating a board
  writes the same bytes** (Issue #6076). Every UUID the schematic model used to
  draw from `uuid4` is now a `uuid5` under one pinned namespace
  (`kicad_tools.core.schematic_uuids`). The root sheet is keyed on project
  name, title and page. Placed and power symbols are keyed on sheet and
  reference, and get that UUID when they are placed, so a PCB path built from
  `symbol.uuid_str` matches the file. Pins are keyed on their symbol's UUID
  and number. Wires, junctions, no-connects, labels and text notes are keyed
  on their content, with an ordinal for duplicates. UUIDs loaded from a file
  are never rewritten, and a new UUID never repeats one the file already
  holds, so load-edit-save of a KiCad schematic renumbers nothing. The same
  scheme now covers `kicad_tools.schema.Schematic.add_*`, `PCB.create` /
  `add_footprint` / `add_trace` / `add_via`, `Project.create`, and the fleet
  generators' own PCB-side `generate_uuid()`. A fresh `generate_design.py`
  run now writes a byte-identical `.kicad_sch` and unrouted `.kicad_pcb`. The
  committed board outputs are not regenerated here. New
  `kicad_tools.core.pcb_path_remap.remap_pcb_file_paths` moves a board's
  footprint `(path ...)` links from an old schematic's UUIDs to a
  regenerated one's by reference designator, and refuses on any unmapped or
  ambiguous footprint.

- **`kct check` keys copper findings by geometry, so waivers survive a
  re-route** (Issue #6088). Tracks, arcs and vias in a finding's `items` (and
  so its `key`) were named by UUID prefix (`Trace-1a2b3c4d`), and an unseeded
  `kct route` mints fresh UUIDs on every run: two routes with identical copper
  shared 6 of 185 UUIDs, so a keyed waiver never carried over. They are now
  named by what they are, in sheet coordinates rounded to 1 µm with end points
  in a fixed order: `Trace@F.Cu:w0.25:24.2/15~24.7/15.5`,
  `Arc@<layer>:w<width>:<start>~<mid>~<end>`,
  `Via@25.2/14.9:F.Cu-B.Cu:d0.3/s0.6`. `width_consistency` names a run as
  `Run@<layer>:w<width>:<end>~<end>:n<tracks>:h<digest>` (the digest covers the run's interior vertices) instead of listing every
  track's UUID. Identical copper now gives identical keys and evidence hashes
  whatever its UUIDs, and copper that moves by more than 1 µm gets a new key.
  Affects `clearance_*` (including segment/via vs. zone fill),
  `diffpair_clearance_intra`, `via_in_pad`, `via_under_body` and
  `width_consistency`.

- **`physical_copper_gap` names copper by geometry, so its waivers survive a
  re-route** (Issue #6106). The opt-in rule named every track, arc and via in a
  finding's `items` (and so its `key`) by raw UUID, which an unseeded
  `kct route` regenerates on every run. Tracks, arcs and vias now use the
  `copper_refs` descriptors #6088 introduced for the other copper rules
  (`Trace@F.Cu:w0.2:0/0~4/0`, `Arc@...`, `Via@...`), in sheet coordinates
  rounded to 1 µm. Identical copper gives identical keys and evidence hashes
  whatever its UUIDs. Pads, footprint polygons and zones keep their existing
  names. The board 05 regression helper `_extract_ref_from_items` no longer
  misreads a `Trace@` item with a negative coordinate as a `<ref>-<pin>` pad.

- **`kct optim fom-debug` shows readable scores** (Issue #6112). Text output now prints the score in scientific notation plus the linear `penalty` (sum of w*term); JSON gains `penalty`. Docs state that scores rank placements of the same board and are not comparable across boards.

- **`kct route` defaults `--manufacturer` from the board's `project.kct`**
  (Issue #6155). Without an explicit `--manufacturer` / `--mfr`, route now
  uses the `target_fab` declared in `project.kct` next to the input board (or
  one directory up), through the same resolver as `kct check`, and prints
  `[INFO] auto-loaded fab profile: <tier> (from project.kct target_fab)`.
  An explicit flag still wins; with no `project.kct` the default is still
  `jlcpcb`. A bare route of a `jlcpcb-tier1` board (board 03) now applies its
  tier-scoped `fabrication_overrides.json` instead of rejecting it, and route
  and `kct check` judge the board at the same tier. Unlike `kct check`, route
  does not read a `fab_profile.json` sidecar, because it writes that file itself.
  With `--lint-gate`, a tier resolved from `project.kct` is passed on to the lint.

- **`export`, `fix-vias`, `fix-silkscreen`, `fix-drc`, `audit`, `pipeline`, `reason` and
  `estimate cost` default `--mfr` from the board's `project.kct`** (Issue #6169). Without
  an explicit `--mfr`, each command now uses the `target_fab` declared in `project.kct`
  next to the board (or one directory up; for a project directory argument, in that
  directory), through the resolver `kct check` and `kct route` share, now in
  `kicad_tools.manufacturers.resolve`. It prints `[INFO] auto-loaded fab profile: <tier>
  (from project.kct target_fab)`. An explicit flag still wins; with no `project.kct` the
  default is still `jlcpcb`. Read-only and repair commands also honour a `fab_profile.json`
  sidecar, as `kct check` does; `pipeline` does not, because its route step writes that
  file. `kct reason` gains a `--mfr` flag it previously lacked at the top level.

- **Stricter authored DRC minima now win by default** (Issue #6191). When kct
  merges a manufacturer profile into a board's `.kicad_pro` and `.kicad_dru`,
  each minimum in `design_settings.rules`, `design_settings.defaults` and the
  `Default` netclass becomes `max(authored, profile)`. The `.kicad_dru` scalar
  floors follow the same rule, so native zone fill and `kicad-cli` DRC honour
  a stricter authored clearance too. The profile still raises any authored
  value below the fab floor. Previously this needed the
  `KCT_PRESERVE_BOARD_RULES=1` opt-in (#5023). That mode is unchanged, and
  `KCT_PRESERVE_BOARD_RULES=0` is a new opt-out that restores the legacy
  overwrite.

- **`rtree` is now a core dependency** (Issue #6198). The router's rtree-less
  collision fallback was never exercised in CI (every job installs the dev
  extras) and hid #6184 and #6185. `rtree` ships prebuilt wheels for
  Linux/macOS/Windows (x86_64 and arm64), so a plain `pip install kicad-tools`
  now always takes the same collision path CI tests. The `drc`/`all` extras
  remain as aliases.

- **`kct route` fine-zone pitch no longer depends on where a part sits**
  (Issue #6328). The adaptive grid's off-grid escalation solved each fine
  zone's pitch from only those pads of the part that were off the global
  coarse grid. That subset changes when the part moves, so a placement-only
  change could swing the pitch: board 05's `U2` got 0.05 mm at 1x and
  0.005 mm at 1.05x. The pitch is now solved from all of the part's pads; the
  off-grid subset still decides whether a part gets a zone, so single-pad and
  one-off-grid-pad parts (e.g. mounting holes) still do not. Routing inputs
  shift on affected boards: board 05's `U2` zone is now 0.0125 mm at every
  scale, raising its estimated cell count at 1x from about 3.93 M to 4.66 M
  (the old 0.05 mm zone left 32 of `U2`'s 59 pads off-grid).

- **`kct route --early-stop-patience 0` now runs the full negotiated budget**
  (Issue #6361). Patience 0 (library: `best_stall_patience=None`/`0`) now
  disables the overflow-stagnation stop as well as the #3101 best-metric
  stop, so the negotiated loop runs to `--iterations` or `--timeout` (ending
  early only when no nets are left to rip up or on the power-net stall
  abort). Both early-stop log lines now report the iteration and timeout
  budget they leave unused.

- **Evolutionary routing genes now reach the router** (Issue #6366). In
  `--strategy evolutionary`, each chromosome's `astar_weights` is applied as a
  per-net A* heuristic weight (Python and C++ pathfinders). `preferred_layers`
  is applied as a per-net layer preference drawn from the board's routable
  layers (hard-avoided layers are never preferred) on the pure-Python
  pathfinder only: the default C++ pathfinder has no layer-preference cost,
  so on that backend the layer gene is switched off and only net order and
  A* weights evolve. A net with no entry has no preference, so the seed
  chromosome still equals the default route. The inert `strategy_flags` gene
  was removed. `--timeout` is now split across the remaining generations, not
  just the current one, so generation 1 no longer consumes nearly the whole
  budget.

- **Board 05 routed copper honours its own net classes** (Issue #6395). The
  27 `netclass_clearance_copper` findings in the committed
  `bldc_controller_routed.kicad_pcb` are repaired in the copper (U2 fan-out and
  signal vias drawn at 0.45/0.2 mm, stitch vias nudged along their stubs, a VM
  run moved off the C8-1 pad), not by loosening any class. `kct check` and
  `kicad-cli pcb drc --all-track-errors --refill-zones` both report 0, the
  board 05 entry is removed from `.github/routed-drc-tolerance.yml`, and the
  saved zone fills, Gerbers and `readiness.json` are re-pinned.

- **`kct route` prints the diff-pair thresholds `kct check` will apply** (Issue #6442).
  After the pair phase the log now lists, for every engaged pair, the continuity
  threshold and skew tolerance resolved by the same code `kct check` uses, so
  board 03's `USB_D` reads 25.0 % in both places instead of the router implying 70 %.
  The class-name `setdefault` collapse in lattice pair detection (per-net variants of
  `HighSpeed`) is replaced by `build_synth_routing`, which no longer lets the first
  net of a class speak for the rest. Also adds a router-produced diff-pair benchmark
  (`scripts/research/expand_route_experiment.py pairs`) for later pair phases.

- **Differential-pair limits now come from one protocol defaults table**
  (Issue #6443, epic #6441). A net class can declare `pair_protocol`
  (`USB2_FS`, `USB2_HS`, `USB3_SS`, `LVDS`, `PCIE_G1`, `PCIE_G2_PLUS`,
  `MIPI_DPHY`, `ETHERNET`, `HDMI_TMDS`, `CUSTOM`) and take its skew limit from
  `kicad_tools.router.pair_protocols.PROTOCOL_DEFAULTS`: 2.5 mm for full-speed
  USB2 (Renesas full-speed board guide, Table 2) and 1.27 mm for high-speed
  USB2 (TI SPRAAR7J App. A). Every table value carries its source or the tag
  `uncited`. A limit resolves as class field, then protocol default, then the
  generic 0.5 mm, in one resolver that the router, its pair report and
  `kct check` all read; each prints where the limit came from. The table also
  holds the per-terminal uncoupled-length allowance (6.35 mm, TI SPRAAR7J
  §3.3), the via count and the gap; the uncoupled length and via count are
  checked by `diffpair_uncoupled_length`. `DifferentialPairRules.for_type` is now a deprecated shim over
  the same table. An unknown `pair_protocol` raises `ValueError` when the
  class is constructed.

- **Dependency updates move from Renovate to Dependabot.** `.github/dependabot.yml`
  is rendered from the operator's dependency policy: weekly on Monday, a 14-day
  cooldown, security fixes not delayed, `nanobind`/`cmake`/`numpy` and Astro kept
  in their own PRs as before. `renovate.json5` goes in a follow-up PR.

- **Renovate config removed.** Dependency updates now come only from Dependabot
  (`.github/dependabot.yml`, #6476); `renovate.json5` is gone.

- **`kct check` and `kct net-status` JSON no longer record absolute PCB
  paths** (Issue #6532). The top-level `"file"` (`kct check --format json`
  and `--output`) and `"pcb"` (`kct net-status --format json`, including
  `--why`) values are now relative to the current directory when the board
  lives under it, and the bare filename otherwise. Committed board evidence
  therefore no longer leaks host paths such as `/home/...` or
  `/private/var/...`. `kct readiness` still records the board-relative
  `output/<name>.kicad_pcb`. Existing evidence is not regenerated; a new test
  only lets the list of boards that still carry an absolute path shrink.

- **`kct route` auto-pour honours `--net-class-map`** (Issue #6599). A net
  whose map entry declares it routed (`is_pour_net: false` and `route_via`
  other than `"pour"`) no longer gets an auto-created pour zone, even when its
  name looks like a power or ground rail; the skip is logged as
  `Auto-pour: skipping VBUS (net-class map declares it routed)`. Nets absent
  from the map keep the name heuristic, and a net forced with `--skip-nets` is
  still poured. All four `kct route` flows (main, layer escalation, adaptive
  rules, combined escalation) pass the map. The reverse case (a map
  `route_via: "pour"` net the heuristic does not recognise) is not added.

- **RSMT Steiner branch points no longer force a junction via** (Issue #6634).
  Virtual Steiner pads accept arrival on any routable layer (Python and C++
  A* goal checks) and are pinned to the layer the first incident edge reached
  them on, so later edges start there. A transition now appears only where the
  routing needs one, not to land on terminal 0's layer.

- **`kct check --mfr pcbway` accepts blind and buried vias on 4- and 6-layer
  boards** (Issue #6640). The PCBWay profile now sets
  `blind_buried_vias_supported` on its 4- and 6-layer configs, citing PCBWay's
  published HDI capability page (verified 2026-10-14), so `via_type_unsupported`
  no longer fires there; drill/diameter/annular-ring floors still apply. Micro
  vias stay rejected until the laser-drill window and adjacent-layer span are
  encoded. The check does not verify the span set is laminable. The 2-layer
  configs and every other shipped profile keep the fail-closed `false` default.

- **`kct route` incomplete-routing hints no longer suggest manual KiCad completion** (Issue #6672). Hints, the `--export-failed-nets` help and the JSON diagnostics now point at `kct net-status --why`, `--timeout` and `--layers`. The fine-pitch report's recommendations now suggest `--fine-pitch-clearance` and `kct net-status --why` instead of manual routing, the MCP `route_net` failure suggestions say to diagnose with `kct net-status --why` or file a router gap, and congestion-analysis suggestions point at `kct placement optimize` or more routing layers.

- **User-facing "do it by hand" hints replaced with `kct` commands** (Issue #6704). Recovery fallback, `kct stitch` skipped pads, PCB netlist-sync origin warning and ERC duplicate-reference fixes now name a concrete command or say to file an issue.

### Performance

- **Pure-Python A\* fallback no longer measures a dead shapely distance per halo neighbour** (Issue #5617, part of #5240). `RouteHaloGeometry.clear` computed `shape.distance(other_shape)` for every neighbour surviving the bounding-box prune, but only the via-vs-via drill-pitch floor reads it; trace and via-vs-trace probes now skip the call (about 16.7 s of the ~101 s fallback in the Diff-Pair re-route step's phase 4). Verdicts and routing output are unchanged.

- **Trace optimizer skips untouched blocked cells in `path_is_clear`** (Issue #5617). `GridCollisionChecker` now pre-reads the obstacle/pad/net planes for a segment's blocked cells in one array op and walks only the cells a branch acts on; verdicts are unchanged. On Board 06 the "Optimizing traces" phase measured 43.9 s -> 17.0 s (single local run each).

- **Trace-optimizer grid collision walk skips unblocked cells** (Issue #5617).
  `GridCollisionChecker.path_is_clear` now reads the blocked plane for the
  whole path window in one array operation and only visits blocked cells,
  instead of allocating a cell view per unblocked cell (24.8 M `cell_at`
  calls in one Board 06 re-route). Verdicts and routing output are unchanged.

- **Pure-Python A\* own-pad step gate skips pads that cannot violate** (Issue #5617, part of #5240). `RoutingGrid.segment_pad_clearance` gained a `violation_only` mode used by the pathfinder's own-pad step check: pads whose bounding circle, grown by the trace half-width and the pad's required clearance, is out of reach of the step's bounding box are skipped before the exact rect/disc distance, and per-component clearance lookups are memoised within a call. The violation verdict is unchanged; routing output is unchanged.

- **Router clearance kernel's segment-to-segment distance is about 2x cheaper per call** (Issue #5617). `core.geometry.segment_to_segment_distance` inlines its intersection test and four point-to-segment distances, and `segments_intersect` no longer rebuilds a closure per call. Arithmetic (including NaN and -0.0 clamp behaviour) is unchanged, so results are bit-identical.

- **S-expression parsing is ~40 % faster on large boards** (Issue #5617, part of #5240). `Parser.parse` now tokenises a well-formed document in a single regex pass with an explicit stack, and pauses the cyclic garbage collector while it builds the tree; anything outside that common case (escaped strings, position tracking, malformed input) still goes through the recursive parser, so trees and `ParseError`s are unchanged. A routed board 06 parses in 232 ms instead of 401 ms, and the Diff-Pair re-route step, which loads the board about ninety times, spends 15.8 s parsing instead of 37.1 s.

- **Pure-Python A\* fallback memoises the route-halo trace verdict** (Issue #5617, part of #5240). `Router._trace_halo_clear` caches the exact `RouteHaloGeometry.clear` result per `(layer, from_cell, cell, net, net class)`, invalidated by the halo's `state_version`, in-place rule changes, `clear_via_cache()` and `set_net_name_to_id()`. Repeated probes of an unchanged grid (exhausted-resume and negotiated re-searches) drop from ~12 us to ~1 us. Verdicts and routing output are unchanged.

- **Native A\* checks a node's via drop once, not once per target layer** (Issue #5617, part of #5240). Both C++ search loops called `is_via_blocked_diag` for every other routable layer, although its verdict cannot depend on the destination layer; on a 4-layer board that repeated the same full-stack check three times per expanded node, 44 % of the Board 06 re-route's routing phase. The first verdict (and its via-via blocker diagnostics) is now replayed for the remaining layers, except while a cross-domain pairwise matrix is installed. The per-step stored-via check also stops gathering and sorting stored-segment candidates it never read, and the clearance kernel gained typed `clear` overloads for segment/via pairs. Routing output is unchanged; locally the Diff-Pair coverage script's routing phase went from 541.7 s to ~351 s and the whole script from 11:27 to 8:15.

- **Native A\* answers each via-drop site once per search call, not once per layer it is reached on** (Issue #5617, part of #5240). The resumable C++ search re-ran the full-stack `is_via_blocked_diag` check every time it expanded a node, although a through via's verdict depends only on the cell `(x, y)` and not on the layer the node sits on, so a node reached by a via repeated the check its source node had just made. Verdicts and their via-via blocker diagnostics are now cached per `(x, y)` for one `run_astar_loop()` call and discarded at the next fresh search or `resume()`; the cache is bypassed while a cross-domain pairwise matrix is installed. Routing output is unchanged; locally the Diff-Pair coverage script's routing phase went from ~346 s to ~290 s and the whole script from ~8:30 to ~7:42.

- **`kct route` launches `kicad-cli version` at most once per path** (Issue #5910). `get_kicad_cli_version` is now memoised per interpreter and `kicad-cli` path (failures are not cached), and reuses the version captured by the discovery probe, removing a ~6 s KiCad launch on hosts with slow startup. Redundant DRC/fill-zones reuse is deliberately not attempted here (see follow-up).

- **`kct route`'s pour-net oracle completion loop is faster, with
  byte-identical output** (Issue #5911). Each round now runs the emitted
  and the source project's `kicad-cli pcb drc` side by side instead of
  back to back. Each closer attempt also writes all of its routed links to
  the board in one load and save, instead of one per link (about 20 per
  attempt on board 03). On an oracle-only replay of board 03, the stage
  takes about 25% less wall time and 28% less Python CPU time. The board
  it leaves is byte-identical, round by round.

- **Diff-pair corridor-yield re-run gets a negotiated-loop budget proportional to the first routing pass** (Issue #5923). The one strategy re-run after a coupled pair yields its corridor now passes `clamp(1.5 x first-pass wall time, 60 s, 300 s)` to the negotiated loop instead of a fixed 300 s. This is a budget for the negotiated loop, checked between nets, **not** a hard wall-time bound on the whole re-run: an in-flight search can overrun it and the post-negotiation sweep / clearance tail runs outside it, so a re-run can still cost noticeably more than the cap (a measured CI re-run was 361 s against a 223 s cap). `KCT_CORRIDOR_YIELD_RERUN_FACTOR` and `KCT_CORRIDOR_YIELD_RERUN_FLOOR_S` tune the derivation; an explicit `KCT_CORRIDOR_YIELD_RERUN_S` is still an absolute negotiated-loop cap, and `KCT_CORRIDOR_YIELD=0` still disables the recovery entirely. Enforcing a deadline across the whole re-run is tracked separately.

- **A hanging `kicad-cli` no longer costs ~30 s per lookup** (Issue #5992). A
  `probe_failed` result is now cached for a short TTL (default 60 s, override
  with `KCT_KICAD_CLI_PROBE_FAILED_TTL`) and re-probed after it expires. It is
  still never reported as "not installed" (#5932); `found` / `not_found` caching
  is unchanged and lookups are thread-safe.

- **`kct pcb annotate-pintypes` and `sync-netlist` no longer stall on dense
  sheets** (Issue #6016). Following a no-connect flag across a sheet's wires
  was O(wires² × junctions); it now indexes wire end points and junctions in
  hash grids and joins wires with a union-find, so a 2000-wire sheet takes
  tens of milliseconds instead of 18 s or more. Which pins get `+no_connect`
  is unchanged.

- **Steiner-tree construction is about 50-160x faster on large nets, with
  bit-identical results** (Issue #6019). The iterated 1-Steiner solver used by
  `build_rsmt` (router RSMT decomposition and the FOM `trace_length_excess` /
  Steiner-lower-bound terms) now scores all Hanan-grid candidates in one
  batched numpy Prim instead of one pure-Python O(n^3) Prim per candidate.
  Prim ties are broken in the same order as before, so every Steiner point,
  edge and length is unchanged. Board 03's 44-pad `GND` net drops from about
  100 s to under 1 s, and one full FOM evaluation of the 7 calibration boards
  from about 18 minutes to about 16 s. The congestion-aware router path
  (custom cost function) keeps the scalar solver.

- **The router stops re-running the slow pure-Python A* on a net it already
  proved unroutable** (Issue #6098). When the C++ search gives up, the net
  still gets one pure-Python attempt, because that search can rescue nets the
  C++ search cannot. If the Python search then explores every reachable cell
  without finding a path, that result is remembered. Later give-ups on an
  unchanged board skip the repeat search instead of running it again on every
  negotiated iteration, rescue pass and rip-up probe. On the #6008 keepout
  wall board, `kct route-auto --strategy hierarchical` dropped from 526 s to
  39 s and `kct route` from 1511 s to 152 s (side-by-side runs on a loaded
  machine). Set `KICAD_ROUTER_SKIP_PROVEN_FALLBACK=0` to restore the old
  behaviour.

- **Lattice router clearance checks skip provably-clear obstacles** (Issue #6182).
  `seg_clear`/`via_clear` now bounding-box-reject far copper before the exact kernel
  gap call, and the lattice spatial index skips its dedupe set for single-cell
  windows. Routed output is byte-identical; ~25-35% less CPU per lattice route.

- **Diff-pair corridor-yield re-run now runs under a hard wall-clock deadline** (Issue #6230). The `clamp(1.5 x first-pass wall time, 60 s, 300 s)` cap from #5923 used to bound only the negotiated loop, checked between nets, so an in-flight search, the post-loop clearance pass and the rescue sweep could run past it (one CI re-run took 361 s against a 223 s cap). It is now a deadline on the whole re-run: every A* search is clamped to the time left, the negotiated loop gets 80 % of the deadline, and the clearance pass and rescue sweep stop at the deadline. If too little time is left for the post-loop DRC safety nets (less than 1.25x what the first pass spent on them), the re-run is abandoned and reverted, so its copper is never kept unchecked. A reverted re-run now restores the exact board the first pass left: before, the revert dropped first-pass routes that the re-run had ripped up and re-routed (board 06 lost all `USB_CC1` / `USB_CC2` copper after an abandoned re-run). The bound is not exact: Python work already running when the deadline passes (rollbacks, the clearance pass's up-front validation, a safety-net pass slower than its estimate) can still overrun it. `KCT_CORRIDOR_YIELD_RERUN_TAIL_FRACTION` tunes the tail reserve.

- **Access-witness replay is about 30x faster, with the same verdicts**
  (Issue #6292). Board 05's replay of 2 stranded terminals over 586
  journal records went from 215 s to 7 s; that replay had taken 143-267 s
  of the 900 s `kct route --timeout` budget. Each terminal now keeps a table
  of which exit stubs are legal. Legal stubs are re-tested only against
  copper added since the terminal's last check, and rejected stubs only
  after a rip-up. Via sites are not tested once a stub is legal, and a
  terminal that already has no way out is not re-checked when copper is
  only added. `compute_access_set` also builds its foreign-copper lists
  once per call instead of once per via candidate. Every witness verdict is
  unchanged; only the sidecar's `evaluations` count can drop.

- **`kct route`: the C++ search stops proposing traces that hug a
  no-connect pad next to the net's own pad** (Issue #6375). Since #6315 the
  native A* kept returning candidates that post-route validation rejected for
  0.10 mm segment-to-pad gaps against a 0.15 mm rule, on steps across the
  routing net's own pad copper and clearance halo (admitted unchecked) beside a
  net-0 neighbour (NC or a pour net). Each rejection cost a resume and often a
  10-100x pure-Python A* fallback. Those steps are now held to the validator's
  own segment-vs-pad check, which the search and `validate_route` share. The
  search applies the validator's same-component carve-outs and the narrowest
  neck-down width, so it is never the stricter of the two. Board 04 (2-layer
  protocol) has no rejections and no fallbacks, and its routing time drops from
  117 s to 13.6 s (10.3 s before #6315). Board 03 no longer falls back on
  USB_D-. Requires a native rebuild (`kct build-native`; router build
  version 51).

- **The Python fallback A\* stops building candidates its own validator
  rejects** (Issue #6397, part of #6473). The bidirectional Python search
  (`Router.route_bidirectional`) now applies the #6375 own-pad step gate that
  the one-way loop got in #6389: a planar step touching the routing net's own
  pad copper or halo must pass the validator's segment-vs-pad check, with the
  same same-component carve-outs and at the narrowest width reconstruction
  can emit. Both Python loops also reject a via closer than
  `max(min_drill_clearance, min_hole_to_hole)` to an earlier via on the same
  path, as the C++ A\* has since #6464, instead of finding the pair and
  losing the whole candidate to post-route validation.

- **Pure-Python A* frees its node tree on return again** (Issue #6526).
  `AStarNode.via_anc` now names the nearest via strictly above a node instead
  of the node itself, so a search that places a via no longer leaves its
  whole node tree (1135-2181 nodes on the #6464 fixture) for the cyclic
  garbage collector. Routed copper is unchanged.

## [0.22.0] - 2026-09-28

### Summary

Three new `kct check` rules — `via_under_body` and `pin1_marker` (both on
by default) and the opt-in `width_consistency` audit — plus native
emission of the different-net SMD-pad clearance floor and a single
clearance resolver for `kct route` (Epic #5509 Phase 2/3). **Read the
upgrade notes:** `kct check --strict` and native `kicad-cli` DRC can
newly fail on boards that passed before, and `kct route` now routes at
the clearance the board's project declares (for example KiCad's stock
0.20 mm `Default` netclass) instead of letting `--mfr` or its own target
override it. Also: routing-plan capacity gating (`--plan-gate`,
`net-status --why` sidecar), the coupled diff-pair search moved onto the
exact-geometry clearance kernel, pre-release correctness fixes to the
three new rules, and router performance work.

### Upgrade notes / behaviour changes

- **`kct check --strict` can newly fail on boards that passed before.**
  Two new categories are on by default: `via_under_body` (Issue #5736) and
  `pin1_marker` (Issue #5737). Their findings are warnings, and plain
  `kct check` exit codes don't change. But `--strict`, and any CI or ship
  gate that counts warnings, will now fail on:
  - a via under a QFN/DFN/SON/LGA package body (`via_under_body`)
  - an orientation-sensitive part with no visible silkscreen pin-1 mark
    (`pin1_marker_missing` / `pin1_marker_obscured`)

  The repo's own board-04 gate hit this and needed waivers (#5745). To keep
  a gate green without fixing the board, either skip both categories with
  `kct check --strict --skip via_under_body,pin1_marker`, or add
  per-reference / per-item entries to `.kct_waivers.json`. Waivers are
  narrower and keep the check active for new parts.

- **`kct route` now routes at the clearance the board's project
  declares** (Issues #5645, #5654). The router reads the `.kicad_pro` and
  `.kicad_dru` beside the `.kicad_pcb` — the same set `kicad-cli pcb drc`
  checks — and treats each declared value as a **floor** that only ever
  *raises* the router's 0.15 mm target, never lowers it:
  - an unconditional `.kicad_dru` clearance rule,
  - the `.kicad_pro` minimum clearance (`min_clearance` /
    `clearance_min`), and
  - the `.kicad_pro` `Default` netclass clearance. A project still
    carrying KiCad's stock **0.20 mm** `Default` is now routed at 0.20 mm.
    Projects kct itself creates now write `Default` at 0.15 mm, so they are
    unaffected.

  In addition, **`--manufacturer` / `--mfr` no longer overrides a clearance
  the board declares**; it only selects which fab floor applies. The
  clearance banner names the layer that won. To keep the previous spacing,
  lower the declared value in the project, or pass `--clearance`, which
  remains the one deliberate waiver and now warns when it undercuts a
  declared rule (#5656).

- **`kicad-cli pcb drc` can newly fail boards whose `.kicad_dru` kct
  regenerates** (Issue #5059). Generated DRU files (and the shipped JLCPCB
  templates) now carry a native `SMD Pad Clearance` rule for SMD pads of
  different footprints whenever the profile sets
  `min_smd_pad_clearance_mm`, so native DRC reports different-net SMD pad
  pairs closer than that floor that it previously passed. KiCad
  10.0.0/10.0.1 never fire the rule, and kct warns when it detects one of
  those versions (#5724).

### Fixed

- **Board 04 now silkscreens pin 1 / polarity on U1, U2, D1 and J1**
  (Issue #5745). `boards/04-stm32-devboard/generate_design.py` emitted all
  four of those footprints with **no** footprint silkscreen graphics at all —
  only the reference text — so `pin1_marker_missing` fired on every one and
  the four findings had to be waived in `output/.kct_waivers.json` when
  #5737 landed the rule. Each generator function now emits its stock KiCad
  library artwork: the body outline plus the filled pin-1 triangle for
  `SOT-223-3_TabPin2` (**Y-mirrored** — this board's pad 1 sits at `y=+2.3`,
  the opposite of the library footprint, so copying the library triangle
  verbatim would have marked pad 3) and for `LQFP-48_7x7mm_P0.5mm` (four
  corner brackets, transferred as-is — the pad coordinates already match the
  library), the open-sided polarity bracket for `LED_0805_2012Metric`, and
  the L-shaped pin-1 corner plus body rectangle for
  `PinHeader_1x06_P2.54mm_Vertical` (shifted from the library's pin-1 origin
  onto this footprint's centered origin). Strokes are 0.15mm, the
  `jlcpcb-tier1` silkscreen floor, rather than the library's 0.12mm, and the
  U1/D1/J1 reference designators move clear of the new outlines so
  `silk_overlap` stays at zero. The four waivers are deleted and the board
  passes the reviewed paid-drill gate (#5009) at strict-0 **without** them —
  `kct check` reports 0 errors / 0 warnings / 0 waived, and `kicad-cli pcb
  drc` independently reports no silk finding of any kind.

- **The coupled diff-pair search now sees the copper it was routing
  through** (Issue #5662, Phase 3c of Epic #5509). #4507 summarised the
  defect as *"the copper this board fails on is invisible to both by
  construction"*, and both halves of that are closed here by moving the
  coupled path onto the shared exact-geometry clearance kernel:
  - **C++ (`CoupledPathfinder::rail_clear`, consumer group 7).** The gate
    was a lambda inside `route()`, wrapped in `if
    (grid_.has_fixed_fills())` and consulting fixed fills alone — so on a
    board with no copper pour it ran *no rail check at all*, and a route
    committed through `add_stored_segment` / `add_stored_via` after the
    last blocked-plane re-sync was invisible to it. It is now a named,
    bound method that always runs and measures the candidate rail against
    the grid's stored route geometry with the clearance kernel, through
    the same 2 mm bin index the single-ended refinement path uses. Its two
    rejection reasons also join the #4459 rejection histogram instead of
    pruning the frontier silently.
  - **Python (`DiffPairRouter`'s gates, consumer group 8).**
    `_segment_cells_clear`, `_span_pad_clear`, `_segment_pad_clear`,
    `_span_via_clear`, `_route_via_clear`, `_pair_has_physical_overlap`,
    `_copper_conflicts` and `find_intra_pair_clearance_violations` now
    read the kernel through one shared translation
    (`router/clearance_shapes.py`) instead of six private predicates.
    `_segment_cells_clear` gains the missing quadrant — a candidate span
    is screened against the constructor's own unmarked shadow copper —
    and the raster's *over*-block is re-decided by exact geometry when
    every blocking cell is attributable to copper the grid can name, the
    same authorisation `Pathfinder._trace_halo_clear` has used since
    #5240. `roundrect` / `oval` pads are now measured as the copper they
    are rather than as their enclosing rectangle.
  - **The oracle rows harden.** Groups 7 and 8 stop being report-only in
    the over-rejection direction: refusing a candidate `kicad-cli` calls
    clean is a test failure
    (`test_migrated_consumer_never_over_rejects`). Group 7 was the epic's
    one *unexposed* consumer; binding `rail_clear` retires
    `NOT_MEASURED_REASONS` entirely, so all nineteen groups are measured.
  - No rule value, `.kicad_dru`, or A\* heuristic term is touched (Epic
    #5509 scope guards #1 and #2); rule resolution stays with each
    consumer, the kernel answers only gaps.

- **Coupled differential-pair search now refines dynamic route-halo rejection
  with physical clearance** (part of Issue #5410). A dynamic route halo is a
  conservative acceleration structure: committed copper is dilated to whole
  grid cells, so a foreign route's halo covers candidates whose actual copper
  and drill gaps satisfy the effective rules. PR #5425 taught the per-net A*
  (both backends) to measure that geometry before rejecting such a candidate;
  the **coupled** joint-state search — the engine that routes every
  differential pair — still answered on the raster alone, so every pair after
  the first refused legal steps beside already-committed copper. Both coupled
  backends now apply the identical check, over the swept trace step (not just
  its endpoint) and the through-via envelope. Hard or unverifiable blockage —
  pad metal, static halos, keepouts, reservations, hidden overlapping owners,
  missing geometry — still rejects, and the refinement is **fail-closed**:
  dormant without the net-name map it needs to resolve pairwise
  (HV-isolation) requirements, and, on the C++ side, dormant unless that
  grid's own cross-domain widening was installed *and* the refiner is armed.
  The refinement re-measures with the candidate's **net-class** trace width,
  clearance and via size on both backends — re-measuring with the global
  `DesignRules` scalar would have waived clearance the raster halo (dilated
  with the net-class value) had enforced, which is the #5673 under-blocking
  direction. Native ABI 42 → 44.
- **Qualify KiCad 10.0.6 for the native mask-to-copper gate** (Issue #5678).
  The CI runner's floating `kicad/kicad:10.0` tag moved from 10.0.5 to 10.0.6,
  and both hard-coded `10.0.5` version pins — `scripts/ci/check_mask_copper_native.py`'s
  gate and `validate/mask_copper_geometry.py`'s native-attribution check —
  rejected it, failing the `Test` job's mask step (which also skipped every
  later step in that job, including `Run tests`). The two pins are now one
  shared `QUALIFIED_NATIVE_VERSIONS = ("10.0.5", "10.0.6")` in
  `validate/mask_copper_geometry.py`, imported by the CI gate so the pair can
  no longer drift apart; an unqualified build still fails loudly, now naming
  the versions it did see.
  10.0.6 was **measured**, not assumed, before being admitted: the full native
  suite was run in both `kicad/kicad@sha256:182c8005…` (10.0.5) and
  `kicad/kicad@sha256:18693567…` (10.0.6), each reporting `32 passed, no
  skips`, and the resulting mask/copper geometry was compared across the two —
  every exported layer polygon and every per-UUID attributed object polygon
  came back byte-identical in WKT (0.000e+00 mm² symmetric difference).

- **The new `kct check` rules shipped in this release got three
  correctness fixes before release.** `pin1_marker`'s bent-component
  tie-break no longer lets a nearby board-level silk shape stand in for a
  footprint's own pin-1 mark (Issue #5748). `via_under_body`'s default
  footprint pattern is now case-insensitive (lower/mixed-case `qfn`,
  `Wson`, `lga` ids are checked) with the `SON` token anchored so
  `Resonator` / `Panasonic` / `Epson` / `Degson` footprints are *not*
  swept in — it selects exactly the same 964 KiCad 10 stock footprints as
  before — and an exposed pad split into several same-net pads is now
  recognised as the thermal pad (Issue #5749). `width_consistency` joins
  track endpoints within `node_tolerance_mm` even when rounding puts them
  in different buckets, so chains no longer end early and hide
  `width_island` / `width_transition` findings (Issue #5750; six
  previously missed transitions on board 05).
- **The route optimizer's grid collision checker no longer rejects legal
  copper near the clearance threshold** (Issue #5625). It dilated
  already-dilated committed copper a second time, so two envelopes merely
  touching read as a violation at roughly twice the real clearance; it now
  runs an exact narrow phase and agrees with the vector checker and
  `kicad-cli` on the conformance corpus's routed-copper (segment-segment)
  probes.
- **A routing-cache hit now writes the `<stem>.routing_plan.json`
  sidecar too** (Issue #5651) — the plan stage previously ran only on a
  cache miss, so identical runs produced different artifact sets.
- **`--clearance` below a declared rule now warns** (Issue #5656). The
  explicit-clearance path skipped reading the board's declared rules, so
  the undercut advisory could never fire from the CLI; the resolved value
  is unchanged.
- **The native mask-geometry export check no longer reports a
  successful `kicad-cli` export as unsupported** because of KiCad's
  single-instance `Warning: Invalid lock file` line (Issue #5707). A
  narrow, anchored allowlist of geometry-neutral stderr lines is ignored
  only when the process exited 0.
- **Writing a `.kicad_dru` now warns when its `SMD Pad Clearance` rule
  is inert on the installed KiCad**
  (Issue #5724). KiCad 10.0.0/10.0.1 silently never fire it (pads there
  don't inherit their footprint's reference), which gave a clean DRC on
  boards violating the 0.15 mm different-net SMD floor.
- **`--deterministic-budget`'s help text and warning now state the
  `--timeout` interaction accurately** (Issue #5765). Under `--auto-layers`
  escalation each attempt gets only a fair slice of `--timeout`, which can
  bind on a slow host; the flag no longer claims an unconditional safety
  backstop, and a run whose stage deadline actually fired is logged as
  having lost determinism.
- **Board 03's saved-plan replay no longer deletes footprint silkscreen**
  (Issue #5744). The demo recipe's routed artifact had lost all 139 silk
  graphics because the plan replay removed every silk child and restored
  only reference text; it now replays text only.

### Added

- **Via-under-package-body rule in `kct check`**: a new default-on
  `via_under_body` category (selectable via `--only`/`--skip`, no new flags)
  with a warning-severity `via_under_body` rule. It flags vias whose copper
  overlaps the body outline of a bottom-terminated package (QFN / DFN / SON /
  LGA by default). The outline is the footprint's `F.Fab`/`B.Fab` outline,
  falling back to the courtyard. Clearance and via-in-pad both pass these vias
  because they miss every pad, yet they can't be probed or inspected once the
  part is assembled. Thermal vias inside the footprint's own exposed pad on
  its net are allowed by default. `ViaUnderBodyRule` exposes
  `footprint_pattern`, `include_references` / `exclude_references`,
  `allow_thermal_pad_vias`, `fallback_to_courtyard` and `severity`. Findings
  are waivable via `.kct_waivers.json`, and the rule id is classified
  advisory, so plain exit codes don't change. All committed fleet boards
  produce zero findings. **`kct check --strict`, and any gate that counts
  warnings, will now fail on a board with a via under a QFN/DFN/SON/LGA
  body, even if that board passed before the upgrade.** To opt out, run
  `kct check --skip via_under_body`, or waive the via or reference in
  `.kct_waivers.json`. See "Upgrade notes" above.

- **Pin-1 / polarity silkscreen-marker rule in `kct check`**: a new
  default-on `pin1_marker` category (selectable via `--only`/`--skip`, no new
  flags) with two warning-severity rules. `pin1_marker_missing` fires when an
  orientation-sensitive footprint has no silkscreen element within 2.5 mm of
  pad 1 that points at it (closer to pad 1 than to any other pad; a
  multi-segment corner mark such as the L on KiCad's stock crystal
  footprints counts when its centroid is closer to pad 1). Covered
  footprints are those with 3+ copper pads including `1`/`A1`, plus
  `Diode_*` / `LED_*` / `CP_*` / tantalum parts. `pin1_marker_obscured` fires
  when the only such marks are under the Fab package-body outline or on pad
  copper. Every other gate passes a board with no visible pin-1 marks, yet
  the parts can then be assembled or reworked backwards. Passives, test
  points, mounting holes, switches and keyed USB-C / coax connectors are
  excluded. `Pin1MarkerRule` exposes the selection patterns, `min_pads`,
  include/exclude references, `search_radius_mm`, `require_asymmetry`,
  `include_board_silk` and `severity`. Findings are waivable per reference
  via `.kct_waivers.json`, and both rule ids are classified advisory, so
  plain exit codes don't change. **`kct check --strict`, and any gate that
  counts warnings, will now fail on a board with an orientation-sensitive
  part that has no visible pin-1 mark, even if that board passed before the
  upgrade.** To opt out, run `kct check --skip pin1_marker`, or waive the
  reference in `.kct_waivers.json`. See "Upgrade notes" above. Marker
  *generation* is out of scope; it remains a listed follow-up of
  `kicad_tools.silkscreen.generator`.

- **Trace width-consistency audit (`width_consistency`).** A new
  heuristic, geometric (not ampacity) advisory rule. It walks each
  net/layer's tracks as chains between pads, vias and branches and reports
  `width_island` (a short constant-width run with narrower copper on both
  sides) and `width_transition` (a neck-down on a two-terminal chain that
  no other-net clearance or pad escape explains, reported with the nearest
  obstacle and the gap before and after widening). Warning severity,
  advisory-quality category, waivable. Opt-in via
  `kct check --only width_consistency` or
  `DRCChecker.check_width_consistency()`, so it does not change existing
  verdicts. It can be configured through `WidthConsistencyRule`
  (`max_island_length_mm`, `clearance_mm`, `report_justified`, `severity`,
  ...).

- **Native emission of the different-net SMD-pad clearance floor**
  (Issue #5059). The `SMD Pad Clearance` rule is now emitted into the
  generated `.kicad_dru` (and the shipped JLCPCB templates) whenever a
  profile sets `min_smd_pad_clearance_mm`, scoped to SMD pads of
  **different** footprints via `A.Reference != B.Reference` — KiCad's
  custom-rule docs state that footprint children (pads) carry their parent
  footprint's reference designator, so package-internal pairs compare equal
  and stay exempt (the fine-pitch QFP/QFN wall that kept this floor
  Python-only). Measured against `kicad-cli pcb drc` 10.0.6 (CI's
  `kicad/kicad:10.0` container exercises the same suite):
  a 0.12 mm gap between two referenced footprints fires
  `rule 'SMD Pad Clearance' ... actual 0.1200 mm`; the same gap inside one
  footprint stays silent. Known boundary, pinned by test: the native scope
  is the reference *string*, so blank or duplicated reference designators
  are not discriminated — `kct check`'s identity-scoped floor remains
  authoritative for that shape. The `dru_generator` legacy-sidecar
  rule-family list learned the new rule name.

- **Measured effective-native-DRC coverage for silkscreen clearance**
  (Issue #5059). A manufacturer profile can emit a constraint that *looks*
  correct in the generated `.kicad_pro` / `.kicad_dru` and still be applied to
  nothing; asserting emitted values cannot detect that.
  `tests/test_effective_silk_clearance_5059.py` asserts only what
  `kicad-cli pcb drc` actually reports for geometry placed a known distance
  below (0.085 mm) or above (0.16 mm) the 0.15 mm JLCPCB silkscreen-to-pad
  floor:
  - the project key `board.design_settings.rules.min_silk_clearance` alone
    reports **no** finding for that gap — measured on KiCad CLI 10.0.1, both
    at the factory floor and with the key raised to 2.0 mm — while a
    positive-control test proves the same project block *is* loaded
    (`min_clearance` raised to 0.5 mm reports `board minimum clearance
    0.5000 mm; actual 0.1200 mm`);
  - the explicit `(constraint silk_clearance …)` rule emitted by
    `generate_dru` reports the gap numerically on byte-identical geometry;
  - the shipped JLCPCB profile gates it end to end through
    `write_drc_constraints`, stays silent above the floor, and does not fire
    for back-side silk against a front-side pad.

  If a future KiCad honours the built-in minimum for this pair, the first
  test fails and says so rather than silently leaving a redundant rule in
  place. The behaviour is characterised, not explained: the upstream cause
  is a lead, not a conclusion this repository has established.
- **`docs/guides/drc-and-validation.md`: "Three gates, not one"** — native
  DRC, `kct check --mfr` and factory DFM are separate gates, and a clean
  `kicad-cli pcb drc` run is not supplier approval. Includes the measured
  silk-clearance evidence above and the standing warning that a blanket
  same-net `physical_clearance` over every copper object is not how to get
  net-independent gap checks (499 warnings on a real board, including
  intentional track joins).

- Make the routing plan's overflow report **consumable and measured**
  (Issue #5521, Phase 1c of Epic #5510). Phases 1a/1b produced a sidecar
  listing over-subscribed tile boundaries; this slice says what they mean
  and what would fix them:
  - **`RoutingPlan.format_overflow_report()`** prints one block per
    overflowed edge — the two regions' bounds, the nearest component ref
    on each side, the overflowed layer indices, demand, capacity,
    overflow, and the nets crossing. It reads only the plan object, so a
    sidecar loaded with `from_dict` formats identically to a plan built
    in-process. New per-edge `refs_a` / `refs_b` (populated on overflowed
    edges only) carry the ref ranking.
  - **Computed relief** (`relief`, empty since 1a, plus a new
    `relief_meta`). Each candidate is *measured*, not guessed: the same
    global pass is re-run under one hypothetical change and the resulting
    board-wide `expected_overflow` recorded, keeping only candidates that
    beat the baseline. `move_component` shifts one adjacent component by
    a 2.0 mm step against a **copy** of the pads — the router, the grid
    and the PCB are never touched, so routed copper stays byte-identical;
    `add_signal_layer` re-plans with one plane index treated as signal
    (advice about capacity, not a claim the plane is removable);
    `swap_pins` is a deferred stub for #5511 and evaluates nothing. The
    search is bounded by a deterministic cap (3 edges x 3 refs x 4 steps)
    and a 5 s wall-clock budget, and runs only when the board actually
    overflows, so a feasible board pays 0 s.
  - **`kct route --plan-gate`** (off by default): when the plan reports
    `feasible: false`, print the report and exit **9** before any
    detailed routing, on both the negotiated and the dense two-phase
    paths. Exit 9 is shared with `--census-advisory-gate` (#4799) and the
    stderr prefix (`[plan-gate]` / `[crosstail-gate]`) says which fired.
    The override is the **existing** `--force`, not a second flag — note
    it also disables grid/DRC validation, so to simply not gate, omit
    `--plan-gate`.
  - **`kct net-status --why` reads the sidecar.** No new flag: `--why`
    auto-discovers `<pcb_stem>.routing_plan.json` beside the board and,
    for each stuck net crossing an overflowed corridor, names that
    corridor and its measured relief immediately *before* the
    `recommendation:` line — a pre-route capacity measurement printed
    alongside the classifier's inference from finished copper, never
    replacing it. In `--format json` this adds exactly two keys, and only
    when a sidecar was loaded: a top-level `routing_plan` block and an
    `overflow_edge` per crossing diagnosis (injected in `output_why`;
    `StuckNetDiagnosis.to_dict()` is untouched). **Without a sidecar the
    output is byte-identical to before**, pinned by golden fixtures in
    `tests/fixtures/net_status_why_golden/`. A sidecar built for another
    board, or older than the PCB, is reported on stderr and ignored.
  - **Fleet precision/recall table** in `docs/reference/routing-plan.md`,
    printed by `scripts/routing_plan_fleet_table.py` (which reads
    sidecars and `kct net-status --format json` dumps — it never routes).
    This replaces Epic #5510 Phase 1's two unmeasurable acceptance lines
    with two falsifiable ones: *recall* (of the nets that really ended
    unrouted, how many cross an overflowed corridor) and *precision* (of
    the overflowed corridors, how many are crossed by an unrouted net).

### Changed

- **One clearance resolver for `kct route`** (Issue #5645, Epic #5509
  Phase 2). `router/clearance_resolver.py` now owns the required
  clearance for every copper pair in one deterministic precedence:
  explicit `--clearance` > legacy `.kicad_pcb` net classes > project
  floors (`.kicad_dru` unconditional clearance, `.kicad_pro` minimum
  clearance and — new — the `Default` netclass clearance, #5654) >
  fab-tier floor > the router target, then per-net-class overrides and
  the #4431 net-pair matrix. The clearance banner names which layer won.
  See the upgrade note on routing clearance.

### Performance

- The pure-Python A\* fallback stops re-deriving four things that do not
  vary with what they were being re-derived for (Issue #5617). A py-spy
  profile of the Diff-Pair regression job's re-route step now attributes
  **67.6 %** of phase 4 ("Routing nets") to the GIL-released native C++ A\*
  and 20.1 % to the Python fallback; this takes about a third of that
  Python remainder:
  - **`ComponentHoleIndex.clear`** is a *physical drill* floor, so a
    predicate of the candidate cell alone — yet it ran once in
    `_check_via_placement_cached` and again per non-plane layer inside
    `_is_via_blocked`, 3–5 identical evaluations per via candidate.
  - **The #5240 non-through-hole pad-drill sweep** is likewise cell-only,
    but ran on *every* `_check_via_placement_cached` call because the
    sibling `_via_cache` is bypassed whenever `allow_sharing` is set —
    exactly the negotiated mode the fallback runs in.
  - **`_is_trace_blocked`'s #3229 Euclidean-disc kernel** is a pure
    function of the radius and the region offsets *relative to the
    centre*, rebuilt from `np.arange` on every neighbour expansion.
  - **`RouteHaloGeometry.cell_known`** reached the four occupancy planes
    through a freshly allocated `_CellView` and four bound-property calls;
    it now indexes the same arrays directly, with the operand order and
    short-circuit points unchanged.

  The two via-site memos are tokened against the state they read — the
  hole memo on `(index object, len(index.holes), index.known)`, the
  pad-drill memo on the identity of the arrays tuple
  `_non_th_pad_geometry` returns — because `RoutingGrid.add_component_hole`
  and `add_pad` mutate the live index in place, mid-route, with no
  router-side hook. Measured locally on `boards/06-diffpair-test --seed 42`
  across six alternating arms, normalised against the native search to
  cancel host load (which moved 5× over the 95 minutes they took): phase 4
  −4.4 % (floor −2.0 % on the most conservative pairing),
  `_check_via_placement_cached` −34.1 %, `cell_known` −35.6 %, the whole
  fallback −20.9 %. `diffpair_test_routed.kicad_pcb` is byte-identical
  across every arm. Method, the full line-level attribution and the six-run
  table are retained in
  `docs/diagnostics/issue-5617/phase4-native-split.md`.
- **Faster exact-geometry clearance checks in the router.** The lattice
  engine's three pair kinds take a type-exact fast path through
  `clearance_kernel.copper_gap` (Issue #5672, recovering most of the ~6 %
  route-CPU cost the kernel migration added on board 02); the coupled
  diff-pair halo's via refinement memoises its geometric half (Issue
  #5696); and the coupled via predicate's three per-cell Python raster
  sweeps become occupancy-plane slices (Issue #5720). Routed copper is
  unchanged.

## [0.21.1] - 2026-09-21

### Summary

Follow-up release cut hours after 0.21.0: PyPI's per-file 100 MB limit
rejected 0.21.0's sdist mid-publish (leaving a wheel-only release on the
index), so 0.21.1 re-ships the same code with a slimmed sdist (127 MB →
18 MB of board artifacts, evidence trees, and tooling copies excluded
from the package — the wheel is unchanged) and an idempotent publish
step. It also carries the work that landed while the release was being
unblocked: honest routing-plan capacity (per-class pitch, plane
exclusion, blockage), the clearance-conformance Phase 1c harness (18 of
19 consumer groups measured), the board 05/06/07 access-loss witness
evidence runs, board06 foreign-input refusal, the access-witness
deadline-kill fix, and CI/router performance recoveries.


### Fixed

- `kct route --step route` on board06 now refuses a **foreign input PCB**
  with a named, actionable error instead of routing it and emitting
  `UNREPAIRED` lines that read like a router regression — the committed
  unrouted input is selected by content, so stale-artifact confusion
  (#5607) can no longer masquerade as a routing failure (Issue #5628).
- The access-witness sidecar is now written even when a route terminates
  on the wall-clock deadline kill, and two verdict-shape defects (an
  empty-pads witness surfacing as a schema key on every diagnosis, and
  net-0 paddings mislabelled in the replay) are fixed (Issue #5639,
  follow-up to the #5508 1a/1b machinery).

### Performance

- CI's Test job recovers most of the `KCT_NATIVE_MAX_CONCURRENCY=1`
  regression by raising the bound to 2 with the OOM-safe slot accounting
  (Issue #5616); the Diff-Pair regression job's 17.6-minute re-route step
  gets filled-polygon edge indexing plus phase-level profiling, and the
  validation hot path rejects far copper by bounding box before the exact
  halo distance (Issues #5617 and #5240's closing sweep).
- The pure-Python A* fallback stops recomputing one route-halo via probe
  per non-plane layer, per candidate cell (Issue #5617).
  `Router._is_via_blocked` opens with
  `_via_halo_clear([], layer, gx, gy, net, require_geometry=False)`, which
  ignores `layer` entirely (empty `cells`, and the probe via always spans
  layer 0 to layer n-1), so `_check_via_placement_cached`'s per-layer loop
  was asking the same question repeatedly — and the sibling `_via_cache`
  could not absorb it, because that cache is bypassed whenever
  `allow_sharing` is set, i.e. in exactly the negotiated mode the fallback
  runs in. The probe is now memoised on `(gx, gy, net)` against a new
  `RouteHaloGeometry.state_version` token (bumped on every halo rebuild,
  which any `record()` or grid-occupancy change forces) and dropped by
  `clear_via_cache()`, so a stale verdict can never be served. Measured
  locally on `boards/06-diffpair-test --seed 42`: phase 4 ("Routing nets")
  831.1 s -> 550.0 s, the probe's own inclusive cost 112.3 s -> 10.7 s, and
  the routed artifacts byte-identical to the pre-change run.
- Zone filling no longer launches `kicad-cli` twice in a row for the same
  fill (Issue #5617). `run_fill_zones()` fills via
  `kicad-cli pcb drc --refill-zones --save-board` — the only branch any
  KiCad 8/9/10 takes, since `pcb fill-zones` does not exist — and then
  handed that freshly filled, saved board straight to the post-fill thermal
  remediation pass, whose pass 0 opened by running *the same command again*
  against a file nothing had touched in between. That second launch is now
  skipped in exactly the case where the caller can prove it redundant (the
  DRC-fallback branch **and** `--refill-zones` support); the native
  `fill-zones` branch, a kicad-cli whose DRC lacks `--refill-zones`, and
  every remediation pass after 0 — where `force_solid_on_pads_by_uuid` has
  actually mutated the board and the refill is load-bearing — all keep the
  original unconditional refill. Real `kicad-cli` launches per
  `run_fill_zones()` call drop from 3 to 2, reproduced across three sessions
  and three source revisions, with all 13 filled boards byte-identical
  (`54af132f…6065`). The Diff-Pair regression job makes five such calls
  (phases `9b`, `10c`x2, `12b`, `13b` — 105.4 s / 16.2 % of the step).

### Changed

- Make the report-only routing plan's capacity model honest (Issue #5575,
  Phase 1b of Epic #5510). The plan stage now runs everywhere (#5520) but
  its overflow numbers were computed three ways that did not describe the
  board:
  - **Per-class pitch.** Edge demand is measured in base-pitch units
    rather than net count: a net whose class pitch
    (`NetClassRouting.trace_width + clearance`) is `k` times the board's
    base pitch now consumes `k` units of an edge's capacity, so a 2.6 mm
    power trunk no longer costs the same as a 0.2 mm signal.
    `RegionGraph` gained `pitch_for_net` / `demand_weight()`, and
    `GlobalRouter` places and rips up with the same weight. Nets with no
    net-class entry weigh exactly `1.0`, so boards without a
    `--net-class-map` are numerically unchanged.
  - **Plane-layer exclusion.** `RegionGraph(signal_layer_indices=...)`
    allocates capacity only on non-PLANE layers, so a 4-layer
    `sig/gnd/pwr/sig` stack no longer advertises 2x the capacity it has.
    The sidecar's `layers.signal` reports the same set (`[0, 3]`, not
    `LayerStack.signal_layers`, which includes planes by design -- #5014),
    and `GlobalRouter.route_all`'s round-robin indexes into that list so
    no net is planned onto a zero-capacity plane.
  - **Blockage beyond pads.** `RegionGraph.register_blockage_rects()`
    subtracts keepout rule areas (each polygon's bounding box, from the
    new shared `Autorouter._keepout_rule_area_polygons()` parse that
    `_lattice_keepout_projection` also consumes) and preserved copper
    (`--preserve-existing` / `--nets` / `--complete`) from tile-boundary
    capacity. Copper pours are deliberately **not** counted -- they are
    filled after routing and flow around traces, so counting them would
    report overflow the detailed router never experiences.

  Sidecar shape change (additive-compatible): `edges[].demand` and
  `edges[].layers.<i>.demand` are now floats in base-pitch units;
  `capacity` / `overflow` stay integers, with overflow rounding any
  fractional excess up. `schema_version` stays `1`.

  Still report-only: the stage reserves nothing, gates nothing and never
  touches `Autorouter.grid`, so routed copper is unchanged. See
  `docs/reference/routing-plan.md`.

## [0.21.0] - 2026-09-21

### Summary

Development since `v0.20.0`: router convergence and reliability work
(stagnation-recovery congestion marking, C++ pathfinder resume-loop fixes,
per-connection avoidance cleanup by default), zone pour determinism and
same-layer fairness, through-hole pad and barrel reuse in pour-bridge
repair, the first phases of the pad-access and clearance-kernel epics
(ordered commit journal + per-pad access witness; exact-geometry clearance
kernel with C++/Python parity), frame-independent board07 pour repair with
native-refill stability, a broad measured performance sweep across the
router/validator/CLI hot paths, and board06/07 recipe and CI
qualification advances.

**Breaking (for `[mcp]` extra users):** the MCP server now requires the
mcp 2.x SDK — `mcp.server.fastmcp.FastMCP` was renamed to
`mcp.server.mcpserver.MCPServer` — with the `fastmcp` pin raised to
`>=4,<5` (#5601). In-process API callers using the stdio `MCPServer`
dataclass are unaffected.

- **Routing, validation and CLI hot-path performance sweep** (#5240, with
  #5070, #5429, #5465, #5467, #5469, #5471) — systematic removal of
  measured hot-path overhead across the router, validator, placement and
  CLI: `RoutingGrid.cell_at()` replaces the remaining
  `grid[layer][y][x]` view allocations in the A\* neighbor loop,
  congestion cost stays in scalar Python floats within a neighbor batch,
  `CppGrid.from_routing_grid` copies only blocked cells, the
  via-placement pad sweep and diff-pair corridor-mask dilation are
  vectorized, and copper-clearance / match-group / net-status candidate
  pairs are pruned by spatial bounds or AABB pre-filters before exact
  geometry runs (`validate_routes` skips provably-far pairs; board06's
  pour-audit and pour-repair pairwise scans prune spatially impossible
  candidates while preserving the exact predicate). `kct build-native`
  now installs the placement and DRC C++ extensions it already compiled
  instead of discarding them; LVS resolves every schematic pin against
  one shared connectivity graph; validation and rule exports resolve
  lazily; and eager CLI/reasoning imports are deferred to cut
  per-invocation startup. Local same-host medians: `import
  kicad_tools` 1.46 s → 0.34 s user CPU (−77 %); the A\* neighbor-batch
  cost 3.67 s → 0.93 s (~3.9×) with identical routes; sampled placement
  force 1.70 s → 1.02 s (2,000 force outputs and final component states
  exactly matched); the placement C++ path ~64 s → 0.55 s; board-05 LVS
  pin resolution 3.5 s → 0.18 s (~20×, `kct check` 108.96 s → 75.95 s
  instrumented, output byte-identical); the board06 pour-connectivity
  audit 0.398 s → 0.270 s. These are local measurements — the
  hosted-CI median comparison for the three target jobs remains open on
  #5240.

- **Legacy `(module …)` footprints are recognized by every remaining
  tree-walk** (#4891, #4892, #4893, #4888, #4894, #4915) — completes the
  legacy-module fix past the parser into the router/zones/DRC/LVS/panel
  walks, the remaining CLI tree-walks, and the pcb/export/reasoning/
  silkscreen consumers, so a KiCad-4-era board is not silently parsed to
  zero footprints downstream of loading; `kct panel` remaps legacy
  footprint reference text instead of dropping it; the board runner
  restores net headers before legacy footprints load; and the corpus
  footprints/pads metrics for all nine boards were re-pinned after the
  fix so the regression baseline reflects the corrected counts.

- **Pad rotation is honored across obstacle, clearance and thermal
  models** (#4910, #5194) — the router's obstacle models apply
  non-cardinal pad rotation, and every clearance and thermal consumer of
  pad geometry reads the rotated footprint, closing the companion gap to
  the non-cardinal pad-clearance fix (#5227) so a rotated pad is priced
  identically everywhere.

- **Copper-LVS and net-status decide on physical copper contact, not
  zone ownership or endpoint luck** (#4982, #4986, #5031, #5060, #5061,
  #5185, #5198, #5062) — `compare_copper_netlist()` no longer suppresses
  a whole net's `open` findings just because the net owns a zone
  somewhere on the board (board05's 44 unconnected power/ground pads
  previously reported PASSED); copper contact is decided over a
  segment's full length rather than its endpoints; duplicate physical
  pad occurrences are preserved through LVS instead of collapsing onto
  one symbol; same-zone fill islands require real copper contact to
  count as connected; dangling tracks are detected by copper-cap
  contact; strict pour chains enforce via spans; and `kct check` gains a
  physical copper-slit detector that flags slits by geometry,
  independently of net identity.

- **Board outlines: shared authoritative bounds, robust Edge.Cuts
  stitching, and bounded curved geometry** (#4978, #4948, #5367) —
  routing and the schema read one authoritative outline-bounds
  computation; `kct stitch` closes Edge.Cuts outlines across gaps and
  multiple contours instead of failing on imperfect geometry; and the
  router preserves bounded circle, arc and Bezier outline geometry:
  `gr_circle` Edge.Cuts elements tessellate into a closed chord chain
  with a certified max sagitta error (1e-5 mm — an order below the
  1e-4 mm clearance-comparison epsilon, with the chord error explicitly
  charged to clearance consumers so a verdict cannot flip), so boards
  with round outlines or circular cutouts route instead of being
  rejected outright; the pinned BeagleConnect Freedom acceptance board's
  11 `gr_circle` elements now clear the loader.

- **Via relocation and in-pad via escapes respect physical copper**
  (#4981, #5010, #5065, #5068, #5069) — relocating stitch vias preserves
  pad and plane contacts and commits only complete, validated stubs;
  unassigned SMD copper acts as a relocation obstacle; project
  hole-to-copper clearance is enforced during via relocation; and a
  blocked in-pad via triggers a search for a safe alternate escape
  rather than a dead end.

- **Pour bridges terminate on existing same-net copper** (#5507, #5551,
  #5606) — when a pour needs a bridge, the zones stage reuses an
  existing same-net via barrel, an existing through-hole pad as the
  bridge terminus (board06 sub-stage D included), instead of
  manufacturing new copper next to copper that already bonds the pour.

- **HV isolation: scoped waivers, refill-safe keepouts, signed equal
  potential** (#4699, #5399, #5446, #5484) — the router's HV
  attach-zone waiver is pad/layer-scoped and backed by a
  preserved-copper audit; HV keepout clearance and authored source
  constraints survive the native zone refill that regenerates them; and
  equal-potential nets keep their clearance even when a zero HV
  threshold would otherwise collapse the pairwise requirement.

- **Match-group and diff-pair tuning preserve physical copper** (#4984,
  #5289, #5290, #5293, #5408, #5496) — split-net copper survives match
  tuning and diff-pair tuning; paired match tuning applies physical
  context; tuning checks span physical via barrels rather than just
  landing pads; authored gaps in differential sizing are preserved; and
  symmetric match-group splices with mismatched host spans are rejected
  rather than silently spliced.

- **Pad-access witness infrastructure (Epic #5508, Phases 1a–1c)**
  (#5516, #5517, #5610, #5599) — the router records every copper
  mutation (commits, rip-ups, restores, wholesale occupancy resyncs) in
  an ordered commit journal tagged with the pass and iteration that
  produced it, persists it next to the routed board as
  `<stem>.access_witness.json`, and replays it into a per-pad access
  witness — so an offline consumer can name the commit that closed a
  pad's access set on a saved board where the live router is long gone.
  Pad access sets gate the search, the resume loop rejects an
  already-accepted goal node, and a localized strict pad-parity check
  pins the invariant.

- **Exact-geometry clearance kernel (Epic #5509, Phase 1b)** (#5514,
  #5589) — one shared exact-geometry clearance kernel over segments,
  vias, edges, pads and zone polygons, ported line-for-line to both C++
  (`router/cpp/clearance_kernel.*`, all-double internals to hold the
  1e-7 mm parity bound; `ROUTER_CPP_BUILD_VERSION` 39 → 40) and pure
  Python (`router/clearance_kernel.py` — no shapely, no `router_cpp`
  import), covering all fifteen unordered shape pairs. `KPad` carries a
  Minkowski decomposition (a convex core of 1–4 board-frame vertices
  dilated by a corner radius) rather than a tessellated outline, so
  `copper_gap(pad, X) = dist(core, X) − radius` is exact with no vertex
  list for the two ports to disagree about. No consumer is switched
  yet; Phases 2–4 migrate them onto the kernel.

- **Declared swap groups with crossing-minimising pin assignment
  (report-only)** (#5522) — the declaration/proposal half of the swap
  pair the #5536 entry below executes: a designer-declared `swap_group`
  on net-class sidecar entries plus `propose_swap_assignment()`, which
  computes the crossing-minimising pad-to-net re-binding for a bundle
  measured genuinely reversed and surfaces it as data
  (`StuckNetDiagnosis.swap_proposal`, the `reorder_pins` placement
  delta, `kct net-status --why --format json`) — nothing is applied to
  any board at this stage.

- **`kct readiness`: scripted manufacturing sign-off** (#4977, #5170,
  #5391) — `kct readiness <board>` produces the hash-bound
  `output/readiness.json` evidence the demo gallery validates,
  orchestrating engines that already exist (`kct check`, `kicad-cli pcb
  drc --refill-zones`, `kct export`, PDF exports) through an injectable
  `Engines` bundle rather than duplicating them — previously the
  sidecar had to be assembled by hand, gate by gate. Component-stress
  rows are wired into readiness blockers, and finished releases are
  verified without replacing recipe contents.

- **MCP server: call observability, design-intent tools, and the 2.x
  SDK** (#4855, #4897, #5102, #5103, #5128, #5601) — both MCP tool
  dispatch boundaries record each call's name, duration, status and
  error kind into a bounded ring buffer exposed by a new
  `get_recent_calls` introspection tool, so a calling LLM can
  self-diagnose a failing call instead of it being opaque; typed
  session-intent tools are registered and a new `get_design_intent`
  tool reads a project's `.kct` constraints and decisions;
  `--transport http` host/port now actually reach the server
  constructor; MCP clearance measurements share the validator's copper
  geometry instead of a parallel model; and the server migrates to the
  mcp 2.x SDK (`FastMCP` → `MCPServer`, run-kwarg transport options,
  fastmcp 4 test client) with a guarded import that distinguishes "not
  installed" from "API moved" — the exact misclassification that
  previously turned mcp 2.x failures into silent test skips.

- **Board-file writes are atomic and lock-aware** (#4898, #5105) — the
  `.kicad_pcb` / `.kicad_sch` / `.kicad_pro` writers stage to a
  temporary sibling and `os.replace` into place, so a crashed write can
  no longer truncate a board file; and writes check KiCad's lock markers
  first, so kct refuses to clobber a board another KiCad instance has
  open.

- **PCB editor: pad edits persist and pad identities are stable**
  (#5049, #5057) — pad edits on newly added footprints are actually
  written back (previously lost on save), and placing a footprint
  assigns persistent unique pad identities so later edits address the
  same physical pad.

- **Pad identity and arc geometry survive loading** (#5112, #5368,
  #5480) — the schema preserves footprint arc midpoints in both modern
  and legacy writers; empty quoted pad identities round-trip instead of
  being dropped; and repeated pad numbers retain their physical targets
  so two same-numbered pads do not collapse onto one router target.

- **Parts sourcing preflight fails honestly** (#4990, #4995, #5029,
  #5033, #5034, #5043) — unpopulated symbols are excluded from sourcing
  preflight; an explicit non-LCSC MPN is never auto-matched to an LCSC
  part; incompatible resistor suggestions are rejected; an unavailable
  lookup is distinguished from catalog absence; inventory provenance is
  preserved and unverified availability rejected; and official API
  error reasons and status survive into reports instead of a generic
  failure.

- **Hash-bound manufacturing submission handoffs** (#5142, #5143,
  #5144, #5146) — offline assembly submission packages are immutable and
  complete: submission coordinates are decimal-context independent;
  inventory evidence is bound to verified handoffs by exact ID; the
  factory-selected component CSV is verified rather than trusted; a
  hash-bound human review record ships with a portable review page; and
  raster DFM reports and OCR transcriptions are bound to the exact
  uploads they describe.

- **Offline JLC upload model with a durable ledger** (#5145) —
  `kicad_tools.manufacturers.jlc_upload` models uploads as a state
  machine (succeeded / failed / uncertain) over an append-only,
  fsync-intent ledger: the published handoff, the hash-bound human
  review and the exact Gerber bytes are re-verified immediately before
  any request is built (a gate failure never reaches the network), and
  the intent is persisted before sending so a crash mid-request folds
  to "uncertain" by construction rather than double-submitting.

- **Manufacturing export bundles its real inputs** (#5071, #5415) —
  project-local symbol library dependencies are bundled into the export
  so it opens away from its source project, and authored board rules
  survive into the manufacturing project archive instead of being
  dropped at packaging.

- **Source-bound soldermask verification** (#5135, #5136, #5137) — the
  validator inspects standard source-bound soldermask geometry, resolves
  the native-exported soldermask and copper geometry, and checks
  source-bound mask-to-copper exposure, so mask openings are judged
  against the board's own geometry rather than assumed.

- **Analog budgets reviewed with bound evidence** (#5041, #5064, #5066,
  #5426) — the analyzer reviews weak-source and gate reservoir budgets,
  reports explicit analog threshold and hysteresis budgets, qualifies
  resistor operating budgets only with bound evidence, and requires
  real resistor topology before granting impedance sense exclusions —
  every pass names the evidence it was computed from.

- **Board07 analog signal integrity is default-on and measured**
  (#4969, #5134, #5153, #5343, #5537) — impedance-corrected MIPI/HDMI
  sizing is the default recipe; via load derives from measured geometry
  with DQ directions split; the total external-load capacitance gate
  replaces per-pin accounting; the standalone board07 generator's
  defaults are isolated so it cannot drift the repo recipe; and a
  committed placement delta writes its netlist back, keeping schematic
  and placed board in agreement.

- **Board03/04/06 manufacturing-pipeline fixes** (#5000, #5176, #5326,
  #5358, #5439, #5472) — board03 applies its manufacturing profile
  before zone fill, patches the AVR core USB suspend/resume clock
  TODOs, and connects isolated supply copper behind a gate on
  saved-byte opens; board04 selects clearance-safe manufacturing via
  bonds and stops emitting unsupported route-failure diagnoses; and
  board06's pour-net audit is via-modelling layer-span aware.

- **Benchmark provenance discipline** (#5072, #5280) — `kct bench`
  distinguishes route outcome from artifact provenance so a stale
  artifact can no longer read as a fresh result, and board09's host-bus
  routing is measured as a benchmark with native validation.

- **kicad-tools.org: dated snapshots and deployed-artifact parity**
  (#5279, #5295, #5296, #5298, #5321, #5318) — the benchmarks page is
  presented as a dated historical snapshot; capability discovery and the
  shared presentation are refreshed; verified development readiness is
  shown without a board summary; external routing outcomes carry their
  dates and limitations; routing status is clarified with readable
  label contrast; and post-deploy verification now catches the live
  site serving pre-repair board copper (board05's 41 arbitrary-angle
  bottom traces) before visitors download it.

- **CLI polish** (#4688, #4689, #4690, #4751, #5028) — auto-skipped
  pour nets join the `--net-class-map` resolution domain instead of
  routing unclassed; the route startup banner shows the user-supplied
  input path; grid-resolution advisories fire only for engines that
  actually route on the grid; `--voltage-map` resolves to an absolute
  path at parse time so it survives the subprocess working directories
  the creepage audit runs in (previously looked up against the output
  dir under `kct build -o <external dir>`); and suppressed format
  defaults render correctly in `--help`.

- **DRC constraint and message integrity** (#4701, #4989, #5024, #5032,
  #5380, #5534) — the via-in-pad advisory names its resolution source
  instead of always printing "Defaulting"; short collinear silkscreen
  overlaps are retained as findings rather than filtered out; manifest
  freshness is verified against PCB content rather than file metadata;
  authored DRC constraints carry across routed-output renames; DRC
  enforces object-specific factory clearances; and the kicad-cli 10
  hole-to-hole wording (`min X mm; actual Y mm`) parses instead of
  being skipped.

- **Router search honors physical clearances end to end** (#4991, #5410,
  #5434, #5450, #5454, #5455, #5458, #5482, #5495) — `_escape_radial`
  gets board-wide foreign-pad clearance; bounded native searches run to
  completion under physical clearances; tapered goal-via landings are
  validated at the emitted width; negotiated physical copper demand and
  tapered escapes replace nominal-width accounting; net-class clearance
  holds across lattice through-vias; the final relief probe diversifies
  its search direction; actual copper width is checked against physical
  board edges; static partner-pad clearance is retained during native
  search; and per-connection avoidance costs are cleared after each
  connection so earlier nets cannot tax later ones.

- **Router state survives resets, replays and budgets** (#5035, #5266,
  #5274, #5302, #5374, #5478, #5545, #5555) — routing cleanup is bounded
  by invocation process supervision; each search stage's budget is
  separated from the hard total timeout so one stage cannot starve the
  rest; grid pathfinders enforce SMD via capability; bare net names
  survive normalization; the board-edge keepout is reinstalled across
  trial resets and worker reconstruction; escape and two-phase routing
  checkpoints are retained; congestion-mark stagnation recovers (board06
  converges without the avoidance leak); and `add_obstacle` keepouts
  persist across grid resets.

- **Routed copper is preserved across replay and repair** (#5223, #5261)
  — legal pour escapes and signal trunk widths survive post-route
  processing, and routing cache replay no longer drops committed copper.

- **Routing under placement exclusion is explicit** (#5346, #5347,
  #5348, #5357) — the router takes a placement disposition and
  fixed-copper handoff, preserves excluded filled copper across routing
  engines (including actual custom-pad copper for placement-excluded
  nets), routes the valid nets while preserving placement-invalid copper
  instead of aborting whole-board, and reports placement-blocked routing
  populations per attempt.

- **Kelvin routing keeps physical branch separation** (#5398, #5475) —
  Kelvin branches preserve physical isolation through the router, and
  Kelvin destination escape copper stays reachable.

- **Zones: reproducible fills and neighbor-aware allocation** (#5578,
  #5590) — generated zone UUIDs derive from zone content so pour fills
  are reproducible, and new pours allocate around pre-existing
  same-layer zones instead of overlapping them.

- **Schematic rail naming and wire-stub repair** (#5015, #5095) — power
  flags stay out of rail names, and exact-grid wire stubs are repaired
  safely.

- **Analysis model corrections** (#5220, #5263, #5362) — current-path
  analysis respects physical copper layers and graph edges; reinforce
  preserves force-path eligibility through segment chaining; and
  fill-stroke encoding resolves by token then file version instead of
  guessing.

- **Reporting-honesty polish** (#4736, #4752, #4967, #5287) — a vacuous
  label-LVS leg prints a dedicated "VACUOUS (treated as FAIL)" summary
  line instead of a misleading generic mismatch count; an exception-path
  rollback announces where the `.failed-<ts>` forensic sidecar was kept;
  empty `(justify …)` residue and zero-angle residue are no longer
  emitted; the negotiated progress line counts only fully routed nets;
  partial snapshots are labeled as current routing state rather than
  final; rollback reports are scoped and difficulty choices derived; and
  the benchmark error format is blessed.

- **`kct build` route-skip paths run the shared creepage audit** (#4733)
  — build's three route-skip paths gate on the same creepage audit the
  pipeline uses (#4649), so a skipped re-route is validated against
  existing copper instead of silently trusted.

- **`kct stitch` exposes the native DRC verdict** (#4973) — stitching
  reports the native DRC verdict for the stitched board and can gate on
  it, instead of asserting connectivity improvements alone.

- **`kct explain` gains Konnect design-review audit-parity checks**
  (#4899) — the explain surface runs the Konnect-derived audit-parity
  mistake checks, surfacing review-process defects (a review that claims
  to cover a gate without showing the gate's evidence) as findings.

- **Verifiable route receipts** (#5436) — `kct route` binds the final
  board and its custom rule artifacts into a verifiable receipt,
  rejecting stale constraints before binding, so a routed artifact and
  the rule set that produced it are provably paired.

- **RoutingPlan overflow accounting** (#5529, #5544) —
  `RegionGraph.get_total_overflow()` no longer undercounts
  reverse-direction edge overflow, and RoutingPlan edge entries sum both
  edge directions, so the serialized plan's demand/overflow matches what
  the global pass actually measured.

- **`kct board-metrics` describes development boards before
  manufacturing export** (#5055) — recovers explicit or unambiguous
  static artifacts, preserves blocked readiness, and requires fresh
  hash-bound native evidence for separate geometry and open counts.
- Stackup factory constructions are named and preserve legacy
  compatibility (#4994).
- The executable OSH Park rules template regenerates correctly from the
  manufacturer profile (#4999).
- `kct render` reports unresolved component 3D models instead of
  silently omitting them (#5008).

- Run the report-only routing-plan stage on **every** default `kct route`,
  not just the dense-package boards that reach the two-phase global router.
  The tile-graph build + negotiated global pass + `RoutingPlan`
  serialization moved out of `TwoPhaseRouter.route_all` into
  `routing_plan.build_plan` / `select_plan_nets`, and the new
  `Autorouter.plan_routing()` runs the identical stage from
  `route_all_negotiated` -- covering all seven CLI dispatch sites (the three
  escalation wrappers plus the fixed-layer `--layers N` / `--no-auto-layers`
  closure), `route_with_progressive_clearance` and library callers with one
  hook. Dense and non-dense boards now share one net-selection path, so
  their plans agree. The stage is silent, never touches the routing grid and
  never runs twice (guarded on `routing_plan is None`), so routed copper is
  unchanged; the new `--no-routing-plan` flag is the only escape hatch and
  the only flag added (Issue #5520, Phase 1b of Epic #5510). See
  `docs/reference/routing-plan.md`.

- Apply a DECLARED swap group's pad re-binding instead of only reporting it: a
  `reorder_pins` placement delta carrying a `pad_map` is now executed by the
  placement-delta feedback loop through the new `StrategyType.REORDER_PINS`
  applicator (`assign_net_to_footprint_pad` on the PCB plus the router's own pad
  list and net membership), kept only on a strict routed-net improvement and
  otherwise reverted atomically -- pad net bindings included. A `reorder_pins`
  delta without a declared `swap_group` stays rationale-only and is still never
  applied, so flag-off behavior is unchanged. `boards/07-matchgroup-test/
  ddr_bundle_isolation_repro.py --mode reversed` measures the result on a
  reversed DDR byte: 9/11 nets reached before the swap, 11/11 after, with the
  proposal reporting `crossings 55 -> 0` (Issue #5536, Phase 2a of Epic #5511).

- Add a report-only `RoutingPlan` sidecar (`<output_stem>.routing_plan.json`)
  serializing the tile-based global-routing pass's per-net corridor
  assignments and per-edge demand/capacity/overflow that `kct route`'s
  default path already computes for dense-package boards but previously
  discarded. Text mode prints a one-line summary; `--format json` adds a
  `routing_plan` key. Building the plan is purely a read of existing
  `RegionGraph` state, so routed copper is unchanged (Issue #5519, Phase 1
  of Epic #5510). See `docs/reference/routing-plan.md`.

- Add transactional physical power stitching (`kct stitch --complete`), retaining
  native refill evidence and rejecting incomplete nets, broken pad bonds, or new
  native findings; use the shared stage in Board05's active recipe (#5388).

- Add bounded +/-90-degree endpoint-orientation (`rotate_align`) placement-delta
  candidates for a stuck net whose two endpoints present a pad-row/pad-column
  mismatch, respecting fixed/locked placement and connector-access
  constraints, reusing the corrected absolute-pad-rotation applicator (#4966)
  and revalidating zone-carried power/mounting-pad connectivity after
  rotation. Candidates carry auditable pad-alignment rationale and are never
  a manufacturability verdict on their own (#4968).
- Normalize legacy center/angle arcs in contour editing, board graphics, and placement linearization so rounded outlines remain connected and replacement preserves only actual mounting-hole contours (#4884).
- Add read-only netclass diagnostics for undefined assignment targets, duplicate
  declarations and KiCad 10.0.5-verified pattern membership against supplied
  board nets, with explicit malformed/unsupported diagnostics (#5334).

- Add a daily CI guard (`.github/workflows/assert-no-bot-external.yml`) that
  fails if any open issue labeled `external` has a Bot-type author (#5310).
  `external` is a hard-exclusion label that blocks Loom dispatch, and a
  GitHub App installation actor (e.g. `loom-fleet-dispatch[bot]`) never
  passes `repos.checkCollaborator`, so every issue it filed before PR #5233
  got mislabeled `external` and silently stalled the pipeline; this catches
  a recurrence of that regression class automatically instead of via a
  stalled sweep. The already-mislabeled open issues were also cleaned up as
  a one-time forge-state fix (label removed by author id, not a blanket
  strip).
- Fix Codex-only installer workflows to resolve generated sibling skills and namespace help, with runtime-appropriate invocation and optional metadata handling.
- Use shared project drill-clearance checks for Board07 relocation and fallback stubs; reject archived moves into foreign zone fill without saving partial repairs.

### Fixed

- Preserve narrow via-annulus contacts between copper-fill fragments in net-status analysis without bridging drill holes or unrelated layers (#5382).
- Correct the historical Board07 HDMI track widths to match their sidecar while preserving measured connectivity, known opens, and angle geometry; add a reproducible native-validated width repair (#5126).

- **Imported copper arcs no longer cause false opens or under-reported
  wirelength** (#4937) — `PCB` parsed `(segment ...)` and `(via ...)` copper
  but had no branch for `(arc ...)`, the curved-track element KiCad 7+ writes
  for rounded copper; an externally-sourced board routed only with an arc
  reported two disconnected islands and 0 mm of trace length. A new `Arc`
  schema class exposes analytic swept length and geometric connectivity
  (`pcb.arcs`, `arcs_on_layer()`, `arcs_in_net()`), threaded into
  `NetStatusAnalyzer` (arc-bearing boards always use real copper geometry,
  even under `strict=False`, since endpoint proximity cannot describe curved
  contact), `routing_quality`, `trace_length`/diff-pair skew, and the
  external-benchmark wirelength metric. PCB saves preserve untouched arc
  source text byte-for-byte (numeric spelling, whitespace, CRLF), while
  edits invalidate the retained text so page-fit/reimport keep working.
  Invalid or nonfinite arc geometry now fails explicitly instead of
  silently degrading to a chord.
- Preserve authored pad shapes through router loading, workers, and native
  conversion (#5229). Square pads no longer lose copper corners to a circular
  approximation. Rotated search bounds enclose copper; unsupported custom or
  layer-specific pad geometry stops routing explicitly.

### Added

- **Declared current paths now fail closed on same-net routed arcs and
  copper pours** (#5273) — `resolve_current_path()` previously built its
  copper graph only from routed `Segment` tracks and via barrels, so a
  same-net routed **arc** or a non-keepout **zone/pour** — either of which
  can form a parallel return path around a declared branch (a plane is the
  archetypal case) — was invisible to it and never affected the result. A
  declared branch on such a net now resolves `"ambiguous"` instead of
  `"resolved"`, naming the unmodeled copper in the reason (endpoint
  resolution failures still take precedence and remain `"unresolved"`).
  New `unmodeled_copper()` inventories same-net arcs and non-keepout
  zones/pours (kind, layer, representative location); keepout rule areas
  are excluded since they carry no copper. `CurrentPathAudit` gains an
  `unmodeled` field, surfaced by `kct pcb current-paths-audit` in both JSON
  and text output, and `kct check`'s `path_ampacity` rule emits a
  `warning` per unmodeled-copper object found (the `ambiguous` status
  already produces the `error`). No change was needed in
  `pcb/reinforce.py`: its allow-list gate only admits copper from a
  *resolved* path, so a net with unmodeled copper drops out of
  reinforcement eligibility for free once `resolve_current_path()` stops
  returning `resolved` for it.
- **Pulsed / duty-cycled current on declared branch current paths** (#4980) —
  `CurrentPathSpec` gains optional `duty_cycle` and `pulse_duration_s`
  alongside the existing `pulsed_a`, which until now was parsed, serialized
  and then ignored by every consumer. A pulsed branch is now checked two
  ways, because a repetitive pulse can destroy copper by a mechanism the
  steady-state width check cannot see:
  - **Thermally**, at the waveform's RMS current
    (`CurrentPathSpec.thermal_design_current()` →
    `physics.ampacity.rms_current_for_duty_cycle()`), not its peak and not
    its average. An 18 A pulse at 8 % duty over a 3 A baseline heats copper
    like 5.85 A, so it no longer demands 8.1 mm of 2 oz copper to pass.
  - **Adiabatically**, against the Onderdonk fusing current for the declared
    pulse duration (`physics.ampacity.adiabatic_fusing_current()`). A trace
    comfortably sized on RMS heating can still be melted by a single inrush
    or fault pulse; that is now an `error` rather than an invisible risk.

  Every assumption is declared, never inferred, and every gap is visible: a
  pulse with no `duty_cycle` is sized at its peak **and says so** (an `info`
  finding naming the missing field), and a pulse with no `pulse_duration_s`
  leaves fusing explicitly unchecked (a `warning`) instead of passing
  silently. Half-declared waveforms — a `duty_cycle` or `pulse_duration_s`
  with no `pulsed_a`, or a `pulsed_a` below `continuous_a` — are rejected at
  load time rather than guessed at. Continuous-only declarations are
  unaffected: their findings are byte-identical to before. Flows through all
  existing surfaces (`kct check`, `kct route`, `kct pcb
  current-paths-audit`), which now also report each branch's thermal design
  current and which waveform assumption produced it.
- **Flat signal-clearance table builder for clock-to-signal spacing**
  (#5021) — `build_signal_clearance_table()` in
  `router/pairwise_clearance.py` generalises the HV pairwise-clearance
  resolver (#4431) past voltage-derived requirements to a flat,
  non-voltage signal-integrity spacing rule, e.g. ST AN4488 §8.4.2's SDRAM
  clock-to-signal guideline (three trace widths, 0.54 mm edge-to-edge). It
  populates the same `PairwiseClearanceTable` data carrier the HV epic
  built, so every downstream consumer (`route_pairwise_violation`,
  `find_pairwise_violations`, `PairwisePathChecker`, the C++ `Grid3D`
  projection) gets identical route-time avoidance and post-route audit
  coverage across tracks, pads and via spans — with no separate
  implementation to keep in sync and no routing-order dependence (the
  board07 "reciprocal clock guard" defect this issue tracks: a clock track
  cleared a foreign net's tracks by the full requirement but passed a
  foreign through-via at the ordinary fabrication clearance because the
  via's copper was never widened). No automatic package-escape exemption
  is applied; a genuine waiver must be an explicit, measured `AttachZone`.
- **`kct route --current-paths` / `--no-current-paths`** (#4980) — the third
  and final consumer of the declared branch-specific current-path model
  (after `kct pcb reinforce`, #5125, and `kct check`, #5184). The route
  command now loads the same `current_paths.json` sidecar (explicit path, or
  auto-discovered next to the input board with the `--net-class-map` probe
  order), runs the `path_ampacity` rule in the post-route DRC so each
  declared branch is judged against its **own** current rather than one
  whole-net `target_ampacity`, and re-emits the declarations as
  `current_paths.json` next to the routed board. That re-emission is what
  makes route-time intent and the later independent final-copper audit read
  identical declarations — a bare `kct check` on the routed board
  auto-discovers it instead of silently passing with `path_ampacity`
  inactive. An explicit sidecar is strict (missing file / malformed JSON
  exits 1 before any routing work); an auto-discovered one degrades to a
  warning; the authored input file is never overwritten (a collision diverts
  the derived sidecar to `current_paths.effective.json`, the #4428 rule).
- **Via-in-pad process-eligibility model** (#5009) — `via_in_pad_supported`
  on `MfrLimits`/`DesignRules` was a bare capability boolean that silenced
  the entire `via_in_pad` DRC rule without validating drill range, layer
  count, annular ring, filled-and-capped construction, or minimum
  component-hole distance against a manufacturer's real published process
  (e.g. JLCPCB's Plated-Over Filled Via/POFV requires 4+ copper layers and
  a 0.2-0.5 mm drill, even though Capability Plus's `via_in_pad_supported`
  flag is `True` on its 2-layer configs too). Adds
  `kicad_tools.manufacturers.fabrication_process.FabricationProcess`, a
  specific orderable process (layer-count floor, drill range, annular-ring
  floor, `requires_filled_and_capped`, minimum component-hole distance,
  source citation) attached to a manufacturer profile config via a new
  `DesignRules.via_in_pad_process_id` field (`jlcpcb-tier1`'s 4+ layer
  configs and every `pcbway` config now carry one; `jlcpcb-tier1`'s
  2-layer configs deliberately do not). `ViaInPadRule` now fails closed
  in three distinct cases: no capability (`via_in_pad`, original #2635
  behavior), capability present but no eligible process declared
  (`via_in_pad_process_missing`), and a declared process whose
  requirements the via's actual geometry does not meet
  (`via_in_pad_process_ineligible`) — a bare capability flag with no
  process selection no longer suppresses a finding. `kct check` and
  `kct export`'s manufacturing bundle (`manifest.json` + `README.txt`)
  now bind the resolved process's machine-readable requirements and
  human-derived ordering instructions into their output when a board's
  layer/copper configuration carries one.
- **`kct analyze component-stress` — operating-state MOSFET VDS/VGS gate**
  (#5039) — a new advisory analyzer
  (`kicad_tools.analysis.component_stress.ComponentStressAnalyzer`) that asks
  the question ERC, DRC, creepage and `analyze electrical-rating` all
  structurally miss: *is the device itself rated for the potential difference
  its own terminals will see?* Stress is computed as a terminal-to-terminal
  differential (`VDS = V(D)-V(S)`, `VGS = V(G)-V(S)`) inside a **single** entry
  of an explicit, reviewed operating-state manifest (`--states`, YAML or JSON),
  so correlated nets are never combined across unrelated states and shifting a
  floating gate-driver domain's reference leaves both differentials unchanged.
  Ratings come only from sourced `Vds_max` / `Vgs_max` symbol fields (no
  built-in defaults); an undeclared state from the required coverage checklist
  (startup, precharge, both mains polarities, support, trip, loss-of-drive), an
  unresolved D/G/S pin role, an unbound terminal or an uncited rating is
  reported `UNRESOLVED` — a release blocker, never a silent pass. Pin-role
  mappings are cached under a part identity that includes the MPN and
  footprint, so a part swap cannot carry a stale pinout forward, and a
  footprint creepage/spacing waiver can never suppress a device-stress finding
  (the two read disjoint inputs). No automatic circuit-state inference is
  performed in this pass.
- **`kct place-silk-refs`: readable silkscreen reference placement** (#5030)
  — a dry-run/apply solver that moves (and, optionally, rotates) visible
  reference-designator text just far enough to clear real pad/via mask
  apertures, other silk/text, and the board edge, while preserving
  visibility, text height, and stroke width exactly (never hiding,
  shrinking, or deleting a label to "clear" a DRC finding). References are
  kept near their own component and never placed on top of a courtyard
  (its own or a neighbor's); a reference with no collision-free candidate
  in the search radius is left untouched and reported explicitly as
  `unplaceable` or `under_component_fallback` (when it was already sitting
  on its own body) rather than silently dropped. Only the reference text's
  own `(at x y [angle])` node is ever written — footprint position, pads,
  copper, and net bindings are untouched. Reads the same geometry helpers
  `kct check`'s silk DRC rules use, so a clean plan reproduces a clean
  native `kicad-cli pcb drc` (`--verify-drc`); `--render` writes an SVG
  review artifact (old vs. new position, pad apertures, courtyards) since
  a passing DRC run does not by itself prove the placement is readable.
  Retires the need for the ad hoc
  `hardware/chorus-test-revA/scripts/place_silk_refs.py` local helper.
- **Installer: explicit Codex and Claude client targets** (#4905) —
  `scripts/install-kct.sh` gains `--client claude|codex|both` (default
  `claude`, fully backward-compatible). Codex selection generates one
  `.agents/skills/kct-<name>/SKILL.md` per skill from the SAME source
  `.claude/commands/kct/<name>.md` files Claude vendors — one maintained
  source, not a hand-duplicated copy — with the `SKILL.md` frontmatter
  carrying only `name`/`description` (Claude's `invocation`/`suggestedModel`
  dispatch metadata is deliberately not copied), plus an additive guarded
  `AGENTS.md` block pointing at the shared `.kct/CONVENTIONS.md`. The shared
  uv dependency, `.kct/ci/` gates, and `.kct/CONVENTIONS.md` stay
  client-independent. Selecting one client never overwrites, removes, or
  duplicates the other client's files or a prior/switched install's valid
  artifacts — `install-metadata.json`'s `clients_installed` /
  `skills_selected` / `installed_files` fields accumulate (union) across
  repeated or switched-client runs instead of being replaced.
- **`kct route --reserve-plane-layers`: controlled-impedance signal-layer
  reservation guardrail** (#5014) — `LayerDefinition.is_routable` treats
  every copper layer as signal-eligible by design, including layers a
  `--layers 4`-style stack designates as a GND/PWR reference plane, so
  ordinary signal could silently consume the continuous plane a
  controlled-impedance recipe depends on. `--reserve-plane-layers`
  hard-restricts routing to the resolved stack's non-`PLANE` layers (via
  `DesignRules.allowed_layers`, issue #715's pre-existing enforcement); a
  new route-time advisory recommends the flag whenever a plane-bearing
  stack is resolved without it, and a post-route audit reports any
  committed signal segment that still landed on a declared plane layer. A
  no-op on stacks with no `PLANE` layers (`--layers 2`, `4-all`, or an
  all-signal `auto`-detected board). See
  `kicad_tools.router.layer_advisories` for the full contract.
- **Konnect item 8 audit: natural-language design-rule store** (#4902, Part
  of #4880) — `docs/konnect-item8-design-rules-audit.md` decides **decline**
  on adding a Konnect-style free-text design-rule store: the repo already
  has five overlapping mechanisms in this space (`DesignIntent.constraints`,
  the typed `kicad_tools.intent` package, manufacturer profiles,
  recipe-embedded intent, and — not previously catalogued — `project.kct`'s
  `decisions:` log written by `kct spec decide`), and
  `.claude/commands/kct/board-recipe-scaffold.md` has already settled the
  adjacent "how do we record project *why*" question on that same
  `decisions:` surface with an explicit "do not invent a fifth artifact"
  principle. Two follow-ups are recommended rather than bundled: registering
  the fully-implemented-but-unwired `declare_interface`/`declare_power_rail`/
  `list_intents`/`clear_intent` MCP tools, and surfacing
  `intent.constraints`/`decisions` into MCP session context so an LLM agent
  can see prior human-recorded guidance without a human re-pasting it.
- **kicad-tools.org benchmarks section** (#4952, Epic #4932 Phase 3) — a new
  `/benchmarks` page on the demo-gallery site presenting Phase 2's committed
  results (PocketBeagle + BeagleConnect Freedom, #4942) side by side with
  DeepPCB's own published numbers (https://deeppcb.ai/benchmarks/), read at
  build time from `benchmarks/external/results/*.json` — no numbers are
  hand-copied into the page. A methodology section distinguishes the
  zero-touch protocol (rules as-shipped) from the tuned protocol (declared
  netclass/diff-pair config, STRF-only, #4943). Both boards refused to route
  zero-touch, so each is honestly annotated with its filed generic-router-
  capability issue (#4945, #4946) rather than presented as a bare, unexplained
  percentage. STRF has no committed report yet in this repository — the page
  says so explicitly (linking Epic #4932 for status) instead of omitting the
  board or fabricating a number. Reproduction instructions list the exact
  harness command plus each board's pinned upstream commit. New site nav link
  ("Benchmarks") added to the shared header.
- **`kct bench external --tuned` -- declared-netclass protocol for STRF**
  (#4943, Epic #4932 Phase 2) — a second protocol for the zero-touch
  harness (#4941/#4944): applies a declared per-board netclass/diff-pair
  config (`benchmarks/external/tuned_rules.py`) before routing, mirroring
  DeepPCB's own published STRF case study
  (USB 90R diff pair at 0.20/0.15mm, SPI bus on its own compact-via
  netclass) rather than routing with rules as-shipped. Currently defined
  for STRF only — other board slugs fail with a clear per-board error
  under `--tuned` instead of silently falling back to zero-touch rules.
  Reported strictly separately from the zero-touch protocol (Epic #4932's
  stated risk register: DeepPCB's case-study numbers involved manual
  netclass setup, its vs-Quilter numbers were zero-touch) — output files
  are named per protocol (`<slug>.<protocol>.json`,
  `report.<protocol>.md`) so a `--tuned` run never overwrites a prior
  zero-touch run in the same `--output-dir`. Same report schema and
  native-backend timing gate as the zero-touch protocol. See
  `benchmarks/external/README.md` for usage.
- **PocketBeagle + BeagleConnect Freedom zero-touch benchmark runs**
  (#4942, Epic #4932 Phase 2) — ran `kct bench external` (#4941/#4944)
  against the two remaining pinned boards and committed the results to
  `benchmarks/external/results/`. Neither board placed a single track:
  the router's own pre-route safety gates refused both whole-board,
  before laying any copper — PocketBeagle's off-grid-escalation fine-zone
  solver blows its cell budget on a large coarse-pitch through-hole
  header (filed as #4945), and BeagleConnect Freedom's `--allow-offboard`
  placement preflight is whole-board rather than scoped, so one
  off-board footprint vetoes routing the other ~455 connections (filed
  as #4946). Both are documented as generic router-capability gaps, not
  board-specific patches — the pinned board files are unmodified. License
  notes (both CC-BY-4.0) were already recorded in
  `benchmarks/external/boards.toml` by Phase 1.
- **`kct bench external` -- zero-touch DeepPCB-comparable benchmark CLI**
  (#4941, Epic #4932 Phase 2) — drives the full pipeline as one command:
  fetch a pinned third-party board (`benchmarks/external/fetch_boards.py`,
  #4933), rip up its copper (`normalize.py`), route it with kicad-tools
  using rules as-shipped (no netclass/diff-pair tuning), then measure the
  result with the Phase 1 metrics module (`kicad_tools.benchmark.external`,
  #4934) and write a JSON + markdown report in the schema documented at
  `docs/benchmark-external-report-schema.md`. Gates wall-clock timing on
  the C++ router backend exactly as `kct build-native --check` reports it
  — the stopwatch is never even started without it, so a Python-fallback
  number can never reach the report (this project's routing-performance
  convention). `--seed` plus the manifest's pinned commit gives
  byte-reproducible runs; `--skip-fetch` reuses an already-fetched board
  and `--skip-kicad-cli-drc` degrades gracefully on hosts without
  `kicad-cli`, both honestly recorded (never rendered as "clean"). See
  `benchmarks/external/README.md` for usage.
- **External DeepPCB-comparison benchmark board acquisition** (#4933, part of
  Epic #4932) — `benchmarks/external/boards.toml` pins the three open-source
  KiCad boards DeepPCB publishes autorouter numbers for (STRF, PocketBeagle,
  BeagleConnect Freedom) to a full commit SHA each, plus their upstream
  license and DeepPCB's published reference numbers.
  `fetch_boards.py` materializes each board's `.kicad_pcb` at its pinned
  commit into a gitignored cache dir (GitHub tarball / GitLab archive API,
  no vendoring, no credentials), verifying the fetched tree actually
  references the pinned SHA. `normalize.py` then loads the fetched board,
  captures the pre-rip-up "human baseline" routing stats
  (`PCB.routing_status()`) as a JSON sidecar, strips all existing tracks and
  vias while preserving footprint placement/nets/netclasses/zones/outline,
  and saves the route-ready result — falling back to `kicad-cli pcb upgrade`
  for boards whose legacy (KiCad 5/6-era) format this repo's parser cannot
  read directly. `PCB.remove_vias()` is new (mirrors the existing
  `remove_segments()`) so the rip-up step has a public via-removal
  primitive to call. Feeds Epic #4932's metrics-extraction phase (#4934).
- **Placement-only pre-route escape-capacity forecast** (part of #4799) —
  `kct route --capacity-forecast` (and `--capacity-forecast-json <path>` for the
  JSON document) prints, before any router or component loading, whether each
  multi-ring pad field's interior pins can physically leave it. Unlike the
  earlier slices of #4799 — the crossing-tail census report (#4852), its replay
  (#4862) and gate (#4865), all of which need a *previous* run's measurement —
  this predictor reads placement and design rules only, so it is exact on the
  first route of a brand-new board. The model
  (`router/capacity_forecast.py`) peels each pad field into rings, counts the
  tracks each inter-pad channel carries, tests whether a via can be dropped at
  the **interstitial** site (the diagonal gap a dogbone actually uses — a
  1.27 mm/0.45 mm array leaves 0.41 mm orthogonally but 0.58 mm diagonally, and
  modelling the wrong one turns a healthy BGA into a false blockage), and
  reports `demand / supply` per ring cut with verdicts
  `not-applicable` / `ample` / `tight` / `over-capacity` / `infeasible`. Pins on
  poured nets are deferred only where a via drop exists. **Advisory in every
  case**: the preflight returns no value for the caller to turn into an exit
  code, and an unreadable board or unwritable report degrades to one stderr
  line. Measured on the fleet (2026-08-15): 1–23 ms per board (0.9 s on a
  90-footprint external design), two multi-ring fields found — boards 06 and
  07's 7×7 arrays, both `ample` at 0.08×/0.05× — and **no board flagged**, the
  honest reading, since both blocked boards fail for mechanisms this counting
  model does not claim to see. Its positive controls are pinned as tests: the
  same array with no legal via drop is 1.04× over capacity, and a 0.5 mm-pitch
  array is `infeasible`. A board-scale RUDY estimate was measured first and
  rejected — 1.2–3.5 % utilisation on every board, including the two that do not
  route. Docs: `docs/reference/capacity-forecast.md`.

- **HV pairwise proof run on the softstart rev-C mains board** (part of #4507,
  epic #4431 Phase 2) — `docs/hv-pairwise-softstart-proof.md` records the T4
  manual criterion's local run: the fixture is local-only by design, so the
  proof is a documented measurement rather than a CI job. Both arms of the same
  recipe on the same placed board, differing only in whether `--voltage-map` is
  supplied: the pairwise search-time avoidance removes **54 → 3** same-layer
  HV track↔track violations (and **107 → 42** board-level `kct creepage`
  census fails) for the cost of one net and ~22 % wall time — retiring the
  fixture's earlier "enforcement costs ~40 % of routability" reading. **T4
  nonetheless FAILS** (49/82 nets, 42 board-level fails, target 0), and the run
  root-causes the residual: `build_pairwise_clearance_table()` normalises net
  potentials with `abs()` before differencing them, so on a *signed* voltage map
  a +150 V net and a −150 V net difference to 0 V instead of 300 V. On
  softstart's own 84-net map that under-constrains **339 of 1922 cross-pairs
  (17.6 %)**, with 99 pairs — every POS↔NEG bank pair, up to 3.20 mm required —
  dropping out of the matrix entirely and becoming invisible to both the search
  and the post-route audit. The census (`creepage/engine.py`) differences the
  same file's values *signed*, so the router and the gate disagree by
  construction. Documentation only; no behavior change in this entry.

- **Cross-domain (HV) failures now name a rip-up target** (part of #4507, epic
  #4431 Phase 2) — the search-time pairwise kernels
  (`Pathfinder::cross_domain_trace_blocked` / `cross_domain_via_blocked`,
  #4511) refused HV-through-LV candidates *silently*: unlike the #2476
  stored-via check they named no blocker, so a net that failed only because a
  low-voltage neighbour crowded its widened HV keepout came back as a bare
  `FAILURE_NO_PATH` — a reason code the negotiated strategy has no targeted
  response for, so it blanket-retried. That is exactly the negotiator thrash
  Phase 2 exists to end. Both kernels now record the foreign net and the
  refused candidate's world position, and a search that drains the open set
  with such an observation reports the new
  `FAILURE_PAIRWISE_BLOCKED` (7) with the blocker in
  `RouteResult.blocking_via_net` — the same field
  `NegotiatedRouter.via_blocked_ripup` already drains, so the HV net's
  crowding neighbour gets ripped up and the HV net is routed first.
  Precedence is deliberately conservative: `FAILURE_VIA_VIA_BLOCKED` still
  wins when both were seen, and TIMEOUT / ITERATION_LIMIT are never
  overridden (they are budget artifacts per #2610, not geometry). An
  attach-zone-waived (#4506) pair records nothing — ripping it up would not
  help. Router log label: `pairwise_blocked`. Strictly dormant without
  `--voltage-map` (single `pairwise_active()` boolean, unchanged route
  output); `ROUTER_CPP_BUILD_VERSION` 20 → 21 for the new binding surface.

- **Pre-route crossing-tail census advisory** (part of #4799) — `kct route
  --census-advisory <report.json>` (or `KCT_CROSSTAIL_CENSUS_ADVISORY=<path>`
  for callers that drive the router API directly) replays a census report
  written by an earlier run *before the first A\* expansion* of the next one,
  turning the crossover census from a post-mortem into a leading indicator:
  same measurement, read earlier. The `[crosstail-advisory]` block reports the
  prior run's `saturated_pct` / `inert_pct` / `verdict`, a per-net roll-up of
  the saturated crossovers (worst first), and the predicted saturated-crossover
  count for this run. Because a replay inherits the previous run's ordering,
  two guards keep it honest: report nets are cross-checked against the nets
  declared by the board being routed (nets that no longer exist do not
  contribute), and a report whose nets overlap the board by less than 50%
  (`STALE_COVERAGE_PCT_THRESHOLD`) is declared `stale` with its prediction
  suppressed rather than presented as this board's forecast. A report that
  scanned nothing stays `not-applicable` (boards 05 and 07 today), never "0%
  saturated". **Advisory only**: the preflight returns 0 unconditionally —
  a missing, malformed or foreign report prints one `no prediction:` line and
  the route proceeds unchanged — and no code branches on the verdict. Wiring
  it into a go/no-go or steering decision remains follow-up work on #4799.
  Measured on board-06's own 166-crossover report: the whole preflight costs
  **0.149 s** (0.0009 s to load, 0.147 s to read the board's nets, 0.0008 s to
  aggregate) and reproduces the run's 150/166 = 90.4% saturation figure —
  ~0.01% of the 28-minute route it now precedes instead of concluding.
  New API in `kicad_tools.router.crosstail_advisory`
  (`LoadedCensusReport`, `build_advisory`, `emit_advisory`, `board_net_names`);
  documented in `docs/reference/crosstail-census-report.md`.

- **Opt-in go/no-go on the crossing-tail census prediction** (part of #4799) —
  `kct route --census-advisory-gate` promotes the advisory above from a printed
  prediction to a decision: when the replayed census is applicable, trusted and
  predicts an **inert** crossover lattice, the route aborts with the new
  **exit code 9** *before any router or component loading* — the one route exit
  code that means "nothing was spent". The `[crosstail-gate] NO-GO` block names
  the worst nets by inert count and points at the layer that can act on it
  (placement / escape planning, not the router), plus how to proceed anyway.
  `--census-advisory-gate-pct PCT` (0–100, argparse-validated; implies the
  gate) overrides the threshold, which otherwise defaults to the same
  `SATURATED_PCT_ADVISORY_THRESHOLD` (90) the printed verdict uses, so verdict
  and gate cannot silently disagree. The gate keys on **inertness**
  (saturated + single-`v1`-site legal sets), not saturation alone, and reports
  which of the two fired. Everything that would make the prediction
  untrustworthy is a **GO** with an explicit reason token — `no-report`,
  `not-applicable` (0 crossovers scanned is not a 0%-saturated pass *or* a
  failure), `stale` (a cross-check the advisory already suppressed can never
  gate), `census-disabled`, `schema-mismatch`, `no-board-crossovers`,
  `below-threshold` — as is any exception inside the predictor. **Defaults are
  unchanged**: without the flag the preflight still returns 0 unconditionally
  and the advisory block still ends in `ADVISORY ONLY`. New API:
  `evaluate_gate` / `emit_gate_decision` / `CensusGateDecision` /
  `GATE_EXIT_CODE` in `kicad_tools.router.crosstail_advisory`; the threshold
  remains uncalibrated beyond the board-06 precedent (#4799 follow-up), along
  with a genuine pre-A\* predictor and coverage beyond diff-pair crossing
  tails.

- **Structured crossing-tail census report** (part of #4799) — the #4580
  diff-pair crossover legality census now also captures what it prints as
  data. Each censused crossover produces a `CrossingTailCensusRecord`
  (`net_name`, `head`, `goal`, `legal`, `total`, `distinct_v1`, `census_s`)
  on the router (`DiffPairRouter._census_records`) and in a process-wide
  collector; `KCT_CROSSTAIL_CENSUS_REPORT=<path>` writes a
  `--format json`-shaped document (`schema_version`, `generated_at`, summary +
  per-crossover detail) at interpreter exit, and the diff-pair phase closes
  with a `[crosstail-census-summary]` block whenever `KCT_CROSSTAIL_CENSUS=1`
  is set. The summary aggregates `saturated_pct` (crossovers with nothing
  legal), `no_ordering_lever_pct` (legal sets confined to a single `v1` site,
  taken over the *unsaturated* set) and `inert_pct` (their union — the share
  where no ordering key could have changed the outcome), plus an advisory
  `verdict` against a documented 90% threshold. **Report-only**: the capture
  runs after the #4635 budget credit is stamped, changes no routing decision
  and no exit code, and costs ~1.1 µs per crossover. A board that never
  synthesizes a shadow-constructed crossover (boards 05 and 07 today) reports
  `crossovers_scanned: 0` / `verdict: "not-applicable"` rather than a
  fabricated 0% saturation — and, since board scripts shell out and children
  inherit the environment, an empty flush never overwrites a report that has
  records. New reference doc: `docs/reference/crosstail-census-report.md`.

- **`--format json` on `build`, `pipeline` and `stitch`** (closes #4674,
  seventh and final batch of the #4543 machine-output sweep) — the multi-stage
  orchestrators now accept the canonical `--format json` and emit one
  deterministic document on stdout instead of prose, wired end-to-end (outer
  parser → shim → inner parser). `build` reports the resolved
  spec/schematic/pcb/routed-pcb paths, the effective manufacturer, and a
  `steps[]` ledger (`step`, `success`, `message`, `output_file`, `elapsed_s`)
  plus `counts`, `manufacturing_output` and `wall_time_s`; `pipeline` reports
  the resolved input/pcb/project/schematic, the mfr + layer stack, a `steps[]`
  ledger carrying each step's skip/warning classification, `commit:
  {requested, created}` and the `exit_code` (0 / 1 / 2-for-`--best-effort`
  partial); `stitch` reports the resolved via geometry and the **complete**
  per-pad ledger — vias placed, pads skipped, connectivity fallbacks, pads
  needing a routed fanout, and the obstacle breakdown. The stitch document is
  deliberately untruncated: its prose caps via lists at 10 entries and
  skipped-pad lists at 5, and a machine caller must not have to re-run with
  different flags to see the pads a run could not place. All three drive
  sub-tools that print progress on stdout, so each wraps its run in the
  stdout-diversion helper (now promoted to
  `cli/format_options.py::stdout_to_stderr_when` rather than copied a fourth
  time): the chatter is replayed on stderr and exactly one document lands on
  stdout. Failure paths emit `{"error": ..., "success": false}` documents with
  exit codes unchanged, and text-mode output is byte-identical. Audit
  (`scripts/audit_machine_output.py`): prose-only 8 → 5, format-json 189 →
  192 — the actionable backlog is now **empty**, the 5 remaining prose-only
  leaves being the 4 documented exemptions (`interactive`, `mcp serve`, `run`,
  `footprint generate`) plus `route`, deferred to the route workstream.
  `tests/test_format_json_sweep_orchestrators.py` guards the new surfaces and
  pins the prose-only bucket against that exemption list.

- **`--format json` on the 5 board-improvement drivers** (part of #4674,
  sixth batch of the #4543 machine-output sweep) — `optimize-placement`,
  `optimize-traces`, `route-auto`, `reason` and `creepage-export-rules` now
  accept the canonical `--format json` and emit one deterministic document on
  stdout instead of prose, wired end-to-end (outer parser → shim → inner
  parser/handler). `optimize-placement` reports the `board` summary, the
  `initial`/`final` score breakdowns (taken straight off the cost dataclass),
  `iterations`, `feasible` + `infeasible_detail` and `written_to`;
  `optimize-traces` reports the enabled `optimizations` and the before/after
  `stats`; `route-auto` reports one `nets[]` entry per requested net (metrics,
  written copper, warnings, partial `pads_connected`/`pads_total`, or the
  failure and its `alternative_strategies`); `reason` reports the board
  summary, the DRC block and the selected `mode`'s payload (`prompt` /
  `analysis` / `state` / `auto_route`); `creepage-export-rules` reports the
  derived `domains`, `rules` and `bridging_exemptions` plus `written`.
  Commands that can run without writing say so structurally (`saved` /
  `written` / `written_to: null` under `--dry-run`), failure paths emit
  `{"error": ..., "success": false}` documents with **exit codes unchanged**
  (including `route-auto`'s usage exit 2 and `optimize-placement`'s
  interrupt exit 2), and text-mode output is byte-identical. Two behaviours
  worth calling out: `kct reason --interactive --format json` is **refused**
  (exit 2, error document) because a stdin/stdout dialogue has no
  single-document form; and `route-auto` / `reason --auto-route` now divert
  the negotiated router's stdout progress log to stderr in JSON mode so the
  document stays parseable. Audit (`scripts/audit_machine_output.py`):
  prose-only 13 → 8, format-json 184 → 189 (only `build`, `pipeline` and
  `stitch` remain actionable). `tests/test_format_json_sweep_drivers.py`
  guards the new surfaces; `docs/reference/machine-output.md` records the
  per-command shapes.

- **Soft HV-avoidance gradient in the pure-Python fallback A\*** (part of
  #4507, epic #4431 Phase 2b) — `Router._pairwise_avoidance_cost` mirrors the
  C++ `Pathfinder::pairwise_avoidance_cost` (#4511 Scope 2) cell-for-cell, so
  both engines now price the same bounded nudge just beyond the hard
  cross-domain block instead of only the C++ search doing so. The fallback's
  hard blocking landed in #4791; without the soft term it ran flush against
  the pairwise limit, and copper that merely *meets* a creepage requirement is
  one rip-up or one `--lattice-optimize` nudge away from violating it. The A\*
  hot loop consults a pre-dilated band bitmap (one bool lookup per neighbour)
  and calls the exact kernel only for cells near cross-domain copper. Strictly
  dormant without `--voltage-map` (one `is None` test per neighbour,
  byte-identical g-scores). **As shipped, #4849 mirrored the C++ reference's
  then-current row-major scan** — including its **two-level** break with a
  conditional outer one (`if (cost > 0.0f) break;`), so that a qualifying cell
  exactly on the halo circle priced `frac == 0` and let the scan run on into
  later rows rather than ending it. **That scan-order contract is superseded
  inside this same unreleased cycle by #4848** (see *Fixed* below): both
  engines now price the **nearest** qualifying cell in the band, neither has a
  two-level break, and an on-circle cell is the band's *farthest* member by
  construction rather than a scan-continuation special case. The Python↔C++
  parity tests still pin the *value*, not just the sign — the whole band,
  off-axis distances, board-edge-clipped scan windows, per-net-class widths, a
  multi-cell case proving both engines settle on the same candidate cell, and
  the zero-`frac` cases — but #4848 renamed several of them (and re-pointed
  their assertions) where the narrowed semantics made the old names describe
  behavior that no longer exists.

- **Placement pad-anchoring audit** (`docs/placement-pad-anchoring-audit.md`,
  part of #4831) — a docs-only, symbol-anchored survey of where the placement
  stack measures distance between component **centres** versus real **pad**
  coordinates, prompted by pcbplace's report that pad-anchored constraints cut
  a demo board's wiring 59.5 → 12.7 mm. Every claim cites a `file.py:line` the
  reader can re-check, and the doc ships the two commands that re-check them.
  Headline finding: a pad-anchored HPWL estimator (`compute_hpwl`,
  `src/kicad_tools/placement/wirelength.py:111`) already exists and is tested,
  but **nothing under `src/` calls it** — the optimizer objective
  (`evaluate_placement`, `src/kicad_tools/placement/cost.py:715`) scores
  centre-anchored HPWL instead, and both front-ends decode transformed pads and
  discard them on every evaluation. Includes an inventory of every objective
  term plus the multi-fidelity, seed/prior and declarative-constraint surfaces;
  a first-party measurement showing the two estimators diverge by 0.2-40% on the
  committed benchmark fixtures (with an explicit note that this bounds *ranking*
  differences and is **not** evidence of shorter routed copper); an
  "already pad-anchored" section (the legacy `optim` spring placer is already
  pin-anchored today); a decline list with rationale so declined terms are not
  re-proposed; and a disambiguation of the three unrelated existing meanings of
  "anchor" in this repo (`--anchor-weight`, `--placement-feedback-anchor`,
  `SpatialConstraint.max_distance(anchor=…)`). Five migration candidates are
  listed as stubs **inside the doc** and deliberately not filed as issues.
  No source changes.

- **OmniLayout / OmniRouting recon** (`docs/research/omnilayout-recon.md`, part
  of #4830, slice 3) — the external LLM layout/routing benchmark at
  omnieda.com was downloaded, characterized and **declined as a corpus**;
  verdict: *adapt the protocol, drop the data*. Evidence: the release carries
  **no data licence at all** (no licence text on either site page, none in
  either archive, none in the released scripts; the only stated licence is
  CC BY-NC-ND 4.0 — non-commercial *and* no-derivatives — on the two papers,
  arXiv:2607.03261 / arXiv:2608.04434), and the redistribution does not surface
  the upstream hardware licences of the third-party boards it is derived from.
  The public drop is also 5 hand-picked boards behind Google Drive links, not
  the advertised 1,681 designs, and the payload is **not KiCad** — every record
  is a JSON transcription of an Eagle 9.6.2 `.brd`, so no slice-1/2 tooling
  applies. Three findings that would have bitten a naive harness are recorded
  with counts: 61 of one board's "routing wires" sit on Eagle layer 19
  (*Unrouted* ratsnest stubs, not copper), *no* record carries a schematic
  despite the "schematic-coupled" billing, and two boards declare an inner
  copper layer that carries neither trace nor pour. What is worth keeping is
  the metric vocabulary (DRC-attributed net routability NRR, pad-pair
  routability PRR, open/physical-short/logical-short split, violation split by
  cause) — mapped field-by-field onto `BenchmarkResult` in the doc, and
  implementable against our own boards and the CC-BY-4.0 open-schematics
  manifest with no licence exposure. `scripts/corpus/omnieda_sample.py` (new,
  **offline**, covered by `tests/test_corpus_omnieda.py`) is the instrument
  that produced the numbers and the re-evaluation trigger if the promised
  open-source release ever lands: it reports per-board contents plus a census
  of fields with no `kicad_tools.schema.pcb` counterpart. No third-party bytes
  are fetched, cached or committed.

- **Curated open-schematics sample manifest + parser scorecard** (part of
  #4830, slice 2) — the corpus probe's one-off sample is now a *standing*
  regression set. `scripts/corpus/manifests/open-schematics-sample.json` is a
  committed, deterministic manifest of 30 real-world artifacts (22
  `.kicad_pcb`, 8 `.kicad_sch`) pinned by `sha256` + datasets-server URL,
  spanning 15 declared KiCad file-format versions (`20160815` → `20250114`,
  plus KiCad-4-era `version 3`/`4`) and 24 (kind, version-era, size-decade)
  strata. **No third-party payload bytes are committed** — the corpus stays
  referenced by URL; manifest validation and a test both reject an entry that
  embeds file content. `scripts/corpus/build_manifest.py` rebuilds the
  manifest (seeded scan pool, every candidate parsed before it can be pinned,
  round-robin stratified selection), and `scripts/corpus/check_manifest.py`
  scores this repo's parsers against it: cache-or-fetch each payload into the
  gitignored `scripts/corpus/.cache/payloads/`, verify the hash, parse, and
  report parse failures, **regressions** against each entry's pinned
  `expected_outcome`, and **shape-metric drift** (a file can load cleanly and
  still yield an empty object graph). Integrity outcomes
  (`missing-cached-payload`, `payload-hash-mismatch`) are separate classes,
  excluded from `parse_failure_rate`, so an upstream row change or a flaky
  fetch never reads as a parser regression. Current status: 30/30 entries
  parse clean, 0 regressions, 0 drift; a warm-cache `--offline` re-run takes
  ~8 s. The dataset-access and taxonomy helpers used by all three scripts were
  extracted to `scripts/corpus/hf_hub.py` and
  `scripts/corpus/parse_taxonomy.py` (the latter is pure/offline and covered by
  `tests/test_corpus_manifest.py`; the network scripts remain unimported by
  `tests/` and unreferenced by any workflow).

- **Open-schematics corpus probe** (`scripts/corpus/probe_open_schematics.py`,
  part of #4830) — a local, opt-in dev script that samples N random records
  from the Hugging Face dataset `bshada/open-schematics` (87,931 real KiCad
  designs, CC-BY-4.0), parses each `.kicad_sch`/`.kicad_pcb` payload with this
  repo's own `Schematic.load` / `PCB.load`, and emits a **parser failure
  taxonomy** as both a JSON report and a human-readable summary. Until now
  every parser measurement ran against the 9 in-repo boards, all generated by
  `kct` itself; this gives parser-hardening work a real-world corpus to
  prioritise against. Outcomes are bucketed so each class maps to a different
  action (`sexpr-syntax-error`, `wrong-root-token`, `missing-node-or-atom`,
  `unexpected-value`, … versus non-defect `skipped-*` / `network-error`
  classes), failure messages are grouped by digit-normalised signature, and
  outcomes are cross-tabbed against the declared KiCad file-format version.
  `--seed` makes sampling reproducible (the effective seed is recorded in every
  report); `--dry-run` exercises the same parse/classify/report path against
  in-repo files with no network. Zero new dependencies (stdlib + `requests`,
  deliberately *not* the `datasets` library): it reads the plain Hugging Face
  HTTP endpoints, fetching only the sampled rows. Nothing is vendored — sampled
  payloads go to a system temp dir removed on exit, reports to the gitignored
  `scripts/corpus/.cache/`, and the script is never invoked from CI nor imported
  by `tests/`. Documented in `scripts/corpus/README.md` (invocation, taxonomy,
  CC-BY-4.0 attribution) and indexed in `scripts/README.md`.

- **`--format json` on the 5 board-artifact producers** (part of #4674,
  fifth batch of the #4543 machine-output sweep) — `board-metrics`,
  `create-pcb`, `panel`, `report generate` and `screenshot` now accept the
  canonical `--format json` and emit one deterministic document on stdout
  instead of prose, wired end-to-end (outer parser → shim → inner
  parser/handler). Each document describes the artifact the command
  produced: `board-metrics` wraps a run envelope
  (`mode`/`dry_run`/`boards[]`) around each board's full `board.json` under
  `boards[].metrics` rather than overloading that artifact's own schema
  (text mode still prints the bare artifact); `create-pcb` reports
  `board` geometry, `components_found`, `placement` (with sorted `failed`
  and `warnings`), `nets` and the workflow `summary`; `panel` reports
  `grid`/`board_count`/`tabs`/`cut_method` plus the frame features;
  `report generate` reports `report_path`, `pdf_path`, the `data_source`
  it used and a `figures` block naming why figures were skipped;
  `screenshot` reports `output`, `width_px`/`height_px` and
  `layers_rendered`. Commands that can run without writing say so
  structurally (`saved` / `output_path: null` under `--dry-run`) so exit 0
  cannot be mistaken for a written file. Failure paths emit
  `{"error": ..., "success": false}` documents with exit codes unchanged,
  and text-mode output is unchanged. `report generate` additionally
  diverts stdout to stderr around the collector / figure / PDF stages,
  because WeasyPrint prints a multi-line installation banner **on stdout**
  when its native libraries are missing — which would otherwise corrupt the
  single-document contract. Audit (`scripts/audit_machine_output.py`):
  prose-only 18 → 13, format-json 179 → 184.
  `tests/test_format_json_sweep_artifacts.py` guards the new surfaces;
  `docs/reference/machine-output.md` records the per-command shapes.

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

- **The pad-anchoring audit is re-anchored on symbols and brought under the
  citation guard** (part of #4831, item 1) —
  `docs/placement-pad-anchoring-audit.md` cited source as `file.py:NNN`, the
  form `tests/test_docs_source_citations.py` (#4764) *bans* precisely because
  it rots. It rotted three times in the eight days after it landed: PRs #4857
  (M1), #4863 (M2) and #4870 (M5) each shifted `cost.py`, `wirelength.py`,
  `multi_fidelity.py` and both optimizer front-ends, and each appended a "line
  citations below predate this patch" disclaimer instead of fixing them. By
  2026-08-21, 60+ of the doc's 86 line citations resolved to the wrong symbol —
  the line cited as `evaluate_placement` had drifted into the body of
  `compute_domain_cohesion`. The doc escaped the guard only because its glob
  list covered `docs/guides/*.md` and `docs/reference/*.md` but not top-level
  `docs/*.md`. Every citation is now a **symbol + path** anchor,
  `GUARDED_DOC_GLOBS` gains `docs/*.md` (blast radius: one file — every other
  top-level doc already complied), and 18 `CITED_SYMBOLS` rows pin the audit's
  load-bearing anchors in both directions, so a rename goes red instead of rotting.
  Substantive claims invalidated by M1/M2/M5 are corrected rather than
  disclaimed: the objective is centre-anchored *by default* (pad anchoring is
  opt-in behind `--pad-anchored-wirelength`), only the MCP front-end still
  discards its pads, and `compute_hpwl` still has no call site in `src/`
  because M1/M2 shipped via `build_pad_position_map` instead. Both measurement
  tables (§4 fixtures, §4.1 fleet) were re-run at `8a98a69f` and reproduce
  exactly — including the finding that argues *against* flipping M1's default:
  two of eight fleet boards measure longer at the pads. Docs-only plus the
  guard; no `src/` change.

- **Multi-fidelity level 1 now measures wirelength at the pads it already
  required** (part of #4831, audit milestone M2) — `_evaluate_fidelity_1`
  (`src/kicad_tools/placement/multi_fidelity.py`) was *handed*
  `PlacedComponent` objects with fully transformed pads (fidelity ≥ 1 raises
  `ValueError` without them, because the pad-to-pad DRC check needs them),
  then projected every part down to its centre before calling
  `compute_wirelength` — pure information loss. It now builds
  `build_pad_position_map(placements_rich)` and passes it as `pad_positions`,
  so fidelity 1 (and 2/3, which build on its breakdown) scores pad-anchored
  HPWL. The most visible consequence: **rotation is no longer invisible to the
  wirelength term** — rotating a part leaves `p.x`/`p.y` untouched, so the
  centre-anchored estimate could not see a pad swing from a facing edge to the
  far edge of a package. Fidelity 0 is deliberately unchanged (it accepts
  centre-only placements, so it cannot assume pads exist), which also keeps the
  historical estimate reachable via `FidelityLevel.HPWL`. Only the wirelength
  term moves — overlap, boundary, area and DRC are untouched. **Unconditional,
  unlike M1's opt-in `kct optimize-placement --pad-anchored-wirelength`**: the
  multi-fidelity evaluator is library/test surface (nothing under `src/` calls
  `evaluate_placement_multifidelity`), so a second opt-in switch would have
  bought no safety while leaving the information loss on by default. As in M1
  the pads are routed *through* `compute_wirelength` rather than calling
  `compute_hpwl` directly, so per-net `Net.weight` survives and pins without a
  pad still fall back to their component centre.
  `docs/placement-pad-anchoring-audit.md` records M2 as landed; M3-M5 remain.

- **The diff-pair shadow constructor stays default-OFF — measured, not
  assumed** (#4800, part of #4409) — all five correctness gates the
  `DifferentialPairConfig.enable_shadow_construction` flip was waiting on
  closed on 2026-08-04 (#4579/#4581/#4582/#4604 merged, #4580 a documented
  negative result), so the flip evaluation was finally run. **Verdict:
  the default stays `False`**, and the code comments that carried the stale
  2026-08-02 numbers now carry the fresh ones. Method: two repeats per arm
  on board 06 (`PYTHONHASHSEED=42 … --seed 42`, C++ backend built, main @
  `fa18a25b` — i.e. *after* #4791 and #4840), board quality read with the CI
  gate itself (`scripts/ci/check_diffpair_coverage.py --skip-route`) and
  cross-checked with `kicad-cli pcb drc --refill-zones`; both arms were
  byte-identical across their repeats modulo `(uuid …)`, so these are
  single-mode figures rather than a Mode-A-vs-Mode-B accident (#4536).
  Shadow-ON buys `coupled-ok` 0/9 → 5/9 (stable **by name**: MIPI_CLK,
  PCIE_RX, USB2_D, USB3_TX1, USB3_TX2) and pays for it with signal reach
  21/21 → **18/21**, DRC 18 → **35**, diff-pair violations 18 → 31, a
  **copper-union pour-connectivity failure** (+3V3 in 3 disjoint groups,
  GND in 5 — a blocker not on the previous list), `kicad-cli` clearance
  errors 11 → 169–176 with 0 → **9 unconnected items**, and **2.16x**
  wall-clock. The `diffpair-routing-regression` gate exits 0 shadow-OFF and
  **2 shadow-ON, on four separate assertions**. Both previously-named
  blockers are re-verified rather than refuted: the corridor-yield planner
  still reports `USB_CC1 stays unroutable with every coupled corridor
  lifted`, and its MIPI_D0 recovery now *reverts* (`reach 18 -> 18 of 21`),
  so shadow-ON has **regressed** from the 19/21 recorded under #4463; on
  wall-clock the CI margin has shrunk, with ten consecutive green `main`
  runs putting the job at 17.9–25.2 min (median 24.5, re-route step 23m30s)
  against its 30-minute ceiling. The decline-count baseline is re-confirmed
  at **46** (the board-06 README's `47` was pre-#4604). Comments/docs only —
  no behaviour, defaults or artifacts change.

- **The pure-Python router's pairwise (HV-isolation) widening bitmap is now
  cached across `route()` calls** (#4794) —
  `Router._pairwise_expanded_blocked` (the search-time bitmap the pure-Python
  fallback A* ORs into its hot-loop mask) recomputed a **full-grid `np.isin`
  plus a full-grid `binary_dilation` per non-dormant foreign domain, on every
  single `route()` call**, even when nothing about the foreign copper had
  changed since the previous call. The per-domain dilated masks are now
  memoized per `(foreign_dom, pair_r)`. The routed net's own class width
  needs no separate key component because it is already folded into `pair_r`
  (post-#4793 the width is resolved per net class), so two classes that widen
  to the same cell radius share a byte-identical mask and two that do not
  land on different keys. The #4506 attach-zone waiver is re-applied per call
  on a copy, never baked into a cached entry, and a result that would alias a
  cache entry is copied before it is returned, so no caller can mutate cached
  state. C++-backed routing is unaffected — the C++ kernels walk a bounded
  annulus per query and never materialize a whole-grid bitmap, so there is
  nothing to cache there.

  **Invalidation** uses a new global monotonic counter,
  `RoutingGrid.occupancy_generation` (public, with
  `RoutingGrid.bump_occupancy_generation()` for code that writes the planes
  directly): every write to `grid._blocked` / `grid._net` anywhere advances
  it, and a cache entry is discarded when the counter, the grid object, or
  the pairwise projection has changed since it was computed. This is the
  deliberately conservative option — any write anywhere invalidates every
  entry, mirroring the safety property the existing `_via_cache` already
  relies on — because a stale mask means the search treats committed foreign
  copper as absent, which is strictly worse than the recomputation the cache
  removes. Completeness is enforced mechanically:
  `tests/router/test_grid_occupancy_generation.py` AST-scans all of
  `src/kicad_tools` for assignments into `_blocked`/`_net` (plus `np.copyto`
  into them) and fails when any enclosing function does not bump. That scan
  found two write sites outside `grid.py` that this change also fixes —
  `Autorouter.route_all_negotiated`'s thread-safe grid rebuild and
  `route_all_multi_resolution`'s trial-ordering rollback, both of which
  replace the occupancy planes wholesale.

  Measured on a 2-domain (150 V vs 0 V) 40×30 mm board at 0.1 mm with the
  pure-Python fallback, 24 `_pairwise_expanded_blocked` calls (full-grid
  dilation count is the deterministic figure; wall clock is the noisier
  one): a burst of `route()` attempts with no intervening commit (retries,
  `route_bidirectional`, negotiated iterations) goes **144 → 6 dilations,
  1.8 s → 0.07 s (~25×)**; a mixed session committing copper every fourth
  call goes **144 → 42 dilations, ~1.9 s → 0.7-1.5 s (1.4-2.9×)**. A session
  that commits after *every* call sees no benefit (144 → 138) by design —
  that is the known ceiling of a global counter, and the per-domain counters
  that would lift it are deliberately left as a follow-up rather than shipped
  with a silent-staleness risk.

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

- **Board 05 (`05-bldc-motor-controller`) silkscreen and inner-layer PTH
  clearance repair** (#5204, Part of #5059) — the released jlcpcb-tier1
  artifact violated two of the object-specific factory floors added for
  #5059. (1) `R4`'s reference designator sat 0.0548 mm from `R1`'s pad-2
  mask aperture, under the 0.15 mm silk-to-pad floor; `redesign/routing.py`
  now mirrors the standard 0805 offset **below** the part (native KiCad
  10.0.6 measures 0.8631 mm, and the tightest remaining silk-to-pad pair on
  the board is a library-intrinsic 0.2073 mm). (2) 28 through-holes — U2's
  24 and U3's 4 exposed-pad thermal vias — sat 0.2521 mm from the `+5V`
  `In2.Cu` pour, under the 0.30 mm inner PTH-hole-to-copper floor. That gap
  is the systemic `0.15 mm` minimum annular ring + `0.1016 mm` zone
  clearance sum and needed **no layout change**: KiCad's zone filler honours
  the emitted `Inner PTH Hole to Copper` `hole_clearance` rule, so a native
  refill against the current `.kicad_dru` carves the pour to 0.3005 mm. The
  committed release artifacts (routed PCB, `.kicad_dru`, gerbers, drawings,
  renders, manifest and readiness evidence) are regenerated through the
  recipe with native refill; the pad/placement/net fingerprint, track count
  and all 8114 copper segments are unchanged. `kct check --mfr
  jlcpcb-tier1 --errors-only` goes 28 blocking errors → 0 and native
  `kicad-cli pcb drc --refill-zones` goes 1 violation → 0. Carried along:
  assembly export now preserves configured rotation corrections for
  library-qualified footprint names, including explicit zero overrides.
  The CPL retains profile corrections D1/D2/D3=180 and U1=270 degrees;
  these express profile consistency, not supplier orientation validation. The
  board's readiness evidence — stale since `redesign/build.py` changed
  without a refresh — validates as `ready` again. This is a DRC/DFM repair,
  not new physical qualification: motor and thermal bench validation remain
  untested.
- **`clearance_pad_segment` reported false-positive DRC violations against
  rotated `roundrect`/`oval` pads' rounded corners** (#4985) — the
  segment-vs-pad clearance path (`_segment_circle_clearance` in
  `validate/rules/clearance.py`) still measured distance to the pad's
  axis-aligned bounding box even after #3826 fixed the analogous pad-vs-pad
  and pad-vs-zone over-approximation. A rotated `roundrect`/`oval` pad's true
  copper cuts back the AABB's corners, so a trace routed near a corner could
  be reported tighter (even below the manufacturing clearance floor) than
  the true rounded geometry allows — reproduced on `chorus-test-revA`'s C19
  footprint, where the AABB path reported 0.0812 mm against a real 0.1846 mm
  (per `pcbnew.PAD.GetEffectivePolygon`), a false violation at the board's
  0.1016 mm floor. `_segment_circle_clearance` now routes `roundrect` and
  non-square `oval`/`obround` pads through the same true-geometry shapely
  polygon (`CopperElement.polygon`, from `_pad_polygon`) already used for
  pad-pad/pad-zone clearance; plain `rect` pads and circular
  pads/vias are unaffected. A genuine sub-clearance violation against the
  true rounded geometry still fires — the fix narrows false positives
  without masking real shorts.
- **Noncardinal pad clearance geometry** (#5227) — orient rect, roundrect,
  and oval copper polygons using KiCad's negative-angle board transform.
  This removes mirrored false overlaps and missed physical overlaps while
  preserving absolute pad angles, footprint-local centers, and clearance floors.
- Grid routing acceptance now retains non-cardinal pad rotation in Python
  segment/via backstops and the native validator, including late pad additions
  (#5182). Native candidate vias now check foreign pad copper using the same
  component clearance and exclusions as finalization. Requires native build v22.


- **Declared current paths reported `unresolved` on real boards whenever a
  trace did not land on the exact pad center** (#4980) — endpoint resolution
  (`router/current_paths.py`) attached a `RefDes.pad` endpoint to the copper
  graph only by an exact pad-center node match. A router may legitimately
  terminate a trace anywhere inside a pad's copper, and a wide power pad is
  routinely entered by several stubs at once (all shorted by the pad itself),
  so ordinary boards failed to resolve: on board09 the `+5V_OUT` force path
  enters the 2.29 × 2.03 mm shunt pad `RSH1.4` through three stubs 0.015,
  0.785 and 0.815 mm off center, and every declaration on the net reported
  "source pad has no routed copper touching it". Endpoints now bind by the
  pad's real extent (exact rectangle/ellipse test in the pad's own rotated
  frame), and all in-pad nodes are shorted through the pad. This is a *false*
  fail-closed being removed, not a relaxation — copper outside the pad extent
  still never attaches, so genuinely moved/removed pads still fail closed.
- **`kct route` left in-pad vias on boards whose fab tier has no orderable
  via-in-pad process** (#5009) — the router's same-net via-in-pad repair
  sweep (`router/drc_nudge.py::_scan_and_repair_via_in_pad`, #3112) no-oped
  whenever `MfrLimits.via_in_pad_supported` was `True`, so a 2-layer board
  routed at `jlcpcb-tier1` kept escape vias drilled into SMT lands even
  though JLCPCB's POFV process requires 4+ layers and cannot actually build
  them. The sweep is now gated on process *eligibility* (the same predicate
  the `via_in_pad` DRC rule applies) rather than the bare capability flag,
  and its detector was corrected from full drill *containment* to the
  drill/land *overlap* test the DRC rule uses — a drill that merely clips a
  land edge is still a via-in-pad defect, and board 02's offending vias
  were of exactly that shape, so the old predicate matched none of them.
  The relocation itself is validated against the rest of the board before
  it is committed: a candidate exit that would put the via inside another
  land, within copper clearance of a foreign-net pad/via/track, or closer
  than the fab hole-to-hole floor to any other drill is rejected, and the
  nearest *surviving* candidate (cardinal, edge-slide or 45° corner escape)
  is taken instead — with the move refused outright, and the original
  `via_in_pad` finding left for DRC, when no legal destination exists.
  Without that gate the exit was chosen purely to minimise displacement
  from the offending pad and committed unchecked, which on board 02 put a
  relocated via 0.073 mm from a foreign-net track (0.127 mm required) and
  cost the board a net (11-of-12 reach, 2 `kicad-cli pcb drc` errors); an
  0805 land pair likewise put the exit 0.035 mm from its neighbouring land
  with 0.185 mm hole-to-hole against a 0.250 mm floor. The sweep also
  visits vias in a canonical `(net, x, y)` order rather than
  negotiated-routing order, so the relocations (each of which becomes an
  obstacle for the next) are a pure function of the pre-nudge geometry
  rather than of `--seed`. Board 02 (`charlieplex_3x3`) routes 0-error at
  `jlcpcb-tier1` with unchanged 22 routes / 24 vias / 8-of-8 reach (total
  length re-baselined 327.93 → 329.23 mm for the relocated vias), and its
  full recipe regeneration at the plain `jlcpcb` profile is clean under
  the native `kicad-cli pcb drc` gate: 12/12 nets, 0 violations, 0
  unconnected items, label- and copper-LVS PASS.
- **The escape router's opportunistic in-pad via placement still read only
  the bare `MfrLimits.via_in_pad_supported` capability boolean, out of step
  with the DRC-side process-eligibility model #5009 landed** (#5201) — the
  auto-fix repair sweep (`drc_nudge.py::_scan_and_repair_via_in_pad`)
  already resolved the board's actual layer count against the selected
  fabrication process, but `EscapeRouter` (`router/escape.py`) still
  routed a 2-layer `jlcpcb-tier1` board exactly like a 4-layer one and
  deliberately drilled vias dead-centre into SMT lands that tier's POFV
  process cannot legalize below 4 layers — the router placed the very
  copper the next `kct check` pass would reject. Both decision points now
  share `kicad_tools.router.via_in_pad_eligibility`
  (`resolve_process`/`via_geometry_eligible`), which delegates to the same
  `FabricationProcess.eligibility_reasons` predicate
  `ViaInPadRule` applies, so router and validator can never disagree about
  which vias are legal: `resolve_process` fails closed on any unresolved
  manufacturer, profile, or board layer count (never silently eligible),
  and `via_geometry_eligible` additionally checks each *specific* candidate
  via's drill/annular-ring/component-hole geometry against the resolved
  process's published envelope — board-level eligibility no longer
  certifies every via that happens to sit on a pad. When no eligible
  process is selected the escape router now falls back to its pre-existing
  non-in-pad strategy for that pad/pitch (the pin stays deferred to the
  main router) instead of placing an unmanufacturable via, and the repair
  sweep individually re-validates every DETECTED in-pad via against the
  same resolved process rather than trusting a board-level "process
  exists" gate alone. Board 02 (`charlieplex_3x3`, 2-layer, `jlcpcb-tier1`)
  stays DRC-clean with its `MAX_DRC_ERRORS` ceiling unchanged at 0 and no
  new grandfathered allowance; boards 03/04 (both 4-layer) and the
  fine-pitch SSOP/TSSOP in-pad escape regression fixtures (#2605) are
  unaffected.
- **Router emitted partial-stack via spans for ordinary multilayer
  transitions, without an HDI process ever being selected** (#5013,
  router counterpart of stitch issue #5001) — `CppPathfinder._convert_result_to_route`
  and `Router._convert_path_to_route` (the C++ and Python A* pathfinders)
  built a via's reported `layers` span directly from the LOGICAL
  current/next search layer (e.g. `F.Cu`/`In2.Cu` on a 4-layer board)
  instead of the via's physical drilled extent. No blind/buried via
  process is ever selected by either pathfinder today (Issue #4007:
  `blind_buried_supported` is False for every board — `hdi_4layer` via
  rules are defined but never instantiated), so every via either
  constructs is manufactured as an ordinary through-hole whose barrel
  spans the full copper stack; under-reporting the span let DRC and
  connectivity code (which trust `via.layers` as the physical barrel
  extent — `validate/connectivity.py`, `validate/rules/clearance.py`,
  `core/layers.py::via_spans_layer`) silently skip barrel-vs-foreign-
  copper clearance checks on layers the via actually passes through but
  did not name as an endpoint. The grid-side obstacle search already
  treated every via as full-stack (`Grid3D::mark_via` /
  `Pathfinder::is_via_blocked_diag` in C++; `RoutingGrid._mark_via` in
  Python both block/check ALL layers unconditionally), so no route is
  newly accepted against copper this fix "discovers" — only the
  *reported* span was wrong. Both pathfinders, plus the shared
  `Route.validate_layer_transitions` missing-via safety net, now
  normalize an ordinary (non-micro) via's span to the full physical
  stack at `Route` construction — upstream of acceptance and export —
  so every downstream consumer of `route.vias` sees the correct span,
  not just the final saved `.kicad_pcb`. Explicit micro-vias are
  untouched. Regression coverage spans both backends, an inner-to-inner
  (`In1.Cu` -> `In2.Cu`) transition, the `validate_layer_transitions`
  fallback, and `via_spans_layer`'s foreign-copper-on-an-intermediate-
  layer visibility.
- **Fine-pitch same-component carve-out silently bypassed authored pad
  clearance** (#5004) — the router's same-component clearance carve-out
  (`RoutingGrid._same_component_carveout_active` /
  `CppPathfinder._same_component_carveout_eligible`) used to exempt a
  FOREIGN-net pad from clearance checks purely because its component's pin
  pitch was below `fine_pitch_threshold` — even when `fine_pitch_clearance`
  was left unset (the default) and no per-component relaxation was actually
  configured. On board07 (STM32F429 + SDRAM, 0.5mm-pitch LQFP144, 0.15mm
  authored clearance) this silently accepted 46 clearance defects (30
  against NC pads, 16 against named signal pads) that the router's own
  `clearance_viol=0` metric never surfaced, while a fresh native KiCad DRC
  on the same routed project reported all 46. The pitch-only branch is now
  gated behind a new opt-in `DesignRules.legacy_fine_pitch_carveout` flag
  (default `False`): with it left unset, the carve-out only activates where
  a relaxation was actually configured or applied for the component — an
  explicit `component_clearances` override, a net-class `escape_clearance`
  override, an applied `fine_pitch_clearance` shrink (narrow-channel guard
  permitting), or a corridor already relaxed by
  `_relax_same_component_clearance` (Issue #2452). Configured relaxations
  retain their existing exclusion behavior; enforcing numerical per-ref
  floors is tracked separately in #5166.
  The gate is shared by the Python search-time validator (`grid.py`), the
  C++ pathfinder's post-route acceptance check (`cpp_backend.py`, which
  builds the `exclude_ref_hashes` list the C++ `Grid3D::validate_route`
  carve-out consumes), and the `validate_routes()` / `drc_nudge`
  "component-inherent" classification (`io.py`) so those paths use the
  same opt-in policy. `CacheKey`/`SubProblemSignature` now key on the new flag so a cache
  entry produced under one setting is never served to a run under the
  other. Pad seeds retain trace-radius clearance, and negotiated routing
  preserves complete physical tree connectivity and best-state geometry.
  Board04 recognizes reviewed oscillator escape variants. Board06 can find
  bounded pour/via escapes and restores impedance-sized connector widths
  beyond a cumulative 0.75 mm pad neck-down. Finalization rolls back if
  refill breaks pour connectivity or introduces a clearance violation.
- **Declared current-path resolution reported `ambiguous` for an entire net
  whenever a benign parallel via array was reachable from an endpoint**
  (#5197) — `_component_has_cycle` (`router/current_paths.py`) flagged any
  cycle reachable from a declared endpoint, including the standard
  high-current practice of splitting a trunk across several parallel vias
  that immediately recombine. On board09, `+5V_OUT`'s shunt pad `RSH1.4`
  fans into three vias reunited by a wide `B.Cu` trace, and this one benign
  array made *every* declaration on the net report ambiguous, including the
  low-current LED and INA226-supply taps that never touch it. Cycle
  detection now recognizes a hub whose legs are ALL via crossings, whose far
  ends mutually tie back together, and contracts exactly that array before
  checking for a genuine loop — a route that merely changes layer once, or
  a hub whose legs leave a residual route uncontracted, still reports
  ambiguous, matching the existing parallel-return-path regression tests.
- **`kct route` accepted KiCad 10 name-only nets but wrote zero copper and
  reported a vacuous "SUCCESS" (0/0 nets)** (#4983) — a PCB saved in KiCad
  10's name-only net syntax (`(net "SIGNAL")` on pads, no numeric net table
  entries at all) made the router's pad-extraction paths
  (`load_pcb_for_routing`, `load_pads_for_analysis` in `router/io.py`)
  resolve every name-only pad's net to `net_num=0` — the "no net" obstacle
  sentinel — because their name-to-id map was built only from the
  numeric-plus-name dialect's top-level `(net N "NAME")` table, which a
  name-only board may not have at all. `--nets <NAME>` preflight is
  schema-level and dialect-aware so it still accepted the request, but the
  routing graph never received a bound pad pair for that net, and `kct
  route` reported `Nets routed: 0/0` / "SUCCESS: All signal nets routed!"
  and exited 0 while writing an unrouted output. A new shared
  `_build_net_number_map()` normalizes both dialects (numeric-plus-name and
  name-only, including synthesizing stable ids for name-only nets with no
  header table) before the routing graph, net-class auto-classification, and
  output connectivity verification are built, so a name-only board routes
  identically to its numeric-dialect equivalent. A `--nets`-specific guard
  (`_reject_lost_route_only_bindings`) now aborts with a non-zero exit
  instead of reporting vacuous success if a requested net's pad bindings are
  ever lost after preflight already confirmed the net exists with 2+ pads.
- **`check_silk_overlap` falsely flagged legitimate footprint-outline
  corner joins as `silk_overlap` violations** (#4987) — the same-footprint
  shared-endpoint exemption matched candidate corners by **exact
  floating-point tuple equality**, which silently stopped covering
  real-world footprint outlines whose nominally-shared endpoints differ by
  a few ULPs (independent rounding at export time). Reproduced against
  chorus v25 (75 of 79 reported warnings were intentional joins, not real
  collisions). The exemption now matches shared endpoints by **distance
  within `_CLEARANCE_EPSILON_MM`** (0.1 micron, the same tolerance already
  used by `check_silk_edge_clearance`), and duplicate-line pairs that
  coincide at *both* endpoints still flag as a real overlap. `silk_overlap`
  violation items now also carry each graphic's own UUID
  (`"U1 (fp_line) {<uuid>}"`) so same-footprint sibling strokes are
  individually addressable for a per-element waiver. The four unrelated
  reference-bounding-box false positives noted in the issue are a separate,
  already-documented AABB-approximation limitation and are out of scope
  here.
- **`PCBEditor.add_zone` silently bound an unknown net to net 0 instead of
  rejecting it, and both `PCBEditor.add_zone` / `ZoneGenerator.add_zone`
  accepted arbitrary/disabled layer strings** (#4907) — `PCBEditor.add_zone`
  resolved `net_name` through `get_net_number`'s `dict.get(name, 0)`
  fallback, so a typo'd or nonexistent net silently produced a zone bound to
  net 0 (`net_name` still recorded the wrong string) rather than an error.
  Both `PCBEditor.add_zone` and `ZoneGenerator.add_zone` (used by
  `kct pcb add-zone` and `kct zones add`) also passed `layer` through
  unchecked, so misspelled layers (`"DefinitelyNotALayer"`), real
  non-copper layers (`"F.SilkS"`), and inner copper layers not declared on
  the specific board (`"In1.Cu"` on a 2-layer board) were written straight
  into the zone node. Both writers now validate the net against the board's
  declared net table and the layer against a new shared
  `core.layers.validate_copper_layer` helper (canonical spelling via the
  existing `COPPER_LAYER_ORDER`, plus board-declared/enabled-copper-layer
  membership) *before* any document mutation, so rejected calls leave the
  PCB untouched; both CLI routes surface the resulting `ValueError` as a
  nonzero exit with an actionable message in text and JSON modes. Valid
  known nets and F.Cu/B.Cu/declared-inner-layer zones are unaffected.
- **`--strict-layers` was silently inert on the lattice engine, which shipped
  copper onto explicitly forbidden layers** (#4979) — `avoid_layers` is
  promoted to a HARD no-go set by
  `NetClassRouting.hard_avoided_layer_indices()` (`--strict-layers`, or
  unconditionally for a class declaring `target_ampacity`), and both grid
  backends have honoured that during search since #4433. The lattice engine —
  the `--complete` default — had no `avoid_layers` plumbing at all, so a
  4-layer softstart rev-C completion pass committed 29 × 2.6 mm `/PGND`
  segments plus 8 thin ones onto the `In2.Cu` reference plane the routing map
  forbade, and reported no layer-intent failure anywhere: not in the banner,
  not in the exit code, not in the completion report. The lattice A* now
  filters hard-avoided layers out of its pad-escape seeds/goals and its
  via-hop landing layers, so no state it visits can carry a forbidden layer;
  a connection with no legal route DECLINES with a layer-attributed reason
  (`layer-constrained-start`/`-end`, `no-path-layer-constrained`) instead of
  shipping forbidden copper as partial progress. A via may still *step over*
  a forbidden layer to reach an allowed one — the emitted through via's
  barrel is DRC's antipad concern and commits no copper there — which keeps
  an F/B-only net able to cross F↔B on a 4-layer board exactly as the grid
  backend already does. Blocking an **outer** layer is the opposite case and
  drops vias from the search entirely: the lattice emits every layer change
  as a through via spanning the whole stack, so its annular ring is real
  copper on both outer layers, and such a net now routes planar or declines
  rather than shipping an annulus onto a layer it forbade (the grid backend
  needs no equivalent — its via spans only the two layers it hops between).
  A `--complete` residual carries a machine-readable `layer_constrained`
  flag in `--complete-report` JSON (and a named line in the printed report),
  so a consumer can tell a deliberate layer-intent refusal from congestion
  without string-matching the reason.
- **No gate caught forbidden-layer copper after the fact** (#4979) — a new
  post-route, engine-agnostic audit (`router/layer_intent.py`) checks the
  copper the output board actually carries against each net's hard layer
  intent, distinguishing violations this run CREATED from ones it INHERITED
  from its input under `--preserve-existing` / `--complete`. Newly created
  ones replace the SUCCESS banner and exit 3 (or 4 below
  `--min-completion`) — checked before the `--complete` exit-8 branch, since
  forbidden copper outranks an unclosed link; inherited ones are reported as
  a NOTE and leave the exit code alone. Wired into `main()` and all three
  escalation wrapper flows (`--auto-layers` is on by default, so gating only
  `main()` would leave it dead on the default path). A strict no-op when no
  net class carries a hard layer constraint.
- **Matrix layer preferences could point a net at a hard-blocked layer**
  (#4979) — `_inject_matrix_layer_preferences` (#2432) already preserved
  `avoid_layers` through its `dataclasses.replace`, so the hard block itself
  was never lost, but the injected `preferred_layers` could still name a
  layer the class forbids. Assignments are now filtered against the net's
  hard-avoided set, and a net whose entire assignment is hard-blocked keeps
  its authored class untouched.
- **`PCB.remove_segments()` silently left copper behind on boards with a
  non-zero `board_origin`** (#4933) — the coordinate-fallback match (for
  segments/vias with no UUID) rebuilt an in-memory removal key without
  re-adding the `board_origin` offset, so it never matched anything once
  `board_origin != (0, 0)`; separately, the offset-correct key on both the
  tree and in-memory sides compared coordinates for exact float equality,
  and the `board_origin` subtract-then-add round trip is not always
  bit-exact, silently dropping a further slice of matches at the old
  1-micron rounding. Found via a real-world fetched board (BeagleConnect
  Freedom, part of #4933's benchmark harness) where the bug's combined
  effect left ~15% of a 2291-segment board's copper un-stripped after
  "rip-up". Both the offset and a tighter, purpose-built
  `_COORD_MATCH_QUANT` (0.1 micron, distinct from the existing 1-micron
  `_DEDUP_QUANT` used for copper-pour deduplication) are now applied
  consistently everywhere a coordinate key is built, verified against all
  three of #4933's real fetched boards.
- **`load_pads_for_analysis` returned anonymous pads on every KiCad 7+ board,
  silently disabling fine-pitch escape planning** (#4872) — the grid-analysis
  pad loader (`router/io.py`) read the footprint reference only from the legacy
  `(fp_text reference U1 …)` spelling. KiCad 7 and later write it exclusively as
  `(property "Reference" "U1" …)`, so on any modern board — including this
  repo's own pcbnew-resaved fixtures — every returned pad carried `ref=""`.
  Positions and sizes were correct, so nothing crashed and nothing warned; the
  pads were simply anonymous. That is the worst failure shape for the consumers
  that group by reference: `identify_fine_pitch_components` skips ref-less pads
  outright, so `compute_multi_resolution_plan` found **zero** fine-pitch
  components and adaptive/multi-resolution **fine-pitch escape routing never
  fired** on a modern-format board, degrading to plain coarse-grid routing with
  no diagnostic. Both routing engines reach this loader through their primary
  construction entry points (`LatticePathfinder.from_board`,
  `MeshPathfinder.from_board`). The reference is now read with the same
  two-step precedence `load_pcb_for_routing` has always used elsewhere in the
  same file — `property "Reference"` first, `fp_text reference` as the legacy
  fallback — so a transitional file carrying both resolves to the property
  value. Measured on the committed
  `boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb`: 34 pads
  collapsed into a single `""` bucket before, now 14 distinct references
  matching `kicad_tools.schema.PCB` exactly. Legacy `fp_text` boards (quoted and
  unquoted) are unaffected — a one-branch parse fix, no geometry or transform
  behaviour touched.

- **Imported boards were gated against our default manufacturer profile
  instead of their own declared rules** (#4875) — `kct route` fed the #3911
  grid-safety gate an `args.clearance` that was *always* either an explicit
  `--clearance` or the flat 0.15 mm default: `--manufacturer` never touched
  it, and a board's own design rules were not parsed anywhere. An externally
  authored board that declares 0.254 mm clearance was therefore refused for
  failing a 0.075 mm grid requirement it never claimed — the tool substituted
  its house rules for the board's, which also biases any route-vs-human
  benchmark (found during the #4830 corpus pilots, PR #4869). Three changes:
  `schema/pcb.py` now parses the **legacy top-level `(net_class …)` blocks**
  pre-KiCad-6 boards carry (siblings of `(setup …)`, so `_parse_setup` never
  saw them) and exposes them as `PCB.net_classes`; `router/mfr_limits.py`
  gains `resolve_clearance(manufacturer)` alongside #4568's
  `resolve_edge_clearance` and #4700's `resolve_min_trace_width`; and
  `route_cmd` resolves the clearance **before** the auto-grid block (rather
  than beside the #4568/#4700 auto-fills, which run after grid selection and
  would be too late for the gate) with the precedence *explicit `--clearance`
  > explicit `--manufacturer` > board-declared `net_class` clearance >
  0.15 mm*, printing one `rules: …` provenance banner naming the source. Two
  decisions were resolved conservatively: only an **actually explicit**
  `--manufacturer` counts as an operator override (a default `jlcpcb` does
  not, so no existing invocation changes clearance), and a declared clearance
  **below the active profile's `min_clearance`** is raised to that floor with
  a stderr warning naming the `--clearance` opt-out, rather than routing
  copper no configured fab can etch. Derivation is gated on the board
  actually carrying a `net_class` block — a token this repo's writer never
  emits — so the demo-board fleet is structurally unaffected
  (`tests/test_grid_safety_gate_fleet.py` now enforces that directly). Only
  pre-KiCad-6 boards benefit: KiCad 6+ keeps net classes in the companion
  `.kicad_pro`, so a modern single-file import still falls back to the
  profile exactly as before. Also fixes the outer `kct route` argv
  forwarding, which dropped a deliberate `--clearance 0.15` /
  `--manufacturer jlcpcb` because it forwarded flags only when their value
  differed from the default.

- **Legacy `(module …)` boards parsed to zero footprints and zero pads —
  silently** (#4873) — KiCad 6 renamed the component block from `(module …)` to
  `(footprint …)`, and `PCB._parse()`'s dispatch chain matched the modern
  spelling only, with no `else` arm. A pre-KiCad-6 board therefore parsed its
  nets, segments, vias and zones fine while its **entire component graph
  vanished**, with no error, no warning and no version signal — a data-shaped
  wrong answer that every downstream consumer (routing, DRC, LVS, BOM) would
  read as "this board has no parts." `module` is now accepted as the legacy
  spelling of `footprint` throughout `schema/pcb.py`: not just the parse
  dispatch, but every tree walk that keys on the tag
  (`_link_footprint_sexp_nodes`, `_translate_footprint_sexp`,
  `_find_footprint_sexp`, reference/value updates, silkscreen text edits, net
  insertion), so a module-sourced board is **editable**, not merely readable —
  position sync, rename, remove, translate and save/reload all round-trip. No
  sub-form translation was needed: `pad` / `at` / `layer` / `fp_text` / `attr`
  are spelled identically on both sides of the rename. Measured on the pinned
  22-board open-schematics corpus census
  (`scripts/corpus/benchmark_readiness.py`): the `legacy-module-schema` and
  `no-pad-graph` blockers drop **9 → 0**, featurizable boards rise **10 → 18**
  and route-vs-human benchmark cases **8 → 16** — the token rename alone was
  costing 41% of the sample. The class of failure is now loud in general: a
  board that carries `(pad …)` nodes but yields no footprint logs a warning
  naming the declared version and the unreadable container tag, and records it
  in the new `PCB.parse_warnings`, so a *future* unknown component container
  cannot silently return an empty component graph either. The sibling legacy
  `gr_arc` centre+angle defect (#4874) is deliberately not in this change.

- **`route --voltage-map` collapsed signed net potentials with `abs()`, so
  +150 V vs −150 V differenced to 0 V** (#4867, blocker for #4507, epic #4431
  Phase 2) — the router normalised its voltage map to *magnitudes* before
  differencing (`build_pairwise_clearance_table`, and again in `route_cmd`'s
  preload, which additionally read the sidecar through placement's
  magnitude-only `load_voltage_map`). The creepage census reads the **same
  file** signed, so on a bipolar map — the normal encoding for an HV bank
  topology, and the one softstart rev-C declares in its own `_comment` — the
  two disagreed by construction: the census requires 3.20 mm between a +150 V
  and a −150 V net, the router required nothing at all. Such a pair was not
  merely under-required but **absent from the matrix**, therefore invisible to
  the search-time kernels *and* to the post-route audit, which can only report
  on pairs its own table contains. On softstart rev-C's 84-net map that
  under-constrained **339 of 1922 cross-pairs (17.6 %)**, 99 of them missing
  entirely, while the run printed a reassuring `HV pairwise clearance: 84
  mapped nets, 1823 cross-pairs` banner and exited 0 over ±300 V copper at the
  DRU floor. Potentials are now differenced **as supplied**, via a new
  `router.pairwise_clearance.load_signed_voltage_map` that shares the census'
  parse contract (`_`-prefixed metadata keys skipped; a #4411 `{min,max}` range
  collapses to its worst-case endpoint *with the sign*). The banner's
  cross-pair count is now the signed count. All-non-negative maps are
  unaffected — `abs()` was the identity there — and a signed span below
  `--hv-threshold` (e.g. ±10 V) still stays out of the matrix, so there is no
  over-correction. Python-only: the C++ grid has no concept of a net's voltage
  and consumes the already-built dense mm matrix, so the corrected requirements
  reach `Grid3D::set_pairwise_domains` with no recompile. Placement's
  magnitude-only `load_voltage_map` is deliberately untouched (its cross-domain
  model has no reference potential). Per the issue's own acceptance criteria,
  softstart rev-C's leak set is expected to **grow** before it shrinks — 99
  pairs re-enter the matrix — and #4507's T4 criterion must be re-scored on top
  of this change.

- **`kct build --quiet` exited 0 when a build step failed** (part of #4674) —
  the success/failure verdict was computed *inside* the `if not args.quiet:`
  summary block, so suppressing the summary also suppressed the non-zero exit
  code and a failed quiet build looked green to CI. The verdict is now hoisted
  out of the guard; prose output is unchanged.

- **The pairwise HV-avoidance gradient is priced from the *nearest* qualifying
  band cell, not the first one in scan order** (#4848, epic #4431 Phase 2b) —
  `Pathfinder::pairwise_avoidance_cost` and its Python mirror
  `Router._pairwise_avoidance_cost` both documented a linear decay "strongest
  just outside the hard radius, zero at the band edge", but both broke at the
  first qualifying cell in **row-major** order, which starts at the *topmost*
  row of the scan window rather than the closest cell. A candidate with foreign
  LV copper 18 cells below (the binding side, `frac = 7/8`) and 24 cells above
  (`frac = 1/8`) priced **0.125 instead of 0.875** — a 7x under-price of the
  real proximity, making the HV↔LV margin nudge effectively random with respect
  to *which* cross-domain copper was actually close. The C++ kernel now walks a
  distance-sorted band-offset table built once per `(hard_r, band)`
  (`Pathfinder::pairwise_band_offsets`, the ring-ordered analogue of
  `via_kernel_offsets_`), each entry carrying its pre-computed decay `frac`, so
  the first qualifying cell *is* the nearest and the load-bearing early exit
  survives — while visiting only the annulus instead of its bounding square
  (2.3x cheaper per call: 1.945 → 0.845 µs over 198k probes on the HV fixture).
  The Python mirror replaces the two-level-break replay with a vectorised `min`
  over the band's squared distances (flat at ~15 µs/call). An on-circle cell
  (`frac == 0`) can no longer swallow the price by construction — it is the
  band's farthest member, so it wins only when nothing nearer qualifies, which
  is exactly when `0.0` is correct — retiring the conditional outer break
  #4849 had to mirror (that entry, above, is amended accordingly). **Route
  output changes**: on the two-domain HV fixture the corrected gradient buys
  +0.163 mm of edge gap (2.000 → 2.163 mm against a 1.6 mm requirement) for
  +5.5 mm of length, and a seeded randomized parity sweep pins both engines
  against an independent nearest-cell oracle. **Consumer-visible:**
  `ROUTER_CPP_BUILD_VERSION` is bumped **19 → 20** (mirrored in
  `cpp_backend.py`) because a stale `.so` would price a *different* gradient
  than the Python fallback. The binding surface is unchanged, but any checkout
  carrying a version-19 extension silently falls back to the pure-Python router
  (10-100x slower) until `uv run kct build-native` is re-run.

- **The board-06 strict-gate test fixture no longer shells out through a nested
  `uv run`** (#4853) — `TestBoard06StrictGateGuard`'s class-scoped
  `strict_gate_result` fixture ran `uv run kct check …` from inside a test.
  `uv run` re-syncs the project environment before exec'ing, and CI installs
  with `uv sync --frozen --extra dev` while the nested call resolves the
  *default* extras — so the inner invocation churned dev packages out from
  under the running `pytest -n auto` session, at an unbounded and partly
  network-dependent cost. That variance pushed the fixture past CI's
  `--timeout=60` reaper, erroring all three tests (the class scope charges the
  whole subprocess to the first test's setup) and turning `main` and every open
  PR red on a signature attributable to no diff. The fixture now invokes
  `[sys.executable, "-m", "kicad_tools.cli", "check", …]` — the same command in
  the already-resolved interpreter, with no resolve/sync step. The class also
  carries `@pytest.mark.timeout(180)` so the timeout the fixture already asked
  of `subprocess.run` is the one that actually applies (a marker takes
  precedence over the command-line `--timeout`), instead of being silently
  capped at a third of it. Test-only change; no library behavior is affected.

- **A lattice escalation attempt no longer gets the whole run's wall-clock
  budget as its cap** (#4798) — every other routing engine receives its
  escalation attempt's fair slice as `timeout=_per_attempt_budgeted_timeout(…)`
  (#2823/#3463), but the lattice engine's only bound is the absolute
  `lattice_deadline` stamped at `load_pcb_for_routing` time, and all three
  escalation sites (`route_with_layer_escalation`, the rule-relaxation tier
  ladder, the combined 2D layers × tiers matrix) passed
  `_lattice_absolute_deadline(args)` — the end-of-invocation deadline spanning
  **every** remaining attempt. Because the lattice negotiates the whole netset
  in a single `route_netset` call, `route_all`'s between-nets `timeout` check
  never fires mid-run, so under `--route-engine lattice --timeout N` with a
  multi-attempt ladder the first attempt could consume the entire remaining
  budget and starve the later attempts — the lattice equivalent of the
  pre-#2823 "first attempt eats everything" regression. A new
  `_lattice_attempt_deadline(args, attempt_timeout)` helper now returns
  `min(whole-run deadline, now + this attempt's fair slice)`, and
  `_attempt_timeout` is computed ahead of the load at those three sites so the
  cap is available there. The result is never *looser* than before, only ever
  tighter, and only when a *positive* fair slice is actually computed: with no
  `--timeout` — including `--deterministic-budget` without `--timeout`, where
  `_per_attempt_budgeted_timeout` returns `None` — and with the documented
  unbounded sentinel `--timeout 0` (or negative), where the slice comes back
  `0`/negative rather than as a real slice, it collapses to
  `_lattice_absolute_deadline(args)` verbatim. The two genuinely single-attempt
  call sites (`main()`'s primary load and the `--order-method` throwaway-router
  factory) and the `--timeout` banner keep the unsliced absolute deadline,
  which is already the correct per-attempt cap there. Hoisting the
  `_per_attempt_budgeted_timeout` call feeds the negotiated / escape /
  two-phase / multi-resolution / evolutionary arms the same inputs as before
  (`args`, the loop indices); only the helper's `time.monotonic()` read moves
  earlier, so each arm's slice grows by the PCB-load duration — measured inert
  on board 04. Gated on a back-to-back host A/B of board 07 (which drives
  `--strategy negotiated --no-auto-layers`, so the change is expected — and
  measured — to be inert there); recorded in
  `boards/07-matchgroup-test/diagnostic-runs/README.md`.

- **The pure-Python pairwise (HV-isolation) search gate now measures from the
  per-net-class copper width** (#4793) — the three Python search kernels added
  by #4791 (`Router._cross_domain_trace_blocked`,
  `Router._cross_domain_via_blocked` and the hot-loop bitmap widener
  `Router._pairwise_expanded_blocked`) computed their widened radius
  `ceil((half_mm + required) / res)` from the **global** `rules.trace_width / 2`
  and `rules.via_diameter / 2`, even though the caller had already resolved a
  per-net-class `scalar_radius`. On a board with a wide class (e.g. a 0.5 mm
  `POWER`/HV class against a 0.2 mm global default) the search-time annulus was
  therefore narrower than the net's real copper warrants, so the fallback A\*
  accepted cells the (authoritative) post-route `route_pairwise_violation` gate
  then rejected — no DRC escape, but exactly the search-proposes /
  validation-rejects thrash #4791 exists to remove, burning the net's budget on
  rip-up/retry or dropping it into negotiated mode instead of converging first
  try. All three sites now resolve the routed net's name from
  `state.id_to_name` and take the extent from `_get_trace_width_for_net` (or the
  new `_get_via_size_for_net` sibling, wrapping the
  `net_class.via_size if net_class else rules.via_diameter` idiom `route()`
  already used inline), falling back to the global rules for nets with no class
  or no name mapping. This brings the fallback into parity with the C++ backend,
  which has always read the per-net `search_trace_half_width_mm_` /
  `search_via_half_diam_mm_` extents `cpp_backend` pushes in via
  `set_search_pair_widths` once per `route()`. Only reachable with a pairwise
  voltage-map table installed **and** a non-default class width **and** the
  pure-Python fallback in use (`--no-cpp-router`, or a worktree where the native
  extension is not built); boards whose classes share the global width are
  byte-identical. `tests/router/test_pairwise_python_search.py` pins the
  class-width band (and that an equal-to-global class is a no-op), and
  `tests/router/test_pairwise_cpp_parity.py` sweeps the whole band asserting
  cell-for-cell Python↔C++ agreement.

- **`kct netlist`, `kct sch unconnected` and `kct sync` no longer leave a
  netlist file beside the user's schematic** (#4795, third installment after
  #4750 and #4763) — `export_netlist()` defaults `output_path` to
  `<schematic dir>/<stem>-netlist.kicad_net` and never deletes it, so every
  *byproduct* call site silently dropped an unrequested `*-netlist.kicad_net`
  into the user's project directory. The nine remaining bare calls now route
  that byproduct through a `tempfile.TemporaryDirectory()` that is torn down
  before the caller returns: `Reconciler._build_net_adjacency` and
  `Reconciler._assign_nets` (both consume the parsed `Netlist` in memory only),
  `sch_find_unconnected`'s netlist cross-check (which keeps its load-bearing
  `fallback=False` — the Python fallback would hide the very bug that check
  exists to catch), and the `analyze` / `list` / `show` / `check` / `compare`
  subcommands of `kct netlist` (`compare` exports *two* schematics, so neither
  the old nor the new project directory gains a file). `kct netlist export` is
  deliberately unchanged: there the netlist file **is** the product the user
  asked for. Exception semantics at every site are preserved (the reconciler's
  best-effort swallowing, the cross-check's three specific `except` messages).
  Per-file `TestNetlistByproductLocation` regression classes in
  `tests/test_sync.py`, `tests/test_cli_sch.py` and `tests/test_cli_netlist.py`
  stub `kicad-cli` and assert the schematic's directory listing is byte-for-byte
  unchanged across each call — plus a guard that `kct netlist export` still
  writes its file at both the default and `-o` locations.

- **Rotted `.py:NNN` citations in board scripts and `match_group_length.py`**
  (#4796) — every cross-file line-number citation in the nine `boards/**`
  generator scripts and in `src/kicad_tools/router/match_group_length.py` is
  re-anchored on a symbol plus a bare file path, the convention #4774
  established for markdown. Each sampled citation was stale: five board scripts
  pointed at `boards/03-usb-joystick/generate_design.py:838` for a
  `route_success` fast-fail gate that has since moved to 849, and
  `escape.py:3303-3304` (cited as "QFN escape segments") now lands in
  `_group_pads_by_ring`, unrelated ring-distance code — that one was re-derived
  from scratch to `EscapeRouter._create_fine_pitch_row_escapes`. Every
  replacement symbol was AST/grep-verified to exist before landing. A new
  `tests/test_py_citation_rot.py` guards the class from regrowing: a fixed
  ten-file allowlist (deliberately not a glob) is scanned for `.py:NNN` in
  comments, docstrings and string literals, and a `CITED_SYMBOLS` table checks
  each anchor in both directions so a rename on either side goes red. ~85
  further citations across 29 other `src/` files (`router/core.py`,
  `cli/route_cmd.py`, `validate/**`, …) are out of scope here and remain a
  follow-up; comments and docstrings only, no behavior change.

- **`CLAUDE.md`'s `## Releasing` section no longer ends mid-sentence** (#4797) —
  the dangling "(the tag must point at a commit that" clause is completed with
  the reason `RELEASING.md` gives: the tag must reference the commit as it
  landed on `main` (squash-merge gives it a different SHA), never a pre-merge
  `release/vX.Y.Z` branch commit.

- **Multi-pin nets are no longer skipped outright on `--deterministic-budget`
  placement-feedback re-routes** (#4776) — `--deterministic-budget` sets
  `args.per_net_timeout = 0.0` as its "wall-clock cutoff disabled" sentinel, and
  ~24 `route_all_negotiated` call sites in `route_cmd.py` normalize it away with
  `or None`. The two placement-feedback helpers (`_run_placement_feedback`,
  `_run_placement_delta_feedback`) did not, so the literal `0.0` reached the
  whole `route_all_negotiated` call — and `0.0` is **not** falsy-safe on the way
  down. `derive_iter_per_net_cap(0.0, remaining)` collapsed to `min(0.0, cap)` =
  `0.0` at every rip-up-loop reroute site, and `route_net_negotiated`'s
  cumulative net deadline (`… if per_net_timeout is not None else None`) became
  an already-expired deadline, short-circuiting **every RSMT edge of every
  ≥3-pad net before a single A\* call**. So on a placement-feedback re-route
  only 2-pin nets routed (those survive because the C++ backend reads `0.0` as
  "no deadline"); every multi-pin net was dropped as a timeout failure. The same
  `0.0` also reached `Autorouter._post_negotiation_sweep_bounds`, which took
  `min(0.0, 10 s)` and made the #4159 rescue sweep a silent no-op on those
  passes. Both call sites now normalize, and the sweep-bounds consumer
  additionally treats a falsy per-net budget as absent (mirroring
  `_relief_subsearch_budget`, where `0.0` has always been falsy) so no future
  caller can poison it; the `is None` gate on the safety-backstop arm is
  deliberately unchanged, so no board changes which arm it selects. Because this
  restores routing decisions that determine routed *reach*, it was
  gated on a back-to-back host A/B against `origin/main` @ `fe662585` (C++
  backend build 19, `PYTHONHASHSEED=42`, `--seed 42`): board 07 keeps 26/31
  reach, the same 5-net open set (`DQ3, DQ4, MIPI_DAT0_N, TMDS_D0_N,
  TMDS_D1_N`), 8 blocking DRC against its floor of 8 with an identical rule
  split (`diffpair_length_skew` 4 + `diffpair_routing_continuity` 4) and
  **byte-identical routed copper** (both placement-delta probes were refused in
  both arms, so the final board is the initial pass's either way — an accept-gate
  outcome, not an invariant); board 04 routes 9/9 and never reaches the
  path at all (0 blocking DRC on the strict-0 gate, 0 unconnected pads, LVS
  clean, identical copper). No allowlist or gate threshold was touched. The
  measurement is recorded in
  `boards/07-matchgroup-test/diagnostic-runs/README.md`.

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

### Fixed

- **Validated per-board fabrication-floor overrides for native DRC-constraint
  emission** (#5006) — `kct check --mfr jlcpcb-tier1 --emit-drc-constraints`
  (and the manufacturing export path, `kct route`'s sidecar emission, and
  `kct mfr apply-rules`) previously overwrote a reviewed project's narrower,
  actually-fab-verified `min_hole_to_hole` floor with the manufacturer
  profile's conservative default, causing native KiCad DRC to report
  spurious `hole_to_hole` warnings on a board that a native DRC pass with
  the reviewed floor showed was clean. A new
  `kicad_tools.manufacturers.fabrication_overrides` module defines a
  validated, cited per-board override contract: a `fabrication_overrides.json`
  sidecar next to the routed board declares a field, an overriding value, the
  manufacturer it applies to, a mandatory `source` citation, a mandatory
  `reason`, and a mandatory `tracking_issue` recording the review that
  approved it — any of the three provenance fields missing fails closed; an
  override is only retained if it names a manufacturer-verified capability
  floor on record (e.g. JLCPCB's published pad-hole-spacing minimum) and does
  not ask for anything looser than that floor — an unrecognized field,
  missing provenance, manufacturer mismatch, unregistered field, or a value
  below the verified floor is rejected and the emission falls back to the
  profile's conservative default instead of silently applying an unsafe
  override. All four native-constraint-emission call sites (`kct check
  --emit-drc-constraints`, the manufacturing export path, `kct route`'s
  sidecar emission, `kct mfr apply-rules`) resolve the same sidecar through
  one shared entry point, `resolve_pcb_fabrication_overrides`, so the Python
  `DRCChecker` and every native-emission surface agree on the identical
  resolved floor for the same board.
  `boards/03-usb-joystick/check_manufacturing.py`'s previously bespoke
  `dataclasses.replace(checker.design_rules, min_hole_to_hole_mm=0.45)` patch
  is migrated onto this shared contract, backed by a new
  `boards/03-usb-joystick/output/fabrication_overrides.json` sidecar. The
  board resolves that sidecar through the module's shared three-directory
  probe (`discover_fabrication_overrides_sidecar`) with a fallback to its own
  committed copy, and `route_pcb` now stages the sidecar next to every
  generated board, so a board copy generated outside
  `boards/03-usb-joystick/output/` still resolves the identical cited floor
  instead of failing on a missing file. `routing_plan.apply_native_fab_floor`
  reads the floor it writes into the `.kicad_pro` from that same sidecar
  rather than restating the literal value.

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

[0.23.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.23.0
[0.22.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.22.0
[0.21.1]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.21.1
[0.21.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.21.0
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

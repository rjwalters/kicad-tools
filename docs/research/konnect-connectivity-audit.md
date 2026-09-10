# Evaluation: Konnect's schematic connectivity primitives vs our ERC/LVS/net-status surface

**Issue**: #4900 (item 6 of #4880's Konnect ideas audit)
**Date**: 2026-09-10
**Verified against**: `origin/main` @ `7ad470f7` (2026-09-10)
**Upstream**: [Konnect](https://github.com/mixelpixx/Konnect) (mixelpixx, Rust, AGPL-3.0) — reference only, no code read for this audit beyond the tool names and one-line descriptions already summarized in the parent issue (#4880) and this issue's body. **No Konnect source was cloned or read for this pass** — every claim below is anchored against our own codebase; Konnect's six primitives are treated as a checklist of *capabilities*, not implementations to imitate line-for-line.
**License constraint**: Konnect is AGPL-3.0, kicad-tools is MIT — no vendored or ported code. Every capability discussed below either already exists in kicad-tools (built independently, often years before this audit) or, where genuinely absent, is scoped as a from-scratch, generic library addition.

## TL;DR

Five of Konnect's six named primitives are **already covered** by existing
kicad-tools surface — in most cases by code that predates this audit and
was built for unrelated issues (#2613's chorus-test single-pad-net sweep,
#3914/#4934's copper-island accounting, #4680's isolated-copper DRC class).
The sixth, `fix_connectivity`'s near-miss auto-snap, is a genuine and narrow
gap: we already **detect** the exact defect class (`find_wire_stubs`) but
have no automated **repair** for it, unlike our existing `fix-erc` pattern
which already proves out the `--dry-run`-gated auto-fix shape for two other
ERC violation classes.

| # | Primitive | Verdict | Where we already do this |
|---|-----------|---------|---------------------------|
| 1 | `find_shorted_nets` | **Already have** | KiCad-native `multiple_net_names` ERC (surfaced + explained), schematic-side; `lvs/copper_lvs.py` shorts, PCB-side |
| 2 | `find_single_pin_nets` | **Already have** | `validate/sch_orphan_label.py` (schematic, named labels), `validate/rules/single_pad_net.py` (PCB, all nets) |
| 3 | `find_orphan_items` | **Already have** | `cli/sch_find_unconnected.py` (`kct sch unconnected`, schematic pins), `validate/rules/zone_fill.py::IsolatedCopperRule` (PCB copper islands) |
| 4 | `trace_from_point` | **Already have** | `operations/net_ops.py::NetTracer.trace_from_point`, wired to `kct nets` / `kicad-nets --net` |
| 5 | `get_net_connectivity` (union-find) | **Already have** | `router/connectivity.py::_UnionFind` (board-wide copper reachability) + `analysis/net_status.py` per-net island BFS/union-find (`island_count`) |
| 6 | `fix_connectivity` (near-miss snap, `dry_run`) | **Adopt** (narrow) | Detection exists (`validate/sch_wire_stub.py::find_wire_stubs`); no auto-fix exists. Follow-up filed. |

**Net:** five already-have, one adopt. One follow-up issue filed (see
"Follow-up issue filed" at the end).

## Methodology

For each primitive: (a) restate what Konnect's tool does, from the
one-line descriptions in #4880/#4900's issue bodies (the only Konnect
material read for this pass — no upstream source browsing, to stay clean
of the AGPL-3.0 boundary even at the "reading for inspiration" level this
kind of narrow, already-obvious mechanism doesn't need); (b) grep the
kicad-tools tree for the closest existing mechanism; (c) read the matching
module far enough to confirm it actually implements the described
behavior (not just a name that sounds similar); (d) give a verdict with
file:line citations, all re-checked against `origin/main` @ `7ad470f7`
(2026-09-10) while writing this document.

## 1. `find_shorted_nets` — Already have

**Konnect**: reports distinct schematic net names that land on one
electrical wire path (a short at the schematic level, before layout).

**What we have**:

- **Schematic-side, native KiCad ERC.** `ERCViolationType.MULTIPLE_NET_NAMES
  = "multiple_net_names"` (`src/kicad_tools/erc/violation.py:45`, described
  at `:107` as "Wire has multiple net names") is a **native KiCad ERC
  violation type** — produced by `kicad-cli sch erc` itself via
  `run_erc()` (`src/kicad_tools/cli/runner.py:65`, invoked from
  `run_erc_on_schematic()` at `src/kicad_tools/cli/erc_cmd.py:254,274`) and
  then parsed, filtered (cross-sheet false-positive suppression,
  `src/kicad_tools/erc/cross_sheet.py`), and explained by
  `_explain_multiple_net_names()`
  (`src/kicad_tools/cli/erc_explain_cmd.py:864`). This is exactly Konnect's
  "distinct net names on one wire path" case — KiCad's own ERC engine
  already flags it, and our tooling already surfaces + explains it through
  `kct erc` / `kct erc explain`. Nothing to build.
- **PCB-side, our own comparator.** `lvs/copper_lvs.py` computes
  `CopperLVSMismatch(kind="short", ...)` when copper routing fuses two
  *different* schematic nets (`src/kicad_tools/lvs/copper_lvs.py:89`,
  predicate documented at `:14-52`, one report per offending net pair via
  `seen_short_pairs` at `:236-254`). This is the layout-time analog: a
  short introduced by routing rather than schematic wiring, which is
  outside what a schematic-only `find_shorted_nets` primitive would ever
  see. `lvs/recipe.py:448-458` wires this into the `kct check` gate output
  ("copper-LVS FAIL: N short(s) / M open(s)").

**Verdict**: already have both the schematic-time (native KiCad ERC,
surfaced) and layout-time (our own copper-vs-netlist comparator) halves of
this idea. No gap.

## 2. `find_single_pin_nets` — Already have

**Konnect**: reports nets with only one connected pin.

**What we have**:

- **Schematic-side, named labels.** `validate/sch_orphan_label.py` (whole
  module) implements exactly this for named local/hierarchical/global
  labels: `find_orphan_labels()` (`:259`) flood-fills from every label
  position through wires (`_collect_label_pin_endpoints`, `:210`) and
  reports any label reaching exactly one pin in the whole design (or one
  pin within its sheet, for local labels), skipping power-rail-shaped
  names (`_is_intended_signal_name`, `:92`) and pins with an explicit
  `no_connect` flag (`:325-328`). The module's own docstring
  (`:1-15`) is explicit that this is deliberately broader than KiCad's
  built-in `isolated_pin_label` ERC check, which only evaluates
  per-sheet and therefore misses cross-sheet single-pin global labels —
  this is filled in by the cross-sheet suppression logic at
  `erc/cross_sheet.py:304-334` for the *false-positive* direction, and by
  `sch_orphan_label.py` for the *true-positive, cross-sheet* direction
  KiCad's own ERC cannot see.
- **PCB-side, all nets.** `validate/rules/single_pad_net.py`
  (`rule_id = "single_pad_net"`, `:144`) is the layout-side complement:
  any declared net with exactly one pad, classified into genuine-NC /
  connector-NC / real-defect buckets (module docstring `:1-38`), wired
  into `kct check` via `PCBValidator.check_single_pad_nets()`
  (`src/kicad_tools/validate/checker.py:1143-1159`).
- **KiCad-native partial coverage.** `ERCViolationType.ISOLATED_PIN_LABEL`
  (`erc/violation.py:36,98`) is KiCad's own per-sheet version of the same
  check; `sch_orphan_label.py`'s docstring documents (with issue #2613 as
  the motivating incident: 39 single-pad nets on the chorus-test board,
  most of them intentional RPi-header no-connects) exactly why the
  per-sheet native check under-catches — it's the reason this module
  exists as a from-scratch addition rather than a thin wrapper.

**Verdict**: already have, on both sides of the schematic/PCB boundary,
with the cross-sheet gap in KiCad's own check already identified and
closed by our own code (#2613). No gap.

## 3. `find_orphan_items` — Already have

**Konnect**: reports items (components/pins) with no connections at all.

**What we have**:

- **Schematic-side.** `cli/sch_find_unconnected.py` (whole module) walks
  the schematic's wire-graph (`sch_connectivity.py`'s shared
  `build_wire_graph`/BFS primitives, see item 5) and reports every pin not
  reached by any wire, label, or junction, plus a `kicad-cli`-netlist
  cross-check for pins silently dropped by a stale `(instances)` block
  (module docstring `:1-18`). This is wired as `kct sch unconnected`
  (`src/kicad_tools/cli/commands/schematic.py:165-176`), a first-class
  subcommand, not a standalone script.
- **PCB-side, copper.** `validate/rules/zone_fill.py::IsolatedCopperRule`
  (`rule_id = "isolated_copper"`, `:227`, class at `:177`) implements
  KiCad's `isolated_copper` DRC predicate natively: a zone-fill island is
  orphaned iff its copper-connectivity cluster (grown transitively through
  tracks/vias/touching fills) contains no pad — i.e. copper that touches
  nothing. This closed issue #4680 (199 of 268 previously-invisible
  findings on the reference board) and is verified against `kicad-cli`
  10.0.5 on fixture boards (module docstring `:1-33`).

**Verdict**: already have both the pin-level (schematic) and copper-level
(PCB) forms of "orphan item." One narrower notion — a fully-disconnected
*component* (every pad on that footprint isolated, as opposed to one
orphan pin or one orphan copper island) — is not modeled as its own named
check, but it is fully implied by the existing pin-level and net-level
checks: a component with zero connected pins shows up as N single-pin-net
findings from item 2's PCB-side rule, one per pad. Not worth a dedicated
"orphan component" primitive on top of that — it would just recompute
the same evidence with a different grouping.

## 4. `trace_from_point` — Already have

**Konnect**: given a point, trace the electrically connected net.

**What we have**: `operations/net_ops.py::NetTracer.trace_from_point()`
(`:129-198`) — the method name is a literal match, and the mechanism is
the same idea: flood-fill from a starting point through wire endpoints
(`_build_wire_graph`, `:102`), picking up labels (`get_label_at`, `:113`)
and junctions along the way, returning a `Net` with every reached pin,
wire, and label. `trace_all_nets()` (`:200`) calls this once per
unvisited wire to enumerate every net in a sheet; `find_net_by_label()`
(`:231`) is the label-name-addressed variant. This is wired to a
first-class CLI entry point — `kct nets --net <label>` /
`kicad-nets <sch> --net <label>` (`src/kicad_tools/cli/nets.py:32-49`,
registered at `src/kicad_tools/cli/parser.py:266` and as its own console
script, `pyproject.toml:69`) — not just internal library code.

**Verdict**: already have, under (coincidentally) almost the same name,
already exposed as a CLI command. No gap.

## 5. `get_net_connectivity` (union-find) — Already have

**Konnect**: a union-find-based net connectivity computation.

**What we have**: two independent implementations, at two different
granularities, both genuinely union-find (not just "graph reachability by
another name"):

- **Board-wide, real per-pad reachability.** `router/connectivity.py`
  defines `class _UnionFind` (`:53-70`, "Minimal union-find over integer
  node ids") with standard path-compressed `find`/`union`. It's used
  (`:186-230`) to union pad/segment/via nodes by real geometric contact
  (shapely polygon intersection, matching `kicad-cli pcb drc` semantics —
  module docstring `:9-14`), producing the connectivity groups that back
  `NetStatusAnalyzer`'s "actually reachable" pad classification.
- **Per-net island accounting.** `analysis/net_status.py` has *two* more
  union-find/connected-components computations of its own, purpose-built
  for different sub-problems in the same file: an inline union-find
  (`def find(x)` / `def union(a, b)`, `:1291-1297`) for merging touching
  zone-fill-island fragments per zone object (`:1051-1105,1274-1320`,
  Issue #3914's "don't union disjoint fills" guarantee), and a separate
  BFS-based connected-components pass, `_find_islands()` (`:1780-1819`),
  that turns a per-net pad/segment/via adjacency graph
  (`_build_connectivity_graph`, `:777`) into the `island_count` exposed on
  `NetStatus` (`:100`, doc comment `:73-83` explains the #4934 rationale:
  `open_connections = island_count - 1` is the "remaining ratsnest"
  metric a `get_net_connectivity`-style consumer would want).
- **Schematic-side equivalent.** `cli/sch_connectivity.py` is the shared
  wire-graph/BFS module (`build_wire_graph`, `flood_fill_net`,
  `propagate_net_names`) backing three CLI surfaces at once —
  `sch_check_connections`, `sch_find_unconnected` (item 3), and
  `sch_pin_map` — the schematic-domain analog of "give me connectivity
  groups," even though its internal algorithm is BFS rather than literal
  union-find (the two are computationally equivalent for this problem;
  union-find just amortizes repeated *online* merges better, which
  matters for a router doing incremental edits, not for a one-shot batch
  analysis pass — which is exactly why the PCB router-side code uses
  union-find and the batch analysis code uses BFS).

**Observation, not a gap**: three separate implementations of "connected
components over a net's copper/wire graph" now exist
(`router/connectivity.py`, two in `analysis/net_status.py`) for three
different call sites with three different input shapes (real geometry,
per-zone fill fragments, and a pad/segment/via id graph respectively).
Consolidating them into one shared primitive is a plausible future
Hermit-style simplification, but it is out of scope here — it is not what
this issue asked for (Konnect's `get_net_connectivity` is a *capability*
gap check, not an internal-architecture review), and touching three live,
independently-tested connectivity engines is exactly the kind of
refactor-while-fixing-something-else scope creep this repo's builder
conventions warn against. Noted here so a future Hermit pass has the
pointer; no issue filed for it (see "Follow-up issue filed" below — only
the genuine capability gap, item 6, gets one).

**Verdict**: already have — in triplicate, at three granularities. No
capability gap; a possible-but-out-of-scope internal consolidation noted
above.

## 6. `fix_connectivity` (near-miss auto-snap, `dry_run`) — Adopt (narrow)

**Konnect**: given a near-miss connectivity gap, automatically snap it
closed, gated by a `dry_run` option that previews without mutating.

**What we have** (detection only): `validate/sch_wire_stub.py` (whole
module) already implements the *diagnosis* half exactly: for every wire
with a free (unconnected) endpoint, it searches along the endpoint's axis
for the nearest pin within `max_stub_grids` grid steps (default 4 × 2.54mm
= 10.16mm) and reports the exact gap as a `WireStubFinding`
(`grid_steps_short`, `candidate_pin_ref`, `candidate_pin_position`, `axis`
— dataclass at `:60-85`). This is wired into `kct sch validate` as the
`wire_stub` category (`cli/sch_validate.py:3469-3516`, `_run("wire_stub",
check_wire_stubs)` at `:3617`) and even renders the exact fix as English
in the message ("extend the wire by N mm along {axis} to land on the
pin" — `sch_validate.py:3499-3502`). This is precisely the defect class
Konnect's near-miss auto-snap targets (module docstring `:1-11` cites the
originating incident: seven wires in a real schematic terminating exactly
one 2.54mm grid step short of their target pins).

**What's missing**: nothing turns a `WireStubFinding` into an actual
edit. Confirmed by exhaustive search — `find_wire_stubs` /
`WireStubFinding` are referenced **only** from `sch_validate.py`'s
read-only reporting path (`git grep -n 'find_wire_stubs\|WireStubFinding'
src/kicad_tools` returns exactly those two call sites); there is no `kct
sch fix-wire-stubs` or equivalent command, and `cli/fix_erc_cmd.py`'s
existing auto-fix pass (`_apply_fixes`, `:173-320`) only handles
`power_pin_not_driven` (insert `PWR_FLAG`), `pin_not_connected` (insert
no-connect marker), and `wire_dangling`/`unconnected_wire_endpoint`
(**remove** the dangling wire) — it never *extends* a wire to close a
near-miss gap, which is a materially different (and materially more
useful, for this specific defect class) repair than delete-and-let-the-
user-redraw.

**Why this is the right one to adopt**: it is the only primitive on
Konnect's list where kicad-tools has the *diagnosis* but not the
*repair* — every other item is either fully covered or the "gap" turns
out to be an already-solved false-positive problem (item 2's cross-sheet
suppression). It is also narrowly scoped: the target defect (a wire
endpoint an exact integer number of grid steps short of a real pin, on
one axis) is precisely the shape `find_wire_stubs` already isolates, so
the fix is "extend this wire's dangling endpoint to this already-computed
pin position" — no new geometry search, no new net-identity reasoning,
and (critically, given `sch_wire_stub.py`'s own conservatism: it only
fires on integer-grid-step gaps, never a fuzzy/proximity match) no risk
of silently bridging two pins that were never meant to connect. The
existing `fix-erc --dry-run` command (`cli/fix_erc_cmd.py:81`, `:223`)
already establishes the exact UX shape this should follow: preview by
default logic inverted to `--dry-run` opt-in, one `FixAction` per
change, machine-readable JSON output.

**What's declined for now**: implementing the actual mutation in *this*
PR. `find_wire_stubs` reads schematics via `kicad_tools.schema.Schematic`
(the read/parse model used throughout `sch_validate.py`), while the only
schematic *mutation* API in the tree —
`kicad_tools.schematic.models.Schematic` (used by `fix_erc_cmd.py` and
`sch_reconnect_pin.py`) — is a materially different class hierarchy with
its own `Wire` dataclass, its own s-expression round-trip, and its own
test fixtures. Bridging the two safely (load via the mutation model,
re-run the same stub-finding predicate or port it faithfully, mutate the
wire's dangling endpoint in place, and validate the round-trip against
real fixtures without regressing either model's existing test suite) is
real, reviewable engineering — not the "cheap enough to fold into an
audit PR" bar that let item 5's sibling PR (#4890) bundle its test code
directly. Scoping it as a follow-up keeps this PR's diff reviewable and
lets the follow-up get its own focused test plan (dry-run preview
correctness, idempotency, and — importantly — a negative test proving it
never snaps across a grid-step gap that *isn't* an exact integer
multiple, which is exactly the conservatism `sch_wire_stub.py` was
designed around and a sloppy auto-fix could easily violate).

**Verdict**: **Adopt.** Genuine, narrow capability gap; diagnosis already
exists and is well-tested; the repair is a small, well-bounded addition.
Follow-up issue filed (see below) rather than implemented in this PR, to
keep the mutation-path engineering reviewable on its own.

## License note

No Konnect source was read beyond the one-line tool descriptions already
present in the parent issue (#4880) and this issue's body (#4900) — this
audit is a grep-and-read pass over kicad-tools' own tree, checking
"do we already do the thing this name describes," not a study of
Konnect's Rust implementation. There is nothing to launder here: every
capability found already existed in kicad-tools before this issue was
filed (several predate #4880 by months — #2613, #3914, #4680, #4934), and
the one genuine gap (item 6) is scoped as a from-scratch addition against
kicad-tools' own existing `find_wire_stubs` detector and `fix-erc
--dry-run` UX pattern, not against anything in Konnect's source tree.

## Follow-up issue filed

| Item | Verdict | Follow-up issue |
|------|---------|------------------|
| 6 — wire-stub auto-snap repair (`dry_run`-gated) for `find_wire_stubs` findings | Adopt | #5095 |

Items 1-5 are already-have — no issues filed. Item 5's noted (but
out-of-scope) three-implementations observation is not filed as an issue;
it is left as a pointer for a future Hermit pass.

## Recommendation

**Confirm coverage, land one narrow follow-up.** Five of Konnect's six
schematic-connectivity primitives are not just "arguably covered" but
concretely implemented, tested, and in several cases wired into `kct
check` / `kct erc` / `kct sch` as first-class commands — often more
thoroughly than the one-line Konnect description implies (e.g. item 2's
cross-sheet handling, item 3's copper-cluster predicate matching
`kicad-cli`'s own DRC semantics exactly). The one real gap,
`fix_connectivity`'s near-miss auto-snap, is worth adopting precisely
*because* it is narrow: the hard part (detection) is already done and
already conservative by construction, so the follow-up is a bounded,
reviewable addition rather than new open-ended design work.

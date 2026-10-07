# Evaluation: circuit-skills (punkfab) as a source of field-tested PCB lessons

**Issue**: #5886
**Date**: 2026-10-07
**Upstream**: https://github.com/punkfab/circuit-skills
**Pinned commit read**: `f6ed02104137f7b8a1cb9d0bd447dd3a11deccac` (the pin already in the registry; the
older `bc1322f9` pin is not used)
**Registry entry**: `kct ecosystem show circuit-skills`
**License**: none committed, so `unlicensed`. This note is **ideas only**. Everything below is restated in
our own words; no upstream text, scripts or skill content was copied, and none of its scripts were run
(a read-only clone sat in a scratch directory outside this repo).

## TL;DR

circuit-skills is a set of Claude Code skills that treat every autorouter as immature and plan to finish
the routing tail by hand in KiCad. Our bar is the opposite (a fully automated, 0-DRC route), so it is a
**complementary peer**, not a model to follow. The registry verdict moves from `watch` to
`complementary`.

What it is good for is the 169-line problem catalog in `docs/history/problems.md`. About two thirds of it
is tscircuit export defects, Freerouting quirks, Blender and SWIG crashes, fab-bundle bookkeeping and shell
hygiene that do not touch our pipeline. Of the rest, almost everything is **already caught by a `kct check`
rule or already handled in the router**. Three real gaps came out of it, each now an issue:

| Gap | Issue |
|---|---|
| Router crosses interior board windows when no edge clearance resolves | #6043 |
| No per-net-group routing policy gate (max vias, max length, missing policy fails) | #6044 |
| `fix-drc` has no rip-up-and-reroute fallback once nudging fails | #6045 |

Its self-reported PCBWorld D3 result (0.98 Clean Pass for the experimental `srj-legal` backend) is
**unverified by us**. Benchmarking it belongs to #5942; nothing here confirms or disputes the number.

## Interior Edge.Cuts keepout: an actual check

The issue hinted that `router/clearance_kernel.py` documents even-odd containment over interior holes of the
outline. That is for **zone fills**, not for the router's obstacle model, so it proves nothing here. The real
paths are:

- `core/board_outline.board_outline_segments` returns every `Edge.Cuts` graphic as a segment, interior loops
  included (nothing distinguishes outer from inner).
- `router/io.load_pcb_for_routing` hands those segments to `grid.add_edge_keepout`, which blocks cells near
  every segment on every routable layer, but **only if `edge_clearance > 0`**. That value comes from
  `--edge-clearance` or from the manufacturer profile (#4568). With neither, there is no keepout at all.
- Post-route, `validate/rules/edge.py` measures each segment against the whole outline polyline, so a trace
  that crosses an inner loop is reported.

Check run (Python backend, scratch board in `/tmp`, not committed): 40x30 mm outline, a 10x10 mm interior
`gr_rect` window, one pad each side of it on the same net.

| Case | Result |
|---|---|
| Window, no edge clearance | 1 straight segment, drawn through the window |
| Window, `edge_clearance=0.3` | 4888 cells blocked (3312 on the same board without the window); route detours with 4 segments and 2 vias; 0 segment samples inside the window |
| No window, `edge_clearance=0.3` | 1 straight segment (the blocked cells are only the outer ring) |
| `kct check --mfr jlcpcb --only edge` on the straight-trace board | fails: `edge_clearance_trace`, "-0.100mm < minimum 0.30mm" |

**Verdict: partially handled.** With a clearance in force the router already treats interior loops as
obstacles, and the window ring is a wall on both layers. Without one it does not, and only `kct check`
catches it after the fact. Filed as #6043. Not traced: the C++ backend (not built in this worktree; the
Python path above feeds the same `add_edge_keepout`), and NPTH mounting-hole footprints, which go through
`router/component_hole_index.py` rather than through `Edge.Cuts`.

## Candidate verdicts

Vocabulary: adopt / already have / not applicable / duplicate-of-#N. "Not traced" marks a claim we did not
verify.

### Original nine

| # | Candidate | Verdict | Evidence pointer |
|---|---|---|---|
| 1 | `problems.md` gotchas | **adopt (partial)**: triaged below, 3 gaps filed | Table below; #6043, #6044, #6045 |
| 2 | Auto-keepouts around interior holes | **adopt (partial)** | Check above; `router/io.py` edge-keepout block, `router/grid.py::add_edge_keepout`; #6043 |
| 3 | Net-fragment reconciliation for per-subcircuit nets | **not applicable** | We do not ingest tscircuit routing. Cross-file net-name mismatches from tscircuit are already analysed in `docs/research/tscircuit-evaluation.md` (Interop gate, #5847; failure caught by LVS, `kct check`/`lvs`). Repairing them belongs upstream. |
| 4 | Enclosure-fit gating from a mechanical model | **not applicable (for now)** | Closest analogue: `optim/keepout.py` (mechanical keepouts, auto-detect from mounting holes/connectors) and `optim/edge_placement.py` (connector edge constraints). No STEP/enclosure datum import exists; not traced for demand. No issue filed; revisit if a board needs a shell. |
| 5 | Placement-first single-command loop | **already have** | `kct pipeline`, `kct route-auto`, anchor-weight recipe in `docs/placement-pad-anchoring-audit.md`, `docs/placement-scoring.md`. Calibration is #5948. |
| 6 | Stackup heuristic (2L+pour vs 4L planes) | **already have** | `kct route --auto-layers` escalates 2L, 4L, 4L-all-signal, then 6L (`cli/route_cmd.py`, "auto-layers" comments ~l.606); `kct create-pcb --layers`. No a-priori recommender; the ladder is empirical instead. Plane fanout for inner planes is also a documented upstream Freerouting hole that does not apply to our router. |
| 7 | KiCad IPC (`kipy`) headless injection | **not applicable** | Already rejected for us in `docs/research/kipy-ipc-api-evaluation.md` (needs a running, attended KiCad; cannot gate CI). We write `.kicad_pcb` directly. `kct ipc` exists for optional live use. |
| 8 | One parametric ngspice deck as source of truth | **not applicable** | We have no simulation flow; `/kct:ee-review` reasons over the board and schematic, not decks. Any SPICE grid would also go to the batch fleet per host rules. Not traced further. |
| 9 | 3D render / docs imagery | **already have** | `kct render` (`cli/render_cmd.py`) produces 2D SVG and 3D renders via `kicad-cli`, built for the demo gallery. Their Blender path adds nothing we lack. |

### Five update ideas (2026-10-05 comment)

| # | Candidate | Verdict | Evidence pointer |
|---|---|---|---|
| U1 | Placement score fitted on routing outcomes (reported 0.89 pairwise on 319 placements, 64 boards; unverified) | **duplicate-of-#5948** | Placement scoring today: `docs/placement-scoring.md`. |
| U2 | Finish-or-re-place diagnosis (closest candidate, are the leftovers in congested cells) | **duplicate-of-#5944** (also #5945 for the best-so-far part) | #5944 classifies congested vs blocked per unrouted connection. |
| U3 | Rip up what DRC flags, then re-route with widening radius | **adopt** | `kct fix-drc` (`cli/fix_drc_cmd.py`) and `repair-clearance` only nudge: `drc/repair_clearance.py`, `drc/repair_drill_clearance.py`. `route_fixed_repair.py` is the natural re-route building block. Filed #6045. |
| U4a | Floating-SMD-pad gate (opposite-layer copper, no via) | **already have** | Checked: a B.Cu trace ending under an F.Cu SMD pad with no via makes `kct check` report `connectivity` ("1 of 2 pads stranded") and `track_dangling`. `validate/connectivity.py` deliberately does not fuse via-less crossings (#3783). |
| U4b | Critical-routing policy gate (max vias, max length, mismatch; missing policy fails) | **adopt (partial)** | Mismatch is covered by `validate/match_group_skew.py` and `validate/diffpair_skew.py`. A via/length cap and "no policy is a failure" are not present (searched `src/kicad_tools` for `max_vias` etc.: not found). Filed #6044. |
| U5 | Multi-router ranking in `route-auto` | **duplicate-of-#5947** (portfolio) / blocked on #5942 | Subprocess-only GPL tools; only worth doing if #5942 shows they beat us. No separate issue. |

## `docs/history/problems.md` triage

Source: `docs/history/problems.md` and `open-items.md` at `f6ed02104137f7b8a1cb9d0bd447dd3a11deccac`. Each
row restates one catalogue entry. Status key: **handled** (kct already prevents or detects it),
**gap** (worth an issue; filed where noted), **n/a** (the problem lives in a tool or workflow we do not
use). "Not traced" means we did not look.

### 1. tscircuit export and solver (n/a unless stated)

| Gotcha | Status | Note |
|---|---|---|
| Top-side parts routed as mirrored bottom parts via deduped DSN images | n/a | We do not export Specctra DSN. |
| Copper pour missing from the export | handled | `kct check`/LVS surface it; `kct zones` adds pours (`src/kicad_tools/zones/`). |
| Via size props ignored, board props not reaching subcircuits | n/a | tscircuit-side; our rules come from the manufacturer profile. |
| Polygon cutouts export as nothing, outline exterior-only | n/a | Upstream limit. We read `gr_poly` outlines (`core/board_outline.py`). |
| Malformed DSN wiring section | n/a | No DSN. |
| Cross-subcircuit nets fragmented | n/a | Handled as an interop finding in `tscircuit-evaluation.md`, caught by LVS. |
| Duplicate refdes across modules | handled | `sync/reconciler.py` and schematic validation (`cli/sch_validate.py`) flag duplicates; not traced in detail. |
| Large board hangs in sub-solvers | n/a | tscircuit solver. |
| Default capacity-mesh router out of iterations | n/a | tscircuit router; we benchmark it in #5848. |
| Sequential-trace output is dirty | n/a | Same. |
| `layoutMode="pack"` stacks parts | n/a | tscircuit placement. |
| In-tool freerouting preset not driving Freerouting | n/a | tscircuit CLI. |
| 512-part board solver limit | n/a | tscircuit layout solver. |
| No keep-in, parts placed off-board | handled | `validate/rules/placement.py` plus `optim` edge constraints (off-board placement checks). |
| `tsci export` filename/path quirks | n/a | tscircuit CLI. |
| `tsci` CLI quirks when piped | n/a | tscircuit CLI. |
| Built-in USB-C part has no footprint | n/a | tscircuit parts engine. |
| ESP32 EPAD split paste pads floating | handled | A floating pad reports as stranded under `connectivity` (check above). Footprint-specific EPAD grounding is not otherwise traced. |

### 2. Freerouting

| Gotcha | Status | Note |
|---|---|---|
| Cannot keep GND/PWR on inner planes | n/a | Freerouting limit. Our zone/stitch tooling (`kct zones`, `kct stitch`) is separate. |
| v2.1.0 ignores pass caps | n/a | Freerouting version. |
| v2.2.4 needs JDK 25 | n/a | Same. |
| SES import fails on empty host version | n/a | No SES. |
| `-i/-o` flag mistake | n/a | Same. |
| Runaway Java process | n/a | We cap our own runs (`kct route --timeout`, `cli/route_cmd.py`). |
| Routes across NPTH holes | handled | Hole obstacles in `router/component_hole_index.py` (NPTH numbered by pin, unnumbered handled). Not traced to the grid in this pass. |
| Opposite-layer copper on SMD pads, no via | handled | `connectivity` stranded-pad check (above). |
| "Non-deterministic" tail | n/a | Their misdiagnosis. |
| Net silently omitted from SES while reporting completion | handled | `kct check` connectivity measures real copper, not the router's own claim; also `router/completion_verdict.py`. |
| Frozen-wiring second pass stalls | n/a | Freerouting. |
| v2.4.1 violations | n/a | Freerouting. |
| Pad-clip shorts past wide pads | handled | Clearance DRC (`validate/rules/clearance`) plus `repair-clearance`. |
| Routed at 0.15 mm vs netclass 0.20 mm; vias below fab minimum | handled | #4568 resolves manufacturer limits at route time; `kct check --mfr`; `.kicad_pro` netclass gap tracked as #5875. |

### 3. KiCad automation

| Gotcha | Status | Note |
|---|---|---|
| No headless Specctra round trip | n/a | We do not use Specctra. |
| `ExportSpecctraDSN` returns False | n/a | Same. |
| SWIG injector guessed nets | n/a | We parse and write files natively. |
| kipy connection refused | n/a | Rejected in `kipy-ipc-api-evaluation.md`. |
| IPC server dies between shell calls | n/a | Same. |
| kipy `create_items` handle semantics | n/a | Same. |
| Injector via size / unmapped nets | n/a | Same. |
| `pcbnew` not importable from a venv | n/a | We do not depend on `pcbnew`. |
| KiCad 9.0.3 SWIG API breakage | n/a | Same. |
| New-board clearances 0 and pour-vs-NPTH | handled | `zones/fill_clearance.py` and `test_board06_pour_repair_hole_clearance.py`; netclass defaults gap is #5875. |
| `kicad-cli drc` never failed the build | handled | `kct check` returns a failing exit code (the summary above shows `Overall: FAILED`). |

### 4. Placement

| Gotcha | Status | Note |
|---|---|---|
| Routing failure is really a placement problem | handled | Placement-first work: #5948, `docs/placement-scoring.md`, `placement_nudge.py`. |
| Decaps crowd the QFN escape | handled | `router/escape.py`, `validate/rules/via_under_body.py`; decap placement in `optim`. |
| Optimising routability pushed blocks into a notch | handled | Outline-aware placement (`optim/placement.py`, `router/placement_nudge.py` keeps footprints inside the outline). |
| Courtyard overlaps cause real shorts | handled | `validate/rules/courtyard.py` (with waivers). |
| Floorplan does not fit | n/a | Design-specific. |
| Board area too tight | handled | `router/auto_pcb_size.py` grows the board/envelope. |
| GPIO escape reassignment made it worse | n/a | Design-specific. |
| Placement nudges do not reach the DSN | n/a | No DSN; we route from the board file. |
| Ratsnest tangle from rotation | handled | `optim` ratsnest/rotation scoring (not traced in detail). |
| 3D keyswitch/tactile collision not gated | gap (low) | We have courtyard checks only, no body-height or enclosure clash gate. See candidate 4; no issue filed, no demand. |

### 5. Finishing the tail

| Gotcha | Status | Note |
|---|---|---|
| Blind finishers add shorts | handled | Our repair loops re-run DRC; adopted-in-spirit rule for #6045: accept only non-regressing passes. |
| Finisher "OK" that connected nothing | handled | Connectivity check (above). |
| Hard-coded-coordinate patches | n/a | Workflow hygiene; our repairs are rule-driven. |
| GND-pad vias shorting against stubs | handled | Clearance and `via_under_body` checks; not traced further. |
| Unconnected pin in congested cap cluster | gap | Diagnosis side is #5944; fix side is #6045. |
| One net cannot cross a corridor | handled | Zero-ohm jumper / local pour idea is a design choice; `kct zones` plus `pour_bridge.py` cover the local-pour half. |

### 6. Verification gates that lied

| Gotcha | Status | Note |
|---|---|---|
| "Fully routed" with unrouted nets | handled | `completion_verdict.py` states the rule mirrors kicad-cli: a stranded pad is not routed. |
| Sweep read empty log as 0 unrouted | n/a | Their harness bug. Our benchmark JSON carries explicit counts (`kct benchmark`). |
| Round-trip fidelity pass with 0 parts | n/a | Their harness. |
| Window crossing checked at endpoints only | handled | `validate/rules/edge.py` uses whole-segment distance to the outline (see check above). |
| Copper over the board edge bucketed as cosmetic | handled | `edge_clearance_trace` is an error (see check above). |
| NPTH hole clearance 0 bucketed as cosmetic | handled | Hole checks in `validate/rules/dimensions.py` (hole-to-hole, drill clearance); not traced to NPTH specifically. |
| `drc_check` printed CLEAN with unconnected > 0 | handled | `kct check` fails on connectivity (check above). |
| KiCad DRC passes a via 0.04 mm from a mount hole | gap (not traced) | Hole-to-copper checks exist in `dimensions.py`; the specific via-near-mount-hole case was not run. |
| Severity downgrades hide real fab dangers | handled | `--strict`, `--mfr` profiles; waivers are explicit (`--waive`, `--waive-reason`). |

### 7. Fab bundle, BOM, sourcing

| Gotcha | Status | Note |
|---|---|---|
| Parts too scarce at JLC | handled | `kct parts` stock lookup. |
| Catalog stock vs assembly-ready stock | gap (not traced) | Whether `kct parts` distinguishes assembly stock was not checked. |
| Invented placeholder LCSC number | handled | Parts lookup validates numbers; not traced further. |
| JLC BOM import errors | handled | `kct export`/`kct bom`; manifest check; not traced in detail. |
| CPL/BOM designator mismatch | handled | Same (`export/pnp.py`). |
| Gerber zip missed copper layers | handled | `kct export` bundle plus manifest check. |
| Cost model off | n/a | Their project's model; ours is `src/kicad_tools/cost`. |

### 8. Simulation

All of section 8 (Falstad drift, `i(Mxxx)`, `np.trapz`, Makefile spaces, ZVS convergence, parallel
non-determinism, brace expressions, F-source polarity, remanence, op-amp pole, solver spikes, MuJoCo
inertia): **n/a**, 11 rows. We ship no simulation flow, and on the shared dispatch hosts any SPICE grid
goes to the batch fleet. If we ever add one, the "run sequentially, single thread, compare to an analytic
check" lessons are the useful part.

### 9. Rendering

| Gotcha | Status | Note |
|---|---|---|
| Blender 5 compositor change | n/a | We use `kicad-cli pcb render`. |
| OCIO segfault ~1 in 6 | n/a | Blender. |
| Blender 5.2.0 headless segfault | n/a | Blender. |
| SIGPIPE from `| head` kills Blender | n/a | Blender. |
| Emissive LEDs render matte | n/a | Blender. |
| Claim that tscircuit has no 3D | n/a | Their misconception. |
| OLED missing from GLB | n/a | Their model set. |
| Stale GLB after a move | n/a | Our render path regenerates per board (`cli/render_cmd.py`). |

### 10. Process and environment

| Gotcha | Status | Note |
|---|---|---|
| `pkill -f` kills its own shell | n/a | Generic shell hygiene (we kill by PID in Loom scripts). |
| bun child survives kill | n/a | Not our runtime. |
| Deployed skill copy drifted | handled | Loom resync commits; `kct skills` installs from one source (`src/kicad_tools/agent_skills/`). |
| Absolute paths break after moves | n/a | Not traced. |
| Parallel sessions clobber one repo | handled | Loom worktrees. |
| KiCad junk committed | n/a | Hygiene. |
| Wrong Freerouting flags in memory | n/a | Their notes. |

## Follow-up issues

- #6043: route: block copper across interior `Edge.Cuts` loops even when no edge clearance resolves
- #6044: check: per-net-group routing policy gate
- #6045: fix-drc: rip-up-and-reroute fallback
- Already covered elsewhere, not re-filed: #5944 (congested vs blocked), #5945 (checkpoint and rollback),
  #5948 (placement score calibration), #5947 (portfolio and TraceMaker techniques), #5942 (D3 benchmark,
  including the 0.98 claim).

## Registry change

`[projects.circuit-skills]`: `verdict = "complementary"`, `research_docs` points at this note,
`last_verified = "2026-10-07"`. Pins and summary are left alone (#5941 owns those).

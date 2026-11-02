# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-10-29*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

_None._

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

_None._

## Important

`loom:important`; taken before unlabelled work.

_None._

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#8215**: optimizer via minimization removes vias that are still needed (unconnected pour stubs)
- **#8216**: new cleanup pass: remove redundant copper (spurs, overlap-only off-centre ends, same-layer loops)
- **#8217**: new cleanup pass: shorten detours when a straight or single-bend path is clear
- **#8218**: run copper cleanup every time: a check rule plus a standard tapeout step (dedupe + optimize, kicad-cli refereed)

## In Progress

Issues currently being built (`loom:building`).

- **#8203**: export: missing 'report' extra silently skips the design report and still exits 0
- **#8204**: report generate: does not resolve the net-class-map sidecar, so sidecar-gated DRC rules run inactive
- **#8207**: optimize-traces --drc-aware: every net rolled back on a clean HV board (0 -> 151 errors), summary still claims 84 optimized, no per-net diagnostics
- **#8208**: parts availability: reports an export-auto-matched line as MISSING; '8/57 available' summary leaves most BOM lines unaccounted for
- **#8214**: optimize-traces: collision-check each rewrite on finished boards instead of whole-net rollback

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#8205**: fix: resolve net-class-map sidecar in kct report generate
- **#8206**: fix: fail export when the report extra is missing
- **#8210**: fix(optimizer): match and emit name-only net refs in replace_segments
- **#8212**: fix: report kept vs attempted nets and per-rule violations in optimize-traces --drc-aware
- **#8213**: fix: auto-match LCSC in parts availability and account for all BOM lines

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

_None._

## Proposed

Issues carrying `loom:curated`.

- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)*
- **#5539**: Champion: Merge-Risk Hold Digest *(curated)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)*
- **#8203**: export: missing 'report' extra silently skips the design report and still exits 0 *(curated)*
- **#8204**: report generate: does not resolve the net-class-map sidecar, so sidecar-gated DRC rules run inactive *(curated)*
- **#8207**: optimize-traces --drc-aware: every net rolled back on a clean HV board (0 -> 151 errors), summary still claims 84 optimized, no per-net diagnostics *(curated)*
- **#8208**: parts availability: reports an export-auto-matched line as MISSING; '8/57 available' summary leaves most BOM lines unaccounted for *(curated)*
- **#8214**: optimize-traces: collision-check each rewrite on finished boards instead of whole-net rollback *(curated)*
- **#8215**: optimizer via minimization removes vias that are still needed (unconnected pour stubs) *(curated)*
- **#8216**: new cleanup pass: remove redundant copper (spurs, overlap-only off-centre ends, same-layer loops) *(curated)*
- **#8217**: new cleanup pass: shorten detours when a straight or single-bend path is clear *(curated)*
- **#8218**: run copper cleanup every time: a check rule plus a standard tapeout step (dedupe + optimize, kicad-cli refereed) *(curated)*
- **#8219**: OffAngleSegmentWarning reports 'net 0' on name-only boards and repeats each warning *(curated)*
- **#8220**: deploy-site.sh leaves the working tree dirty: re-renders committed board images, untracked .wrangler/ *(curated)*
- **#8221**: deploy-site.sh header and verify default are stale: custom domain is live, token auth undocumented *(curated)*

## Proposed (Architect / Hermit)

_None._

## Epics

- **#4431**: route: per-net scalar clearance cannot express HV isolation — need class-pair (net-class × net-class) clearance rules in the router; both workarounds proven inadequate on a mains board
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC
- **#5952**: Epic: Harness-neutral kicad-tools (Claude Code, Codex CLI, opencode)

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 0 |
| Very important | 0 |
| Important | 0 |
| Ready (`loom:issue`) | 4 |
| In Progress (`loom:building`) | 5 |
| PRs awaiting review | 5 |
| Approved PRs awaiting merge | 0 |
| Curated | 15 |
| Architect / Hermit proposals | 0 |
| Active epics | 3 |
<!-- guide:plan-body:end -->

## Release History (static archive)

_Historical release notes below this point are a static archive, not regenerated by Guide — see WORK_LOG.md for the current chronological record of merged PRs and closed issues._

| Release | Highlights |
|---------|-----------|
| **v0.20.0** (2026-08-06) | `kct route --complete` completion pass, route-time HV-isolation enforcement in every engine, `.kct_waivers.json` waiver mechanism, `net-status` strict-by-default, KiCad-10 net-dialect / export-manifest / LVS-identity correctness sweep; shipped as a 13-PR train through a GitHub Actions outage |
| **v0.19.0** (2026-07-20) | HV-isolation design loop (`kct creepage --voltage-map`, `zones hv-keepout`, HV-aware placement, `/kct:hv-isolation-loop` skill) + via-in-pad manufacturability (`kct fix-vias` off-pad relocation), `analyze electrical-rating`, `--emit-dru`/`--emit-drc-constraints` rule-identity sidecars |
| **v0.18.0** (2026-07-20) | HV / analog manufacturing gates — `kct creepage` (surface-path creepage audit vs IEC 60664-1/62368-1) and `kct analyze current-sense` (analog layout lint). Plus real `--nets` route filter, `pcb reinforce` multi-branch anchoring, `route --layers auto` inner-layer advisory |
| **v0.17.0** (2026-07-17) | Experimental alternative routing substrate — adaptive octilinear **lattice** engine + constrained-Delaunay **navmesh/mesh** engine, both default-OFF; routes large mixed-pitch boards the grid can't fit in memory |
| **v0.16.0** (2026-07-15) | Region-bounded routing, ampacity-aware net-class min-width + DRC (IPC-2221), copper dedupe, `pcb reinforce` anchor-PTH rows, `net-status --strict` real-geometry connectivity, off-board preflight |
| **v0.15.0** (2026-07-13) | Router feasibility certificates + constructive escape ordering, coupled diff-pair corridor attractors + C++ joint-state A\* port, copper-LVS gates across boards 01–07, LCSC/EasyEDA + cross-library 3D model resolver tiers |
| **v0.14.0** (2026-06-16) | Demo gallery website (kicad-tools.org), zone-fill foreign-pad clearance fix, PCB `page_fit`, oblique 3D + 2D-SVG renders, gallery LVS status, ERC/LVS/Manifest meta sub-checks for `kct check` |

**LVS Soundness Epic — COMPLETE (2026-06-17).** Independent copper-LVS soundness epic (motivated by #3742), shipped via a `/loom:sweep` run processing 16 PRs: gate + extractor hardening (independent copper-extracted LVS gate, label-free pour extraction, per-zone pour-pad bonding, foreign-net track-segment carve, layer-aware segment chaining), fleet rollout across boards 00–07, and real defects the gate surfaced and fixed on boards 02/03/04 that DRC had missed.

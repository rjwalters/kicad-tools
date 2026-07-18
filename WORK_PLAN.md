# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-06-17*

---

## Urgent (Top Priority)

*No issues currently carry the `loom:urgent` label.*

## In Progress (`loom:building`)

*Nothing in flight.* 0 open PRs.

## Ready for Work (`loom:issue`)

*Empty.* No approved issues are currently queued for Builders.

## LVS Soundness Epic — COMPLETE (2026-06-17)

The independent copper-LVS soundness epic (motivated by #3742) is shipped. A `/loom:sweep` run on 2026-06-16/17 processed the #3762/#3763 Architect proposals and their entire follow-on tree — **16 PRs merged**:

- **Gate + extractor hardening**: independent copper-extracted LVS gate (#3757); label-free pour extraction (#3761); per-zone pour-pad bonding across disjoint fill islands (#3772); foreign-net track-segment carve in zone fill (#3773); layer-aware segment chaining to kill phantom via-less-crossover shorts (#3783/#3792).
- **Fleet rollout**: shared `write_lvs_report` helper + board 00/01 hard gates (#3762); boards 02/06/07 wired (#3779); boards 03/04 advisory (#3780).
- **Board fixes the gate surfaced** (real defects DRC missed): board-03 GND F.Cu/B.Cu plane stitching (#3787); board-04 OSC_IN↔OSC_OUT crystal-pin short re-route (#3785) + GND re-pour (#3791); board-03/04 schematic↔PCB net-drift reconciliation (#3764/#3765); fleet staleness-detection fix (#3767); the 2026-06 parity audit (#3763).

Net result: boards 00/01/02/03 are copper-LVS clean and reproducible; the gate caught (and fixed) genuine shorts on boards 02/03/04 that passed DRC.

## Remaining — Human-led design decisions (`loom:architect` / `loom:blocked`)

| Issue | Title | Status |
|-------|-------|--------|
| **#3775** | board-05 U3-south relayout to free PHASE_A/B/C escape channels | **Research-grade.** Curator (2026-06-17) confirmed not safely automatable in one builder pass: U3 south edge packs 20 nets across 9.5mm @ 0.5mm pitch, zero slack, all prior widening levers spent. Needs a human to pick a relayout strategy (inner-layer PHASE corridors / ISENSE-return relocation / J2 re-clustering) — full analysis posted on the issue. Stays `loom:architect`. |
| **#3766** | board-05: complete the 7 blocking unrouted nets | **Blocked** behind #3775 (the relayout is the prerequisite). |

## Tracked follow-ups (advisory / low-priority)

`loom:architect`: none currently beyond #3775. (Board-03/04 LVS hard-gate *graduation* is the deferred Part 2 of #3780, gated on the board defects now fixed — a tight follow-up.)

## Recently Completed

The May 1–8 sprint cleared an enormous backlog of router-pipeline polish, board generation, and CI hardening. Highlights since the last `WORK_PLAN` refresh (2026-03-18):

| Theme | Outcome |
|-------|---------|
| **v0.11.0 release** | Multi-resolution routing, R-tree spatial indexing, crossing-aware A* pathfinding (2026-04-12) |
| **v0.12.0 release** | Manufacturing export pipeline (BOM, CPL, gerber), JLCPCB integration, Jinja2 design report (2026-04-15) |
| **v0.13.0 release** | Two-phase global routing with RSMT decomposition, RUDY congestion estimator, Specctra DSN export (2026-04-28) |
| **v0.14.0 release** | Demo gallery website (kicad-tools.org), zone-fill foreign-pad clearance fix, PCB `page_fit`, oblique 3D + 2D-SVG renders, `kct render` / `board-metrics` / `pcb page-fit` commands, gallery LVS status, ERC/LVS/Manifest meta sub-checks for `kct check` (2026-06-16) |
| **v0.15.0 release** | Router feasibility certificates + constructive escape ordering, coupled diff-pair corridor attractors + C++ joint-state A\* port, slack-budget corridor widening, copper-LVS gates across boards 01–07, LCSC/EasyEDA + cross-library 3D model resolver tiers, thin-copper/silk-clearance/net-0-bridge DRC rules, gallery-hardened board fixtures 00–07 (2026-07-13) |
| **v0.16.0 release** | Region-bounded routing (`--region` + boundary stub reconnection), ampacity-aware net-class min-width + DRC (IPC-2221), copper dedupe (`pcb dedupe` + emission-time), `pcb reinforce` anchor-PTH rows, `pcb padmap` / `sch fix-annotation` / `pcb strip --region`, `net-status --strict` real-geometry connectivity, off-board preflight; pre-tag fleet validation fixed #4226 (junction-dot-gated wire union), #4227 (courtyard bbox-fallback annotation), #4229 (zone-pour plane-pad connectivity) (2026-07-15) |
| **v0.17.0 release (current)** | Experimental alternative routing substrate — adaptive octilinear **lattice** engine (`--route-engine lattice`) + constrained-Delaunay **navmesh/mesh** engine (`--route-engine mesh`), both default-OFF (epic #4267, P0→P4); routes large mixed-pitch boards the grid can't fit in memory (softstart rev-C: 74/77 nets DRC-clean at ~3% grid memory). Plus `--max-cells` (#4249), analytical `route --dry-run` (#4266), settable schematic `in_bom`/`dnp` (#4303), `net-status --why` ranked fix recommender (#4261/#4286), parts-catalog fixes (#4295–#4299); board-07 Track A closed placement-bound (#4256–#4258). `--route-engine grid` default byte-identical to 0.16.0 (2026-07-17) |
| **C++ pathfinder hardening** | `cpp_backend` with stale-`.so` build-version guard (#2501), DRC violation cost feedback (#2442), pre-computed blocked bitmap (#2437), pad metal area expansion (#2434), resumable A* (#2449) |
| **Auto-pour zones** | Power-net pour zones generated automatically with proper edge-clearance inset and per-net priority (#2407, #2417, #2422, #2461, #2519) |
| **Boards 02–05 brought online** | Placement and full routing for charlieplex (boards/02), USB joystick (boards/03, polished by #2536), STM32F103 board 04 (#2538, #2545), BLDC controller / DRV8301 (#2535, #2551) |
| **CI hardening** | kicad-cli round-trip smoke test on every emitted PCB (#2507), build-time PCB validity smoke check (#2505), `_routed.kicad_pcb` validation gate (#2552) |
| **Pipeline UX** | `kct pipeline` end-to-end workflow (#1307), `/release` skill for guided semver releases, `--commit` flag for pipeline runs |
| **Differential-pair groundwork** | CoupledPathfinder routing, N-pad coupling (#2478), `--differential-pairs` CLI flag (#2474), HIGH_CURRENT_SIGNAL net class (#2471) — all merged before Epic #2556 was scoped |
| **Loom upgraded to 0.7.1** | Includes #2547 incremental-commit protocol (PR #2554) — builders and doctors must now commit incrementally |

## Backlog Health

| Metric | Value |
|--------|-------|
| Total open issues | 0 |
| Ready for work (`loom:issue`) | 0 |
| Urgent (`loom:urgent`) | 0 |
| Building (`loom:building`) | 0 |
| Blocked (`loom:blocked`) | 0 |
| Proposals pending approval (`loom:architect`) | 0 (Architect pass in flight to repopulate) |
| Active epics | 0 |

**Assessment:** The backlog is **empty** as of 2026-06-16 — both open issues and open PRs are at zero. Epic #2556 (differential-pair first-class support, Phases 1A–1D) has fully merged, and v0.14.0 shipped to PyPI (demo gallery, renders, board-metrics, and the first independent copper-LVS soundness gate). With the queue clear, an Architect pass was launched to generate the next wave of work, prioritizing: (1) finishing the LVS soundness story (#3742 follow-ups — robust zone-pour extraction and copper-LVS as a first-class manufacturability leg), (2) re-scoping board-parity (#2394) after the recent board 00/04/05/06 DRC/LVS burst, and (3) other high-value epics. Re-run the Guide once those proposals are filed and approved.

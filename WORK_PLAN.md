# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-05-08*

---

## Urgent (Top Priority)

*No issues currently carry the `loom:urgent` label.* Builders are saturated on Epic #2556 Phase 1 (see "In Progress" below); the Guide will revisit urgent assignment once Phase 1A/1B land and the next-priority issues are unblocked.

## In Progress (`loom:building`)

| Issue | Title | Notes |
|-------|-------|-------|
| **#2558** | [Epic #2556] Phase 1B: Reliable diff-pair detection (explicit + KiCad group + suffix) | Builder active — depends on #2557 |
| **#2557** | [Epic #2556] Phase 1A: Add `NetClass.intra_pair_clearance` field | Builder active — foundation for Phase 1C/1D |

Do not retarget these issues — Phase 1A/1B underpin the rest of Epic #2556.

## Ready for Work (`loom:issue`)

| Issue | Title | Blocked By |
|-------|-------|------------|
| **#2559** | [Epic #2556] Phase 1C: Thread `intra_pair_clearance` through pathfinder + cpp_backend | #2557, #2558 |
| **#2560** | [Epic #2556] Phase 1D: New DRC rule `diffpair_clearance_intra` | #2557, #2559 |

Both Phase 1C/1D are queued for Builders once their Phase 1A/1B prerequisites merge. They carry `loom:approved` and `loom:curated` and are eligible to claim as soon as the dependency chain clears.

## Proposals Awaiting Human Approval (`loom:architect`)

| Issue | Title | Status |
|-------|-------|--------|
| **#2556** | Epic: First-class differential pair support across the routing pipeline | Phase 1 already broken into #2557–#2560; Architect may extend with Phase 2 (length matching) once Phase 1 ships |
| **#2394** | Roadmap: bring all five example boards to manufacturer-ready parity with board 01 | Long-running roadmap. Significant progress in late April / early May (boards 02, 03, 04, 05 placement and routing). Architect should re-evaluate which sub-tasks remain after the recent board burst |

## Recently Completed

The May 1–8 sprint cleared an enormous backlog of router-pipeline polish, board generation, and CI hardening. Highlights since the last `WORK_PLAN` refresh (2026-03-18):

| Theme | Outcome |
|-------|---------|
| **v0.11.0 release** | Multi-resolution routing, R-tree spatial indexing, crossing-aware A* pathfinding (2026-04-12) |
| **v0.12.0 release** | Manufacturing export pipeline (BOM, CPL, gerber), JLCPCB integration, Jinja2 design report (2026-04-15) |
| **v0.13.0 release** | Two-phase global routing with RSMT decomposition, RUDY congestion estimator, Specctra DSN export (2026-04-28) |
| **v0.14.0 release (current)** | Demo gallery website (kicad-tools.org), zone-fill foreign-pad clearance fix, PCB `page_fit`, oblique 3D + 2D-SVG renders, `kct render` / `board-metrics` / `pcb page-fit` commands, gallery LVS status, ERC/LVS/Manifest meta sub-checks for `kct check` (2026-06-16) |
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
| Total open issues | 6 |
| Ready for work (`loom:issue`) | 2 (#2559, #2560) |
| Urgent (`loom:urgent`) | 0 |
| Building (`loom:building`) | 2 (#2557, #2558) |
| Blocked (`loom:blocked`) | 0 |
| Proposals pending approval (`loom:architect`) | 2 (#2556, #2394) |
| Active epics | 1 (Epic #2556 — diff-pair support, Phase 1 in flight) |

**Assessment:** The backlog is tightly focused around Epic #2556 (differential-pair first-class support). Phase 1 is fully scoped into four sequential issues: 1A and 1B are actively building; 1C and 1D are approved and ready to claim once their dependencies merge. The roadmap epic #2394 (board parity) remains as a meta-tracker — it benefited heavily from the May 1–8 board burst and should be re-curated by the Architect to identify any remaining gaps. No urgent issues; no blocked issues. Project velocity is high (693 commits since 2026-02-27, three releases shipped).

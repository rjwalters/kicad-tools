# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-06-16*

---

## Urgent (Top Priority)

*No issues currently carry the `loom:urgent` label.*

## In Progress (`loom:building`)

*Nothing in flight.* The backlog was fully cleared on 2026-06-16 — Epic #2556 (diff-pair support, Phases 1A–1D) merged, and the v0.14.0 release shipped.

## Ready for Work (`loom:issue`)

*Empty.* No approved issues are currently queued for Builders.

## Proposals Awaiting Human Approval (`loom:architect`)

An Architect pass on 2026-06-16 repopulated the backlog with three proposals:

| Issue | Title | Focus |
|-------|-------|-------|
| **#3761** | Robust label-free zone-pour copper extraction for independent LVS (#3742 follow-up) | LVS soundness — closes the declared-net false-negative gap in `extract_pad_partition()`; sequence-first |
| **#3762** | Make copper-LVS a first-class manufacturability leg across all demo boards (#3742 follow-up) | LVS soundness — rolls board 00's LVS recipe/CI pattern fleet-wide; depends conceptually on #3761 |
| **#3763** | Board parity refresh: re-audit boards 02–07 against board-01 end-state (supersedes #2394) | Board parity — read-only audit deliverable that scopes remaining per-board slices |

Suggested order: **#3761 → #3762** (robust extraction before fleet rollout); **#3763** is a cheap, read-only audit that can run anytime and will scope further board work.

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
| Total open issues | 0 |
| Ready for work (`loom:issue`) | 0 |
| Urgent (`loom:urgent`) | 0 |
| Building (`loom:building`) | 0 |
| Blocked (`loom:blocked`) | 0 |
| Proposals pending approval (`loom:architect`) | 0 (Architect pass in flight to repopulate) |
| Active epics | 0 |

**Assessment:** The backlog is **empty** as of 2026-06-16 — both open issues and open PRs are at zero. Epic #2556 (differential-pair first-class support, Phases 1A–1D) has fully merged, and v0.14.0 shipped to PyPI (demo gallery, renders, board-metrics, and the first independent copper-LVS soundness gate). With the queue clear, an Architect pass was launched to generate the next wave of work, prioritizing: (1) finishing the LVS soundness story (#3742 follow-ups — robust zone-pour extraction and copper-LVS as a first-class manufacturability leg), (2) re-scoping board-parity (#2394) after the recent board 00/04/05/06 DRC/LVS burst, and (3) other high-value epics. Re-run the Guide once those proposals are filed and approved.

# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-02-26*

---

## Urgent (Top Priority)

| # | Issue | Tier | Status |
|---|-------|------|--------|
| 1 | **#1192** Implement full pipeline strategy in routing orchestrator | goal-advancing | `loom:issue` — ready for Builder |
| 2 | **#1193** Add route-auto MCP tool and CLI command for orchestrator-based routing | goal-advancing | `loom:issue` — ready for Builder |
| 3 | **#1179** label-external-issues workflow fails on every push event | maintenance | `loom:issue` — ready for Builder |

## Ready for Work (`loom:issue`)

| # | Issue | Tier |
|---|-------|------|
| 1 | **#1192** Implement full pipeline strategy in routing orchestrator | goal-advancing |
| 2 | **#1193** Add route-auto MCP tool and CLI command for orchestrator-based routing | goal-advancing |
| 3 | **#1179** label-external-issues workflow fails on every push event | maintenance |
| 4 | **#1178** Add mypy configuration to pyproject.toml for v0.11.0 type safety | goal-supporting |
| 5 | **#1177** Remove unused generate_grid_stress_test function (92 lines) | maintenance |
| 6 | **#1181** Purge kicad footprints/symbols which are not referenced | maintenance |

> 6 issues approved for work. Healthy pipeline with clear priority ordering.

## Proposals Awaiting Human Approval

### Curated Issues (`loom:curated`)
- **#1192** Implement full pipeline strategy in routing orchestrator — *tier:goal-advancing* (also has `loom:issue`)

### External Contributions (`loom:triage`)
- **#1181** Purge kicad footprints/symbols which are not referenced — *tier:maintenance* (also has `loom:issue`)

## Backlog Health

| Metric | Value |
|--------|-------|
| Total open issues | 6 |
| Ready for work (`loom:issue`) | 6 |
| Urgent (`loom:urgent`) | 3 |
| Building (`loom:building`) | 0 |
| Blocked (`loom:blocked`) | 0 |
| Proposals pending approval | 0 |
| Active epics | 0 |

**Assessment:** Healthy backlog with 6 approved issues. Routing orchestrator work (#1192, #1193) is the critical path — completing the epic started in #1141. Dependencies for #1192 (PRs #1194 and #1195) are already merged. Good mix of goal-advancing and maintenance work.

## Version Roadmap

### v0.11.0 — Typed Interfaces & Constraints (Next)
- #1178 mypy configuration *(ready, goal-supporting)*
- #1192 Full pipeline routing strategy *(urgent, goal-advancing)*
- #1193 route-auto MCP tool + CLI *(urgent, goal-advancing)*

### Maintenance
- #1179 Fix broken CI workflow *(urgent)*
- #1177 Remove unused function *(ready)*
- #1181 Library purge tool *(external contribution, ready)*

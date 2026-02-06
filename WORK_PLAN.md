# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-02-05*

---

## Urgent (Top Priority)

| # | Issue | Tier | Status |
|---|-------|------|--------|
| 1 | **#1156** Add GitHub Actions CI pipeline | goal-supporting | `loom:issue` — ready for Builder |

## Ready for Work (`loom:issue`)

| # | Issue | Tier |
|---|-------|------|
| 1 | **#1156** Add GitHub Actions CI pipeline for automated testing, linting, and type checking | goal-supporting |

> Only 1 issue is approved for work. The pipeline is starved — proposals below need human promotion.

## Recently Unblocked (Awaiting Approval)

| # | Issue | Tier | Previous State |
|---|-------|------|----------------|
| 1 | **#1132** Router real-world validation — end-to-end signal routing on actual boards | goal-supporting | Unblocked 2026-02-05 |
| 2 | **#1141** [force-mode] Follow-on: Work identified in PR #1140 | goal-advancing | Unblocked 2026-02-05 |

## Still Blocked

| # | Issue | Tier | Blocked By |
|---|-------|------|------------|
| 1 | **#1133** Fab-aware DRC violation categorization (JLCPCB/OSHPark profiles) | goal-supporting | Needs #1132 validation first |
| 2 | **#1134** Post-stitch workflow — automatic zone fill and DRC validation | goal-supporting | Needs #1132, #1133 |

## Proposals Awaiting Human Approval

### Architect Proposals (`loom:architect`)
- **#1169** Add typed interface ports to circuit blocks for type-checked connections (v0.11.0) — *tier:goal-advancing*

### Hermit Proposals (`loom:hermit`)
- **#1171** Clean up 8 unused exports from `__all__` declarations across 7 modules — *tier:maintenance*

### Curated Issues (`loom:curated`)
- **#1166** Implement Interval type system for parametric constraints (v0.11.0 foundation) — *tier:goal-advancing*
- **#1158** 12 test failures on main branch (commit dedb9b8) — *tier:goal-supporting*

## Other Open Issues (No Loom Label)

| # | Issue | Tier | Notes |
|---|-------|------|-------|
| 1 | **#1149** MCP server fails: kct binary not found | goal-supporting | Needs triage |
| 2 | **#1155** Remove 5 unused classes from exceptions.py (~540 LOC) | maintenance | Needs triage |
| 3 | **#1173** 30 test failures on main (commit 35e7c78) | — | Tagged `loom:auditor` |

## Backlog Health

| Metric | Value |
|--------|-------|
| Total open issues | 12 |
| Ready for work (`loom:issue`) | 1 |
| Urgent (`loom:urgent`) | 1 |
| Building (`loom:building`) | 0 |
| Blocked (`loom:blocked`) | 2 |
| Proposals pending approval | 4 |
| Active epics | 0 |

**Assessment:** The backlog is critically low on approved work. Only 1 issue has `loom:issue`. Multiple curated and architect proposals await human promotion to keep Builders busy.

## Version Roadmap

### v0.11.0 — Typed Interfaces & Constraints (Next)
- #1169 Typed interface ports *(architect proposal)*
- #1166 Interval type system *(curated)*

### Pre-v0.11.0 — Foundation Work
- #1156 CI pipeline *(urgent, ready)*
- #1173 / #1158 Fix test failures *(needs approval)*
- #1132 Router validation *(unblocked, needs approval)*

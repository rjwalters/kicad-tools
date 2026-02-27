# Work Log

Chronological record of merged PRs and closed issues. Maintained by the Guide triage agent.

---

### 2026-02-27

- **PR #1198**: Install Loom 0.3.0

### 2026-02-17

- **PR #1197**: Fix 8 test failures from API and coordinate changes
- **Issue #1173** (closed): 30 test failures on main branch (commit 35e7c78) — regression from 12

### 2026-02-16

- **PR #1195**: feat(router): wire via conflict resolution and clearance repair into orchestrator
- **PR #1194**: feat(router): wire real router strategies into orchestrator placeholders
- **PR #1189**: Remove 8 unused exports from __all__ declarations
- **PR #1188**: feat(stitch): add --drc flag for post-stitch DRC validation
- **PR #1187**: feat(drc): add fab-aware severity reclassification to kicad-drc-summary
- **PR #1186**: feat(mcp): add kct mcp setup command to auto-configure MCP clients
- **PR #1185**: Install Loom 0.2.3
- **PR #1184**: Add typed interface ports for type-checked connections
- **PR #1175**: docs: Guide document maintenance — initialize WORK_LOG and WORK_PLAN
- **Issue #1191** (closed): Wire via conflict resolution and clearance repair into orchestrator
- **Issue #1190** (closed): Wire real router strategies into orchestrator placeholders
- **Issue #1171** (closed): Clean up 8 unused exports from __all__ declarations across 7 modules
- **Issue #1169** (closed): Add typed interface ports to circuit blocks for type-checked connections (v0.11.0)
- **Issue #1166** (closed): Implement Interval type system for parametric constraints (v0.11.0 foundation)
- **Issue #1156** (closed): Add GitHub Actions CI pipeline for automated testing, linting, and type checking
- **Issue #1155** (closed): Remove 5 unused classes and 2 unused functions from exceptions.py (~540 LOC)
- **Issue #1149** (closed): MCP server fails: kct binary not found at ~/.local/bin/kct
- **Issue #1141** (closed): [force-mode] Follow-on: Work identified in PR #1140

### 2026-02-06

- **PR #1183**: Install Loom 0.2.0
- **PR #1182**: Install Loom 0.2.0 (c0154d2)
- **PR #1180**: Install Loom 0.2.0 (18b26ef)
- **PR #1176**: Install Loom 0.2.0 (c74dff3)
- **Issue #1158** (closed): 12 test failures on main branch (commit dedb9b8)

### 2026-02-06

- **PR #1174**: Install Loom 0.2.0 (130fa9f)
- **PR #1168**: Install Loom 0.2.0 (c86aecd)

### 2026-02-05

- **PR #1164**: Install Loom 0.2.0 (ddbfafe)

### 2026-02-01

- **PR #1162**: Install Loom 0.2.0 (61bd6b6)
- **PR #1161**: Install Loom 0.2.0 (35c0b10)
- **PR #1157**: feat(router): adaptive grid routing — fine grid near pads, coarse grid in channels
- **Issue #1135** (closed): Feature: adaptive grid routing — fine grid near pads, coarse grid in channels

### 2026-01-31

- **PR #1160**: Install Loom 0.2.0 (00d61d5)

### 2026-01-30

- **PR #1159**: Install Loom 0.2.0 (8c18fd4)
- **PR #1154**: Install Loom 0.2.0 (903ad26)
- **PR #1153**: Issue #1139: Auto-recovered PR
- **PR #1152**: Install Loom 0.2.0 (11b9090)
- **PR #1151**: Install Loom 0.2.0 (a29d435)
- **PR #1150**: feat(stitch): add dog-leg routing for fine-pitch components
- **Issue #1139** (closed): Refactor Autorouter: Extract 89 methods into focused strategy classes
- **Issue #1130** (closed): Feature: stitch extended placement for fine-pitch components (dog-leg routing)

### 2026-01-28

- **PR #1148**: Install Loom 0.2.0 (5f55541)

### 2026-01-27

- **PR #1147**: chore: refresh uv.lock and gitignore loom runtime state files
- **PR #1146**: Install Loom 0.2.0 (4bd83a8)
- **PR #1145**: Remove Loom orchestration framework
- **PR #1144**: fix(stitch): Copy .kicad_pro file alongside PCB output for DRC compatibility
- **PR #1143**: fix(stitch): add pad clearance checking to prevent shorts with other footprints
- **PR #1142**: fix(stitch): Check trace path clearance to prevent shorts from pad-to-via connections
- **PR #1140**: Add unified routing orchestration layer to coordinate multi-strategy routing
- **PR #1137**: Install Loom 0.2.0 (87e2104)
- **PR #1136**: Remove Loom orchestration framework
- **Issue #1128** (closed): Bug: stitch clearance check ignores pads from other footprints, causing shorts
- **Issue #1131** (closed): Bug: stitch -o output file causes phantom DRC violations (missing .kicad_pro)
- **Issue #1129** (closed): Bug: stitch connecting trace path not checked for clearance, causing shorts
- **Issue #1138** (closed): Add unified routing orchestration layer to coordinate multi-strategy routing

### 2026-01-26

- **PR #1127**: feat(router): Add hierarchical routing foundation with global router (#1095)
- **PR #1126**: refactor(cli): Add command protocol and migrate config command (#1123)
- **PR #1125**: feat(router): improve C++ backend discoverability and performance warnings
- **PR #1124**: feat(router): Add via conflict management for blocked pad access
- **PR #1121**: feat(drc): Add trace clearance repair tool (nudge traces to fix DRC violations)
- **PR #1120**: feat(router): Add sub-grid routing for fine-pitch components
- **PR #1119**: fix(stitch): Check clearance against other-net copper before placing vias
- **Issue #1095** (closed): Architectural Proposal: Hierarchical Routing with Global-to-Detailed Flow
- **Issue #1123** (closed): Architectural Proposal: Eliminate CLI Dual-Parsing Anti-Pattern
- **Issue #1122** (closed): Clean up 18 stale worktrees consuming 3.2 GB of disk space
- **Issue #1112** (closed): Feature: Native C++ router backend for practical performance
- **Issue #1111** (closed): Feature: Router should manage via conflicts
- **Issue #1110** (closed): Feature: Trace clearance repair tool
- **Issue #1109** (closed): Feature: Router support for fine-pitch components (sub-grid routing)
- **Issue #1108** (closed): Bug: route command generates invalid PCB files
- **Issue #1107** (closed): Bug: fix-vias doesn't detect annular ring violations
- **Issue #1106** (closed): Bug: kicad-pcb-stitch doesn't connect vias to pads with traces
- **Issue #1105** (closed): Bug: kicad-pcb-stitch places vias that short different nets
- **Issue #1104** (closed): Bug: kicad-pcb-stitch adds invalid via format with rotation parameter

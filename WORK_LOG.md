# Work Log

Chronological record of merged PRs and closed issues. Maintained by the Guide triage agent.

---

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

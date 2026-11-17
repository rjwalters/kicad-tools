# Work Log Archive — 2026-02

Sections moved byte-for-byte from [WORK_LOG.md](../WORK_LOG.md) by `loom-daemon work-docs archive`.

### 2026-02-27

- **PR #1237**: refactor: remove 4 dead methods from router/core.py and spec/parser.py
- **PR #1236**: feat(mcp): add `optimize_placement` and `evaluate_placement` tools
- **PR #1235**: feat(placement): add Bayesian Optimization strategy using Ax/BoTorch
- **PR #1234**: feat(cli): add `optimize-placement` command for CMA-ES board optimization
- **PR #1233**: feat(placement): add multi-fidelity evaluation pipeline
- **PR #1231**: feat(placement): add netlist graph analysis for placement priors
- **PR #1230**: feat(placement): add optimization progress visualization module
- **PR #1229**: feat(placement): add benchmark test boards for placement optimizer validation
- **PR #1228**: feat(placement): add DRC clearance checker for courtyard and pad spacing
- **PR #1227**: feat(placement): add PlacementStrategy ABC and CMA-ES optimizer
- **PR #1226**: feat(placement): add force-directed and random seed placement heuristics
- **PR #1225**: feat(placement): add overlap and boundary violation geometry detectors
- **PR #1224**: feat(placement): add HPWL wirelength estimator using transformed pad coordinates
- **PR #1223**: feat(placement): define PlacementVector type and placement decode/encode
- **PR #1222**: feat(library): add unused symbol/footprint detection for project libraries
- **PR #1220**: feat: add route-auto MCP tool and CLI command for orchestrator-based routing
- **PR #1219**: feat: implement weighted cost function aggregator for placement scoring
- **PR #1218**: feat: implement full pipeline strategy in routing orchestrator
- **PR #1217**: fix: remove push trigger from label-external-issues workflow
- **PR #1216**: feat: add mypy configuration to pyproject.toml for v0.11.0 type safety
- **PR #1215**: Remove unused `generate_grid_stress_test` function
- **PR #1200**: docs: Guide document maintenance update
- **PR #1198**: Install Loom 0.3.0
- **Issue #1232** (closed): Remove 4 dead methods/functions from router/core.py and spec/parser.py
- **Issue #1214** (closed): Add MCP tool for agent-driven placement optimization
- **Issue #1213** (closed): Create benchmark test boards for placement optimizer validation
- **Issue #1212** (closed): Add placement optimization progress visualization
- **Issue #1211** (closed): Add netlist graph analysis for placement priors
- **Issue #1210** (closed): Add multi-fidelity evaluation pipeline for placement scoring
- **Issue #1209** (closed): Add Bayesian Optimization placement strategy (Ax/BoTorch)
- **Issue #1208** (closed): Add `optimize-placement` CLI command
- **Issue #1207** (closed): Add initial placement heuristic (force-directed seed)
- **Issue #1206** (closed): Implement PlacementStrategy ABC and CMA-ES optimizer
- **Issue #1205** (closed): Implement weighted cost function aggregator for placement scoring
- **Issue #1204** (closed): Implement placement DRC clearance checker
- **Issue #1203** (closed): Implement component overlap and board boundary violation detectors
- **Issue #1202** (closed): Implement HPWL wirelength estimator for placement scoring
- **Issue #1201** (closed): Define PlacementVector type and placement decode/encode
- **Issue #1199** (closed): Global Optimization Framework for PCB Component Placement and Routing
- **Issue #1193** (closed): Add route-auto MCP tool and CLI command for orchestrator-based routing
- **Issue #1192** (closed): Implement full pipeline strategy in routing orchestrator
- **Issue #1181** (closed): Purge kicad footprints/symbols which are not referenced
- **Issue #1179** (closed): label-external-issues workflow fails on every push event
- **Issue #1178** (closed): Add mypy configuration to pyproject.toml for v0.11.0 type safety
- **Issue #1177** (closed): Remove unused `generate_grid_stress_test` function (92 lines)

### 2026-02-17

- **PR #1197**: Fix 8 test failures from API and coordinate changes
- **Issue #1173** (closed): 30 test failures on main branch (commit 35e7c78) — regression from 12

### 2026-02-16

- **PR #1195**: feat(router): wire via conflict resolution and clearance repair into orchestrator
- **PR #1194**: feat(router): wire real router strategies into orchestrator placeholders
- **PR #1189**: Remove 8 unused exports from `__all__` declarations
- **PR #1188**: feat(stitch): add `--drc` flag for post-stitch DRC validation
- **PR #1187**: feat(drc): add fab-aware severity reclassification to `kicad-drc-summary`
- **PR #1186**: feat(mcp): add `kct mcp setup` command to auto-configure MCP clients
- **PR #1185**: Install Loom 0.2.3
- **PR #1184**: Add typed interface ports for type-checked connections
- **PR #1175**: docs: Guide document maintenance — initialize WORK_LOG and WORK_PLAN
- **Issue #1191** (closed): Wire via conflict resolution and clearance repair into orchestrator
- **Issue #1190** (closed): Wire real router strategies into orchestrator placeholders
- **Issue #1171** (closed): Clean up 8 unused exports from `__all__` declarations across 7 modules
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


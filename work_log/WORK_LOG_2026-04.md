# Work Log Archive — 2026-04

Sections moved byte-for-byte from [WORK_LOG.md](../WORK_LOG.md) by `loom-daemon work-docs archive`.

### 2026-04-30

- **PR #2377**: refactor: consolidate duplicated geometry primitives into `core.geometry` (#2349)
- **PR #2375**: feat(pcb): add panelization engine with tab and mousebite generation
- **PR #2374**: docs(blocks): add composition operator examples (voltage divider, filter networks)
- **PR #2373**: fix(router): add `extra_goal_cells` parameter to `CppPathfinder.route()`
- **PR #2357**: feat(router): add stochastic cost perturbation to escape local minima
- **PR #2369**: feat(drc): add centralized DRU generator with condition expressions and full rule coverage
- **PR #2372**: fix(router): use auto-derived `fine_pitch_clearance` for SSOP escape routing
- **PR #2371**: fix(router): aggregate sub-grid escape failures per-package at WARNING level
- **PR #2370**: feat(router): add PullTight post-processing for trace optimization
- **PR #2368**: fix(router): use connectivity-aware counting in two-phase routing summary
- **PR #2367**: feat(footprint): add compressed footprint DSL for LLM-friendly generation
- **PR #2366**: feat(pcb): add Shapely-based BoardGeometry engine for board outline operations
- **PR #2364**: feat(report): add interactive HTML reports with Canvas 2D PCB viewer
- **PR #2363**: feat(ipc): KiCad IPC API client for live instance interaction
- **PR #2362**: feat(validate): add DRC/ERC violation filter engine with TOML config
- **PR #2361**: feat(blocks): add algebraic composition operators for circuit blocks
- **PR #2360**: feat(library): add composition-based part model with parametric search
- **PR #2359**: feat(router): add consolidated geometry module and vector collision checker
- **PR #2356**: feat(export): add unified manufacturer preset system with rotation corrections
- **PR #2355**: feat(router): cache solved routing sub-problems for recurring patterns
- **PR #2354**: feat(router): add EMA smoothing, exponential cost, congestion auto-tune, and hotset-only mode
- **PR #2353**: feat(router): clearance-compensated spatial indexing (#2335)

### 2026-04-29

- **PR #2332**: fix(router): boost net priority for off-grid pads and fix Steiner pad ref
- **PR #2331**: feat(router): inject off-grid pad positions as waypoint nodes in A* search
- **PR #2327**: fix(router): cap via transition cost and skip plane layers in via check
- **PR 38ee62a2**: fix(test): update `_get_net_priority` unpacking to match 6-tuple return
- **PR #2326**: fix(router): resolve `--iterations` fallback for two-phase routing mode
- **PR #2323**: fix(router): add segment-to-pad clearance checks in escape routing
- **PR #2322**: fix(router): add early-stop to two-phase loop when overflow regresses
- **PR #2321**: fix(cli): wire `--per-net-timeout` and `--timeout` flags to route subcommand
- **PR #2320**: feat(router): emit per-net progress during two-phase rip-up reroute iterations
- **PR #2311**: feat(router): add `--two-phase-iterations` CLI flag for configurable rip-up loops
- **PR #2314**: fix(router): make escape routes rip-up eligible and add sub-grid prepass to escape path
- **PR #2313**: fix(router): add overflow-tolerant collision checking to preserve routes during optimization
- **PR #2296**: feat(benchmark): register chorus-test-revA as HARD benchmark case with routing results
- **PR #2315**: perf(router): add incremental Steiner target-set expansion for multi-terminal nets
- **PR #2309**: feat(router): make corridor penalty decay rate and floor configurable
- **PR #2312**: perf(router): add per-net rip-up stall filtering and tighten low-overflow early termination
- **PR #2310**: feat(router): track best routing state across two-phase iterations

### 2026-04-28

- **PR #2302**: feat(router): integrate two-phase global routing into escape routing path (#2301)
- **PR #2300**: fix(router): limit full-reorder fallback to once per iteration
- **PR #2298**: fix(router): enable neighborhood rip-up in standard routing path
- **PR #2293**: fix(router): read corridor_penalty from `DesignRules.cost_corridor_deviation`
- **PR #2291**: feat(router): wire corridor cost into A* expansion for global routing guidance
- **PR #2290**: feat(router): wire congestion estimator through NegotiatedRouter to `build_rsmt`
- **PR #2286**: feat(router): add neighborhood rip-up with relaxed A* blocker detection
- **PR #2285**: feat(router): add MULTI_ROW_CONNECTOR escape with row-aware layer fanout
- **PR #2284**: feat(router): add RUDY pre-route congestion estimator for net ordering
- **PR #2283**: feat(router): penalize over-utilized layers during A* search
- **PR #2282**: feat(router): add tile-based global routing with geometry capacity and negotiated iteration
- **PR #2280**: feat(router): add RSMT decomposition via Hanan grid and 1-Steiner insertion
- **PR #2281**: refactor(router): centralize cleanup-stats-sexp sequence in `_finalize_routes` helper
- **PR #2272**: feat(export): add Specctra DSN export and SES import for Freerouting
- **PR #2271**: fix(router): add connector escape strategy, stall recovery, and inner-layer cost application
- **PR #2270**: feat(router): add output connectivity verification after PCB write
- **PR #2269**: fix(router): run `cleanup_artifacts` before `get_statistics` in CLI route flow
- **PR #2268**: fix(router): guard escape strategies against zero-overflow triggering
- **PR #2267**: fix(sexp): skip quoting for numeric-looking strings in `_needs_quoting()`
- **Release**: v0.13.0 (commit c6ac27e8)

### 2026-04-27

- **PR #2260**: fix(router): make `cleanup_artifacts` connectivity-aware to preserve valid routes
- **PR #2258**: feat(pcb): add pin-to-pad mapping for Python fallback netlist path
- **PR #2257**: feat(router): validate pad-to-pad connectivity and suppress false success banners
- **PR #2256**: feat(pcb): add `--check-connectivity` flag to `pcb nets` command
- **PR #2255**: fix(sch): filter PWR_FLAG from net name registration in wire graph
- **PR #2250**: feat(pcb): run pad net assignment unconditionally in `sync-netlist`
- **PR #2249**: fix(sch): detect sub-mm dangling wire stubs in `cleanup-wires`
- **PR #2248**: feat(sch): adjust wire endpoints when symbol pin spacing differs on replace
- **PR #2247**: feat(pcb): add `net-audit` command to detect stale/duplicate net names
- **PR #2246**: feat(pcb): add zone connectivity fields to `pcb nets` JSON output
- **PR #2245**: feat(pcb): detect footprint pin-count mismatches in `sync-netlist`
- **PR be0090a8**: chore(test): refresh routing benchmark results

### 2026-04-26

- **PR #2238**: fix(sch): use wire-graph BFS for pin connectivity checks
- **PR #2237**: feat(sch): add `connected` field and synthetic `_local_N` nets to pin-map output
- **PR #2236**: feat(erc): re-attribute symbol and label violations to correct sheets in hierarchical designs
- **PR #2235**: feat(sch): use embedded `lib_symbols` when `--lib` not provided in `sch pins`
- **PR #2234**: feat(validate): add `value_consistency` check for mixed capacitor voltage formatting
- **PR #2233**: fix(review): use pre-computed pin-map data instead of LLM coordinate math in `review-schematic`
- **PR #2232**: fix(sync): use `on_board` instead of `is_virtual` to filter `sync-netlist` components
- **PR #2231**: feat(export): add seeed manufacturer profile to export command
- **PR #2230**: fix(erc): handle `unconnected_wire_endpoint` and `wire_dangling` in `fix-erc`
- **PR #2229**: fix(pipeline): pass manufacturer via specs to stitch subprocess
- **PR #2218**: feat(drc): add footprint nudge for pad-pad clearance violations in `repair-clearance`
- **PR #2215**: feat(sync): route value updates through PCB API, add orphan removal
- **PR #2214**: fix(route): filter exported failed nets to multi-pad routing candidates
- **PR #2217**: fix(erc): filter phantom `wire_dangling` violations with no matching schematic coordinates
- **PR #2213**: feat(sch): trace net names through `Device:NetTie` symbols in pin-map
- **PR #2216**: fix(sch): use wire-graph BFS for pin connectivity checks
- **PR #2212**: fix(sync): use `on_board` flag instead of `is_virtual`/`dnp` to filter sync components

### 2026-04-25

- **PR #2203**: fix(sch): use strict electrical connectivity for stub detection in `cleanup-wires`
- **PR #2202**: feat(sch): use embedded `lib_symbols` for connection checks, add hierarchy support
- **PR #2201**: feat(sch): traverse full hierarchy in pin-map command
- **PR #2200**: feat(sch): enrich `show-pins` output with name, type, net, position fields
- **PR #2194**: fix(sch): detect mid-segment stubs, tighten quantization, add collinear overlap detection
- **PR #2193**: fix(drc): resolve net names in DRC violations from net numbers
- **PR #2192**: fix(erc): expand wire-dangling violation re-attribution coverage
- **PR #2186**: feat(sch): add `move-component` command to reposition symbols
- **PR #2185**: feat(validate): flag unnecessary footprint variety for same-value passives

### 2026-04-24

- **PR #2183**: feat(sch): add `reconnect-pin` command for atomic pin-to-net reassignment
- **PR #2182**: fix(validate): distinguish bypass caps from filter caps in channel symmetry check
- **PR #2181**: feat(sch): add `set-symbol-property` command for boolean flags
- **PR #2180**: feat(sch): add `set-reference` command to rename reference designators
- **PR #2179**: feat(cleanup-wires): detect and remove sub-mm dangling wire stubs
- **PR #2178**: feat(pcb): add board dimensions to summary output
- **PR #2177**: fix(report): update stale test assertions to match YAML front matter template
- **PR 8627bd76**: feat(report): add stackup section, fix narrative loading, collapse single-pad nets
- **PR #2167**: fix(sch-validate): warn on unresolved power pins and add power-symbol test fixtures

### 2026-04-23

- **PR #2155–#2166** (high volume): silkscreen repair improvements, schematic validation expansion, MCP enhancements
- Themes (specific PR-to-title bindings dropped — see `git log` for verified attributions):
  - schematic editing: `connect-net` / `disconnect-pin` / `add-symbol` / `add-wire` commands for net editing and programmatic schematic edits
  - report integration: review-schematic narrative generation and integration into design report
  - validation: channel symmetry check for differential / multi-channel designs

### 2026-04-22

- **PR #2140–#2154** (high volume): board generation improvements (boards 02, 03), router stability fixes, BOM enrichment
- Themes (specific PR-to-title bindings dropped — see `git log` for verified attributions):
  - boards/03 (USB joystick) and boards/02 (charlieplex) PCB generation with placement optimizer
  - audit pipeline: per-section ACTION ITEMS aggregator
  - silkscreen repair: full repair pass (line widths, text heights, overlap) wired into pipeline

### 2026-04-21

- **PR #2120–#2139** (high volume): export pipeline (BOM, CPL, gerber), preflight expansion, manufacturer presets
- Themes (specific PR-to-title bindings dropped — see `git log` for verified attributions):
  - export pipeline: gerber export step in manufacturing pipeline
  - rotation correction tables for JLCPCB and PCBWay CPL
  - preflight: expanded check coverage to silkscreen, courtyard, hole sizes

### 2026-04-20

- **PR #2090–#2119** (high volume): router cache versioning, escape routing, pipeline integration
- Themes (specific PR-to-title bindings dropped — see `git log` for verified attributions):
  - audit: ROUTING section with completion / DRC / time-to-route metrics
  - pipeline: `--auto-fix` cascade for ERC, DRC, vias, silkscreen
  - router: cache routing solutions keyed by board geometry hash

### 2026-04-19

- **PR #2030–#2089** (very high volume, 58 commits): cache versioning, MCP expansion, board generation, audit reorg
- **PR #1649**: fix(cache): wire `CACHE_VERSION` into cache key computation for partial and full routes
- Themes (specific PR-to-title bindings dropped — see `git log` for verified attributions):
  - audit: split into preflight / drc / erc / report sub-steps
  - mcp: `analyze_routing` and `analyze_placement` tools

### 2026-04-18

- **PR #2005–#2029**: routing strategy retry loop, fix-drc enhancements, schematic editing API
- Themes (specific PR-to-title bindings dropped — see `git log` for verified attributions):
  - router: automatic strategy retry loop on routing failure
  - drc: multi-segment cluster rerouting for grouped via violations

### 2026-04-17

- **PR #1990–#2004**: pipeline scheduler, ERC violation handlers, fix-erc command
- Themes (specific PR-to-title bindings dropped — see `git log` for verified attributions):
  - erc: `fix-erc` command to auto-fix common ERC violations
  - pipeline: FIX_ERC step for automatic ERC remediation

### 2026-04-16

- **PR #1976–#1989**: report generator (PDF/HTML/Markdown), screenshot rendering, action items
- Themes (specific PR-to-title bindings dropped — see `git log` for verified attributions):
  - report: professional PDF layout with cover block, PCB grid, page controls
  - report: `ReportFigureGenerator` wired into `report generate` CLI

### 2026-04-15

- **PR #1940–#1975** (high volume, 33 commits): manufacturing export pipeline, JLCPCB integration, design report v1
- **Release**: v0.12.0 (commit e19e38aa)
- Themes (specific PR-to-title bindings dropped — see `git log` for verified attributions):
  - report: Jinja2 Markdown report generator with CLI
  - report: HTML/PDF renderers with styled CSS template
  - cli: pipeline subcommand for end-to-end PCB repair workflow (actual merge: #1307)
  - export: auto-match LCSC part numbers during JLCPCB BOM export
  - export: `kct export` command for manufacturing package generation
- **PR 5e9bbb2d**: feat: add `/release` skill for guided semver release process

### 2026-04-14

- **PR #1900–#1939** (high volume, 34 commits): fix-drc command, audit gating, ERC integration
- Themes (specific PR-to-title bindings dropped — see `git log` for verified attributions):
  - drc: `fix-drc` command for automated DRC violation repair (actual merge: #1262)
  - audit: READY-verdict gating across DRC/ERC/connectivity
  - zones: zone fill CLI command delegating to `kicad-cli` (actual merge: #1260)

### 2026-04-13

- **PR #1860–#1899** (high volume, 31 commits): C++ router backend, multi-resolution routing, MCP analysis tools
- Themes (specific PR-to-title bindings dropped — see `git log` for verified attributions):
  - router: multi-resolution routing with fine-grid fallback (actual merge: #1254)
  - router: R-tree spatial indexing for segment clearance queries (actual merge: #1253)
  - mcp: `screenshot_board` and `screenshot_schematic` tools (actual merge: #1247)

### 2026-04-12

- **PR #1850–#1859**: orchestrator wiring, type checking, force-mode follow-ups
- **Release**: v0.11.0 (commit aa9dc545)
- **Commit 117f58e3**: ci: add PyPI publish workflow and speed up CI tests (direct push, no PR)

### 2026-04-11

- **PR #1840–#1849**: initial commits resuming Loom-driven development after February pause
- Resumed orchestration cadence after several weeks of dormancy.


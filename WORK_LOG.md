# Work Log

Chronological record of merged PRs and closed issues. Maintained by the Guide triage agent.

---

### 2026-07-20

- **Release**: v0.18.0 — feature release cutting a 15-commit run since v0.17.0, focused on **high-voltage / analog manufacturing gates** for zero-GUI agent flows. Two new inspection capabilities: **`kct creepage`** (HV surface-path/creepage audit — per-pair slot-aware census #4334; required values derived from **IEC 60664-1 / 62368-1** tables via `--working-voltage`/`--pollution-degree`/`--material-group`/`--standard` #4332/#4338; HV/isolation section + manufacturing-readiness gate in `kct audit` #4333/#4341) and **`kct analyze current-sense`** (analog layout lint — sense↔high-current parallel-run #4335, sense-loop area #4337, Kelvin-tap integrity #4331). Plus a real **`--nets`** route filter (#4325), **`pcb reinforce` multi-branch anchoring** (#4323), and a `route --layers auto` inner-layer advisory (#4315). Safety-relevant `kct check` fixes: `--net-class-map` now enforces `target_ampacity` (#4324) and sources copper weight from the declared stackup (#4326); `analyze current-sense` evaluates FAIL against all blockers, not just nearest (#4339); creepage clearance-table label corrected to **Case A (inhomogeneous)** per IEC 60664-1 — verified against a controlled copy of the standard (#4343). The two large capabilities were shipped as **phased MVPs (1→3) with tracked follow-up issues**. One parallel-merge collision broke main mid-sweep (#4337/#4339 both mutating `CurrentSenseResult.to_dict`, green individually) and was recovered; two Loom process issues filed upstream (rjwalters/loom#3647, #3648). Version bumped 0.17.0 → 0.18.0; hygiene pass folded in (Loom 0.10.9→0.11.0 + Repo Skills 0.4.1→0.4.3 vendored bump #4344, ~132 MB caches tidied, 15 stale branches pruned, local main synced). All 9 CI gate jobs green on main pre-tag. Tag `v0.18.0` → `publish.yml` (`uv build && uv publish`) to PyPI.

### 2026-07-17

- **Release**: v0.17.0 — feature release cutting a 39-commit sweep since v0.16.0. Headline: an **experimental alternative routing substrate** — the adaptive octilinear **lattice** engine (`--route-engine lattice`) and a constrained-Delaunay **navmesh/mesh** engine (`--route-engine mesh`), both default-OFF — that routes large mixed-pitch boards the uniform-grid router cannot fit in memory. Validated on softstart rev-C (160×100mm 4-layer): **74/77 signal nets DRC-clean, 0 errors, ~3% of the grid's memory** (the grid produced zero clean nets on this board). The lattice line landed as epic #4267 (P0 spike → P1 poly2tri CDT + navmesh/funnel → P2 negotiation → P2.5 in-corridor lane assignment → P2.6 2.5D via injection → P2.7 octilinear lattice engine → P3 coupled diff-pairs → P4 softstart proof), then hardened via #4280 (loud strategy gate), #4281 (post-pass gating), #4284/#4285 (via-in-pad tier gate), #4291 (hole-to-hole floors), #4293 (oversize-class neck-down escape), #4292 (CLI-tail RSS attribution). Also: `--max-cells` (#4249), analytical `route --dry-run` (#4266), `route-auto --via-drill/--via-diameter` (#4250), settable schematic `in_bom`/`dnp` (#4303), `net-status --why` ranked fix recommender with pin-order-verified reversed-bundle detection (#4261/#4286), and a parts-catalog fix cluster (#4295/#4296/#4297/#4299). board-07 Track A closed at its placement-bound plateau (#4256–#4258); the operator-approved de-reversal experiment (#4253) proved DQ3/DQ4 closable but the fixture's reversed bus is ratified by design. Version bumped 0.16.0 → 0.17.0; CHANGELOG `[0.17.0]` entry from `git log v0.16.0..main` (39 commits). Hygiene pass folded in: Loom 0.10.7→0.10.9, Repo Skills v0.4.0→v0.4.1 (#4312), ~213 MB caches tidied, 35 stale branches / 2 worktrees / 2 stashes pruned. `--route-engine grid` (default) byte-identical to 0.16.0; all 16 CI gate jobs green on the tagged tree. Tag `v0.17.0` → `publish.yml` (`uv build && uv publish`) to PyPI.

### 2026-07-15

- **Release**: v0.16.0 — feature release cutting a ~46-issue sweep since v0.15.1. Region-bounded routing (`--region` on `route`/`route-auto` + boundary stub-terminal detection & reconnection), ampacity-aware net-class min-width via IPC-2221 + ampacity DRC check, copper dedupe (`pcb dedupe` + emission-time), `pcb reinforce` anchor-PTH rows, `pcb padmap` / `sch fix-annotation` / `pcb strip --region` inspection & repair commands, `net-status --strict` real-geometry connectivity, off-board preflight, and hardened datasheet fetching / zone-edit defaults / S-expression quoting / different-net short guards. Pre-tag fleet validation (boards 00–07 against 0.16.0) surfaced and fixed three regressions before shipping: #4226 (junction-dot-gated wire union — fixed the #4157 over-merge that produced false copper-LVS shorts on board-05), #4227 (pad-bbox-fallback courtyard annotation), #4229 (zone-pour plane-pad connectivity). Version bumped 0.15.1 → 0.16.0; all 16 CI gate jobs green on the tagged tree. Tag `v0.16.0` pushed → `publish.yml` (`uv build && uv publish`) to PyPI. Closes release-request #4152 (chorus#24 unblocked via `sch fix-annotation` #4142 + `pcb strip --region` #4147).

### 2026-07-13

- **Release**: v0.15.0 — router feasibility certificates + constructive escape ordering, coupled diff-pair corridor attractors, C++ coupled joint-state A\* port, slack-budget corridor widening + lateral-trace reservation keep-out, hierarchical-schematic LVS, copper-LVS gates wired across boards 01–07, JLCPCB parts stack (offline jlcparts catalog + BYO-key official API tier), /kct:tapeout skill, LCSC/EasyEDA fetch-on-demand + cross-library 3D model resolver tiers, thin-copper sliver / silk-clearance / net-0 bridge DRC rules, and gallery-hardened board fixtures (00–07 at 0 blocking DRC + clean copper-LVS). Version bumped 0.14.0 → 0.15.0; CHANGELOG `[0.15.0]` entry synthesized from `git log v0.14.0..main` (~200 commits), grouped by subsystem. Tag/publish per operator (push `v0.15.0` triggers `publish.yml`).

### 2026-06-16

- **Release**: v0.14.0 (prepared) — demo gallery website (kicad-tools.org), zone-fill foreign-pad clearance fix, PCB `page_fit`, oblique 3D + 2D-SVG renders, `kct render` / `board-metrics` / `pcb page-fit` commands, LVS status in gallery, ERC/LVS/Manifest meta sub-checks for `kct check`. Version bumped to 0.14.0; CHANGELOG `[0.14.0]` entry backfilled from `git log v0.13.0..main` (751 commits). Tag/publish deferred to operator (push `v0.14.0` triggers `publish.yml`).

### 2026-05-08

- **PR #2555**: fix(route): auto-pour preserves INPUT.kicad_pcb when output path differs (closes #2548)
- **PR #2554**: feat(loom): require incremental commits in builder/doctor role contracts (closes #2547)
- **PR #2553**: feat(router): auto-build C++ backend on first use when router_cpp.so is missing (closes #2549)
- **PR #2552**: feat(ci): validate committed `_routed.kicad_pcb` files via `kct check` (closes #2546)
- **Issue #2556** (epic, opened): First-class differential pair support across the routing pipeline (Phase 1 sub-issues #2557–#2560 created)
- Builders began Epic #2556 Phase 1 work (#2557 NetClass field, #2558 diff-pair detection)

### 2026-05-07

- **PR #2551**: fix(boards/05): improve routing via finer grid + auto-layer escalation
- **PR #2545**: fix(boards/04): regenerate routed PCB to clear stale 5-DRC-error state
- **PR #2544**: fix(router): preserve best-of-iterations saved-partial result; iter-1 timeout no longer destroys iter-0 successes (closes #2540)
- **PR #2543**: fix(placement/fixer): respect anchored set on EDGE_CLEARANCE conflicts (closes #2541)
- **PR #2535**: feat(boards/05): add STM32G431K8Tx, complete DRV8301 footprint, wire gate-driver/Hall/sense nets (closes #2532)
- **PR #2539**: fix(placement): plumb `--fixed` through routing-aware path / PlaceRouteOptimizer (closes #2537)
- **PR #2538**: feat(boards/04): add STM32F103C8T6 placement and wire SWD/oscillator/peripherals (closes #2531)
- **PR #2536**: fix(router,boards/03): wire BLOCKED_BY_COMPONENT rip-up into two-phase stall path; align USB pin assignment (closes #2527)

### 2026-05-06

- **PR #2534**: fix(tests): repair pre-existing TestIncrementalSteinerRouting and TestNegotiatedRouterCongestionEstimator failures (closes #2530)
- **PR #2533**: fix(net_class): classify +3V3-style no-decimal voltage names as POWER (closes #2528)
- **PR #2526**: fix(router): escape coverage for TQFP-32 U1 and 2-row USB-C J1 (closes #2513)
- **PR #2525**: feat(check): detect single-pad nets as design defects (closes #2521)
- **PR #2524**: fix(router): stagnation recovery for never-routed nets (closes #2515)
- **PR #2523**: fix(router): wire BLOCKED_BY_COMPONENT rip-up into negotiated strategy (closes #2517)
- **PR #2522**: fix(router): hoist wall-clock timeout into per-net inner loop on negotiated strategy (closes #2518)
- **PR #2520**: fix(router): handle single-pad nets cleanly; clearer C++ backend hint
- **PR #2519**: fix(route,export): fill copper-pour zones so Gerbers contain plane copper (closes #2516)

### 2026-05-05

- **PR a8b06c7b**: fix(preflight): downgrade `pad_grid` from error to warning

### 2026-05-04

- **PR #2507**: test(ci): add kicad-cli round-trip smoke test on every emitted PCB
- **PR #2508**: router_cpp: detect stale `.so` via build version guard (closes #2501)
- **PR #2511**: fix(router): rip up lower-priority siblings on BLOCKED_BY_COMPONENT in `route_all`
- **PR #2512**: fix(router): converge USB-pitch diff pairs in CoupledPathfinder
- **PR #2510**: fix(sexp): preserve quoted-string semantics on round-trip
- **PR #2509**: feat(router): add pad-grid preflight check before routing
- **PR #2506**: fix(router): exclude skipped pour/CC nets from routing summary failures
- **PR #2505**: feat(build): smoke-check emitted PCB after each write step (#2495)
- **PR #2504**: feat(silkscreen): track marking identity in `*.kct.json` sidecar
- **PR #2503**: fix(build): pass manufacturer edge_clearance to auto-pour zone step
- **PR #2502**: fix(build-cpp): use uv venv interpreter and pass `Python_EXECUTABLE`
- **PR 30256d40**: fix: emit kicad-cli-compatible PCBs (generator_version + drop kct_marking)
- **PR #2489**: feat(router): bump connector-siblings of prerouted nets in negotiated ordering
- **PR #2488**: fix(router): dedupe RSMT sub-route vias and invalidate cpp stored vias on rip-up
- **PR #2486**: fix(router): add via-anchor guard to chain-aware DRC nudge
- **PR #2485**: fix(router): thread per-call spacing in CoupledPathfinder.route_coupled
- **PR #2491**: Install Loom 0.7.1 orchestration framework

### 2026-05-03

- **PR #2479**: fix(router): preserve chain connectivity in DRC nudge to keep PHASE_B 4/4
- **PR #2477**: fix(router): surface via-vs-via failure reason for targeted rip-up (#2476)
- **PR #2478**: feat(router): extend CoupledPathfinder to N-pad differential pairs
- **PR #2474**: feat(router): wire `--differential-pairs` through modern CLI with coupled-only pre-pass
- **PR #2472**: fix(router): align via blocking with validator's geometric clearance
- **PR #2471**: fix(router,drc): add HIGH_CURRENT_SIGNAL net class and pad-pad clearance epsilon
- **PR #2470**: fix(router): enforce GA `--timeout` and flush per-generation progress lines (#2467)
- **PR #2468**: fix(cli): thread edge_clearance through user-explicit `--power-nets` path
- **PR #2469**: fix(auto-pour): scan full file with balanced-paren for multi-line zones
- **PR #2461**: fix(zones): add pure-Python rect inset fallback and reinset existing un-inset zones
- **PR #2460**: test(router): add strategy dispatch coverage for adaptive grid routing
- **PR #2459**: fix(router): serialize `_pour_nets_without_zones` for parallel workers
- **PR #2458**: feat(router): add same-component pad clearance relaxation for tight-pitch escape routing (#2452)
- **PR #2455**: feat(router): add DRC violation penalty to Monte Carlo solution scoring (#2450)
- **PR #2457**: feat(router): add net ordering tier promotion for long-span nets (#2451)
- **PR #2453**: fix(cli): wire evolutionary and monte-carlo strategies through adaptive grid router
- **PR #2449**: feat(router): add resumable A* search to C++ pathfinder for validation retry
- **PR #2448**: feat(router): add per-net Python fallback when C++ pathfinder fails
- **PR #2444**: feat(optim): add RoutingEvaluator to replace spacing proxy in placement fitness
- **PR #2446**: feat(router): add evolutionary routing optimizer with GA-style operators
- **PR #2443**: perf(router): port geometric validation to C++ for faster route exploration
- **PR #2442**: feat(router): add DRC violation avoidance cost feedback to C++ pathfinder
- **PR #2437**: perf(router): pre-compute blocked bitmap and spatial crossing index for A* search
- **PR #2436**: fix(router): clamp trace-width boundary check and record corridor routing failures
- **PR #2435**: fix(router): detect charlieplex matrix topology and assign alternating layer preferences
- **PR #2434**: fix(router): add pad metal area expansion and approach zone relaxation to C++ pathfinder
- **PR #2433**: fix(router): add motor/actuator power net patterns to POWER classification
- **PR #2429**: fix(router): run `cleanup_artifacts()` before `get_statistics()` in escalation loops
- **PR #2428**: fix(drc): add floating-point epsilon to edge clearance comparisons

### 2026-05-02

- **PR #2422**: fix(router): inset auto-pour zone boundaries by edge clearance
- **PR #2423**: fix(router): add early termination to layer escalation when results stagnate
- **PR #2420**: fix(router): break rip-up loop when `nets_to_reroute` is empty after stall filtering
- **PR #2421**: fix(router): match full layers block in stackup update regex
- **PR #2419**: fix(router): propagate `per_net_timeout` and `escape_budget` to escape strategies
- **PR #2418**: fix(router): exclude single-pad nets from 'nets routed' count and convergence
- **PR #2417**: fix(zones): assign distinct priorities to power net zones on the same layer
- **PR #2409**: fix(router): route between escape endpoints, not original pad centers
- **PR #2408**: fix(router): pristine state per layer-escalation attempt and failed-net recovery (#2396)
- **PR #2407**: feat(router): auto-create copper pour zones for power nets before routing
- **PR #2406**: fix(router): prevent via minimizer from breaking layer connectivity
- **PR #2405**: test(router): add pad dimension rotation tests for 0/90/180/270 degrees
- **PR #2404**: fix(router): exclude single-pad nets from two-phase routed count
- **PR 1fb1fc7f**: feat(cli): add `--fine-pitch-clearance` flag to route subcommand
- **PR f52d897a**: fix(router): rotate pad dimensions to PCB space for clearance checks
- **PR #2399**: fix(router): use connectivity tiebreaker when completion ties in adaptive rules
- **PR #2398**: fix(sexp): add x, y, xy to unquoted keywords for bare mirror values

### 2026-05-01

- **PR #2391**: feat(router): early-abort + default `--auto-layers` for power-net stalls (#2388)
- **PR #2392**: fix(router): preserve Y/T-junction connectivity in trace optimizer (#2389)
- **PR #2393**: fix(router): relax grid candidate filter and pass board dims for adaptive plan (#2387)
- **PR #2390**: fix(router): add `find_blocking_nets_relaxed` to CppPathfinder
- **PR #2384**: feat(router): add early termination to adaptive-rules tier loop
- **PR #2383**: fix(router): register SIGTERM/SIGINT handlers in adaptive-rules routing
- **PR #2382**: refactor(panel): remove unused Substrate class and clean up Panel init
- **PR 11d321c8**: fix: suppress off-grid pad warnings when waypoint injection is active

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
- **PR #1144**: fix(stitch): Copy `.kicad_pro` file alongside PCB output for DRC compatibility
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

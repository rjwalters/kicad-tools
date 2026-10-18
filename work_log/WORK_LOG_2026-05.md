# Work Log Archive — 2026-05

Sections moved byte-for-byte from [WORK_LOG.md](../WORK_LOG.md) by `loom-daemon work-docs archive`.

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


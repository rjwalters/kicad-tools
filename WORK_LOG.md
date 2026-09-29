# Work Log

Chronological record of merged PRs and closed issues. Maintained by the Guide triage agent.

---
### 2026-09-29

- **PR #5797**: fix(router): default kct route to reserve declared plane layers from signal
- **PR #5796**: chore(claude): fix malformed ./scripts/** Bash permission rules
- **PR #5794**: fix(router): exclude already-connected nets from --preserve-existing route set
- **PR #5793**: chore(loom): discover_epic_children gains a title-prefix epic-child source
- **PR #5792**: chore(loom): scope sweep-lease peer check to same-issue, add worktree co-occupancy guard
- **Issue #5791** (closed): champion-common.md: discover_epic_children misses title-prefix-only phase issues (recurred on #5774 and #5784)
- **Issue #5789** (closed): kct route routes signals on declared reference-plane layers by default (board 06: 22.8 mm on In1.Cu); default --reserve-plane-layers on
- **Issue #5788** (closed): kct route --preserve-existing re-routes already-complete nets and drops their copper (board 06a: 64/64 LVDS segments lost)
- **Issue #5783** (closed): loom: sweep-lease mechanism failed to prevent 4-way concurrent claim + worktree collision on issue #5781
- **PR #5780**: ci(release): tag merged release SHA, GitHub Release, PyPI check (#5777)
- **Issue #5777** (closed): [Epic #5774] Phase 3: tag merged SHA, publish via workflow_call, GitHub Release on release-PR merge
- **Issue #5774** (closed): Epic: automated daily releases through CI

### 2026-09-28

- **PR #5782**: docs(research): compare and benchmark KiCadRoutingTools vs kct route (#5781)
- **Issue #5781** (closed): research: explore drandyhaas/KiCadRoutingTools — compare, benchmark, and triage borrowable routing techniques
- **PR #5779**: ci(release): daily automated release-PR workflow (#5776)
- **PR #5778**: ci(changelog): changelog.d fragments, per-PR fragment check, assembly script
- **Issue #5776** (closed): [Epic #5774] Phase 2: daily release-PR workflow (gate, bump level, one PR at a time)
- **Issue #5775** (closed): [Epic #5774] Phase 1: changelog fragments + per-PR changelog check
- **PR #5773**: ci: stop CPU-pinning the self-hosted kicad job containers (#5694)
- **PR #5772**: chore(release): bump version to 0.22.0
- **PR #5771**: fix(boards): regenerate stale committed .kicad_dru sidecars (#5059 follow-up)
- **PR #5770**: fix: correct --deterministic-budget's --timeout backstop claim, log determinism loss
- **PR #5769**: ci: give the fork-PR Test job a 90-minute budget
- **PR #5766**: test(board03): drop wall-clock --timeout from frozen reach recipe (#5752)
- **Issue #5765** (closed): --deterministic-budget + --timeout: per-attempt fair slice makes --timeout bind at timeout/N, not a safety backstop
- **PR #5763**: fix(board03): stop the saved-plan replay from deleting footprint silkscreen
- **PR #5760**: fix(width_consistency): join endpoints within tolerance across rounding buckets
- **PR #5759**: feat(board04): emit pin-1 / polarity silkscreen for U1, U2, D1, J1
- **PR #5758**: docs: note via_under_body/pin1_marker can newly fail kct check --strict
- **PR #5757**: fix(tests): stop board-05 export test rewriting committed .kicad_dru
- **PR #5756**: fix(via_under_body): case-insensitive default pattern; recognise split exposed pads
- **Issue #5755** (closed): Regenerate stale committed .kicad_dru sidecars for boards 00-04, 06, 07, legacy DRV8301 (missing #5059 SMD Pad Clearance rule)
- **PR #5754**: fix(pin1_marker): corner-mark tie-break must only group own silk
- **PR #5753**: ci: pin kicad containers to cpuset 6-31 on the 32-CPU runner host (#5694)
- **Issue #5752** (closed): flaky: board-03 test_reach_meets_floor / test_per_net_reach_usb_signals fail in long serial runs, pass standalone
- **Issue #5751** (closed): docs: default-on via_under_body / pin1_marker newly fail kct check --strict — note in CHANGELOG/release notes
- **Issue #5750** (closed): width_consistency: rounded endpoint keys can split a chain early (missed findings)
- **Issue #5749** (closed): via_under_body: case-sensitive default footprint pattern; split exposed pads not recognized as thermal
- **Issue #5748** (closed): pin1_marker: corner-mark tie-break also groups board-level silk (can pass a footprint with no own mark)
- **Issue #5747** (closed): ci: Test can never finish on fork PRs (GitHub-hosted ~40 min vs 45-min timeout)
- **Issue #5746** (closed): test suite rewrites committed board-05 .kicad_dru in place (stale DRU + kct export writes beside input) → 28 #3580 guard errors
- **Issue #5745** (closed): Board 04 generator: emit pin-1 / polarity silkscreen marks for U1, U2, D1, J1
- **Issue #5744** (closed): board-03: routed artifact has lost all footprint silkscreen graphics (139 → 0)
- **PR #5739**: feat(validate): opt-in trace width-consistency audit rule
- **PR #5737**: feat(check): pin-1 / polarity silkscreen-marker rule (pin1_marker category)
- **PR #5736**: feat(check): via-under-package-body rule (via_under_body category)
- **PR #5733**: chore(deps-dev): bump nanobind from 3.0.1 to 3.1.0
- **PR #5732**: chore(deps): bump markitdown from 0.1.7 to 0.1.8 in the python-minor-patch group
- **PR #5731**: chore(deps): bump astral-sh/setup-uv from 10.1.0 to 10.2.0 in the github-actions group
- **PR #5730**: chore(deps): bump astro from 7.3.2 to 7.3.3 in /site
- **PR #5729**: chore(deps-dev): bump @types/node from 26.5.1 to 26.6.2 in /site in the site-minor-patch group

### 2026-09-27

- **PR #5742**: fix(merge-pr): route the #8248 freshness guard's stale-checks call through the #6074/#6752 credential ladder
- **Issue #5741** (closed): merge-pr.sh: _check_required_check_freshness's stale-checks call bypasses the #6074/#6752 permission-escalation ladder
- **PR #5740**: fix(router): keep late keep-outs hard against route-halo refinement

### 2026-09-26

- **PR #5735**: test(conformance): hard-gate the route-halo refinement against kicad-cli
- **PR #5734**: fix: give the optimizer's grid collision checker an exact narrow phase
- **Issue #5625** (closed): router/optimizer: GridCollisionChecker and VectorCollisionChecker disagree on near-threshold copper

### 2026-09-25

- **PR #5728**: cleanup(router/mesh): retire merge_overlapping and segment_intersects_* helpers
- **PR #5727**: perf(router): vectorise the coupled via predicate's raster sweeps
- **PR #5726**: fix(mfr): warn when the emitted SMD Pad Clearance rule is inert on the installed KiCad
- **Issue #5724** (closed): fix(mfr): warn when the emitted SMD Pad Clearance rule is inert on the installed KiCad
- **Issue #5720** (closed): router: the coupled Python via predicate's raster sweeps dominate what is left after #5696's memo
- **Issue #5708** (closed): Flaky perf gate (recurrence): test_dormant_partner_optimized_not_slower fails at 1.33x on shared CI runners (threshold 1.25, #3581 closed)
- **Issue #5685** (closed): cleanup(router/mesh): retire the three helpers Epic #5509 Phase 3e left without a production caller

### 2026-09-24

- **PR #5725**: test(mfr): gate native SMD Pad Clearance on a measured availability probe
- **PR #5723**: test(drc): match native DRC findings on the bare rule name
- **PR #5722**: test: converge kicad-cli discovery on find_kicad_cli()
- **Issue #5721** (closed): Native SMD Pad Clearance rule does not fire on kicad-cli 10.0.1 (undeclared minimum version)
- **PR #5719**: perf(router): memoise the coupled diff-pair halo via refinement on the pure-Python fallback
- **PR #5718**: fix(validate): stop classifying KiCad's instance-lock warning as a failed export
- **PR #5717**: test(perf): replace the dormant-partner timing gate with a structural assertion
- **PR #5716**: test(mfr): scope ampacity DRU fallback to what it actually guards
- **PR #5715**: fix(router): apply the diff-pair partner clearance waiver in the coupled halo refinement (#5711), and rerun the #5410 DQ3 prefix on current main
- **Issue #5714** (closed): test: converge kicad-cli discovery on find_kicad_cli() — 14 shutil.which sites across 12 modules
- **Issue #5713** (closed): test(mfr): SMD Pad Clearance rule silently absent on KiCad 10.0.1 — plus a negative assertion that passes vacuously when it is
- **Issue #5711** (closed): Coupled halo refinement: C++ arm omits the diff-pair partner-clearance waiver the Python arm applies (212-cell over-block)
- **Issue #5707** (closed): Flaky CI: test_native_advanced_layer_matches_independent_gerber fails on kicad-cli 'Invalid lock file' (native export returned 0)
- **Issue #5703** (closed): Native DRC assertions match on a KiCad-version-dependent description prefix (rule '...')
- **Issue #5702** (closed): test_mfr: ampacity DRU fallback pins a stale rule-name set, fails whenever kicad-cli is off PATH
- **PR #5698**: perf(router): read collision-grid planes directly in the optimizer's obstacle scan
- **Issue #5696** (closed): router: memoise the coupled diff-pair halo refinement on the pure-Python fallback

### 2026-09-23

- **PR #5710**: feat(drc): emit SMD pad clearance floor natively, scoped by reference
- **PR #5709**: ci: pin KiCad CI containers by digest via workflow-level KICAD_IMAGE
- **PR #5706**: fix(ci): exclude the vendored .agents/ surface from ruff, unblocking main
- **PR #5705**: test(mfr): measure effective native silk-clearance, not emitted values
- **PR #5699**: fix(router): apply the fab hole-to-hole floor to same-net via drops
- **Issue #5697** (closed): main branch: Lint & Format CI failing again — .agents/ Loom mirror not in ruff extend-exclude
- **PR #5695**: fix(router): refine coupled diff-pair halo rejection with physical clearance
- **PR #5692**: fix(merge-pr): warn when a partial-increment trailer is backticked
- **PR #5691**: perf(router): memoise the per-cell via-site predicates + drop cell_known's _CellView (phase 4 of the diff-pair re-route step)
- **Issue #5690** (closed): Partial-increment reset (#3667) silently skipped when "Part of #N" trailer is backticked
- **PR #5689**: docs: fix inline code spans with embedded line breaks
- **PR #5688**: perf(sexp): walk SExp trees with an explicit stack instead of nested generators
- **Issue #5687** (closed): README.md: three inline code spans broken by mid-span line wraps
- **PR #5686**: perf(validate): memoise each copper element's shapely geometry in the clearance rule
- **PR #5684**: refactor(router/mesh): take ObstacleModel.is_clear's verdicts from the clearance kernel
- **PR #5683**: ci: qualify KiCad 10.0.6 for the native mask-to-copper gate
- **Issue #5682** (closed): ci: pin the kicad/kicad container by digest so a Docker Hub tag move can't silently change measured native behaviour
- **PR #5681**: docs: correct autorouter example and relocated board links
- **PR #5680**: docs: refresh README What's New for 0.21.x and bring scripts/README current
- **Issue #5678** (closed): ci: Test job's native mask gate hard-pins KiCad 10.0.5, fails on runners with 10.0.6
- **PR #5677**: perf(router): fast-path copper_gap dispatch for the lattice engine's pair kinds
- **PR #5676**: fix(router): route the coupled diff-pair search through the clearance kernel (Epic #5509 Phase 3c)
- **Issue #5672** (closed): perf(router): kernel-backed lattice clearance predicates cost ~1.7x the pre-kernel arithmetic (Epic #5509 Phase 3d follow-up)
- **Issue #5664** (closed): [Epic #5509] Phase 3e: switch mesh engine predicate (mesh/obstacles.py) to the clearance kernel
- **Issue #5662** (closed): [Epic #5509] Phase 3c: switch coupled diff-pair search (coupled_pathfinder.cpp + diffpair_routing.py) to the clearance kernel
- **Issue #5059** (closed): Manufacturing profiles: verify effective silk/pad DRC coverage and preserve custom rules
- **Issue #4507** (closed): router Phase 2 (#4431): search-time pairwise HV avoidance + C++ validate_route domain-matrix extension — make HV boards actually converge

### 2026-09-22

- **PR #5679**: perf(zones): skip the post-fill remediation pass's redundant first kicad-cli refill
- **PR #5675**: refactor(router): switch the fixed-copper predicate to the shared clearance kernel (Epic #5509 Phase 3f)
- **PR #5674**: fix(router): surface the declared-rule undercut warning on the explicit --clearance path
- **PR #5671**: perf(router): tombstone R-tree segment deletions instead of deleting eagerly
- **PR #5670**: refactor(router): switch the lattice engine's clearance predicates to the shared kernel (Epic #5509 Phase 3d)
- **PR #5669**: perf(router): memoise the per-layer route-halo via probe (phase 4 of the diff-pair re-route step)
- **PR #5667**: feat(router): read the .kicad_pro Default netclass as a clearance floor
- **PR #5666**: fix(router): run the plan stage on a cache-hit route too
- **Issue #5665** (closed): [Epic #5509] Phase 3f: switch fixed-copper predicate (fixed_copper.py + grid.cpp) to the clearance kernel
- **Issue #5663** (closed): [Epic #5509] Phase 3d: switch lattice engine predicates to the clearance kernel
- **PR #5659**: test(conformance): add seg-zone, via-zone and copper-edge pair kinds
- **PR #5658**: perf(router): vectorize + memoize the pure-Python A* via-clearance kernel (phase 4 of the diff-pair re-route step)
- **PR #5657**: perf(stitch): index foreign-net tracks for the pad-aware via-placement ladders
- **Issue #5656** (closed): router: explicit --clearance never surfaces the "undercuts a declared rule" warning the resolver builds
- **PR #5655**: feat(router): unify clearance rule resolution behind one resolver
- **Issue #5654** (closed): router: promote .kicad_pro netclass clearance to a resolver input once the project writer stops emitting KiCad's stock 0.20mm default
- **PR #5653**: perf(router): whole-board wall-clock attribution for the resume-exhaustion path (diff-pair re-route step)
- **PR #5652**: feat(router,cli): overflow report with computed relief, net-status --why consumer, --plan-gate, and the fleet precision/recall table
- **Issue #5651** (closed): router: a routing-cache hit silently suppresses the routing-plan sidecar (plan stage lives inside route_all_negotiated)
- **PR #5649**: perf(router): vectorize CongestionMap's cell-skip scan
- **Issue #5645** (closed): [Epic #5509] Phase 2: rule resolver unification (DRU precedence, pair matrix, fab-tier floors, trace/via symmetry)
- **Issue #5644** (closed): [Epic #5509] Phase 1c follow-up: zone and edge pair kinds so group 18's zone/edge rules and group 10's pour/outline branches can be measured
- **Issue #5626** (closed): conformance: add a pad-pad pair kind so Epic #5509 group 19 (drc_cpp) can be measured
- **Issue #5613** (closed): tests/router/test_monotone_reach.py::TestDDRByteReach fails at origin/main: DDR byte bundle routes 10/11 in ~15min, DQS_P/DQS_N unroutable
- **Issue #5521** (closed): [Epic #5510] router/cli: overflow report with computed relief, net-status --why consumer, --plan-gate/--force, and the fleet precision/recall table
- **Issue #5510** (closed): Epic: capacity-aware routing plan on the default path — layer assignment, hard corridors, and an infeasibility certificate

### 2026-09-21

- **PR #5647**: chore(release): bump version to 0.21.1
- **PR #5646**: fix(router): engage the within-pair clearance override only when it relaxes
- **PR #5643**: test(conformance): Epic #5509 Phase 1c -- oracle adapters for 18 of 19 consumer groups, kernel control row, CI wiring (#5515)
- **PR #5642**: fix(router): write access-witness sidecar on deadline kill, fix verdict-shape and net0 defects
- **PR #5641**: perf(router): reject far copper by bounding box before the exact halo distance
- **PR #5640**: docs(diagnostics): Phase 1c access-loss witness runs on boards 05/06/07 (evidence only)
- **Issue #5639** (closed): access-witness (1a/1b): no sidecar on deadline-terminated routes, plus two verdict-shape defects
- **PR #5638**: test(board06): pin _path_ok's foreign drill-hole rejection (#5608 follow-up)
- **PR #5637**: perf(stitch): index filled-polygon edges + add phase-level profiling to the diff-pair re-route step
- **PR #5636**: feat(router): honest plan capacity — per-class pitch, plane exclusion, keepout/preserved-copper blockage (#5575)
- **PR #5635**: fix(board06): name a foreign --step route input instead of letting it look like a regression (#5628)
- **PR #5634**: perf(ci): raise KCT_NATIVE_MAX_CONCURRENCY from 1 to 2
- **PR #5633**: fix(release): uv's idempotent-publish flag is --check-url, not --skip-existing
- **PR #5632**: fix(release): idempotent PyPI publish via --skip-existing
- **Issue #5631** (closed): board06: add dedicated regression test for _path_ok's hole-clearance rejection (#5608 follow-up)
- **PR #5630**: fix(release): slim the sdist under PyPI's 100MB limit
- **PR #5629**: fix(board06): enforce hole_clearance in pour-repair path acceptance (#5608)
- **Issue #5628** (closed): board06: --step route on main now leaves +3V3 U3.1 UNREPAIRED (6 lines) and produces 0 hole_clearance errors — route outcome shifted from the #5606-era baseline
- **PR #5627**: fix(zones): coverage-gate the queued-vs-queued overlap warning (#5618)
- **PR #5624**: perf(ci): exclude kicad-cli --help/--version probes from the native permit gate
- **PR #5623**: chore(release): bump version to 0.21.0
- **PR #5622**: fix(board07): frame-independent pour-repair bounds + refill stability
- **Issue #5618** (closed): zones: coverage-gate the queued-vs-queued overlap warning like existing-vs-new (follow-up to #5590)
- **Issue #5616** (closed): perf(ci): recover the +4.7 min Test-job median regression from KCT_NATIVE_MAX_CONCURRENCY=1 without reintroducing the cgroup OOM
- **Issue #5608** (closed): board06: pour-repair bridge can land 3um inside the hole_clearance floor vs a GND via
- **Issue #5603** (closed): perf(ci): cache kicad-cli capability probes so --help launches stop taking native permits
- **Issue #5575** (closed): [Epic #5510] router: honest plan capacity — per-class pitch, plane exclusion, keepout/preserved-copper blockage, channel fixture (#5520 PR-B)
- **Issue #5520** (closed): [Epic #5510] router: run the plan stage on every default kct route with honest capacity (per-class pitch, plane exclusion, zone/keepout blockage, --no-routing-plan)
- **Issue #5518** (closed): [Epic #5508] Phase 1c: access-loss witness runs on boards 05 / 06 / 07 with DQ3 as negative control (evidence only)
- **Issue #5515** (closed): [Epic #5509] Phase 1c: oracle adapters for the remaining 14 consumer groups, kernel adapter, CI wiring, full disagreement table
- **Issue #5507** (closed): router: preserve power-pad access for Board07 U4.C2 (+1V2)

### 2026-09-20

- **PR #5621**: chore(changelog): reconcile [Unreleased] since v0.20.0
- **PR #5620**: feat(board06): reuse through-hole pads as pour-bridge termini in sub-stage D (#5606)
- **PR #5619**: feat(router): replay commit journal into per-pad access witness
- **PR #5615**: fix(zones): allocate new pours around pre-existing same-layer zones
- **PR #5614**: fix(router): resume loop rejects accepted goal node + localized strict pad parity check
- **PR #5612**: fix(ci): cache kicad-cli capability probes to cut native-launch amplification
- **PR #5611**: feat(router): ordered commit journal + access-witness sidecar
- **Issue #5610** (closed): [Epic #5508] Phase 1b (PR 2): access-witness replay + net-status/StuckNetDiagnosis consumers
- **PR #5609**: fix(router): congestion-mark stagnation recovery so board06 converges without the avoidance leak
- **Issue #5606** (closed): board06: port the through-hole pad pour-bridge reuse (#5564) into sub-stage D
- **PR #5605**: fix(board06): reuse an existing same-net barrel for a pour bridge
- **PR #5604**: feat(mcp): migrate to mcp 2.x SDK (FastMCP -> MCPServer) + fastmcp 4 (#5601)
- **PR #5602**: fix(ci): board06 determinism smoke -- explicit output dir for --step route (#5597)
- **Issue #5601** (closed): chore(mcp): migrate to fastmcp 4 / mcp 2.x (FastMCP renamed to MCPServer)
- **PR #5600**: fix(ci): fix board 02/04 determinism-smoke flag drift + raise board-02 timeout
- **Issue #5599** (closed): router: C++ pathfinder gives up on post-route clearance validation, falls back to slow Python A* (blocks board 02/04 routes)
- **PR #5598**: fix(ci): delegate board06 smoke + load-independence copper hash to normalize_copper.py
- **Issue #5597** (closed): board06_determinism_smoke.sh: generate_design.py's default output_dir (regression-output) breaks --step route without an explicit output dir
- **PR #5596**: fix(tests): compose finite-mode route budget in test_route_artifact_receipt
- **PR #5595**: feat(router): add pad outlines and zone polygons to the clearance kernel
- **Issue #5594** (closed): test(router): board02 jlcpcb-tier1 baseline fails on main -- GND net partially routed, 2 of 3 pads stranded
- **PR #5593**: fix(zones): derive generated zone UUIDs from content so pour fills are reproducible
- **PR #5592**: feat(router): add exact-geometry clearance kernel for segments, vias and edges
- **Issue #5591** (closed): pcb: generated boards emit pads with no (uuid ...), so KiCad invents a random one per pad on every load
- **Issue #5590** (closed): zones: an auto-poured zone that overlaps an equal-priority zone on the same layer is starved of copper
- **Issue #5589** (closed): feat(router): extend the clearance kernel with pad outlines and zone polygons
- **Issue #5587** (closed): ci: board_route_determinism_smoke.sh 02 and 04 fail on a loaded host (wall-clock stage deadline / route deadline)
- **Issue #5586** (closed): ci: two more line-based copper normalizers compare element COUNTS (board06 smoke, load-independence test)
- **Issue #5584** (closed): flake risk: test_route_artifact_receipt's finite mode carries the same flat-30s route budget fixed in #5579
- **Issue #5578** (closed): router: zone pour-fill island decomposition is not reproducible run-to-run (routed copper is)
- **PR #5569**: chore(deps): bump anyio from 4.12.0 to 4.14.2
- **Issue #5566** (closed): flake: lattice post-pass gating tests time out in native_concurrency acquire
- **PR #5564**: fix(zones): reuse an existing through-hole pad as a pour-bridge terminus
- **PR #5562**: chore(deps-dev): bump cmake from 4.4.2 to 4.4.3
- **PR #5561**: chore(deps): bump the python-minor-patch group across 1 directory with 2 updates
- **PR #5560**: chore(deps): bump actions/upload-artifact from 4 to 7
- **PR #5559**: chore(deps): bump astral-sh/setup-uv from 10.0.1 to 10.1.0 in the github-actions group across 1 directory
- **PR #5558**: chore(deps-dev): bump the site-minor-patch group in /site with 2 updates
- **Issue #5551** (closed): board06: port the existing-barrel pour-bridge planner into sub-stage D
- **Issue #5545** (closed): router: cohort-reroute converges on board06 U2.B1 stranding without the stale-penalty leak PR #5541 removes
- **Issue #5517** (closed): [Epic #5508] Phase 1b: ordered commit journal + offline access-loss witness surfaced in `net-status --why`
- **Issue #5514** (closed): [Epic #5509] Phase 1b: clearance kernel API in router/cpp with Python port and mandatory parity test (no consumer switched)
- **Issue #5505** (closed): CI runtime: representative-cohort remeasurement and close-out for #5240
- **Issue #5504** (closed): router: make per-connection avoidance cleanup the default once Board06 U2.B1 stranding is explained

### 2026-09-19

- **PR #5588**: docs(routing-plan): fix understated elapsed_s claim in Cost section
- **PR #5585**: fix(ci): compare whole copper nodes, not header lines, in the route-determinism gate
- **PR #5583**: test(ci): size the partial-placement route budget against the native-permit queue (#5579)
- **Issue #5582** (closed): docs(routing-plan): "Cost" section understates the measured plan-stage elapsed_s by ~60x
- **PR #5581**: feat(router): run the report-only routing plan stage on every default route (+ --no-routing-plan)
- **Issue #5580** (closed): ci: board_route_determinism_smoke.sh normalize_copper() compares copper element COUNTS, not geometry
- **Issue #5579** (closed): flake: test_route_partial_placement_cli deadline-exceeded failures redden the Test job on main
- **PR #5577**: perf(board06): spatially prune the pour-repair obstacle clearance scans
- **PR #5576**: fix(ci): credit native-slot wait back to the waiting test's pytest-timeout budget
- **PR #5574**: fix(judge): restore draft-PR fallback exclusion reverted by installed-surface resync
- **PR #5573**: test(ci): add evidence-sized pytest-timeout overrides to the three 60s-reaped test groups
- **Issue #5572** (closed): CI: native-slot wait from KCT_NATIVE_MAX_CONCURRENCY=1 is charged to the waiting test's pytest-timeout budget
- **PR #5571**: perf(router): hoist loop-invariant clearance lookups in validate_routes
- **PR #5570**: fix(router): persist add_obstacle keepouts across grid resets
- **Issue #5567** (closed): Flaky >60s pytest-timeout in tests/router/lattice/test_post_pass_gating.py CLI tests
- **Issue #5557** (closed): judge-fallback-guard.sh: exclude draft PRs from fallback evaluation
- **Issue #5556** (closed): CI: Test job flakes on a fixed 60s pytest-timeout hitting an arbitrary test (seen on main and PRs)
- **Issue #5555** (closed): router: _reset_for_new_trial drops add_obstacle keepouts, silently reopening the grid
- **Issue #5552** (closed): chore: resync installed Loom surfaces silently reverted merged fix #5543 (draft-PR fallback exclusion)
- **Issue #5513** (closed): [Epic #5509] Phase 1a: kicad-cli clearance conformance oracle (report-only) with four adapters and named fixtures

### 2026-09-18

- **PR #5568**: fix(router): sum both edge directions in RoutingPlan edge entries
- **PR #5565**: test(conformance): wrap five clearance consumers as adapters and populate the conformance table
- **PR #5554**: perf(router): write segment clearance envelopes with slice assignment
- **PR #5553**: feat(router): apply declared swap groups via a pad-rebind applicator
- **PR #5549**: fix(zones): reuse an existing same-net barrel for a pour bridge (#5507)
- **PR #5548**: feat(board-07): netlist write-back for generator-owned boards (Epic #5511 Phase 2b)
- **PR #5547**: chore(deps): bump soupsieve from 2.8.4 to 2.9
- **PR #5546**: chore(deps): bump devalue from 5.8.1 to 5.9.2 in /site
- **Issue #5544** (closed): RoutingPlan.from_global_result() may disagree with get_total_overflow() on reverse-direction-only overflow
- **PR #5542**: fix: parse kicad-cli 10 hole-to-hole 'min X mm; actual Y mm' wording
- **PR #5540**: fix: RegionGraph.get_total_overflow() undercounts reverse-direction edge overflow
- **Issue #5538** (closed): classify-dependency-block.sh's ref extraction treats a parenthetical epic mention as a second open blocker, wedging phase issues in DEFER forever
- **Issue #5537** (closed): [Epic #5511] Phase 2b: netlist write-back for generator-owned boards
- **Issue #5536** (closed): [Epic #5511] Phase 2a: pad-rebind applicator for declared swap groups
- **Issue #5534** (closed): drc/report.py's clearance extractor doesn't parse kicad-cli 10's hole-to-hole wording
- **Issue #5533** (closed): [Epic #5509] Phase 1a (2/2): five consumer adapters + populated clearance-conformance table
- **Issue #5529** (closed): RegionGraph.get_total_overflow() can undercount overflow on reverse-direction edge traversals
- **PR #5524**: ci: bound native KiCad subprocess concurrency from measured attribution
- **Issue #5523** (closed): loom-fleet-dispatch auto-relabels draft PRs to loom:review-requested
- **Issue #5501** (closed): CI: bound remaining native subprocess concurrency after confirmed Test cgroup OOM

### 2026-09-17

- **PR #5543**: fix(judge): exclude draft PRs from fallback-mode evaluation
- **Issue #5535** (closed): judge-fallback-guard.sh does not exclude draft PRs from fallback-mode evaluation
- **PR #5532**: test(conformance): kicad-cli clearance oracle harness + named fixtures (Epic #5509 Phase 1a)
- **PR #5531**: perf(analysis): spatially prune net-status segment-adjacency scan
- **PR #5528**: feat(router): emit RoutingPlan sidecar from two-phase global pass (#5519)
- **PR #5527**: perf(router): vectorize diff-pair corridor mask dilation
- **PR #5526**: feat(router): declared swap groups + crossing-minimising pin assignment
- **PR #5525**: feat(router): pad access sets + Kelvin/competing-net fixture (Epic #5508 Phase 1a)
- **Issue #5522** (closed): [Epic #5511] Declared swap groups + crossing-minimising pin assignment (report-only)
- **Issue #5519** (closed): [Epic #5510] router: emit RoutingPlan sidecar from the existing two-phase global pass (report-only, byte-identical copper)
- **Issue #5516** (closed): [Epic #5508] Phase 1a: `router/pad_access.py` access-set computation + 4-pad Kelvin/competing-net fixture

### 2026-09-16

- **PR #5512**: ci: pin routed job containers to the kicad slots' sibling-pair cores (6-31,38-63)
- **PR #5503**: ci: attribute native Test workloads before selecting an OOM policy
- **PR #5500**: perf(router): avoid sqrt in hole-to-hole clearance hot loop
- **PR #5499**: feat(manufacturers): enable reviewed JLC Gerber upload transport
- **PR #5498**: fix(router): check tuning across physical via barrel spans
- **PR #5497**: fix(router): clear avoidance costs after each connection
- **Issue #5496** (closed): router: validate tuning against physical via barrel spans
- **Issue #5495** (closed): router: clear native avoidance penalties after each pad connection
- **PR #5494**: test(router): preserve native import state across diagnostic reloads
- **Issue #5493** (closed): tests: restore native backend import state after diagnostic reloads
- **PR #5492**: docs: check coverage overhead before suspecting a hang in slow single-test runs
- **PR #5491**: docs: record pinned-main softstart qualification
- **Issue #5490** (closed): test_board02_real_hardware.py::test_generated_real_schematic_and_pcb_agree does not terminate quickly — verify actual failure mode
- **PR #5489**: perf(lvs): resolve every schematic pin against one connectivity graph
- **PR #5488**: perf(board06): spatially prune the pour-connectivity audit's pairwise scan
- **PR #5487**: docs: record current softstart residual attribution
- **PR #5486**: fix(router): preserve static partner-pad clearance in native search
- **PR #5485**: fix: handle equal-potential pairs at zero HV threshold
- **Issue #5484** (closed): Zero HV threshold crashes on equal-potential net pairs
- **PR #5483**: fix(router): retain physical targets for repeated pad numbers
- **Issue #5482** (closed): router: preserve static partner-pad clearance during native differential search
- **Issue #5481** (closed): loom-daemon: loom:blocked stripped/re-dispatched within ~2 minutes on #5333 (105+ lease claims/16h, 3 hosts)
- **Issue #5480** (closed): router: retain disconnected same-number pad lands as physical routing targets
- **PR #5479**: fix(router): retain escape and two-phase routing checkpoints
- **Issue #5478** (closed): router: retain checkpoints through escape and two-phase routing
- **PR #5477**: fix(router): keep Kelvin destination escape copper reachable
- **PR #5476**: perf(router): prune distant match-group clearance comparisons
- **Issue #5475** (closed): router: keep destination escape copper reachable during Kelvin branch isolation
- **PR #5474**: fix(board04): avoid unsupported route failure diagnoses
- **PR #5473**: perf(router): AABB pre-filter for diff-pair length-tuning clearance check
- **Issue #5472** (closed): board04: do not attribute every partial route to timeout
- **Issue #5471** (closed): perf(router): AABB pre-filter for match-group post-insertion clearance checks
- **PR #5470**: perf(router): keep scalar congestion arithmetic in Python floats
- **Issue #5469** (closed): perf(router): use Python floats for scalar congestion arithmetic
- **PR #5468**: perf(router): reuse congestion cost within neighbor batches
- **Issue #5467** (closed): perf(router): reuse coarse congestion cost within neighbor batches
- **Issue #5464** (closed): loom-daemon repeatedly redispatches issue #5240 despite an open non-closing 'Part of #5240' PR under review
- **Issue #5455** (closed): router: diversify final bounded relief probe direction for two-terminal nets
- **Issue #5435** (closed): router: reject negotiated trace crossings of foreign via copper during native search
- **PR #5425**: fix(router): preserve physical clearances and complete bounded native searches
- **Issue #5405** (closed): router: extract budget-exhaustion precedence fix from held Board07 draft
- **Issue #5351** (closed): ci: containerd snapshots fill runner root disk despite mostly empty cache volume
- **Issue #5145** (closed): JLC handoff: resumable exact-byte Gerber upload intents and bound receipts
- **Issue #5056** (closed): Feature: Python JLCPCB submission preparation with verified handoff, live stock, review receipts and resumable upload
- **Issue #5000** (closed): board03: qualify USB firmware identity and suspend behavior before factory flashing
- **Issue #4956** (closed): Champion: Merge-Risk Hold Digest

### 2026-09-15

- **PR #5466**: perf(router): avoid cell-view allocation in zone lookup
- **Issue #5465** (closed): perf(router): avoid cell-view allocations in zone lookup
- **PR #5463**: fix(router): negotiate physical copper demand and tapered escapes
- **PR #5462**: chore: install Squad repository coordination workflows
- **Issue #5461** (closed): Install Squad repository adapters for cross-agent coordination
- **PR #5460**: perf(router): hoist per-node pad-exit flags out of the A* neighbor loop
- **PR #5459**: fix(router): enforce physical board edges for lattice copper
- **Issue #5458** (closed): fix(router): enforce physical board-edge clearance for lattice class-width copper
- **PR #5457**: fix(router): honor class clearance across lattice through-vias
- **PR #5456**: router: reverse the final bounded two-terminal relief probe
- **Issue #5454** (closed): router: honor net-class clearance across lattice through-via spans
- **PR #5453**: ci: isolate native zone acceptance from parallel pytest workers
- **Issue #5452** (closed): ci: prevent native zone acceptance from exhausting the Test container memory limit
- **PR #5451**: perf(sch): reuse footprint pad counts within bulk assignment
- **Issue #5450** (closed): router: make lattice negotiation history cover physical escape and unequal-width conflicts
- **PR #5449**: fix(router): preserve physical Kelvin branch separation in lattice routing
- **PR #5448**: test(parts): simulate retry waits while checking timing policy
- **PR #5447**: fix(zones): preserve source constraints before HV keepout refill
- **Issue #5446** (closed): zones: preserve source project and custom rules through HV keepout output and refill
- **PR #5445**: perf(router): load public exports on demand
- **Issue #5444** (closed): router: preserve physical Kelvin branch separation in lattice routing
- **PR #5443**: fix(router): preserve physical pitches in differential routing
- **Issue #5442** (closed): copper LVS misses a native-confirmed +1V2 open despite 244 bound pads
- **PR #5440**: fix(board04): select clearance-safe manufacturing via bonds
- **Issue #5439** (closed): Board04 off-pad manufacturing via relocation shorts fresh routed copper
- **PR #5438**: feat(route): bind final board and custom rules in a verifiable receipt
- **PR #5437**: fix(router): validate tapered goal-via landings at emitted width
- **Issue #5436** (closed): Route outputs: bind effective board/project/custom-rule bytes in a verifiable receipt
- **Issue #5434** (closed): router: validate tapered goal-via arrivals at emitted copper width
- **PR #5433**: perf(validate): load public validation rules on demand
- **PR #5432**: fix(impedance): require resistor topology for sense exclusions
- **PR #5431**: perf(schema): index zone descendants once instead of 13 repeated finds
- **Issue #5429** (closed): perf: lazy-load kicad_tools.validate/validate.rules to stop unrelated DRC-rule imports pulling in the whole router
- **PR #5428**: perf(cli): defer eager CLI/reasoning imports to cut per-invocation startup cost
- **PR #5427**: perf(router): incremental clearance-window dilation for trace collision checks
- **Issue #5426** (closed): impedance.py exclude_current_sense over-broad name match can silently disable diff-pair DRC
- **PR #5424**: perf(validate): vectorize broad-phase candidate-pair queries
- **PR #5423**: fix(router): preserve physical identity for repeated footprint references
- **PR #5422**: docs(board07): complete DQ3 stage-boundary diagnosis
- **PR #5421**: fix(router): widen interior attachment search for wide pad escapes
- **PR #5420**: ci: serialize native physical-stitch acceptance
- **Issue #5418** (closed): Preserve physical terminal identity across repeated or empty footprint references
- **Issue #5417** (closed): ci: serialize native physical-stitch acceptance to prevent measured cgroup OOM
- **PR #5416**: fix(export): preserve board rules in manufacturing project archive
- **Issue #5415** (closed): Fix generic manufacturing archive preservation of custom DRU rules
- **PR #5414**: docs(router): record fresh softstart pairwise proof
- **PR #5413**: fix: route valid nets while preserving placement-invalid copper
- **PR #5411**: fix(router): bound Kelvin contact by native pad and trace width
- **PR #5409**: fix(router): preserve authored gaps in differential sizing
- **Issue #5408** (closed): router: preserve authored pair gaps during impedance sizing
- **PR #5404**: fix(board03): make saved copper native-refill stable
- **PR #5403**: perf(clearance): prune spatially impossible coupling pairs
- **PR #5402**: fix(router): preserve physical Kelvin branch isolation
- **PR #5401**: fix(router): attach wide escape necks inside pad copper
- **PR #5400**: fix(zones): preserve HV keepout gaps through native refill
- **Issue #5399** (closed): HV pour keepouts: preserve requested copper gap through native refill (1.593915 mm vs 1.600)
- **PR #5397**: docs(board07): retain DQ3 and DDR-only diagnostic evidence
- **PR #5396**: feat(stitch): validate physical power completion before promotion
- **PR #5395**: perf(router): avoid cell views in diagonal corner checks
- **PR #5394**: fix(readiness): verify finished releases without replacing recipe contents
- **Issue #5393** (closed): [Epic #3438] Reproduce and localize the remaining Board07 DQ3 open
- **PR #5392**: fix(router): audit HV pad copper and align search waiver geometry
- **Issue #5391** (closed): kct readiness: zone_fill gate is vacuous, and the run degrades recipe-finalised bundles
- **PR #5390**: perf(router): avoid per-call NumPy allocation in _is_via_blocked hot path
- **PR #5389**: fix(board07): correct HDMI witness widths with native parity checks
- **Issue #5388** (closed): [Epic #4410] Make via stitching short-free and complete physical power connections
- **PR #5387**: docs(board07): decide scope of HDMI witness width correction
- **PR #5386**: fix(board03): preserve corrected copper and report blocked native parity
- **PR #5385**: fix(analysis): preserve narrow via contacts with copper fills
- **PR #5384**: fix(schema): preserve the format default for explicit fill-stroke yes
- **PR #5383**: feat(router): preserve bounded arc and Bezier outline geometry
- **Issue #5382** (closed): analysis: verify current-main native parity for stroke-encoded fill fragments
- **PR #5381**: fix(analysis): resolve fill-stroke encoding by token then file version
- **Issue #5380** (closed): Board03: repair 44 PTH-hole clearance failures under PR #5152 factory rules
- **PR #5377**: fix(router): reinstall board-edge keepout across trial resets and worker reconstruction
- **PR #5376**: feat(router): preserve actual custom-pad copper for placement-excluded nets
- **Issue #5374** (closed): Preserve board-edge keepouts across trial resets and routing grid reconstruction
- **PR #5373**: feat(router): preserve bounded circle, arc and Bezier outline geometry
- **Issue #5372** (closed): Router loader refuses gr_curve Edge.Cuts outlines -- next blocker after #5367's gr_circle fix
- **Issue #5367** (closed): Router loader refuses circular Edge.Cuts outlines (gr_circle) before any pad is parsed
- **Issue #5362** (closed): analysis: native R13/R14 VCC open is missed by combined physical-contact analyzers
- **Issue #5357** (closed): [Parent #4946] Preserve actual custom-pad copper for placement-excluded nets
- **Issue #5348** (closed): [Parent #4946] Enable default partial routing around invalid placement across all CLI modes
- **Issue #5204** (closed): board-05: real silk-to-pad and inner PTH-hole clearance violations from PR #5152's new factory rules
- **Issue #5201** (closed): Router via-in-pad placement/repair is not process-eligibility aware (regresses board02 at jlcpcb-tier1)
- **PR #5189**: feat(manufacturing): model via-in-pad fabrication process eligibility
- **PR #5152**: fix(drc): enforce object-specific factory clearances
- **Issue #5126** (closed): board07: regenerate HDMI/TMDS copper to match corrected 0.225mm sidecar width
- **Issue #5009** (closed): manufacturing: via-in-pad capability bypasses actual process eligibility and ordering requirements
- **Issue #4946** (closed): router: --allow-offboard is whole-board, not scoped — one off-board footprint vetoes zero-touch routing of an otherwise-clean multi-hundred-net board (BeagleConnect Freedom)
- **Issue #4409** (closed): router/diffpair: coupled router must actually couple pairs (board 06: 0/9 couple, all budget-exit)
- **Issue #3803** (closed): Router/DRC fidelity: kct reports routing PASS while native KiCad DRC finds 400+ violations (incl. shorts)

### 2026-09-14

- **PR #5378**: fix(router): distinguish unknown from verified-empty via-in-pad hole census
- **PR #5375**: fix(board05): repair clearances and preserve assembly rotation corrections
- **PR #5371**: fix(ci): repair nested content-only path exclusions in Detect Changes filter
- **PR #5370**: fix(router): preserve empty quoted pad identities
- **PR #5369**: feat(export): bind raster DFM reports and OCR transcriptions to exact uploads
- **Issue #5368** (closed): Empty pad numbers break the placement pad/net identity multiset check
- **Issue #5366** (closed): ci: nested content-only paths bypass the full-suite exclusion filter
- **PR #5365**: chore(loom): resync canonical lease lifecycle repairs
- **PR #5364**: fix(router): thread via-in-pad process eligibility through escape placement
- **PR #5363**: feat(router): propose bounded 90-degree endpoint-orientation alignment (#4968)
- **PR #5361**: fix(board03): connect isolated supply copper and gate saved-byte opens
- **PR #5360**: ci: preserve Board03 failure artifacts and recipe logs
- **Issue #5359** (closed): ci: retain Board03 generated artifacts when recipe or connectivity gates fail
- **Issue #5358** (closed): Board 03 regenerates with 4 real KiCad-reported unconnected items (GND C10.2/U2.2, VCC C1.1/U1.2/U1.44) that every CI gate is blind to
- **PR #5356**: feat(router): preserve excluded filled copper across routing engines
- **PR #5355**: feat: report placement-blocked routing populations per attempt
- **PR #5354**: docs: align Board00 component table with procurement BOM
- **PR #5353**: ci: fix routed-PCB PR provenance and add native KiCad path for Board04
- **Issue #5352** (closed): ci: recover runner root disk consumed by containerd and revalidate ENOSPC failures
- **PR #5350**: feat(router): add placement disposition and fixed copper handoff
- **Issue #5349** (closed): ci: routed PCB DRC job lacks KiCad required by Board04 paid-drill validator
- **Issue #5347** (closed): [Parent #4946] Preserve placement-invalid disposition in routing and benchmark reports
- **Issue #5346** (closed): [Parent #4946] Add placement-invalid net disposition and obstacle-preserving loader exclusion
- **PR #5345**: fix: isolate standalone Board07 generator defaults
- **Issue #5343** (closed): board-07 legacy generators still default to output/, overwriting the real SDRAM design
- **PR #5342**: docs(board-07): fix stale output/ paths in regression-fixture README
- **Issue #5341** (closed): boards/07-matchgroup-test/regression-fixture/README.md still documents pre-redesign output/ paths
- **Issue #5340** (closed): boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb is a different board (no MIPI/TMDS nets)
- **PR #5339**: docs: audit Konnect item 1 (on-demand MCP toolsets) — decline
- **PR #5250**: fix(ci): preserve content contracts on docs-only PRs
- **Issue #5249** (closed): ci: preserve documentation and agent-content tests on docs-only PRs
- **PR #5236**: fix(connectivity): recover physical pad via and pour paths
- **PR #5157**: fix(analysis): require real copper contact between same-zone fill islands
- **PR #5150**: fix(lvs): stop suppressing copper-LVS opens by zone ownership alone
- **Issue #5146** (closed): JLC handoff: bind raster DFM reports and qualified transcriptions to exact uploads
- **Issue #5133** (closed): connectivity: frozen Board06 U1.17/U1.32 remain singleton despite existing copper-to-pour paths
- **PR #5107**: ci: retain failed board routing bundles for diagnostic reproduction
- **Issue #5086** (closed): Daemon candidate resolution should filter out external-labeled issues before dispatch
- **Issue #5067** (closed): ci: retain failed board routing bundles for deterministic reproduction
- **Issue #5031** (closed): net-status --strict misses two pad-bearing GNDD islands after native refill (follow-up #4498)
- **Issue #4982** (closed): Copper LVS suppresses real power-net opens solely because the net owns a zone
- **Issue #4971** (closed): Demo BOM procurement: wrong MCU package, current-sense capacitors, and incompatible source LCSC assignments
- **Issue #4968** (closed): placement feedback: propose 90-degree endpoint alignment for pad-row/column mismatches
- **PR #4955**: feat(build-native): MSVC/Windows support for kct build-native
- **Issue #4931** (closed): Champion: Merge-Risk Hold Digest
- **Issue #4929** (closed): [Epic #4410] Stop reporting success on a failed board-05 regen
- **Issue #4896** (closed): Explore Konnect item 1: on-demand MCP toolsets for context economy

### 2026-09-13

- **PR #5338**: fix(submission): bind inventory evidence to verified handoffs
- **PR #5337**: fix(schema): normalize legacy arcs in contour and placement consumers
- **PR #5335**: feat: diagnose netclass targets and verified pattern memberships
- **Issue #5334** (closed): Netclass diagnostics: expose missing class targets and board-net pattern matches without mutating projects
- **PR #5332**: fix(loom): close publish-side yield-exclusion gap causing lease claim thrash
- **Issue #5331** (closed): Fleet lease race: 4+ hosts thrash claim/yield on one issue for 36h with no convergence (observed on #5240)
- **PR #5330**: perf(router): vectorize non-through-hole pad sweep in via placement check
- **PR #5329**: fix(board03): apply manufacturing profile before zone fill in pipeline
- **PR #5327**: perf(router): skip validate_routes exact geometry for provably-far pairs
- **Issue #5326** (closed): Board03 generator's cached fill diverges from post-manufacturing-profile refill — CI reports 4 real floating pads (GND C10.2, VCC C1.1/U1.2/U1.44)
- **PR #5322**: fix(pcb): analyze imported copper arcs without false opens or missing length
- **PR #5174**: fix(router): span ordinary vias across the full physical copper stack
- **Issue #5142** (closed): JLC handoff: immutable preparation plan and exact-ID procurement demand
- **PR #5090**: fix(parts): preserve inventory provenance and reject unverified availability
- **Issue #5034** (closed): Offline catalog stock loses provenance and receives fresh fetched_at on every read
- **Issue #5013** (closed): router: ordinary multilayer transitions emit partial-stack via spans without an HDI process
- **Issue #4963** (closed): Curator operator-premise-recheck self-perpetuates heartbeat comments after body fix (#4507)
- **Issue #4937** (closed): PCB schema does not parse copper (arc ...) tracks — false opens in connectivity + under-reported copper on external boards
- **Issue #4901** (closed): Explore Konnect item 7: netclass / .kicad_pro tooling for HV class-pair clearance round-trip
- **Issue #4884** (closed): schema/pcb: list_edge_contours() still reads raw gr_arc (start) as an on-arc point — legacy center+angle arcs split a rounded outline into 4 contours

### 2026-09-12

- **PR #5325**: perf(router): copy only blocked cells in CppGrid.from_routing_grid
- **PR #5324**: test(site): verify final-source site checks and add repeatable link verification (#5321)
- **PR #5323**: fix(site): clarify routing status, improve label contrast and document publication
- **Issue #5321** (closed): [Epic #5278] Verify and publish the website refresh with repeatable evidence updates
- **Issue #5320** (closed): [Epic #5278] Verify and publish the website refresh with reproducible update procedures
- **PR #5319**: fix: verify deployed board PCBs match built site after deploy
- **Issue #5318** (closed): Board05: live site serves pre-repair copper with 41 arbitrary-angle bottom traces
- **PR #5317**: fix(validate): derive board07 via load from measured geometry, split DQ directions
- **PR #5316**: feat(explain): add Konnect design-review audit-parity mistake checks (#4899)
- **PR #5315**: feat(mcp): add call observability ring buffer + error taxonomy
- **PR #5314**: fix(router): choose affordable safe grids before refusing routing
- **PR #5313**: feat(board09): wire component-stress rows into readiness blockers
- **PR #5312**: perf(router): finish the cell_at() sweep across remaining grid[layer][y][x] call sites
- **PR #5311**: ci: add standing guard against Bot-authored issues carrying 'external'
- **Issue #5310** (closed): Stale 'external' label on pre-#5233 fleet-authored issues blocks Loom dispatch
- **PR #5309**: fix(router): clamp same-component carve-out to the configured clearance
- **PR #5308**: perf(router): use cell_at() in CppGrid.from_routing_grid's bulk copy
- **PR #5307**: perf(router): extend cell_at() to RoutingGrid's remaining mark/unmark hot paths
- **PR #5306**: fix(router): repair the three #5219 regressions (quantizer corridor, outline contract, cache determinism)
- **PR #5305**: feat(site): present dated external routing outcomes and limitations
- **PR #5304**: docs(bench): publish dated external routing evidence and limitations
- **PR #5303**: fix(router): retain bare net names through normalization
- **Issue #5302** (closed): fix(router): bare net-name atoms silently become net0 after normalization
- **PR #5301**: fix(router): preserve split-net copper during diff-pair tuning
- **PR #5300**: feat(site): refresh capability discovery and shared presentation
- **PR #5299**: fix(site): show verified development readiness without board summary
- **Issue #5298** (closed): [Epic #5278] Present external-board routing status with per-run evidence
- **Issue #5297** (closed): [Epic #5278] Collect current pinned external-board routing evidence
- **Issue #5296** (closed): [Epic #5278] Show verified board readiness progress without a manufacturing package
- **Issue #5295** (closed): [Epic #5278] Refresh shared site presentation and homepage capability discovery
- **Issue #5293** (closed): router: preserve split-net copper and collision state in diff-pair tuning
- **PR #5292**: fix(router): apply physical context to paired match tuning
- **PR #5291**: fix(router): preserve split-net copper during match tuning
- **Issue #5290** (closed): router: pair-aware match tuning ignores via length and via/pad clearance context
- **Issue #5289** (closed): router: match-group tuning drops escape fragments and duplicates channel copper for split nets
- **PR #5288**: fix(route): report truthful partial-snapshot provenance
- **Issue #5287** (closed): route: clean partial exit labels post-tuning copper as a pre-optimization snapshot
- **PR #5285**: feat(benchmark): distinguish route outcome and artifact provenance
- **PR #5284**: docs: audit kicad-tools.org content, navigation and presentation
- **PR #5283**: fix(site): present benchmarks page as a dated historical snapshot
- **PR #5282**: ci: require native mask-to-copper tests without skips
- **Issue #5281** (closed): [Epic #5278] Audit website content, navigation and presentation against project evidence
- **Issue #5280** (closed): [Epic #5278] Distinguish benchmark outcomes and measured-artifact provenance
- **Issue #5279** (closed): [Epic #5278] Present existing benchmark attempts as a dated historical snapshot
- **Issue #5278** (closed): Epic: Refresh kicad-tools.org to fairly showcase project capabilities and evidence
- **PR #5277**: fix(test): replace stale rule-count floor in DRU ampacity fallback test
- **PR #5276**: fix(router): fail closed on same-net arcs and pours in declared current paths
- **PR #5275**: perf(router): skip intermediate view objects in grid[layer][y][x] access
- **Issue #5274** (closed): PR #5219: three algorithmic regressions at head f623a984 (quantizer/via-in-pad clearance, outline strictness, cache determinism)
- **Issue #5273** (closed): Declared current paths silently ignore same-net routed arcs and copper pours
- **PR #5272**: feat: check pulsed and duty-cycled current on declared branch paths
- **Issue #5271** (closed): test_ampacity_dru_accepted_by_kicad_cli fails on main: DRU rule count is 12, assertion expects >= 13
- **Issue #5270** (closed): PR #5087: clear two capacitor escape drills after Board02 finalization
- **PR #5269**: perf(router): drop per-call NumPy overhead in A* neighbor batch costs
- **PR #5268**: fix(router): separate per-search-stage budget from the hard total timeout
- **PR #5267**: fix(build-native): build and install the drc C++ extension too
- **Issue #5266** (closed): Routing: separate Board 07 search-stage budgets from the hard total deadline
- **PR #5265**: fix(router): preserve copper across routing cache replay
- **PR #5264**: fix(reinforce): preserve force-path eligibility through segment chaining
- **Issue #5263** (closed): fix(reinforce): preserve path eligibility through chain reversal and force/sense boundaries
- **PR #5262**: test: start optimizer interruption timing after interpreter setup
- **Issue #5261** (closed): fix(router): preserve physical copper across routing cache miss and hit
- **PR #5260**: fix(router): narrow endpoint-fanout fallback so ordinary branches resolve, not ambiguous (#4980)
- **Issue #5259** (closed): mask-to-copper native tests (merged-region, UUID-exclusion) never run in CI
- **Issue #5258** (closed): test: make optimizer deadline regression independent of interpreter startup speed
- **Issue #5257** (closed): router: C++ A* reports no-path on trivial region-bounded net, forcing slow Python fallback and CI timeouts (PR #5165)
- **PR #5256**: fix(build-native): install the placement C++ extension, not just router
- **PR #5255**: feat(validate): check source-bound mask-to-copper exposure
- **PR #5254**: fix(router): scope current-path cycle detection past benign via arrays
- **PR #5253**: perf(placement): reduce allocations in CPU force sampling
- **PR #5252**: ci: Test, Board 06 and Diff-Pair run on the fleet CI runner for same-repo PRs too
- **Issue #5251** (closed): board03: fix USB wake-ISR clock/ack ordering and gate sends until resume completes (PR #5129)
- **PR #5245**: fix(router): preserve pad shape in grid clearance checks
- **PR #5231**: fix(mcp): use shared copper geometry for clearance measurements
- **Issue #5229** (closed): fix(router): grid pad backstops miss square and rotated copper overlaps
- **Issue #5226** (closed): PR #5165's same-component carve-out tightening breaks basic reachability for fine-pitch/adjacent pads under default DesignRules()
- **Issue #5221** (closed): [#5197 follow-up] Resolve proved local via-array motifs with complete full-current copper evidence
- **PR #5219**: fix(router): enforce SMD via capability in grid pathfinders
- **Issue #5216** (closed): router: grid search allows same-net SMD via-in-pad on unsupported manufacturing profiles
- **Issue #5197** (closed): Current-path resolution reports whole-net 'ambiguous' for benign parallel via arrays
- **Issue #5179** (closed): kct readiness --assembly mode: procurement identity, THT instructions, per-rule warning review
- **Issue #5170** (closed): Wire component-stress FAIL/UNRESOLVED rows into per-board readiness.json blockers
- **PR #5169**: feat: add operating-state MOSFET VDS/VGS component-stress analyzer
- **Issue #5166** (closed): C++ same-component carve-out should clamp to configured clearance, not skip entirely
- **PR #5165**: fix(router): enforce pad clearance and preserve physical routing
- **PR #5159**: feat(silkscreen): add place-silk-refs reference placement solver
- **PR #5155**: fix(validate): route roundrect/oval pad-segment clearance through true polygon
- **PR #5138**: feat(check): detect physical copper slits independently of net identity
- **Issue #5137** (closed): Check mask openings against copper with explicit exposure intent
- **Issue #5134** (closed): validate: board07 SDRAM load gate uses declared allowances, not measured via geometry or confirmed MCU corners
- **PR #5129**: fix(board03): patch AVR core USB suspend/resume clock TODOs
- **Issue #5128** (closed): Router-side and MCP-tool clearance checkers still use disc/AABB pad models (roundrect corners diverge from #3826/#4985)
- **PR #5119**: Installer: add explicit Codex and Claude client targets
- **PR #5091**: feat: search safe alternate escapes for blocked in-pad vias
- **PR #5087**: fix(router): share authoritative outline bounds across routing and schema
- **PR #5085**: fix: enforce project hole-to-copper clearance during via relocation
- **Issue #5069** (closed): fix-vias: enforce project hole-to-copper clearance when relocating drills
- **Issue #5068** (closed): fix-vias: search safe alternatives when a single escape slide is blocked
- **Issue #5063** (closed): Feature: check soldermask openings against nearby copper, separate from pad expansion
- **Issue #5062** (closed): Feature: geometry-aware same-net copper gap preflight without flagging valid joins
- **Issue #5039** (closed): Feature: operating-state component stress checks for MOSFET VDS/VGS and release readiness
- **Issue #5030** (closed): Feature: place readable silkscreen references while preserving labels and component association
- **Issue #5004** (closed): Fine-pitch same-component carve-out silently bypasses authored pad clearance
- **Issue #4985** (closed): clearance_pad_segment still reports roundrect bounding-box false positives after #3826
- **Issue #4980** (closed): Model branch-specific current and reinforcement intent for power trunks sharing nets with sense taps
- **Issue #4978** (closed): route loader silently uses 65x56 HAT extent for a valid four-line Edge.Cuts outline
- **Issue #4945** (closed): router: off-grid-escalation fine-zone sizing blows the auto-grid cell budget on a large coarse-pitch header (PocketBeagle P1/P2), refusing to route the whole board
- **Issue #4905** (closed): Installer: add explicit Codex and Claude client targets
- **Issue #4899** (closed): Explore Konnect item 4: design-review audit rule content for kct check / detect_mistakes
- **Issue #4897** (closed): Explore Konnect item 2: call observability (recent-calls ring buffer + error taxonomy) for kct mcp

### 2026-09-11

- **PR #5248**: fix(ci): exempt changes detector from local validation manifest
- **Issue #5247** (closed): ci: exempt changes detector from local validation-job manifest
- **PR #5246**: ci: skip everything but lint/typecheck on docs/site/agent-config-only PRs
- **PR #5244**: test(copper-candidates): stub PCBs need footprints since #5217 (main is red)
- **PR #5243**: ci: 12 GB / 8 GB for the runner-hosted Test and Board 06 job containers
- **PR #5242**: Resolve native exported soldermask and copper geometry
- **PR #5239**: ci: one live run per ref; main-push heavy jobs on the fleet CI runner
- **Issue #5237** (closed): test: supply physical pad identities in spatial-candidate PCB fixtures
- **PR #5235**: fix: preserve legal pour escapes and signal trunk widths
- **PR #5234**: fix(router): validate rotated pad clearance in Python and native grids
- **PR #5233**: fix(ci): recognize verified Loom app issue authors
- **PR #5232**: feat(mcp): add get_design_intent tool for reading project .kct constraints and decisions
- **PR #5230**: fix(clearance): orient pad polygons with KiCad's rotation sign
- **PR #5228**: fix(router): honor pad rotation across clearance and thermal consumers
- **Issue #5227** (closed): fix(clearance): pad polygons use the opposite KiCad rotation sign
- **PR #5225**: feat(core): check KiCad lock markers before file writes
- **PR #5224**: feat(schematic): safely repair exact-grid wire stubs
- **Issue #5223** (closed): Board06: resolve J1.B8 pour disconnect and non-pair DRC failures observed on #5219
- **PR #5222**: fix(current-paths): respect physical copper layers and graph edges
- **Issue #5220** (closed): [#5197 prerequisite] Make current-path graph layer-aware with physical via transitions and split-edge identity
- **PR #5218**: docs: point board recipe authors to schematic block factories
- **PR #5217**: fix(connectivity): preserve duplicate physical pad occurrences through LVS
- **PR #5215**: chore(deps-dev): bump nanobind from 2.10.2 to 3.0.1
- **PR #5214**: test(lattice): restore historical board06 coupled routing witness
- **PR #5213**: fix(net-status): enforce via spans in strict pour chains
- **PR #5212**: chore(deps): bump the python-minor-patch group with 2 updates
- **PR #5211**: test: keep CLI JSON contract check independent of full demo boards
- **PR #5210**: chore(deps-dev): bump vitest from 4.1.11 to 5.0.0 in /site
- **PR #5209**: chore(deps): bump astro from 7.2.8 to 7.3.2 in /site
- **PR #5208**: chore(deps-dev): bump @types/node from 26.4.0 to 26.5.0 in /site in the site-minor-patch group
- **PR #5207**: test(lvs): separate shorted hierarchical fixture routes
- **PR #5206**: feat(manufacturers): add offline JLC upload model and durable ledger
- **PR #5205**: feat(export): add hash-bound human review record and portable review page
- **PR #5203**: fix(board06): make _audit_pour_nets via modelling layer-span aware
- **Issue #5202** (closed): CI Test job red on main: test_copper_lvs_vacuity_guard_no_longer_fires (copper LVS not clean)
- **PR #5200**: feat(route): wire branch current paths into kct route (#4980)
- **Issue #5199** (closed): CI flake: test_cli_json_output_contains_per_rule_counter runs ~44s against a 60s pytest-timeout
- **Issue #5198** (closed): NetStatusAnalyzer: layer-agnostic chain merge leaks a floating pad's pour bond across unrelated copper
- **Issue #5196** (closed): board-06 coupled witness test pins a stale pair census (4 engaged, test asserts 9) — fails on main
- **PR #5195**: fix(router): enforce hard layer intent in the lattice engine and gate the written copper (#4979)
- **Issue #5194** (closed): router: audit non-obstacle-model pad half-extent sites for #4910-class rotation gap
- **PR #5193**: feat(export): add narrow exact-ID inventory adapter for submission plans
- **Issue #5192** (closed): CI: test_copper_lvs_vacuity_guard_no_longer_fires fails on main (pre-existing, unrelated to #5171)
- **Issue #5191** (closed): board03: pre-existing test_board_03_regression failures (annular_width DRC drift; route_demo warns 14 in fresh regen tmpdir)
- **PR #5190**: fix: retain validated per-board fabrication-floor overrides in DRC emission
- **Issue #5188** (closed): CI flake: test_copper_lvs_vacuity_guard_no_longer_fires fails intermittently under full-suite xdist, unrelated to PR diff
- **PR #5187**: feat(cli): add kct readiness manufacturing sign-off runner
- **Issue #5186** (closed): Migrate board03 check_manufacturing.py to the #5006 fabrication-overrides contract
- **Issue #5185** (closed): ConnectivityValidator may collapse duplicate pad numbers the same way net_status.py did before #5061
- **PR #5184**: feat(check): wire branch current-path checks into kct check
- **Issue #5183** (closed): Cross-reference schematic.blocks library from board-recipe-scaffold's fill-in guidance
- **Issue #5182** (closed): router: same non-cardinal pad rotation gap in grid.py clearance validators and C++ Grid3D::validate_route
- **PR #5181**: fix(router): honor non-cardinal pad rotation in obstacle models
- **PR #5180**: fix(ci): discover fleet angle census artifacts beyond _routed suffix
- **Issue #5178** (closed): route: post-route clearance/plane-layer audits never run on the escalation paths (incl. the --auto-layers default)
- **Issue #5177** (closed): board06 live regen: J1.A1/U1.15 GND pads genuinely disconnected from copper (surfaced by #4982)
- **Issue #5176** (closed): board06 live regen: U1.15 GND escape stub is left dangling (no plane stitch via) and the recipe's pour audit falsely reports PASS
- **PR #5175**: fix(router): give _escape_radial board-wide foreign-pad clearance
- **PR #5173**: fix(curator): stop operator-premise-recheck self-perpetuating on quoted heartbeat comments
- **PR #5172**: route: add --reserve-plane-layers controlled-impedance signal-layer guardrail
- **PR #5171**: feat: prepare immutable offline assembly submission handoffs
- **PR #5168**: fix(schema): retain modern and legacy footprint arc midpoints
- **PR #5167**: fix(mcp): register typed session intent tools
- **PR #5163**: fix(board07): use total external-load capacitance gate
- **PR #5162**: feat(export): verify explicit factory-selected component CSV
- **PR #5161**: ci: apply the Board 07 pause to its unit-test module
- **Issue #5160** (closed): Apply temporary Board 07 CI pause to its unit-test module
- **PR #5158**: fix(export): never auto-match LCSC for explicit non-LCSC MPN sourcing
- **PR #5156**: feat(router): add flat signal-clearance table builder for #5021
- **PR #5154**: fix(validate): tolerate float-rounding in silk-overlap corner exemption
- **Issue #5153** (closed): validate: retire vestigial 20 pF trace-only capacitance gate and stale capacitance_scope string in board07 SDRAM validate.py
- **PR #5151**: fix(connectivity): preserve physical duplicate pad occurrences
- **PR #5149**: feat(analysis): review weak-source loading and gate-drive budgets
- **PR #5148**: Report explicit analog threshold and hysteresis budgets
- **Issue #5147** (closed): board03/04 committed routed PCB artifacts lack (net N "name") net-table entries, breaking 4 stitching-via tests
- **Issue #5144** (closed): JLC handoff: verify explicit factory-selected component CSV against expected references
- **Issue #5143** (closed): JLC handoff: hash-bound human review state and portable review page
- **PR #5141**: fix: bound routing cleanup with invocation process supervision
- **PR #5140**: Inspect source-bound standard soldermask geometry
- **PR #5139**: feat(analysis): qualify resistor operating budgets with bound evidence
- **Issue #5136** (closed): Complete exported soldermask geometry coverage and Gerber parity
- **Issue #5135** (closed): Resolve source-bound standard pad and via soldermask geometry
- **Issue #5132** (closed): main branch CI broken since commit d95b6eff36 — blocks entire PR review queue
- **PR #5131**: test: cover gallery source and Gerber download combinations
- **Issue #5130** (closed): loom-daemon repeatedly redispatches external-labeled issue #4897, wasting sweep runs
- **PR #5127**: fix(board07): make impedance-corrected MIPI/HDMI sizing the default
- **PR #5125**: feat: declared branch-specific current-path intent for shared power/sense nets
- **Issue #5124** (closed): Propagate declared current-path intent (#4980) into pathfinder width selection and kct check/route CLI wiring
- **Issue #5123** (closed): loom-daemon work-finder repeatedly re-dispatches external-labeled, unapproved issue #4899 despite 3+ prior declines
- **PR #5122**: fix(lvs): decide copper contact over full segment length, not endpoints
- **PR #5121**: fix(router): normalize KiCad 10 name-only nets before building routing graph
- **Issue #5120** (closed): Multiple open PRs contaminated with unmerged feature/issue-5044 content (197k-line diff bloat)
- **PR #5118**: fix: stitch Edge.Cuts outlines robustly across gaps and multiple contours
- **PR #5117**: fix(pcb): validate zone net and copper layer before mutation
- **PR #5116**: fix: reject symmetric match-group splices with mismatched host spans
- **PR #5115**: fix(parts): distinguish unavailable lookups from catalog absence
- **Issue #5114** (closed): chore: fix mypy baseline drift (stripline.py, pcb.py, via_pad_geometry.py)
- **PR #5113**: fix(stackup): name factory constructions and preserve legacy compatibility
- **Issue #5112** (closed): schema/pcb: FootprintGraphic has no mid support, so fp_arc geometry (modern or legacy) is unmodeled
- **PR #5111**: fix(check): verify manifest freshness using PCB content
- **PR #5110**: feat(board-metrics): describe development boards before manufacturing export
- **PR #5109**: fix(stitch): expose DRC results and optional strict validation gate
- **PR #5108**: refactor(pcb): share legacy-aware footprint traversal
- **PR #5106**: feat(core): harden .kicad_pcb/.kicad_sch/.kicad_pro writers with atomic writes
- **Issue #5105** (closed): Refuse-to-write (or warn) when a sibling .kicad_*.lck file indicates a live KiCad session
- **Issue #5103** (closed): spec+mcp: surface project.kct intent.constraints and decisions as standing design guidance for LLM sessions
- **Issue #5102** (closed): mcp: register declare_interface/declare_power_rail/list_intents/clear_intent in the tool registry
- **PR #5101**: fix(render): report missing component 3D models
- **PR #5100**: fix: carry authored DRC constraints across routed output renames
- **PR #5098**: docs(boards): reconcile packaged procurement reviews with current designs
- **PR #5096**: docs(research): audit Konnect's schematic connectivity primitives vs ERC/LVS surface
- **Issue #5095** (closed): Auto-fix wire-stub near-misses: dry_run-gated snap for find_wire_stubs findings
- **PR #5094**: fix(check): detect dangling tracks by copper-cap contact
- **PR #5092**: chore(deps): bump weasyprint from 68.1 to 70.0
- **PR #5088**: test: pin joystick capability geometry and retain bounded routing diagnostics
- **Issue #5084** (closed): ci: fleet angle census omits routed boards without a _routed filename
- **PR #5083**: feat(benchmark): measure Board09 host-bus routing with native validation
- **PR #5082**: perf: filter copper clearance and chain pairs with spatial bounds
- **PR #5081**: fix: preserve pad and plane contacts when relocating stitch vias
- **PR #5080**: fix(export): bundle project-local symbol library dependencies
- **PR #5079**: fix: canonicalize mypy diagnostic aliases and annotate inference gaps
- **PR #5078**: fix: validate complete via relocation stubs before committing
- **PR #5077**: fix(runner): restore net headers before legacy footprints
- **PR #5076**: fix: retain unassigned SMD copper as via relocation obstacles
- **PR #5075**: fix(mfr): regenerate executable OSH Park rules template
- **PR #5074**: fix(panel): remap legacy footprint reference text
- **PR #5073**: fix(deploy): require the exact scoped Pages project
- **Issue #5072** (closed): router: add feasible board09 four-net outer-layer host-bus benchmark
- **Issue #5071** (closed): export: include referenced project-local symbol libraries in KiCad project ZIP
- **Issue #5070** (closed): check: avoid exhaustive copper-pair scans on dense routed boards
- **Issue #5066** (closed): Feature: resistor derating, heatsink prerequisites and waveform power qualification
- **Issue #5065** (closed): fix-vias: accepted relocation can short another net through an unchecked connecting stub
- **Issue #5064** (closed): Feature: schematic-bound analog threshold and hysteresis tolerance budgets
- **Issue #5061** (closed): net-status: false GND islands for capacitor via arrays and USB shield contacts after native refill
- **Issue #5060** (closed): copper LVS: false opens at mid-track branches and inline pads; splitting unchanged copper passes
- **PR #5058**: fix(schematic): keep power flags out of rail names
- **Issue #5055** (closed): board-metrics: report development PCB metadata before manufacturing export
- **PR #5054**: fix(check): retain short collinear silkscreen overlaps
- **PR #5052**: fix(parts): preserve official API error reasons and status
- **PR #5051**: test(pcb): cover pad-angle invariants for move-footprint --rotation
- **PR #5050**: fix(parts): reject incompatible resistor suggestions
- **Issue #5048** (closed): docs(boards): reconcile packaged procurement reviews with current demo designs
- **Issue #5043** (closed): Official Parts API discards JSON reason on HTTP 403 and misclassifies IP whitelist denial as auth failure
- **Issue #5042** (closed): tests: isolate historical joystick router fixtures from the real revision-B design
- **Issue #5041** (closed): Feature: weak-source loading and gate-drive voltage, charge, and hold-up budget checks
- **Issue #5035** (closed): route: total timeout expires but post-pass work continues for minutes before optimization
- **Issue #5033** (closed): Parts lookup conflates unavailable API with not found; anonymous detail endpoint returns 404
- **Issue #5032** (closed): route: renamed output drops source project rule-preservation flag and authored DRC floors
- **Issue #5029** (closed): parts suggest ranks wrong resistance values at 0.9–1.0 confidence (16k → 3.16k; 470k HV → 0R)
- **Issue #5027** (closed): deploy: support scoped Pages tokens in Cloudflare account guard
- **Issue #5025** (closed): Gallery: distinguish Gerber fabrication archive from KiCad project source ZIP download
- **Issue #5024** (closed): Gallery: bind Ready badge and manufacturing freshness checks to content-hash-verified evidence, not filesystem mtimes
- **Issue #5021** (closed): route/validate: enforce clock-to-signal spacing symmetrically for tracks, pads and through vias
- **Issue #5020** (closed): validate: SDRAM load budget omits receiver and full via capacitance and misses 15 pF SDCLK condition
- **Issue #5015** (closed): schematic validation treats PWR_FLAG as a signal net and reports false rail shorts
- **Issue #5014** (closed): route: controlled-impedance plane assignments need an explicit signal-layer reservation guardrail
- **Issue #5010** (closed): fix-vias relocation ignores unassigned SMT pads and creates physical shorts
- **Issue #5008** (closed): render: missing STEP models silently omit IC bodies while reporting success
- **Issue #5006** (closed): check: native constraint emission overwrites reviewed per-board pad-hole spacing
- **Issue #4999** (closed): Generated solder_mask_margin rule silently disables native custom DRC rules
- **Issue #4995** (closed): export: auto-enrichment can assign generic LCSC substitutes to explicit non-LCSC MPNs
- **Issue #4994** (closed): physics: JLC04161H-3313 preset declares incompatible 7628 dielectric construction
- **Issue #4991** (closed): router: ordinary 0805 power escapes place vias across adjacent pads on board02 revision B
- **Issue #4989** (closed): check: joined library silkscreen outline corners produce false silk_overlap warnings
- **Issue #4987** (closed): silk_overlap flags intentional footprint outline joins and omits element UUIDs
- **Issue #4986** (closed): track_dangling ignores real pad, via and mid-track copper contact; findings lack track UUIDs
- **Issue #4984** (closed): router: symmetric match-group splice assumes equal P/N host spans and can duplicate copper
- **Issue #4983** (closed): route accepts KiCad 10 name-only nets but writes zero copper and reports SUCCESS (0/0 nets)
- **Issue #4981** (closed): fix-vias --relocate-in-pad strands SMD pads when the connecting pour is on an inner layer
- **Issue #4979** (closed): lattice --strict-layers writes PGND copper on forbidden In2.Cu during --complete
- **Issue #4977** (closed): Add scriptable manufacturing-readiness runner producing hash-bound gallery evidence
- **Issue #4973** (closed): stitch JSON omits requested DRC verdict and reports success on incomplete boards
- **Issue #4969** (closed): board07: reconcile pair sidecar geometry and size 100-ohm pairs at authored gap
- **Issue #4966** (closed): pcb move-footprint --rotation leaves absolute pad copper angles stale
- **Issue #4962** (closed): Unblock mypy 2.3.1: normalize equivalent baseline diagnostics and fix residual typing errors
- **Issue #4948** (closed): Edge.Cuts outline detection picks a tiny sliver polygon on STRF board (blocks external benchmark routing)
- **PR #4922**: chore(deps): bump mypy from 1.19.1 to 2.3.1
- **Issue #4915** (closed): runner.py's _restore_net_declarations content_tags set doesn't recognize legacy (module ...) footprints
- **Issue #4913** (closed): Deduplicate _find_all_footprints helper across drc/zones/lvs modules
- **Issue #4910** (closed): router: lattice/mesh obstacle pad geometry ignores non-cardinal pad rotation
- **Issue #4907** (closed): Validate declared nets and enabled copper layers before adding zones
- **Issue #4903** (closed): Explore Konnect item 9: reference-circuit templates vs /kct:board-recipe-scaffold
- **Issue #4900** (closed): Explore Konnect item 6: schematic connectivity primitives vs our ERC/LVS/net-status surface
- **Issue #4898** (closed): Explore Konnect item 3: durable multi-file write transactions for .kicad_pcb/.kicad_sch/.kicad_pro writers
- **Issue #4888** (closed): panel.py: _remap_reference silently no-ops on fp_text-format (KiCad 7 / legacy module) reference designators

### 2026-09-10

- **PR #5104**: docs: audit Konnect item 8 natural-language design-rule store, decline
- **PR #5099**: ci: temporarily suspend dedicated Board 07 gates
- **Issue #5097** (closed): Temporarily suspend dedicated Board 07 CI gates
- **Issue #5057** (closed): pcb: added footprints lack unique persistent pad UUIDs for native DRC correlation
- **Issue #5053** (closed): check_mypy_baseline.py fails on main with 3 errors beyond the baseline
- **Issue #5049** (closed): pcb: newly added footprint pad mutations are silently omitted by save()
- **Issue #5047** (closed): board05: R10-R12 current-sense shunt and U1 buck regulator LCSC assignments don't match required parts
- **Issue #5046** (closed): board03: U1 MCU footprint mismatch — TQFP-32 7x7mm required, C44854 is a QFP-44 10x10mm ATMEGA32U4-AU
- **PR #5045**: fix(ci): restore demo-board integration checks after redesign
- **Issue #5044** (closed): main CI broadly red as of 2026-09-10 (d95b6eff): board readiness status='partial', new ruff-format drift, cascading E2E failures
- **PR #5040**: fix(parts): exclude unpopulated symbols from sourcing preflight
- **PR #5038**: fix(cli): handle suppressed format defaults in help
- **Issue #5028** (closed): parts CLI crashes on Python 3.14: SUPPRESS default interpolated in format help
- **PR #5026**: fix(router): count only fully routed nets in negotiated progress line
- **Issue #5023** (closed): export/check: preserve reviewed native rule floors to keep exported plane copper reproducible
- **Issue #5022** (closed): ci: synthetic routing recipes can check or overwrite assembled demo outputs
- **Issue #5019** (closed): pcb-modify flip reports success while leaving pads on front layers; modern references are not found
- **Issue #5018** (closed): Stripline geometry treats intervening signal copper as a reference plane and reverses upper/lower gaps
- **Issue #5017** (closed): PCB stackup parser drops native addsublayer dielectric strata and material properties
- **Issue #5016** (closed): physics: asymmetric stripline formula overestimates a verified six-layer geometry by about 42 percent
- **Issue #5012** (closed): via-in-pad DRC misses drill holes that partially cut across SMT soldering lands
- **Issue #5011** (closed): board04: schematic mirrors invalid AMS1117 power pin mapping, allowing false clean LVS
- **Issue #5007** (closed): check: copper-pour keepout rule areas falsely report disabled fill and missing net
- **Issue #5005** (closed): Off-grid pad-edge A* seeds ignore trace radius and repeat invalid escape tails
- **PR #5003**: chore(deps): bump @vitest/mocker and vitest in /site
- **Issue #5002** (closed): Router impedance synthesis ignores explicit PCB stackup and blocks fine-pitch escapes
- **Issue #5001** (closed): stitch: inner-plane targets create partial-stack spans on standard through vias
- **Issue #4998** (closed): JLCPCB profiles need layer-specific PTH annular-ring floors separate from vias
- **Issue #4997** (closed): physics: bottom microstrip reads exterior paste instead of inward substrate
- **Issue #4996** (closed): check: exact-minimum PTH annular rings fail due to floating-point rounding
- **Issue #4993** (closed): board05: DRV8301 power/charge-pump topology contains electrical shorts and unrealizable power footprints
- **Issue #4992** (closed): LVS: round header pad bounding boxes create false shorts across plane antipads
- **Issue #4990** (closed): parts suggest preflight counts BOM-excluded symbols as missing LCSC parts
- **Issue #4988** (closed): route: main CLI rejects supported --no-auto-pour option
- **Issue #4976** (closed): label LVS loses pad bindings after KiCad 10 saves name-only net syntax
- **Issue #4975** (closed): router: size final diffpair trombone to deficit instead of always adding a full loop
- **Issue #4974** (closed): Export includes through-hole parts in SMT CPL when footprint assembly attributes are absent
- **Issue #4972** (closed): route: cache ignores net-class sidecar and coupled-routing mode, replays invalid copper
- **Issue #4970** (closed): Gallery: require current manufacturing evidence for Ready and distinguish Gerbers from KiCad project downloads
- **Issue #4967** (closed): router progress counts empty and partial routes as completed nets
- **PR #4965**: chore(deps): bump astro from 7.2.1 to 7.2.8 in /site
- **PR #4964**: chore(deps): bump svgo from 4.0.2 to 4.1.0 in /site
- **PR #4961**: chore(deps): bump the python-minor-patch group across 1 directory with 9 updates
- **PR #4960**: chore(deps-dev): bump fast-uri from 3.1.5 to 3.1.7 in /site
- **PR #4958**: chore(deps-dev): bump the site-minor-patch group across 1 directory with 2 updates
- **Issue #4927** (closed): Lint & Format CI check fails on main: 58 pre-existing UP038 ruff errors
- **PR #4924**: chore(deps): bump rich from 14.2.0 to 15.0.0
- **PR #4921**: chore(deps): bump weasyprint from 68.1 to 69.0
- **PR #4918**: chore(deps): bump astral-sh/setup-uv from 9.0.0 to 10.0.1
- **Issue #4906** (closed): Explore Konnect item 12: IPC-API edit patterns as mocked-IPC test-harness reference
- **Issue #4904** (closed): Explore Konnect item 10: manufacturing pre-flight shape + offline JLCPCB catalog vs kct:tapeout
- **Issue #4902** (closed): Explore Konnect item 8: layered config with natural-language design rules vs manufacturer profiles/recipes
- **PR #4835**: chore(deps): bump nanoid from 3.3.12 to 3.3.18 in /site

### 2026-09-06

- **Issue #4954** (closed): mypy 2.x bump surfaces ~50 new type errors (implicit-Optional + missing generic args)


### 2026-08-06

- **Release**: v0.20.0 — development since v0.19.0: a targeted `kct route --complete` completion pass, route-time enforcement of high-voltage isolation across every routing engine, a general `.kct_waivers.json` waiver mechanism for `kct check`, and a broad correctness sweep (KiCad-10 net dialect, export manifests, LVS identity, diff-pair shadow constructor). Headline behavior change: `net-status` strict is now the default. Board-edge keepout is auto-resolved from manufacturer limits for in-process API callers (#4568), and a mypy version-drift guard + `post-worktree.sh` env sync closed the phantom-baseline-error trap (#4558). Shipped as a 13-PR train through a GitHub Actions outage (content-judge + local integration preview + operator-approved single-gate merges). Version bumped 0.19.0 → 0.20.0 via PR #4669; tag `v0.20.0` → `publish.yml` to PyPI. No breaking changes.

### 2026-07-20 (evening)

- **Release**: v0.19.0 — feature release centered on the **HV-isolation design loop** and **via-in-pad manufacturability**: `kct creepage --voltage-map` per-net voltage model (#4371), `kct zones hv-keepout` plane voids (#4372), HV-aware placement with creepage-keepout feasibility (#4373), the `/kct:hv-isolation-loop` orchestration skill (#4374), `kct analyze electrical-rating` advisory LED/capacitor rating lint (#4381), `kct check --emit-dru`/`--emit-drc-constraints` rule-identity sidecars (#4375), `kct fix-vias` off-pad relocation (#4363/#4376/#4377), and a `kct doctor` version-record drift check (#4349). Plus correctness fixes across creepage, netlist-sync, router, and writer subsystems. No breaking changes. Version bumped 0.18.0 → 0.19.0; tag `v0.19.0` → `publish.yml` to PyPI.

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

## 2026-08-06 — v0.20.0 released

- v0.20.0 tagged and published to PyPI: 16-issue sweep (waves 3-5) merged as a 12-PR train after the GitHub Actions outage; headliners: lattice search-time HV pairwise clearance (#4602) + keepout rule-areas (#4605), kct sch tidy (#4596) + schematic field lint (#4595), net-status strict default (#4557), .kicad_dru managed-block hardening (#4600/#4667).

## 2026-09-28 — v0.22.0 release cut

- v0.22.0 bump PR opened after an in-session release-prep sweep: CI runner cpuset fix for the resized heavy-lane host (#5753), pre-release fixes to the new pin1_marker/via_under_body/width_consistency rules (#5748/#5749/#5750), board-05 DRU test isolation (#5746), board-03 reach-test flake (#5752), CHANGELOG reconciled to a zero-gap report; headliners: new kct check rules, SMD pad clearance emission, unified route clearance resolver.

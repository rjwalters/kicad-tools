# Work Log

Chronological record of merged PRs and closed issues. Maintained by the Guide triage agent.

---
### 2026-10-17

- **PR #6920**: test: guard MCP schemas against unread params and reject unknown arguments
- **PR #6919**: fix: stop MCP export/validation tools reporting success on empty results
- **PR #6916**: docs: classify Konnect v0.8-v0.13 ideas and refresh its registry entry
- **PR #6909**: test: stub real routing in guide-route probe deadline test
- **PR #6906**: docs: refresh README What's New, --format json claim and docs index
- **PR #6904**: fix: build kct check --only/--skip help from CHECK_CATEGORIES
- **PR #6903**: fix: accept run-DRC options on kct drc and deprecate kicad-drc --rules/--compare
- **PR #6902**: test: assert every standalone export flag is forwarded by kct export
- **PR #6890**: refactor(router): structured PairOutcome records in router/pairplan (#6837 slice 2c-ii)
- **PR #6888**: fix(router): keep the trace optimizer from joining Kelvin branches (#5398)
- **PR #6884**: feat(router): per-net-class allow_plane_layers opt-in honoured by every layer path
- **Issue #6914** (closed): test: CI guard that MCP tool schemas advertise only parameters that are used
- **Issue #6913** (closed): chore: audit kct mcp and kct check responses against a truth contract
- **Issue #6911** (closed): Re-explore Konnect v0.8–v0.13 (mixelpixx): placement scoring, visual baselines, Specctra round-trip, truth contract
- **Issue #6908** (closed): test flake: test_guide_route_probe_receives_deadline timeout and test_escape_strategy_without_install_would_intrude on CI Test job
- **Issue #6898** (closed): cli: kct drc lacks the standalone kicad-drc's --compare / --rules
- **Issue #6897** (closed): docs: stale README What's New (v0.21.1), unverified --format json claim, drc/ vs validate/ disagreement, 8 unindexed docs
- **Issue #6896** (closed): cli: kct check --only help lists 4 categories but the code accepts ~39
- **Issue #6895** (closed): test: assert every kct export flag reaches export_cmd (pass-through, not just parser parity; follow-up #6881)
- **Issue #6876** (closed): router: per-net-class allow_plane_layers opt-in honoured by grid, C++, lattice and coupled paths (board 03 USB_D on In2.Cu)
- **Issue #5943** (closed): Benchmark TraceMaker and fastroute vs kct route on the demo fleet under one kicad-cli referee
- **Issue #5942** (closed): Benchmark kct route on PCBWorld D3-A/B (Clean Pass vs Freerouting 0.80/0.78, KiCadRoutingTools 0.74/0.20)
- **Issue #5398** (closed): [Epic #4410] Complete stranded Kelvin sense nets through generic routing and batch rescue
- **PR #6892**: chore: restore the board renders .gitignore rule dropped in #6879
- **PR #6886**: fix(router): find S-expression tokens outside quoted strings (#6873)
- **Issue #6873** (closed): router: (footprint/(module/(zone inside quoted strings crash load_pcb_for_routing or skip zone removal (follow-up #6740)
- **PR #6880**: test(router): give the sub-grid keepout tests an off-grid pitch (#6878)
- **Issue #6878** (closed): test_escape_strategy_without_install_would_intrude fails on main: subgrid route now emits no copper (control test, #6059)
- **PR #6877**: fix(route): report the grid pitch used, cap explicit grids, surface coarsening (#6875)
- **Issue #6875** (closed): route: record grid pitch used, add explicit-grid cell ceiling, stop silent coarsening in route-auto/route_pcb/MCP (follow-up #6741)
- **PR #6865**: fix(router): --matrix layer hints never overwrite an authored preferred_layers

### 2026-10-16

- **Issue #6694** (closed): kct route: no routing-plan sidecar for the board saved on the deadline/partial-save exit
- **Issue #6471** (closed): ci: assert-no-bot-external red daily since 10-09 -- Renovate's Dependency Dashboard (#6275) auto-labeled 'external'
- **Issue #6333** (closed): [Parent #5942] PCBWorld Phase 3: run D3-B and publish the final comparative verdict
- **Issue #6332** (closed): [Parent #5942] PCBWorld Phase 2: run and publish reproducible D3-A CP@1/CP@5 baseline
- **Issue #6275** (closed): Dependency Dashboard
- **Issue #5877** (closed): kicad-cli hangs forever on a macOS host (TCC-blocked ~/Documents scan in LoadGlobalTables), silently disabling the second DRC engine
- **Issue #5650** (closed): Champion auto-merge fully blocked: GitHub App missing Administration permission for branch-protection GraphQL query (#8248 guard)
- **PR #6887**: docs(research): record the measured chorus-test plan fleet-table row (#6694)
- **PR #6882**: fix(cli): accept --stackup-id and other missing flags in unified kct export parser
- **Issue #6881** (closed): kct export rejects --stackup-id: unified parser omits a flag export_cmd.py defines and docs document
- **PR #6879**: docs: fix CLI examples that drifted from the kct parser; reconcile .gitignore with committed files
- **PR #6872**: fix(router): make rss_trace import cleanly without the Unix-only resource module
- **Issue #6739** (closed): kct route crashes on Windows: ModuleNotFoundError: No module named 'resource' (rss_trace imported unconditionally)
- **PR #6871**: fix(route): honour an explicit --grid instead of silently coarsening it on large boards
- **Issue #6741** (closed): kct route: explicit --grid is silently coarsened by the --max-cells budget on large boards
- **PR #6870**: fix(router): skip quoted strings when extracting pad blocks (#6740)
- **Issue #6740** (closed): kct route: '(pad ' inside a footprint descr string is parsed as a pad -> 'Unsupported routing geometry'
- **PR #6868**: fix(router): diff-pair tie step honours allowed_layers and hard avoid_layers
- **Issue #6866** (closed): router: diff-pair tie step ignores allowed_layers / plane reservation (USB_D- on In1.Cu GND plane on board 03)
- **PR #6867**: docs: record live Board07 --differential-pairs measurement in recipe notes

- **PR #6864**: fix(router): keep the better of joint and pose-centreline coupled candidates
- **Issue #6738** (closed): ci: Board 07 End-to-End: add a dormant copper-safety (clearance/short) DRC ratchet (live once BOARD_07_CI_ENABLED returns)
- **PR #6764**: ci(board-07): add dormant copper-safety DRC ratchet with committed floor
- **Issue #6862** (closed): test: corridor-guard deadline test flakes under parallel suite load (test_negotiated_tail_abandons_...)
- **PR #6863**: test(router): make the spent-deadline tail test reach the tail on every timing (#6862)
- **Issue #6860** (closed): router: coupled search routes on a hard-avoided pad layer instead of declining like the single-ended router (follow-up #6857)
- **PR #6861**: fix(router): coupled search declines a pair with an SMD pad on a forbidden layer
- **Issue #6287** (closed): board 05: route PHASE_A/B/C instead of skipping them (placement-limited; 12 of 18 unconnected items)
- **Issue #6288** (closed): board 05: pour stitching finds no legal via site for 16 pads at U3 (6 open pour links)
- **Issue #6852** (closed): router: Board07 coupled search exhausts 20k-iteration cap on 6 of 7 pairs once --differential-pairs is enabled
- **PR #6856**: test: replay five more Board07 coupled searches with bounded controls
- **Issue #6836** (closed): [Epic #6441] Phase 2b: dual-row USB-C end — reach the handedness-consistent row through a pass-under gateway (06rf USB2_D)
- **PR #6849**: fix(router): couple 06rf USB2_D through a pass-under gateway (#6836)
- **Issue #6857** (closed): router: coupled diff-pair search ignores DesignRules.allowed_layers, committing signal on reference-plane In1.Cu (Board07 MIPI_DAT0 at 700k)
- **PR #6859**: fix(router): coupled diff-pair search honours allowed_layers and hard avoid_layers
- **PR #6854**: feat(router): lattice search biases toward a net class's preferred_layers
- **Issue #6853** (closed): router: polarity-swap via move emits crossing P/N copper and writes a different path than it checks (allow_swap_via=True only)
- **PR #6855**: refactor: remove unsound swap-via move from CoupledPathfinder
- **Issue #6829** (closed): diffpair: undeclared-protocol USB pairs get generic 0.5 mm skew in check/report though name detection maps USB2 -> USB2_FS
- **PR #6851**: fix(board03): route harnesses and smoke load the committed net-class map
- **Issue #6847** (closed): router: polarity-swap via move skips via-in-pad and records partner's cell; make #6840 tests behavioural (follow-up #6846)
- **PR #6848**: fix(diffpair): swap via move honours via-in-pad and records each rail's own drill (#6847)
- **Issue #6842** (closed): lattice gateways: offer fan-via gateway to tap-capable trunks whose tap retry did not run or was discarded
- **PR #6845**: fix(router): offer the gateway retry to trunks the tap chain did not serve
- **Issue #6839** (closed): lattice: 06b LVDS1[0] U1.7->R1.1 declines pair-partner-pad — fat centerline turns inside the U1 pad row (split from #6836)
- **PR #6843**: fix(router): retry lattice pair search after a partner-pad turn decline
- **Issue #6840** (closed): router: coupled search clears partner vias at trace spacing, so Board07 coupled routes are rejected for P/N overlap (+ NaN in rejection log, optimizer un-couples committed pairs)
- **PR #6846**: fix(diffpair): via-aware partner clearance in the coupled search (#6840)
- **Issue #6834** (closed): lattice gateways (#6444 2a) follow-ups: tap-retry diagnostic restore, tap-trunk gateway gap, chain-wide via limit
- **PR #6844**: fix(router): restore gateway diagnostics on discarded retries and count chain vias per net
- **PR #6841**: refactor(router): extract engine-free router/pairplan package (#6837 slice 2c-i)
- **Issue #6823** (closed): router: oracle-completion pour-link weld emits off-45° pad-to-via stubs (blocks board 03 plan capture, #6394)
- **PR #6825**: fix(stitch): write every pad-to-via stub 45-degree-legal (#6823)
- **Issue #6025** (closed): Annotate pad pin types on fleet boards 00-07 and regenerate their outputs
- **PR #6827**: fix(boards): annotate pad pin types on boards 04-07 and re-pin their evidence (#6025)
- **Issue #6444** (closed): [Epic #6441] Phase 2: pairplan layer — chains through inline parts, terminal gateways, USB-C equivalent pins (lattice consumer)
- **PR #6833**: feat(lattice): fan-via terminal gateways for declined diff-pair segments (#6444 slice 2a)
- **Issue #6826** (closed): router: bound coupled-search spacing tolerance in endpoint zones by pad pitch (Board07 TMDS_D1 needs 402k iterations vs 20k budget)
- **PR #6832**: perf(router): bound coupled endpoint-zone spacing tolerance by pad pitch (#6826)
- **Issue #6820** (closed): fix(check): net-class-map length_constraint without net_id crashes kct check; half-routed capped diff pair skipped silently
- **PR #6830**: fix(check): clean error for length_constraint without net_id; check half-routed pair via caps
- **Issue #6822** (closed): fix(router): mesh engine and parallel grid rebuild ignore interior Edge.Cuts keepout (follow-up #6043)
- **PR #6831**: fix(router): mesh engine and parallel grid rebuild honour interior Edge.Cuts cutouts
- **Issue #6043** (closed): route: block copper across interior Edge.Cuts loops even when no edge clearance resolves
- **PR #6821**: fix(router): block copper across Edge.Cuts cuts when no edge clearance is set
- **Issue #6815** (closed): kct fleet status reports '0 nets in PCB' schematic drift on 5 of 8 boards (false NO); boards 08/09 not surveyed
- **PR #6817**: fix(fleet): false 0-nets-in-PCB drift on KiCad 10 boards; discover board 09; report skipped
- **Issue #6044** (closed): check: per-net-group routing policy gate (max vias, max length, missing policy fails)
- **PR #6818**: feat(check): routing_policy category for per-net via caps and max length (#6044)
- **Issue #6816** (closed): router: capture Board07 TMDS_D1 coupled-search inputs and replay the exhaustion as a unit test
- **PR #6819**: test(router): replay Board07 TMDS_D1 coupled search from a captured fixture
- **PR #6828**: docs(research): correct fix-drc row U3 (local re-route already exists)
- **Issue #5617** (closed): perf(router): profile and reduce the Diff-Pair regression job's re-route step (17.55 min = 96% of the job, unchanged across #5403/#5433/#5488/#5500)
- **Issue #6382** (closed): grid router: wide nets' 45-degree escapes from U2 (board 05) cross neighbour pins' straight lanes, stranding GND_SENSE and VM
- **Issue #6388** (closed): Tracking: boards 03/04/05 lattice runs exit 3 on open pour links (A-D: #6399-#6402); verify board outcome
- **Issue #6378** (closed): lattice: chain-ordered diff-pair endpoints so pairs through pass-through parts (board 03 USB_D via U2) can couple
- **Issue #6045** (closed): fix-drc: rip-up-and-reroute fallback when clearance nudging cannot clear a violation
- **Issue #6810** (closed): 06rf: kct route exits 3 on pre-save clearance self-check that flags coupled MIPI_D0 at its own diff-pair gap (0.102 vs 0.150), while kct check and kicad-cli pass
- **PR #6811**: chore: bump Repo Skills 0.24.1 -> 0.24.3
- **PR #6814**: fix(router): hold coupled P/N traces to the pair gap in validate_routes
- **Issue #6291** (closed): docs: document kct route exit code 10 (UNVERIFIED) from #6282
- **PR #6301**: docs: document kct route exit code 10 (UNVERIFIED)
- **Issue #6812** (closed): test: lock route_geometry via-dedupe + file-order together (follow-up #6794)
- **PR #6813**: test: lock route_geometry via-dedupe + file-order together (#6812)
- **Issue #6800** (closed): fleet_routers: 22 class-clearance 'losses' come from pairing the pre-route .kicad_pro with the routed .kicad_dru
- **PR #6803**: fix(bench): treat class clearance on empty classes as not-routing in dsn_audit
- **Issue #6788** (closed): 06rf: 3 uncoupled diff pairs over the skew limit - pair tuner overshoots on multi-loop inserts (USB3_TX2, PCIE_TX) and tries one placement only (USB3_TX1)
- **Issue #6778** (closed): Tuner and kct check can disagree by a track stub at 3+-way junctions (kicad-cli merge order)
- **PR #6794**: fix(router): tuners measure each net in the order kct check reads it
- **Issue #5947** (closed): router research: evaluate TraceMaker techniques (min-cost-flow escape reservation, nogood cache, boxed-in escalation, deterministic portfolio)
- **Issue #6807** (closed): test_partial_exit_keeps_budget_exit_promotion fails on macOS arm64 main since ~Oct 10 (green on Linux CI)
- **PR #6808**: test(router): take the pose-search wall-clock race out of the partial-exit diff-pair test (#6807)
- **Issue #6799** (closed): board 05: GND_SENSE U2.6–U2.7 still unconnected after #6761 escape fences (last open signal net)
- **PR #6806**: fix(router): tie neighbouring duplicate pins on Kelvin nets (#6799)
- **Issue #6290** (closed): board 05: MOSFET footprints use pads 1/2/3 while symbols use D/G/S (36 LVS pad-name mismatches)
- **PR #6299**: fix(board05): MOSFET symbol pins match TO-220 pad names (#6290)
- **Issue #6296** (closed): flaky: test_per_net_deadline_audit::test_python_deadline_scales asserts wall-clock scaling and fails under runner load
- **PR #6302**: test: make test_python_deadline_scales robust to runner load
- **Issue #5694** (closed): ci: self-hosted 'heavy' runner lane offline since ~10:00Z — all router regression gates queued indefinitely
- **Issue #6805** (closed): ci: kct-check parity tests only run on the Slow Tests schedule, so PRs that move board-07 counts merge green
- **PR #6809**: test: run kct-check parity tests in the PR Test job
- **Issue #6802** (closed): test_kct_check_parity: 4 board-07 parity tests fail on main after length-measurement changes (#6725/#6780)
- **PR #6804**: test: re-pin board-07 kct check parity counts to the kicad-cli length model (#6802)
- **Issue #6795** (closed): pair tuner: add uncoupled-length guard and register the 'overshoot' reason (follow-up to #6791)
- **PR #6797**: fix: guard diff-pair skew tuner on uncoupled length and report overshoot

### 2026-10-15

- **Issue #6798** (closed): fleet comparison docs: class-clearance losses are already carried on main; stop citing #6770/#6765 as the fix
- **PR #6801**: docs(fleet_routers): correct stale #6770/#6765 class-clearance claims
- **Issue #6761** (closed): board 05: re-route PHASE_C escape (layer/approach) or VM pour link so U2.11 connects
- **PR #6796**: fix(router): hard escape fences keep fine-pitch neighbour exits open (#6761)
- **Issue #6792** (closed): kicad_length: Route-form fragments double-count a THT-pad via's span when the net uses two inner layers (+0.53 mm)
- **PR #6793**: fix(router): count a shared via's span once across Route fragments
- **PR #6791**: fix(router): exact-fit diff-pair skew tuning with placement search
- **Issue #6780** (closed): router: per-Route length measure treats every through-hole pad as F.Cu, so tuners mis-measure vias in back-side THT pads (follow-up to #6779)
- **PR #6790**: fix(router): length model reads router pads' footprint side (#6780)
- **Issue #6731** (closed): kicad_length: board 07 BOOT0 reads 0.020 mm off kicad-cli (roundrect corner pad trim)
- **PR #6789**: test(length): pin board 07 BOOT0 net length to kicad-cli (#6731)
- **Issue #6784** (closed): Fleet DSN gate: export copper-edge clearance and recognise implied diff_pair_gap so fastroute rows can become comparable
- **PR #6785**: fix(dsn): carry copper-to-edge clearance via inset boundary; audit implied diff_pair_gap
- **Issue #6786** (closed): flaky: format-json single-document test times out on 'mfr apply-rules' (50s subprocess budget) in CI
- **PR #6787**: ci: stop gating `kicad-cli version` probes behind the native permit (#6786)
- **Issue #6335** (closed): [Parent #5943] Measure and publish the TraceMaker/fastroute/kct fleet comparison
- **PR #6783**: docs: publish live TraceMaker/fastroute/kct fleet comparison
- **Issue #6703** (closed): lattice: 07rf TMDS_D0/D1 skew grows 0.6/1.6 -> 7.2/8.4 mm after #6673's +1V8 pour-continuity keep-outs - escape row B coupled instead of detouring
- **Issue #6781** (closed): test: #6735's partner-aware fallback keep rule has no failing-on-revert unit test
- **PR #6782**: test(router): pin the partner-aware flush keep rule with failing-on-revert tests
- **Issue #6742** (closed): kicad-cli audit follow-up (#6724): render/SVG exports and the DRC safety floor ignore a killed kicad-cli
- **PR #6774**: fix: fail render/SVG exports on killed kicad-cli and warn on empty DRC report
- **Issue #6772** (closed): board06: route Power-class copper to the 0.2 mm clearance (3 netclass_clearance_copper errors)
- **PR #6777**: ci(board06): drop the #6763 Power-clearance allowance from the diffpair gate
- **Issue #6725** (closed): kct check and kicad-cli disagree on via drilled length (1.600 vs ~1.573 mm): 07rf DQS skew 0.000 vs 0.052 mm
- **PR #6779**: fix(length): match kicad-cli line merging and via clipping; #6725 bench table
- **Issue #6773** (closed): Follow-up to PR #6735: pour-neck protection, flush deadline, narrower pad floor for fallback legs
- **PR #6735**: fix(lattice): partner-aware uncoupled diff-pair fallback legs (#6703)
- **Issue #6775** (closed): drc: retry without --refill-zones on rejection, warn when refill is skipped, and cover kct drc + connectivity (follow-up to #6724)
- **PR #6776**: fix(drc): retry without --refill-zones on rejection; classify kicad-cli exits by cause (#6775)
- **Issue #6748** (closed): Board 06 pour repair: use per-pair net-class clearance and true pad shape (sibling of #6736)
- **PR #6762**: fix(board06): hold pour-repair copper off foreign copper at the net-class clearance (#6748)
- **Issue #6763** (closed): board 06: generated .kicad_pro omits the recipe's net classes, so kicad-cli never enforces Power clearance
- **PR #6769**: fix(board06): declare router net classes in the routed .kicad_pro
- **PR #6729**: fix(router): kct check and tuners measure net length exactly as kicad-cli does (#6725)
- **Issue #6724** (closed): run_geometric_drc reports a killed kicad-cli as a clean DRC (empty pre-created report)
- **PR #6746**: fix(drc): a killed or failed kicad-cli is never a clean DRC (#6724)
- **Issue #6733** (closed): flaky: route --lint-gate format-json contract test times out (60s) in CI Test job
- **Issue #6744** (closed): test_format_json_flag_variant_stdout_is_a_single_json_document flakes at its 60s timeout
- **PR #6754**: test: give json flag-variant test a timeout above its inner subprocess guard
- **Issue #6756** (closed): Fleet DSN gate: 10 .kicad_dru losses on board 00 after #6593/#6594 (1 expressible Power clearance, 9 fab-only rules)
- **PR #6768**: fix(benchmarks): classify fab-only and class-scoped DRU rules in DSN audit
- **Issue #6334** (closed): [Parent #5943] Validate a reproducible three-router fleet protocol and DSN comparability gate
- **PR #6747**: chore(benchmarks): record live three-router smoke on board 00 and re-freeze fleet manifest
- **Issue #6722** (closed): Flaky: test_led_matrix_32x32_is_fast wall-clock budget (1.0 s) fails under CI xdist load
- **PR #6751**: test: replace wall-clock LED matrix perf guard with walk work counter
- **Issue #6749** (closed): ecosystem: sweep RepoRecon's KiCad AI index (259 repos) against our registry
- **PR #6767**: docs(ecosystem): record RepoRecon KiCad AI sweep verdicts and add diff script
- **Issue #6766** (closed): ecosystem: offer kicad-tools as a tool under test in devbisme/ai-circuit-design-tests
- **PR #6771**: docs: evaluate ai-circuit-design-tests bench and draft listing offer
- **Issue #6728** (closed): Parity test: cover roundrect_rratio on the native authored-clearance gate (C++ build 56)
- **PR #6758**: test(router): cover roundrect_rratio on the native authored-clearance gate
- **Issue #6736** (closed): Board 07: new 'Power' clearance error (+1V2 stub vs U1.24 GND, 0.1913 < 0.2 mm) after #6713
- **PR #6745**: fix(board07): hold pour-repair copper off foreign pads at the net-class clearance (#6736)
- **Issue #6522** (closed): Verify #6489 board-level acceptance: board 03 P0 benchmark USB_D + board 04/05 slow manufacturability after PR #6498
- **Issue #6750** (closed): test flake: test_bidirectional_exhausted_status times out (>60s) in CI Test job
- **PR #6757**: test: confine bidirectional exhausted-status test to small pockets
- **Issue #6415** (closed): grid router: charge per-pad budgets over the via/trace footprint, not just the centre cell (board 05 VM U2.11)
- **PR #6760**: docs(router): record tip-weighted via-footprint charge experiment on board 05
- **PR #6752**: chore(changelog): close the five v0.23.0 release-gate gaps
- **PR #6755**: chore(release): bump version to 0.23.0
- **Issue #6697** (closed): lattice: board 03 USB_MCU_D and USB_D U2->R4 do not couple with VBUS routed as declared (harness change since #6599; USB_MCU_D skew 3.228 mm fails kct check and kicad-cli)
- **PR #6734**: fix(router): lattice pairs leave a sealed routed pad its escape via (board 03 VBUS U2.5) (#6697)
- **Issue #6743** (closed): lattice: 06b couples 0/4 LVDS pairs since #6720 -- tap-chain retry gate misses pair-no-path-keepout-constrained (slow test_06b_lattice_route_couples_with_copper_on_both_nets fails)
- **PR #6753**: fix(router): tap-chain retry accepts every exhausted pair-no-path reason (#6743)
- **Issue #5693** (closed): Upstream the #5690 backticked-trailer fix to rjwalters/loom and retire the resync-ignore pin
- **Issue #6714** (closed): 06rf: last 2 open pour links after #6671 — GND U2.A1 via sites blocked by signal escape; +3V3 J1.B8 no clearance-legal link
- **PR #6737**: fix(zones): give the inner power plane to the board-wide rail (#6714)
- **PR #6732**: perf(router): answer each via-drop site once per native search call (Part of #5617)
- **PR #6727**: feat(router): opt-in bidirectional A* for the C++ Python fallback
- **Issue #6675** (closed): match-group tuner declines every diff-pair member on lattice output (07rf: DDR/MIPI/TMDS groups stuck at 16.5/7.6/6.0 mm)
- **PR #6726**: fix(router): tune diff-pair members of lattice match groups (#6675)
- **Issue #5899** (closed): Oracle completion: close boxed-in pour pockets (board 03 residual GND island) by local rip-up
- **Issue #5506** (closed): router(HV): attribute and resolve the /FUSED_LINE J2.1→Q2B.2 no-path-layer-constrained leg on softstart rev-C (#4507 T4)
- **Issue #6718** (closed): kct route pre-save clearance check measures round pads as squares (false 0.006 mm violations at board 03 J1 ties)
- **PR #6723**: fix(router): validate_routes measures round/oval/roundrect pads with the clearance kernel (#6718)
- **Issue #6721** (closed): Rebase PR #6713 onto #6699: single pad model + 5-board before/after evidence
- **Issue #6716** (closed): native slot gate: pytest-timeout still fires while a test is queued for a permit (credit applied only after the wait)
- **PR #6717**: test: credit native slot-queue wait to the pytest-timeout deadline during the wait (#6716)
- **Issue #6671** (closed): pour oracle: 06rf closes 0 of 128 GND/+3V3 links - U2 BGA welds break Power clearance and the 2-retry budget discards the whole round
- **PR #6713**: fix(route): pour oracle keeps clean closures; exact roundrect weld clearance (#6671)
- **PR #6715**: chore: bump Repo Skills 0.19.5 -> 0.24.1
- **PR #6719**: perf(router): check a node's via drop once, not once per target layer (Part of #5617)
- **PR #6720**: fix(router): lattice diff pairs keep coupling with VBUS routed (board 03) (#6697)
- **Issue #6693** (closed): chorus-test U8 "routing-caused" shorts are input pad overlaps (stale pad angles); kct route misattributes them to routing
- **PR #6711**: fix(route): attribute shorts against overlapping input pads to the input (#6693)
- **PR #6707**: fix(route): write routing-plan sidecar on deadline/partial-save exit
- **Issue #6698** (closed): lattice: launch USB-C pairs from the row facing the chain and tie the other row with generated necked stubs (board 03 USB_D uncoupled 36 -> ~10 mm; #6444 slice)
- **PR #6712**: fix(router): launch USB-C pairs from the facing row and tie the far row (#6698)
- **Issue #6670** (closed): lattice: 06rf USB3_RX2+/USB3_TX2- uncoupled-fallback legs pass 0.17 mm from U2 GND balls (Power 0.2 mm) and are demoted by the authored-floor backstop
- **PR #6705**: fix(router): grow a pad array whole when lattice class shortfalls recur on it
- **Issue #6683** (closed): kct stitch: extended-escape via clearance models rect pads as circles (board 07 U4.E3/E5 vs F3/F5 at 0.1931mm)
- **PR #6699**: fix(stitch): shape-aware via-vs-pad clearance for rect/square pads (#6683)
- **Issue #6664** (closed): lattice: auto-run diff-pair skew tuning on lattice output, engaged on kct check's skew limit (board 06b: 4 skew errors -> manufacturable)
- **PR #6706**: fix(router): auto-run diff-pair skew tuning on lattice output at kct check's limit (#6664)
- **Issue #6710** (closed): Re-run #6578 chorus plan-overflow measurement (operator host)
- **Issue #6701** (closed): kct stitch places through vias for a net whose only pour is on the pad's own layer (board 07: all 14 +1V8 stitch vias via_dangling)
- **PR #6709**: fix(stitch): skip pads whose net is poured only on the pad's own layer (#6701)
- **Issue #6704** (closed): Remove remaining manual-intervention hints (recovery, stitch, netlist sync, ERC explain)
- **PR #6708**: fix: replace remaining manual-intervention hints with kct commands
- **Issue #6678** (closed): Via span_type follow-ups: diffpair_length via barrel length and MCP via writer ignore blind/buried
- **PR #6690**: fix(router): blind/buried vias keep their span in diff-pair length and MCP via writer
- **PR #6702**: feat(bench): PCBWorld D3 CP@1/CP@5 aggregation, rollouts and keyed resume (part of #6332)
- **Issue #6508** (closed): router: Python fallback A* admits a step 0.07 mm from an off-grid foreign via; 44/44 PWM_C candidates rejected on board 05
- **PR #6680**: fix(router): re-stamp surviving route halos after rip-up (#6508)
- **Issue #6669** (closed): kct route: --net-class-map pour net with a non-power name (06rf VBUS_USB) gets neither a zone nor traces
- **PR #6686**: fix(router): honour --net-class-map pour declarations in auto-pour and auto-skip (#6669)
- **Issue #6673** (closed): lattice: TMDS single-ended legs cut U4's BGA +1V8 field on F.Cu, stranding a pour island the oracle cannot close (board 07rf 33/34)
- **PR #6700**: fix(router): keep area-array pour clusters whole on the lattice engine
- **Issue #6684** (closed): kct stitch: via_in_pad post-filter drops a via after later pads counted it as connectivity
- **PR #6696**: fix(stitch): decide via_in_pad / hole-to-hole drops before recording a placement (#6684)
- **Issue #6672** (closed): kct route: replace 'complete manually in KiCad' hints with automated next steps
- **PR #6676**: fix(route): replace manual-KiCad completion hints with automated next steps
- **Issue #6674** (closed): lattice: auto-run match-group length tuning on lattice output (board 07rf: ADDR_BUS 25.1 mm -> 0.002 mm, never engaged without --length-match-groups)
- **PR #6692**: fix(router): auto-run match-group length tuning on lattice output (#6674)
- **Issue #6666** (closed): chorus-test R2 recipe routes 0/37 nets on main (was 30/51): J2/J4 placement-invalid, U5/U8 escape collapse at 0.0508 mm grid
- **PR #6695**: fix(route): fall back to the escape pre-phase when adaptive Phase 1 would hard-fail; expose --grid-strategy (#6666)
- **Issue #6685** (closed): kct stitch --thermal/--blanket: sibling target nets' existing tracks/vias are not obstacles (multi-net)
- **PR #6689**: fix(stitch): thermal/blanket honour sibling target nets' existing copper
- **Issue #6443** (closed): [Epic #6441] Phase 1: protocol-aware pair rules — uncoupled length in mm, KiCad-native diff_pair_gap/uncoupled/skew/via_count, continuity demoted
- **PR #6691**: chore(loom): unpin upstreamed Loom files and re-port champion files (2am#4345)
- **Issue #6640** (closed): Source blind/buried and microvia capability for at least one HDI-capable fab profile
- **PR #6681**: feat(mfr): source PCBWay blind/buried and microvia capability
- **PR #6687**: chore(loom): replace retired operator labels in pinned Loom files (2am#4345)

### 2026-10-14

- **PR #6688**: chore(labels): stop declaring retired Loom labels in labels.yml (2am#4345)
- **Issue #6677** (closed): Via.span_type: round-trip through access-witness sidecar; clear it when a merge widens a via to both outer layers
- **PR #6679**: fix(router): keep Via.span_type honest across sidecar and merges
- **Issue #6528** (closed): router: align the Python own-via spacing check with the validator's epsilon once #6520 and #6521 are both merged
- **PR #6663**: fix(router): Python own-path via check uses the kernel epsilon (#6528)
- **Issue #6668** (closed): kct stitch: multi-net pass ignores pre-existing sibling-target-net tracks/vias (board 07 U3 GND/+1V8 short)
- **PR #6682**: fix(stitch): treat sibling stitch nets' existing tracks/vias as obstacles (#6668)
- **Issue #6662** (closed): test_lattice_kelvin_physical_5444: two buried-via span tests fail on main
- **PR #6665**: fix(router): honor KiCad blind/buried vias in Via.drilled_layers (#6662)
- **Issue #6572** (closed): Board 07 regression recipe: 0.15mm via clearance adds one kicad-cli tracks_crossing (26 vs 25 errors)
- **Issue #6578** (closed): Measure plan-overflow vs stuck-class on chorus-test (deferred half of #6309)
- **PR #6667**: docs(research): record chorus-test plan-overflow vs stuck-class run (#6578)
- **Issue #6645** (closed): flaky: test_kept_copper_vs_foreign_pad_6229 TestCliPreserveExisting hits 60s timeout on CI (≈5s locally)
- **PR #6659**: test: stub kicad-cli discovery in kept-copper CLI tests
- **Issue #6638** (closed): diffpair_bench: continuity_threshold_pct missing from _LIMIT_KEYS, so pair_digest changes when only a limit changes
- **PR #6660**: chore(research): exclude continuity/skew limits from diffpair pair_digest
- **Issue #6626** (closed): kct route: _partial and checkpoint writes skip preserved-copper dedupe (preserved + re-routed net doubled)
- **PR #6658**: fix(route): dedupe preserved copper in _partial and checkpoint writes
- **Issue #6625** (closed): kct route: adaptive/combined deadline or Ctrl+C during the first tier saves no _partial
- **PR #6657**: fix(route): save _partial on first-tier deadline in adaptive/combined flows
- **Issue #6630** (closed): Router: remaining via.layers barrel readers outside the #6502 scope (terminal_connectivity, kelvin, io, lattice, escape)
- **PR #6655**: fix(router): read Via.drilled_layers at remaining barrel readers
- **Issue #6644** (closed): flaky: test_drc_skipped_for_time_banner_and_exit times out waiting for native-concurrency slot
- **PR #6652**: test: stop DRC-skipped verdict tests queueing for a native slot
- **Issue #6615** (closed): diffpair_uncoupled_length: add the per-terminal (gather-ignore) uncoupled figure #6443 designed
- **PR #6651**: feat(validate): per-terminal uncoupled-length figure for diff pairs (#6615)
- **Issue #6634** (closed): router: RSMT Steiner virtual pads pin the first terminal's layer, forcing junction vias during routing
- **PR #6654**: fix(router): make RSMT Steiner virtual pads layer-agnostic
- **Issue #6648** (closed): kct route --analyze --dry-run still routes in memory on escalation flows (no analysis printed)
- **PR #6650**: fix(route): --analyze --dry-run never routes, on any flow (#6648)
- **Issue #6602** (closed): SES import: Via[i-j] fallback indexes a different layer list than dsn.py writes it from
- **PR #6649**: fix(export): SES Via[i-j] fallback no longer yields blind/buried vias
- **Issue #6646** (closed): board 05: 11 full-stack vias mislabelled as micro in routed PCBs (now flagged by via_type check)
- **PR #6653**: fix(router): decline in-pad micro via when landing layer is non-adjacent
- **Issue #6352** (closed): Experiment: min-cost-flow escape-corridor reservation for dense packages
- **PR #6656**: feat(router): experimental min-cost-flow escape-corridor assignment (#6352)
- **Issue #6532** (closed): Board evidence JSON records absolute host paths in "file" (boards 00, 04-07)
- **PR #6623**: fix(check): record portable PCB paths in kct check / net-status JSON (#6532)
- **PR #6641**: perf(geometry): inline segment_to_segment_distance leaf (Part of #5617)
- **Issue #6526** (closed): router: Python AStarNode.via_anc self-reference turns every search's node tree into cyclic garbage
- **PR #6639**: perf(router): Python A* via_anc no longer self-references via nodes
- **Issue #6567** (closed): diffpair bench: run the missing seed 43, 06rf and grid 06b rows for #6443 piece (b)
- **PR #6637**: docs(research): diffpair bench gap rows for #6443 piece (b)
- **Issue #6563** (closed): kct route --dry-run routes for real and writes files on the layer-escalation path
- **PR #6633**: fix(route): --dry-run prints the plan and exits before any routing or writes
- **PR #6647**: docs(changelog): correct boards affected by #6599 auto-pour change
- **Issue #6553** (closed): kct route: adaptive/combined/main flows' partial saves can drop --preserve-existing copper
- **PR #6624**: fix(route): keep --preserve-existing copper in adaptive/combined/main partial saves
- **Issue #6353** (closed): Experiment: validity-aware nogood cache for negotiated routing retries
- **PR #6605**: feat(route): opt-in validity-aware nogood cache experiment
- **Issue #6561** (closed): Reconcile jlcpcb mfr_limits min clearance (0.127) with per-layer .kicad_dru floors (0.1016/0.0889) (#6534 follow-up)
- **PR #6629**: fix(router): stackup-aware fab clearance floor matches the emitted .kicad_dru (#6561)
- **Issue #6518** (closed): kct check --mfr does not flag blind/buried vias on a fab profile that cannot build them
- **PR #6636**: feat(check): flag blind/buried/micro vias on fab profiles that cannot build them
- **Issue #6614** (closed): Route remaining via-layer readers through kicad_read_via_layers (#6493 follow-up)
- **PR #6635**: fix: route the remaining via-layer readers through kicad_read_via_layers (#6614)
- **Issue #6599** (closed): kct route auto-pour ignores --net-class-map (board 03 VBUS declared routed is poured anyway)
- **PR #6631**: fix(route): auto-pour honours --net-class-map routed declarations
- **Issue #6642** (closed): main red: test_dsn_audit_detects_omitted_constraints and stored-copper span2 via test fail after #6600/#6613
- **PR #6643**: chore(research): repair DSN audit and span2 via test after #6594/#6502
- **Issue #6560** (closed): Floor the remaining coupled-path pair-gap readers (serpentine, _pair_seg_clearance, C++ halo) (#6534 follow-up)
- **PR #6611**: fix(route): floor the remaining coupled-path pair-gap readers (#6560)
- **Issue #6566** (closed): mesh engine routes only 2/16 (06b) and 5/24 (03) nets; 2-pad nets come back partial
- **PR #6604**: fix(router): mesh negotiator exempts a net's own committed copper
- **Issue #6576** (closed): diffpair_uncoupled_length polish: upgrade-note wording on via-count warning, 'vias' metric label, pair-block header comment
- **PR #6620**: fix: label via-count findings 'vias' and polish pair-block header (#6576)
- **Issue #6544** (closed): Flaky: test_fine_pitch_advisories_fire_for_grid_engine fails intermittently on main
- **PR #6618**: test(route): pin --backend in the fine-pitch grid advisory test
- **Issue #6609** (closed): Flaky: test_analysis_is_read_only trips on a concurrent board-00 .kicad_pro.lck under xdist
- **PR #6617**: test(net-status): scope read-only check to board 05 and ignore KiCad lock files
- **Issue #6596** (closed): router: emits redundant single-layer junction via (via_dangling warning) on board 01 VOUT
- **PR #6632**: fix(router): drop single-layer junction vias in cleanup_artifacts (#6596)
- **Issue #6493** (closed): Read a keyword-less partial-span (via ...) as a through via, as KiCad does (reader half of #6485)
- **PR #6613**: fix(pcb): read a keyword-less partial-span via as a through via, as KiCad does (#6493)
- **Issue #6589** (closed): lattice: #6401 plane-via site keep-out ignores plane fill pullback, so board 03's VBUS U1.7 pour link stays open (blocks #6394)
- **PR #6612**: fix(route): keep a reserved plane-via site on its pour's fill (#6589)
- **Issue #6509** (closed): router: Python segment-vs-pad validator has no clearance epsilon; rejects exact-clearance steps the C++ validator accepts
- **PR #6627**: fix(router): give the Python clearance validators the native 1e-4 mm slack
- **Issue #6502** (closed): Router producers still hold a logical layer pair on non-micro vias in memory
- **PR #6628**: fix(router): clearance, connectivity and length readers use Via.drilled_layers
- **Issue #6594** (closed): DSN export ignores .kicad_pro net classes, .kicad_dru rules, zones and keepouts
- **PR #6600**: fix(export): DSN export reads .kicad_pro classes, .kicad_dru floors, zones and KiCad 10 nets
- **Issue #6533** (closed): Slow mfr-tier tests error at setup on main: _assert_deterministic matches the #6329 banner text (was: assert 7 > 7)
- **PR #6621**: test: anchor mfr-tier deadline-fired check to the line-start marker
- **Issue #6557** (closed): Reverted rotate_180 placement delta leaves keep-upright silk/fab text turned 180 degrees
- **PR #6619**: fix(router): restore keep-upright text when reverting a rotate delta
- **Issue #6582** (closed): board09 routing-investigation README: SENSE_P track coordinates don't match the .kicad_pcb files
- **PR #6622**: docs(board09): quote SENSE_P artifact track in file coordinates
- **Issue #6310** (closed): Probe: shrink board 03's 1.5x rigid-cluster route back to 1x and see whether USB_D+ survives
- **PR #6579**: docs(research): shrink board 03 rigid 1.5x route to 1x under the lattice protocol, no-go (#6310)
- **Issue #6531** (closed): route_cmd: _INPUT_COPPER_BASELINE is never cleared when staging is skipped (in-place route)
- **PR #6616**: fix(route): record copper-loss baseline on in-place staging runs
- **Issue #6555** (closed): board 03: committed readiness.json is stale (net_class_map.json changed) after #6443 piece (a)
- **Issue #6608** (closed): fix(validate): mypy union-attr errors on main from #6603 (check_diffpair_clearance_intra)
- **Issue #6610** (closed): Flaky: test_analysis_is_read_only trips on a stray boards/00 .kicad_pro.lck written by a concurrent test
- **Issue #6593** (closed): SES import drops every wire from standard Specctra sessions (network_out nesting, resolution ignored)
- **PR #6601**: fix(export): import standard Specctra sessions in SES importer (#6593)
- **PR #6606**: perf(router): walk only acted-on blocked cells in GridCollisionChecker.path_is_clear (Part of #5617)
- **PR #6607**: chore(types): fix main Type Check — narrow Optionals in diffpair intra check
- **PR #6595**: chore(research): three-router fleet protocol and DSN comparability gate (#6334)
- **Issue #6499** (closed): router: coupled diff-pair search accepts a goal reached on the wrong layer (SMD goal pads)
- **PR #6597**: fix(router): coupled diff-pair search must reach SMD goal pads on their layer (#6499)
- **Issue #6575** (closed): kct check: hold unrecognised-name pairs (X_POS/X_NEG) to the floored pair gap, not the class intra_pair_clearance
- **PR #6603**: fix(check): hold diff pairs to the floored pair gap in diffpair_clearance_intra
- **Issue #6329** (closed): kct route --deterministic-budget --seed: same-seed runs of board 05 differ on 4 nets, and seeds 42 and 43 can match exactly
- **PR #6585**: fix(route): log the wall clocks that still bind under --deterministic-budget (#6329)
- **Issue #6309** (closed): Evaluate: report over-capacity global-routing edges as a placement-spread trigger (successor to expand-route-shrink)
- **PR #6574**: docs(research): plan overflow edges vs stuck classes, no-go on placement_nudge trigger
- **Issue #6317** (closed): board-view: draw zone fill, DRC markers, before/after pairs and net-class colours
- **PR #6577**: feat(board-view): zone fill, DRC markers, before/after pairs, net-class colours
- **Issue #6391** (closed): Access witness: compute replay before grid release so closing_markings survive (follow-up to #6346)
- **PR #6598**: fix(router): prime the access witness before the routing grid is released (#6391)
- **Issue #6475** (closed): diffpair bench: 06b grid seed 42 is non-deterministic under --deterministic-rescue (P0 #6442 AC gap)
- **PR #6588**: fix(route): --deterministic-rescue covers escape budget, per-iteration cap and checkpoint RNG (#6475)
- **Issue #6366** (closed): Evolutionary router: plumb or remove the inert astar_weights / preferred_layers / strategy_flags genes
- **PR #6590**: feat(router): plumb evolutionary astar_weights/preferred_layers, drop strategy_flags
- **PR #6586**: feat(board03): annotate pad pin types for the Board 03 family (Part of #6025)
- **Issue #6339** (closed): Precompute crossing and layer-assignability of nets to spot unroutable bundles before and during routing
- **PR #6591**: feat(net-status): add --crossings pre-route bundle crossing report (#6339)
- **Issue #6331** (closed): [Parent #5942] PCBWorld Phase 1: integrate pinned D3 acquisition, guide preparation, and dual scoring
- **PR #6592**: feat(bench): add PCBWorld D3 suite to kct bench external (#6331)
- **Issue #6484** (closed): Board 09 routing-investigation boards: SENSE_P track shorts U3.9 (SENSE_N), reported by kicad-cli
- **PR #6581**: docs(board09): record SENSE_P/U3.9 short as known artifact (#6484)
- **PR #6556**: feat(check): uncoupled-length rule, continuity to info, native pair rules (#6443 piece b)
- **Issue #6354** (closed): Board 01 generator drifts from committed outputs: different route (15 seg/4 vias vs 21/2) and drops 3D model blocks
- **PR #6587**: fix(board01): generator emits 3D models; regenerate routed PCB and package (#6354)
- **Issue #6319** (closed): board-view: missing-link choice differs between frames on ties; duplicate pad numbers give same-named link ends
- **PR #6580**: fix(board-view): frame-independent missing links and end coordinates in text (#6319)
- **Issue #6285** (closed): Board 07: routing frame has no .kicad_pro/.kicad_dru beside it, so kct route sees none of the board's clearance rules
- **PR #6571**: fix(board07): copy project sidecars beside the routing frame (#6285)
- **Issue #6307** (closed): router: board 04 loses net SWO when its placement is spread to 2x (more room, fewer nets routed)
- **PR #6573**: test(route): pin board 04 uniform-2x routes 9/9 (#6307)
- **Issue #6330** (closed): kct route --timeout: a deadline during placement-delta feedback can leave a _partial board with snapshot_saved false and no snapshot_state
- **PR #6583**: fix(route): publish partial-save snapshot facts before the witness write
- **PR #6558**: perf(router): memoise the route-halo trace verdict in the pure-Python A* fallback (Part of #5617)
- **Issue #6495** (closed): connectivity: lone-fill zone_island warnings still counted as unconnected by summary, validate output and fix-drc
- **PR #6584**: fix(validate): stop counting lone fill islands as unconnected items (#6495)
- **Issue #6284** (closed): Board 04: re-review reviewed escape repair at the declared 0.15 mm via rule (drop --via-clearance 0.2 pin)
- **PR #6569**: fix(board04): drop the --via-clearance 0.2 pin (#6284)
- **Issue #6321** (closed): kct route: a deadline during --placement-feedback saves the in-progress re-route in place of the loop's best routes
- **PR #6549**: fix(route): placement-feedback partial save keeps the loop's best routes (#6321)
- **Issue #6551** (closed): mesh engine + --differential-pairs may double-route pair nets like the lattice did (#6535 follow-up)
- **PR #6565**: fix(route): mesh engine owns its diff-pair nets; reject mesh + --differential-pairs (#6551)
- **Issue #6550** (closed): lattice tap-chain retry: score all pairs before keeping, and test the discard path (#6535 follow-up)
- **PR #6568**: fix(route): lattice tap-chain retry scores every pair before keeping (#6550)
- **Issue #6337** (closed): docs(research): expand-route-shrink note: classifier-count loss range is 0 to 4, not 0 to 3; list lead-in says three for four bullets
- **PR #6564**: docs(research): fix classifier-count loss range and list lead-in in expand-route-shrink note
- **Issue #6361** (closed): kct route: --early-stop-patience 0 cannot make the negotiated loop use its budget; the overflow-stagnation stop still ends board 05 at iteration 8 of 40
- **PR #6570**: fix(route): make --early-stop-patience 0 disable the overflow-stagnation stop (#6361)
- **Issue #6308** (closed): kct route --timeout: sidecar unverified_output names a copper-less board when the deadline lands in the routing stage
- **PR #6546**: fix(route): sidecar names copper-bearing _partial after routing-stage deadline
- **Issue #6328** (closed): kct route auto-grid: board 05's U2 fine zone flips between 0.05 mm and 0.005 mm when the part is translated
- **PR #6562**: fix(router): solve a fine zone's pitch from all of the part's pads (#6328)
- **Issue #6364** (closed): monte-carlo/evolutionary: best-trial restore hides an inconsistent placement restore; progress-callback cancel still waits on workers
- **PR #6559**: fix(route): keep 'inconsistent' through best-trial restore; MC cancel returns best without waiting
- **Issue #6325** (closed): kct route: an interrupt inside a mirror trial's application can save flipped silk/fab for that footprint in the partial board
- **PR #6547**: fix(route): signal inside a mirror trial no longer saves flipped silk/fab
- **PR #6548**: perf(router): prune provably-clear pads in the A* own-pad step gate (Part of #5617)
- **Issue #6322** (closed): kct route: the default layer-escalation flow saves no partial board on a deadline, and a kept placement delta replaces the checkpoint with a copper-less board
- **PR #6552**: fix(route): save a _partial board from the default layer-escalation flow (#6322)
- **Issue #6478** (closed): slow-tests: mfr_tier_4l integration fixture runs ~4200 s and holds a worker ~70 min; Sunday board 05 guard leaves little headroom
- **PR #6539**: test: bound the board 04 4L mfr-tier fixture at 5k expansions and 1100 s; print slow durations
- **PR #6534**: fix(router): floor the coupled diff-pair gap at the authored board minimum; emit native diff_pair_gap rules (#6443 P1c)
- **Issue #6351** (closed): kct route --nets: copper-loss guard compares against the run's own checkpoint and refuses to save
- **PR #6511**: fix(route): copper-loss guard compares against input copper, not own checkpoint
- **Issue #6535** (closed): lattice couples 0/4 pairs on 06b on main; #6440 head coupled 3/4 (regression since merge)
- **PR #6542**: fix(lattice): one engine owns diff-pair copper; retry a declined trunk as a tap chain (#6535)
- **Issue #6540** (closed): Slow Tests red on main: board 06 regression-fixture class-map hash pin stale after #6517
- **PR #6541**: test: re-pin board 06 regression-fixture class map after #6517 (#6540)
- **Issue #6369** (closed): kct net-status --why: carry the router's per-net search-failure reason so full-board routes are not mislabelled budget_starved
- **PR #6538**: fix(net-status): carry router per-net search-failure reason so full-board routes are not mislabelled budget_starved
- **Issue #6543** (closed): main red: test_via_type_keyword_6485 coupled-route via count 2 != 4 (cpp + python)
- **PR #6545**: test(router): give the #6485 coupled fixture a legal hole-to-hole route
- **Issue #6306** (closed): kct route refuses a 120x80 mm board with one LQFP-48: auto-grid error omits --max-cells and advises enlarging the board
- **PR #6537**: fix(router): auto-grid refusal names concrete --max-cells value (#6306)
- **Issue #6360** (closed): kct route --route-engine mesh ignores plane-layer reservation and net-class avoid_layers (needs through-via transitions over blocked inner layers)
- **PR #6525**: fix(router): mesh engine honours plane-layer reservation and avoid_layers
- **Issue #6491** (closed): net-status: status is 'complete' while open_connections is 1 when a detached track or via exists
- **PR #6515**: fix(net-status): fail the gates on stranded copper (#6491)
- **PR #6530**: perf(router): skip unblocked cells in optimizer grid collision walk (Part of #5617)
- **Issue #6494** (closed): Board 03: trim J1 USB-C silk to clear Edge.Cuts (2 silk_edge_clearance warnings block readiness)
- **PR #6529**: fix(board03): clip J1 silk back from Edge.Cuts to the silk_edge_clearance floor (#6494)
- **Issue #6485** (closed): Keyword-less partial-span (via ...) is through in KiCad but blind in kicad-tools connectivity
- **PR #6514**: fix(router): never write a partial-span via without its type keyword (#6485)
- **Issue #6397** (closed): router: pure-Python A* lacks #6375 own-pad step gate; fallback burns minutes on validator-rejected pad-exit tails
- **PR #6521**: perf(router): own-pad step gate in the bidirectional Python A* (#6397)
- **Issue #6473** (closed): router: Python fallback A* and C++ CoupledPathfinder lack the #6464 within-path via hole-to-hole check
- **PR #6520**: fix(router): coupled diff-pair search holds its own vias to the hole-to-hole floor (#6473)
- **Issue #6269** (closed): Board 04: readiness.json is a stale legacy report with no producer (and U2 reference silk over pads)
- **PR #6512**: fix(boards): re-issue board 04 readiness via gated producer; move U2 ref silk off pads
- **PR #6517**: feat(diffpair): one protocol defaults table; board 03 USB skew at 2.5 mm (#6443)
- **PR #6492**: fix(boards): annotate pad pin types on board 02 and regenerate its package
- **Issue #6256** (closed): optimize-placement decoupling snap leaves 7 silkscreen warnings on board 04 (reference text not considered)
- **PR #6503**: fix(placement): re-anchor reference text disturbed by the decoupling snap (#6256)
- **Issue #6320** (closed): kct route: the access-witness sidecar of an interrupted placement-delta trial journals copper the saved board does not contain
- **PR #6524**: fix(router): truncate the commit journal when a placement trial is discarded without a grid rebuild
- **Issue #6362** (closed): kct route --route-engine lattice: negotiation round cap is hardcoded (8, 12 with pairs) and --iterations does not reach it
- **PR #6523**: feat(route): add --lattice-iterations and log what stopped lattice negotiation
- **Issue #6489** (closed): board 03 under the P0 pair benchmark: authored-netclass final pass demotes USB_D+/USB_D- to unrouted (0.175 mm vs 0.2 mm at a net-0 pad)
- **PR #6498**: fix(router): re-negotiate with grown pads when class-pad repair cannot clear a slot
- **PR #6497**: perf(sexp): single-pass parse path + GC pause for large boards (#5617)
- **Issue #6283** (closed): kct route-auto: resolve trace/via clearance from .kicad_pro/.kicad_dru, not the .kicad_pcb alone
- **PR #6519**: fix(route-auto): resolve trace/via clearance from .kicad_pro/.kicad_dru (#6283)
- **Issue #6490** (closed): Connectivity: exact-abutment pads (shared edge) disagree with kicad-cli in the copper-contact model
- **PR #6516**: fix(validate): bond abutting exact pad outlines like kicad-cli (#6490)
- **Issue #6347** (closed): Evolutionary worker trials skip keepout rule areas (#6008 payload not installed)
- **PR #6513**: fix(router): evolutionary trials honour keepout rule areas via shared worker builder (#6347)
- **Issue #6324** (closed): kct route: Ctrl+C does not always stop the run (SystemExit raised in the signal handler is discarded inside a destructor)
- **PR #6507**: fix(route): stop at the next stage boundary when an interrupt's exit is swallowed
- **Issue #6286** (closed): Router: relief rescue commits vias below via_clearance from foreign pads (masked by the old 0.20 mm default)
- **PR #6510**: fix(router): validate relief probe path before committing it as a fallback
- **Issue #6327** (closed): stuck classifier: a power net with no zone is labelled pour_discontinuous (board 05 VM)
- **PR #6506**: fix(router): stuck classifier requires a zone before calling a power net pour_discontinuous
- **Issue #6303** (closed): bug(witness): access-witness replay judges exit stubs against the FINAL board's segment R-tree, not the replayed copper
- **PR #6504**: fix(router): access-witness replay suspends the segment R-tree (#6303)
- **Issue #6294** (closed): clearance_resolver: via-scoped DRU recogniser misreads parenthesised '(… || …) && X' conditions as board-wide via rules
- **PR #6505**: fix(router): split DRU via conditions only at paren depth 0 (#6294)
- **Issue #6486** (closed): chore(deps): pin site/ typescript back to ^6 (TS 7 from #6481 breaks npm ci against @astrojs/check)
- **PR #6500**: chore(deps): pin site/ typescript back to ^6.0.3
- **Issue #6338** (closed): test: test_board06_default_repeat_export_preserves_non_lcsc_spec fails when a KiCad lock file appears in boards/06-diffpair-test during the run
- **PR #6501**: test: ignore KiCad lock files in board 06 export snapshot (#6338)
- **Issue #6468** (closed): main CI red: flaky timing budgets (deadline_hit assert + 60s pytest-timeouts) across Test/Test (extended)
- **PR #6496**: test: give test_json_report_schema a generous per-link budget (#6468)
- **Issue #6429** (closed): Router coupled-routes diff pairs KiCad's isCoupledDiffPair() does not recognise (_POS/_NEG, lowercase _p/_n), so both engines flag their intra-pair gap
- **PR #6488**: fix(drc): exempt router-coupled pairs KiCad does not recognise by name (#6429)
- **Issue #6467** (closed): router: board 06b grid-router run with seed 42 is nondeterministic across repeated runs
- **Issue #6313** (closed): Changelog workflow's cancel-in-progress leaves cancelled check runs on a PR head, which post-verdict.sh reads as red and refuses approval
- **PR #6440**: fix(lattice): chain-ordered diff-pair segments through pass-through parts (board 03 USB_D)
- **Issue #6395** (closed): board 05: 27 copper violations of the router's own net classes (Power/HighCurrentSignal/Digital) exposed by #6390
- **Issue #6295** (closed): Finish Dependabot→Renovate migration: disable Dependabot PR generation
- **Issue #6289** (closed): pour stitching ignores fab hole-to-hole spacing (board 05: 5 stitch vias at 0.457–0.496 mm vs 0.5 mm)
- **Issue #6470** (closed): ConnectivityValidator.validate() matches trace endpoints to pad centres, not pad copper (false partial when a trace lands off-centre)
- **Issue #6453** (closed): NetStatusAnalyzer misses thermal-spoke contact shallower than POUR_PAD_ERODE (fresh board 04: +3.3V 5 islands, KiCad connected)
- **Issue #6452** (closed): NetStatusAnalyzer misses a via that bridges fill islands of two different zones (fresh board 03: false GND open)
- **PR #6487**: fix(connectivity): one copper-contact model matching kicad-cli (#6452, #6453, #6470)

### 2026-10-13

- **Issue #6437** (closed): lattice class-pad repair (#6395): skip or skew-check diff-pair nets routed uncoupled; repair or report via shortfalls; log the HV skip
- **PR #6477**: fix(lattice): hold diff-pair nets to a 0.01 mm length budget in class-pad repair, report via shortfalls, log HV skip (#6437)
- **PR #6349**: fix(boards): annotate pad pin types on boards 00 and 01 and regenerate their packages
- **Issue #6425** (closed): Oracle DRC gate bans every weld when the recipe's declared via is below the project minimums (board 04)
- **PR #6480**: fix(oracle): waive declared-size closer via dimensions in the DRC gate; de-dup route DRC; board 04 drops its reservation pin (#6425)
- **PR #6300**: fix(stitch): apply fab hole-to-hole minimum to stitch via placement
- **Issue #5768** (closed): ci: replace stale fork Test projection with hosted measurements after #6013
- **PR #6326**: docs(ci): measured hosted Test durations, 75m timeout, current PR CI guidance
- **PR #6462**: perf(router): skip the dead shapely distance in RouteHaloGeometry.clear (Part of #5617)
- **PR #6481**: chore(deps-dev): bump typescript from 5.9.3 to 7.0.2 in /site
- **PR #6438**: fix(board05): repair 27 netclass_clearance_copper findings in committed copper (Part of #6395)
- **PR #6454**: chore(boards): refill saved zones and refresh bundles/readiness for boards 00-02, 04, 06, 07
- **PR #6372**: chore(deps): update dependency typescript to v7
- **PR #6482**: chore(deps): bump fastmcp from 4.0.5 to 4.0.10 in the uv-minor-patch group across 1 directory
- **PR #6359**: chore(deps): update dependency pandas to v3
- **PR #6357**: chore(deps): update dependency python to 3.14
- **Issue #6463** (closed): test: run the lattice manufacturable guard (board 04) in nightly slow tests; it is currently hidden by its env-var skip
- **PR #6474**: test: run the lattice manufacturable guard (board 04) in nightly slow tests
- **PR #6483**: Dependabot: render dependabot.yml from the org policy
- **PR #6479**: Remove Renovate config: Dependabot takes over
- **PR #6476**: Dependabot: render dependabot.yml from the org policy
- **Issue #6442** (closed): [Epic #6441] Phase 0: diff-pair benchmark, router-produced guard, board 06 gap-error discrepancy, router/checker threshold parity
- **PR #6461**: feat(bench): router-produced diff-pair benchmark, board 06 gap root cause, threshold parity (Phase 0, #6442)
- **Issue #6464** (closed): grid router commits same-net signal vias below fab hole-to-hole minimum (board 05: EN_A 0.461 mm vs 0.5 mm)
- **PR #6472**: fix(router): hold a route's own vias to the fab hole-to-hole floor (#6464)
- **Issue #6431** (closed): kct fix-drc reports 'All violations repaired!' while kicad-cli still finds the same net-class clearance violations (board 05)
- **Issue #6434** (closed): repair-clearance reports 38/40 'repaired' while both DRC engines still see all 40 (nudges slide along the track axis)
- **PR #6459**: fix(drc): re-measure clearance repairs; fix-drc gates success on a re-check
- **Issue #6428** (closed): Stitcher stub checks are layer-blind for non-reserved candidates; making them layer-aware needs a per-net oracle link gate
- **PR #6466**: fix(oracle): per-net link gate, layer-aware stub checks, static corridor flanks (#6428)
- **Issue #6460** (closed): net-status: strict trace-to-pad contact (_pad_polys) ignores absolute pad rotation, giving false contacts and opens
- **PR #6469**: fix(connectivity): orient all pad copper by absolute pad rotation
- **Issue #6363** (closed): router: extend the #6342 rip-up repeat guard to the recovery, overflow-silent and relief rescue passes; drop victim partial copper before retry
- **PR #6465**: fix(router): guard the relief rescue against rip-up cycling (#6363)
- **Issue #6412** (closed): Oracle closer: try nearest-island join when the plane join is withdrawn for KiCad DRC
- **PR #6450**: fix(router): oracle closer tries the nearest-island join when its plane join is withdrawn
- **Issue #6435** (closed): NetStatusAnalyzer splits GND on exposed-pad thermal-via footprints (board 05: 5 islands, KiCad: 0 unconnected)
- **PR #6457**: fix(net-status): bond overlapping pad copper so exposed-pad GND is not split
- **Issue #6455** (closed): board 07: regenerate reviewed evidence and bundle artefacts from current copper (validate.py fills without net classes) — split from #6404
- **PR #6456**: fix(board-07): regenerate reviewed evidence and bundle from current copper; validate.py refills with net classes (#6455)
- **Issue #6451** (closed): regression: board 04 lattice routes 6/9 (OSC_IN, OSC_OUT, SWDIO unrouted) on main after #6439 (#6427) -- was 9/9, exit 0
- **Issue #6449** (closed): regression: board 05 lattice strands GND_SENSE at U2:6 (no-path-keepout-constrained) on main after #6430 — 33/33 → 32/33
- **PR #6458**: fix(router): lattice pad masks grow by designer floors only — restores board 05 33/33 and board 04 9/9 (#6449, #6451)
- **Issue #6420** (closed): research harness under-counts DRC: kct check without net-class map, diff-pair errors dropped, kicad-cli without --all-track-errors
- **PR #6433**: fix(research): grade expand-route rows like a manual kct check (#6420)
- **Issue #6427** (closed): Board 02 fresh route violates its own Power class: 1 kicad-cli clearance, kct route exits 3 (exposed by #6390)
- **PR #6439**: fix(router): enforce router net-class clearances on foreign copper
- **Issue #6396** (closed): board 07: 42 (kct) / 40 (kicad-cli) copper violations of the router's own SDRAM/AUX classes (0.175 mm) exposed by #6390
- **PR #6436**: fix(board-07): clear the board's own 0.175 mm classes by nudging 24 vias (#6396)
- **Issue #6413** (closed): Plane-via reservation: grid-engine oracle regression on board 04 recipe, and signal-pin stranding risk
- **PR #6432**: fix(plane-via): never flank a signal pin with reserved sites; layer-aware stub check at reserved sites
- **PR #6430**: fix(router): lattice holds track-vs-pad pairs to the pad's own net class
- **Issue #6417** (closed): kct pcb diff OLD NEW: uuid + content-hash geometric board diff for reviewing routing PRs
- **PR #6424**: feat(pcb): add kct pcb diff OLD NEW geometric board diff
- **Issue #6389** (closed): router: pure-Python A* lacks #6315 exact pad-cell resolution, double-counts foreign pad clearance and detours through vias where C++ routes on one layer
- **PR #6398**: fix(router): exact pad-cell resolution in the pure-Python A* (#6389)
- **Issue #6399** (closed): pour completion: print per-link closer reasons and join stranded pour islands to the plane, not the nearest sliver (#6388 part A)
- **PR #6403**: fix(route): print per-link closer reasons; join stranded pour islands to the plane (#6399)
- **Issue #6390** (closed): DRC sidecars only carry fab floors: net-class clearance violations (0.15-0.18 mm vs 0.2 mm) pass both kct check and kicad-cli
- **PR #6414**: fix(route): declare the router's net classes in routed DRC sidecars (#6390)
- **Issue #6418** (closed): kct mcp setup --client claude-code writes ~/.claude/mcp.json, which Claude Code never reads
- **PR #6423**: fix(mcp): register Claude Code server in ~/.claude.json, not ~/.claude/mcp.json
- **Issue #6406** (closed): Evaluate i2cjak/Backplane (agentic KiCad/FreeCAD hub with per-tool-call design history and board diff) for ideas and integration
- **PR #6419**: docs(ecosystem): evaluate Backplane and KiStack; register both
- **Issue #6410** (closed): stitch: run_thermal_stitch and run_blanket_stitch have the same multi-net sibling-fill bug as #6400
- **PR #6416**: fix(stitch): gate thermal/blanket multi-net vias on each net's own fill (#6410)
- **Issue #6409** (closed): stitch: same-net fill gate treats KiCad fill fracture bridges as pour edges, rejecting legal welds (board 03 VCC R14.1, VBUS U1.7)
- **PR #6421**: fix(stitch): ignore fill fracture bridges in the same-net margin test
- **Issue #6402** (closed): auto-pour: bounding-box island for one pour net swallows another pour net's pads (board 03 VBUS covers VCC U1/C1) (#6388 part D)
- **PR #6405**: fix(zones): carve auto-pour bbox islands off incumbent pours' pads (#6402)
- **PR #6408**: fix(router): soft entry-lane cost for fine-pitch neighbour pins (#6382)
- **Issue #6401** (closed): kct route: reserve a legal via site per pour-net SMD pad before signal routing, grid and lattice (#6388 part C)
- **PR #6411**: fix(router): reserve a via site per pour-net IC pin before routing (#6401)
- **Issue #6368** (closed): kct route: wide traces land off their pad centreline and close the neighbour pin's entry lane
- **PR #6387**: fix(router): land traces on the pad centreline (#6368)
- **Issue #6400** (closed): stitch: multi-net run_stitch places a via for one net inside a sibling target net's fill (#6388 part B)
- **PR #6407**: fix(stitch): gate multi-net stitch vias on their own net's fill (#6400)

### 2026-10-12

- **Issue #6384** (closed): pour weld path: 5 mm neighbourhood for class clearance can miss a stricter net within stitch's escape reach (~5.3 mm)
- **PR #6393**: fix(router): derive pour weld neighbourhood from stitch's via reach (#6384)
- **Issue #6346** (closed): Access-witness replay fails with IndexError on lattice-engine partial routes (board 05)
- **PR #6392**: fix(router): access-witness replay survives a released routing grid (#6346)
- **Issue #6381** (closed): lattice: board 05 PHASE_B (U2.8 -> J2.2) declined no-path-layer-constrained -- last open net for the best engine
- **PR #6386**: fix(router): lattice neck fallback for dead-end fine-pitch escapes; board 05 33/33 (#6381)
- **Issue #6376** (closed): kct route: auto-layers (layer-escalation) flow skips post-save output connectivity verification
- **PR #6385**: fix(route): verify the written board in every kct route flow (#6376)
- **Issue #6374** (closed): pour completion (PourLinkCloser) routes at --clearance instead of net-class clearance: board 04 GND link 0.15 mm from OSC_IN where Power class needs 0.2 mm
- **PR #6383**: fix(router): pour-link completion keeps net-class clearance (#6374)
- **Issue #6371** (closed): kct route: board 03 reports 24/24 nets connected and 'all nets verified' while USB_D- pad J1.B7 is unconnected
- **PR #6379**: fix(router): judge pad connectivity by layer-aware copper contact (#6371)
- **Issue #6375** (closed): C++ pad-only clearance bypass (#6370) accepts steps the Python validator rejects (seg-pad gap 0.100 mm < 0.15 mm); board 04 routing 10 s -> 143 s via Python A* fallback
- **PR #6380**: perf(router): hold own-pad A* steps to the validator's seg-vs-pad verdict (#6375)
- **Issue #6373** (closed): lattice engine: board 03 USB_D pair hits pair-no-path and leaves 2 pads per net unconnected, with no uncoupled fallback
- **PR #6377**: fix(router): lattice uncoupled fallback for declined diff pairs (#6373)
- **Issue #6315** (closed): kct route: PHASE_C on board 05 is unroutable whenever PHASE_B is routed with it, even on an empty board
- **PR #6370**: fix(router): resolve pad clearance cells by exact geometry so wide nets leave fine-pitch pads (#6315)
- **Issue #6345** (closed): Monte Carlo / evolutionary trials give identical results on board 05 (net-order variation has no effect)
- **PR #6365**: fix(router): route each Monte Carlo / evolutionary trial in its own order (#6345)
- **Issue #6350** (closed): kct route --route-engine lattice ignores plane-layer reservation: board 05 routes 325 signal segments on In1/In2 under --layers 4
- **PR #6367**: fix(router): lattice engine honours plane-layer reservation (#6350)
- **Issue #5949** (closed): [Epic proposal] Agentic PCBA ladder benchmark: 10 demo boards, timed agent runs, own repo
- **Issue #6340** (closed): kct route: a four-net fine-pitch bundle with one crossing fails alone on board 05 (escape via closes the neighbouring pin)
- **Issue #6342** (closed): kct route: access-loss rip-up cycles on board 05 (rip blocker, blocker reseals the same pin) until no-progress stop with budget left
- **PR #6355**: fix(router): retry a targeted rip-up against freed C++ copper and bound access-loss rip-up cycles
- **Issue #6343** (closed): kct route --strategy monte-carlo/evolutionary: deadline writes an empty board (snapshot_saved false) after 20 minutes of trials
- **PR #6344**: fix(router): keep the best Monte Carlo/evolutionary trial when the deadline fires

### 2026-10-11

- **Issue #6341** (closed): kct route --route-engine lattice: board 05 negotiation finds 56/57 connections, then route_all's outer timeout discards them and writes 0 nets
- **PR #6348**: fix(router): serve cached lattice/mesh routes when route_all's outer timeout fires
- **Issue #6312** (closed): Measure rigid-cluster expansion on the boards that are partial at 1x (board 05; chorus-test needs a clustering rule first)
- **PR #6336**: docs(research): rigid-cluster expansion does not help board 05 (measured), with harness probes and committed runs
- **Issue #6314** (closed): kct route: a deadline during placement-delta feedback saves trial copper against the original placement
- **PR #6323**: fix(route): save the last accepted state when interrupted during placement-delta feedback

### 2026-10-10

- **Issue #6316** (closed): Question-specific board images for agents: net highlight, ratsnest and coordinates
- **PR #6318**: feat(board-view): net-aware board images with ratsnest, mm axes and an MCP tool
- **Issue #6304** (closed): Experiment: expand-route-shrink layout (route on a 2x-scaled placement, then compact back with trace repair)
- **PR #6311**: docs(research): expand-route-shrink experiment and harness
- **Issue #6292** (closed): perf: board 05 access-witness replay takes 143–267 s of the 900 s budget
- **PR #6305**: perf(router): access-witness replay 215 s -> 7 s on board 05, identical verdicts (#6292)

### 2026-10-09

- **PR #6297**: docs(board05): record measured PHASE_A/B/C un-skip result (Refs #6287)
- **Issue #6280** (closed): kct route: resolve via_clearance from board rules instead of the 0.20 mm default
- **PR #6293**: fix(route): resolve via_clearance from board rules instead of the 0.20 mm default
- **Issue #6273** (closed): Board 05 legacy Kelvin recipe exhausts its 900 s route budget before an accepted board (blocks #5398 acceptance)
- **PR #6282**: fix(route): finish inside --timeout -- keep complete two-phase state, reserve the post-route stages (#6273)
- **Issue #6272** (closed): router: trace-vs-via clearance depends on insertion order (port b92bf554)
- **PR #6279**: fix(router): enforce via clearance in both copper insertion orders (#6272)
- **Issue #6076** (closed): schematic: fleet generators emit random uuid4s, so regenerated schematics are never byte-reproducible (need seeded/deterministic UUID mode)
- **PR #6281**: chore(boards): regenerate 00-simple-led with deterministic UUIDs (#6076)
- **Issue #5764** (closed): Boards 00 and 03: generators emit footprints with no pin-1 / polarity silkscreen mark
- **PR #6222**: fix(board00): emit cathode silkscreen for D1
- **Issue #6277** (closed): kct route --no-auto-layers --format json emits no routing summary
- **PR #6278**: fix(route): emit JSON routing summary on --no-auto-layers path (#6277)
- **PR #6270**: chore(boards): regenerate 03-usb-joystick, 04-stm32-devboard with deterministic UUIDs (#6076)
- **Issue #6234** (closed): optimize-placement: --route-check acceptance mode (confirm a placement with a bounded real route)
- **PR #6276**: feat(placement): optimize-placement --route-check acceptance mode (#6234)
- **PR #6274**: chore(deps): migrate from Dependabot to Renovate
- **PR #5917**: chore(deps): bump astro from 7.3.3 to 7.3.5 in /site
- **PR #5916**: chore(deps-dev): bump the site-minor-patch group in /site with 2 updates
- **PR #6066**: chore(deps): bump smol-toml from 1.8.0 to 1.9.0 in /site
- **PR #5918**: chore(deps): bump the python-minor-patch group with 3 updates
- **PR #6067**: chore(deps): bump werkzeug from 3.1.8 to 3.1.9
- **PR #6065**: chore(deps): bump sharp from 0.35.4 to 0.35.5 in /site
- **PR #5842**: chore(deps): bump pyjwt from 2.13.0 to 2.15.0
- **PR #6069**: chore(deps): bump fsspec from 2026.2.0 to 2026.6.0
- **PR #6068**: chore(deps): bump source-map-js from 1.2.1 to 1.2.2 in /site
- **PR #5888**: chore(deps): bump devalue from 5.9.2 to 5.9.4 in /site
- **PR #6271**: feat(router): bounded inward off-pad access for trapped Kelvin terminals
- **PR #5841**: chore(deps): bump urllib3 from 2.7.0 to 2.8.0
- **Issue #6241** (closed): ci: pin expensive class/module-scoped slow-test fixtures to one xdist worker (--dist loadgroup)
- **PR #6242**: ci: pin slow-test heavy-fixture classes to one xdist worker
- **PR #6264**: fix(boards): regenerate 01-voltage-divider and 02-charlieplex-led with deterministic UUIDs; producer-built packages (#6076)
- **PR #6266**: chore(boards): regenerate 05-bldc-motor-controller, 06-diffpair-test, 07-matchgroup-test with deterministic UUIDs (#6076)
- **Issue #6267** (closed): tests: route-subprocess tests time out from kicad-cli queue wait and cold evolutionary cache (#6262 leftovers)
- **PR #6268**: fix(cli): forward kct route --pop-size/--generations; queue-aware budgets for route tests (#6267)
- **Issue #6262** (closed): main CI red: anchor-weight placement test, ProjectClearanceError on project without net_settings, duplicate-reference options6
- **PR #6265**: fix: unbreak main CI -- unversioned net_settings, locked footprints in optimize-placement, native-queue route budget (#6262)
- **PR #6263**: chore(boards): regenerate 09-usbc-pd-power with deterministic UUIDs (#6076)

### 2026-10-08

- **Issue #6258** (closed): Board 06 assembled LVDS demo: router commits OUT4/GND short (8/8 reported routed), native DRC fails
- **PR #6261**: fix(route): keep --preserve-existing copper registered for post-route passes (#6258)
- **Issue #6254** (closed): route-auto: run the #6243 authored-netclass finalize census and share its resolver
- **PR #6260**: fix(route-auto): run the authored-netclass final pass (#6254)
- **PR #6247**: fix(schematic): deterministic uuid5 UUIDs for generated schematics (#6076)
- **Issue #6249** (closed): kct check: enforce stricter .kicad_pro netclass clearances on routed copper (parity with kicad-cli after #6243)
- **PR #6259**: fix(check): enforce authored netclass clearance on routed copper
- **PR #6246**: fix(router): keep pending Kelvin sense pins protected by the pad-access gate
- **Issue #6243** (closed): router: enforce authored per-net (netclass) clearance minima through the shared clearance kernel (re-cut of #5502, part 1)
- **PR #6252**: fix(router): enforce authored netclass clearance minima through the shared kernel
- **Issue #6250** (closed): optimize-placement --seed current: slide-off pre-pass nudges a clean hand floorplan off-grid and breaks fine-pitch escapes
- **PR #6257**: fix(placement): warm-start slide-off only moves real violations, on grid (#6250)
- **Issue #6253** (closed): MCP optimize_placement tool ignores the new decoupling affinity term and snap pass (#6020 parity)
- **PR #6255**: fix(mcp): optimize_placement shares the CLI decoupling term and snap pass
- **Issue #6191** (closed): merge_project_rules: decide whether stricter authored clearance should win by default
- **PR #6248**: feat(manufacturers): keep stricter authored DRC minima by default (#6191)
- **Issue #6020** (closed): board 04 stm32-devboard: decoupling caps sit 6-14 mm from U2 VDD pins
- **PR #6251**: feat(placement): decoupling-cap affinity pulls caps toward their IC supply pins (#6020)
- **Issue #6237** (closed): route: preserved copper is never checked against other preserved copper in the post-route short check
- **PR #6245**: fix(route): check kept copper against kept copper and for clearance near-misses (#6237)
- **Issue #6172** (closed): Board 06: project.kct target_fab (jlcpcb-tier1) disagrees with the tier its recipe builds at (jlcpcb)
- **PR #6244**: chore(board06): target base jlcpcb in project.kct, drop redundant route pin
- **Issue #6130** (closed): perf: reuse kicad-cli pcb drc / fill-zones results across identical consecutive passes in kct route
- **Issue #6230** (closed): router: enforce a hard wall-time deadline on the corridor-yield re-run (follow-up to #5923 / PR #6227)
- **PR #6236**: perf(router): enforce a hard wall-time deadline on the corridor-yield re-run (#6230)
- **Issue #6217** (closed): router: --auto-mfr-tier never escalates on board 04 after via-in-pad process eligibility (#5189); escalation guard is layer-blind
- **PR #6228**: fix(router): layer-aware via-in-pad tier escalation; restore missed-rescue signal (#6217)
- **Issue #6239** (closed): kct route prints 'Status: SUCCESS' before the ROUTING FAILED banner when the post-route short check fails
- **PR #6240**: fix(route): no Status: SUCCESS before ROUTING FAILED banner
- **Issue #6229** (closed): route: preserved-existing copper is never checked against other nets' pads in the post-route short check
- **PR #6238**: fix(router): check preserved copper against foreign pads in post-route short check (#6229)
- **Issue #6233** (closed): research: fold the fresh-routed placement-score corpus (7 fleet boards) into the #6232 calibration study
- **PR #6235**: docs(research): fold the fresh-routed placement-score corpus into the #6232 study
- **Issue #5923** (closed): Pose-centerline diff-pair default (PR #5922): board 06 +3V3 pour breaks post-legalize and Diff-Pair job runtime ~2.7x
- **PR #6227**: perf(router): cap the corridor-yield re-run in proportion to the first main pass (#5923)
- **Issue #5948** (closed): placement: calibrate a pre-route placement score against actual routing outcomes and use it as acceptance test
- **PR #6232**: docs(research): calibrate pre-route placement signals vs routing outcomes (#5948)
- **Issue #5991** (closed): ci: Slow Tests workflow red on main — 11 long-standing failures + 1 flaky (coupled board06 timeouts, mfr-tier, chorus feedback, board04 drill, board03 demo)
- **Issue #6225** (closed): kct route exits 3 on boards 06a/06b: post-route short check flags 0.075mm header-pad overlaps that kicad-cli and kct check do not
- **PR #6226**: fix(router): match skipped pour-net pads by name in post-route short check (#6225)
- **Issue #6218** (closed): schematic registry silently returns the wrong symbol for KiCad 7 (space-indented) libraries
- **PR #6223**: fix(schematic): indentation-agnostic symbol extraction in registry (#6218)
- **Issue #5795** (closed): Regenerate board 06a rows in the KRT routing-tools comparison after the #5788 --preserve-existing fix
- **PR #6224**: docs(research): regenerate board 06a rows in the KRT comparison after #5788
- **Issue #5921** (closed): Epic #5508: board-05 ISENSE and board-07 U4.C2 are not access-loss instances — re-point those criteria at #5509 / the pour-escape model
- **PR #6219**: ci: run Slow Tests in the pinned KiCad container and triage its failures (#5991)
- **Issue #6220** (closed): Lint & Format red on main: unused `import sys` in tests/test_placement_native_fill.py (from #6213)
- **PR #6221**: test: drop unused sys import in native-fill test (#6220)
- **Issue #6215** (closed): investigate: KiCad 10.0.6 kicad-cli fills same-net-track pours nondeterministically; can the board-03 reproducibility test flake?
- **PR #6216**: test: pin kicad-cli fill reproducibility to short-free copper (#6215)
- **Issue #6115** (closed): test: fast fill-order-sensitivity fixture for board 03 (follow-up to #6052)
- **PR #6210**: test: fast fill-order-sensitivity fixture (#6115)
- **Issue #6213** (closed): Production fill silently yields empty multilayer zones on pcbnew < 10.0.2
- **PR #6214**: fix(zones): fail loudly on multilayer fill with pcbnew < 10.0.2 (#6213)
- **Issue #6101** (closed): tests: native-fill suites fail locally on KiCad 10.0.1 (CI pins 10.0.6); _fixed_fill_worker stderr is swallowed
- **PR #6209**: fix(tests): gate multilayer native-fill tests on KiCad >= 10.0.2
- **Issue #5762** (closed): board-03: footprint silkscreen ships at 0.12mm, below the jlcpcb-tier1 0.15mm floor (139 warnings)
- **PR #6200**: fix(board03): widen footprint silk to jlcpcb-tier1 0.15mm floor
- **Issue #6211** (closed): test: V-score fail-closed tests — partial-overlap case is vacuous; near-crossing check untested
- **PR #6212**: test: V-score fail-closed tests - non-vacuous partial overlap, near-crossing coverage
- **Issue #6193** (closed): pcb_vscore_layers: detect partial/jump V-scoring on third-party panels
- **PR #6203**: fix: detect partial and jump V-scoring on third-party panels
- **Issue #5767** (closed): via_under_body: IGNORECASE matches word-interior SON/LGA; same-net cluster can absorb the real EP
- **PR #6208**: fix(validate): via_under_body LGA word-interior match and EP cluster absorption
- **Issue #5993** (closed): test: native fleet relationship parity fails for board 05 on main (58 == 57)
- **Issue #6201** (closed): route-auto gate: kicad-cli 10.0.2+ reads a pad's A.Layer as its footprint's layer, not the pad's -- gate under-reports on mismatched sides
- **PR #6207**: fix(router): read a pad's A.Layer as its footprint's layer in the route-auto gate (#6201)
- **Issue #6205** (closed): kct panel --cut vcut --mfr oshpark should warn that OSHPark does not offer V-scoring
- **PR #6206**: feat(panel): warn when --cut vcut targets a fab without V-scoring
- **Issue #6177** (closed): Add sourced per-fab V-score copper clearance to manufacturer profiles
- **PR #6204**: feat: per-fab V-score copper clearance in manufacturer profiles (#6177)
- **Issue #6198** (closed): Router: rtree-less collision fallback is untested in CI; make rtree core or add a no-rtree lane
- **PR #6202**: build: make rtree a core dependency (#6198)
- **Issue #6185** (closed): Board 03 kct route: USB_CC1 relief probe refused for stranding J1.A6, ends 23/24
- **Issue #6184** (closed): Board 03 baseline test: post-optimize backstop demotes USB_D+ as a cross-net short (12/13)
- **Issue #6151** (closed): board 03: USB_CC1 unrouted (23/24), test_board03_routing_baseline fails on main (pre-existing)
- **PR #6197**: fix(router): no optimizer cross-net short without rtree; THT pad access spans all layers
- **Issue #6170** (closed): test: test_preserved_copper_cpp_grid_6103 calls _py_grid_state_stamp unbound after #6152 made it an instance method
- **Issue #6196** (closed): main CI red: board06 escape search rejects new DRU rules; route-auto gate hole_clearance disagrees on null pad layer
- **PR #6199**: fix: unbreak main CI (board 06 escape DRU parser, route-auto pad A.Layer, #6170 test)

### 2026-10-07

- **Issue #6158** (closed): kct panel squares off rounded/non-rectangular board outlines
- **PR #6194**: fix(panel): keep rounded/shaped board outlines as real arcs (#6158)
- **Issue #6182** (closed): test(ci): lattice post-pass-gating skips_both tests still exceed 180 s in Test (extended) after #6140
- **PR #6195**: test(router): share one lattice route across post-pass gating skip tests
- **PR #6192**: perf(router): exact bbox skip in lattice clearance checks (#6182)
- **Issue #6165** (closed): pcb_vscore_layers misses overshooting/KiKit score lines and unquoted layers; flags lines on the outline edge
- **PR #6189**: fix(export): robust V-score layer detection (#6165)
- **Issue #6180** (closed): PCB.export_drill() also plots copper Gerbers and returns gerbers.zip instead of a drill-file directory
- **PR #6190**: fix(pcb): export_drill writes drill files only, no Gerbers or zip
- **Issue #6171** (closed): generate_dru crashes on 2-layer profiles when a net class sets target_ampacity (inner_copper_oz=0)
- **PR #6188**: fix(dru): skip internal ampacity rule on 2-layer profiles
- **Issue #6169** (closed): Default --mfr from project.kct target_fab in the remaining jlcpcb-defaulted commands (export, fix-*, audit, pipeline, reason, estimate)
- **PR #6183**: feat(cli): default --mfr from project.kct target_fab in the remaining jlcpcb-defaulted commands
- **PR #6178**: fix(router): relief rescue counts a victim re-landed only when complete (#6151)
- **Issue #6186** (closed): test_clearance_kernel_parity red on main: panel/vscore.py (#6176) references the clearance kernel without an allowlist entry
- **PR #6187**: test(router): allow panel/vscore.py to reference the clearance kernel
- **Issue #6175** (closed): main CI red: native-fill tests fail on KiCad 10.0.6 too; lint-gate SIGTERM test gets exit 130
- **PR #6181**: fix: unbreak main CI -- native-fill identity after #6052, SIGTERM handler leak (#6175)
- **Issue #6167** (closed): Gerber drill export passes flags kicad-cli rejects (--merge-npth, --minimal-header); OSH Park preset always fails
- **PR #6179**: fix(export): pass real kicad-cli drill flags; OSH Park preset exports again
- **Issue #6150** (closed): route-auto clearance gate follow-ups: severity ignore, DRU parse warning, hole-to-hole, physical_clearance
- **PR #6160**: fix(router): route-auto gate honours severity ignore, warns on discarded DRUs, checks hole-to-hole and physical clearance
- **Issue #6164** (closed): kct panel --cut vcut still routes 2 mm gaps and tabs between copies; V-scored panels should be butted
- **PR #6176**: fix(panel): butt V-cut panels and score the shared edges
- **Issue #6163** (closed): Gerber manufacturer presets' layer_rename maps are never applied (dead config)
- **PR #6174**: fix(export): remove the never-applied Gerber layer_rename maps
- **Issue #6166** (closed): tests: tests/test_mcp_optimize_placement.py::TestOptimizePlacement (4 tests) fail on main locally (result['success'] is False)
- **PR #6173**: test(mcp): skip optimize_placement success tests when cmaes is absent
- **Issue #6155** (closed): kct route: default --manufacturer from the board's project.kct target_fab (as kct check does)
- **PR #6168**: feat(route): default --manufacturer from the board's project.kct target_fab
- **Issue #6126** (closed): Footprint rotation leaves property/fp_text angles stale (KiCad stores them board-absolute)
- **PR #6161**: fix(pcb): turn footprint text angles with their footprint (#6126)
- **Issue #6156** (closed): kct export drops V-score lines: Cmts.User is never plotted to Gerbers after #6143
- **PR #6162**: fix(export): ship V-score lines for V-cut panels; add kct panel --vcut-layer
- **Issue #6095** (closed): pour oracle: live routes now merge the emitted project; source-project-only links fail a board that is connected under its shipped rules
- **PR #6157**: fix(router): judge pour-oracle links under the shipped project only (#6095)
- **Issue #6106** (closed): physical_copper_gap still keys copper by UUID; route-auto-style geometry keys (follow-up to #6088)
- **PR #6159**: fix(check): name physical_copper_gap copper by geometry, not UUID
- **Issue #6088** (closed): check: geometry-based finding keys for copper (track/via) findings so waivers survive re-routing
- **PR #6091**: feat(check): key copper findings by geometry so waivers survive re-routing
- **Issue #6143** (closed): Panel tabs produce malformed Edge.Cuts outline (invalid_outline DRC errors)
- **PR #6154**: fix(panel): render Edge.Cuts as closed loops with tabs spliced in
- **Issue #6133** (closed): router: proven-unreachable memo fingerprint omits corridor reservations; fixed fills/keepouts compared by identity only (follow-up to #6098)
- **PR #6152**: fix(router): proven-unreachable memo fingerprint covers reservations and fill/keepout contents
- **Issue #6147** (closed): kct route -o outside the board tree ignores the input's fabrication_overrides.json (phantom hole_to_hole on board 03)
- **PR #6153**: fix(route): resolve fabrication_overrides.json from the input board when -o is elsewhere
- **Issue #5886** (closed): Explore circuit-skills (punkfab): mine Claude Code PCB skills for gotchas, auto-keepouts, enclosure-fit gating and placement-first loop ideas
- **PR #6047**: docs: evaluate circuit-skills (ideas only), verdict complementary
- **Issue #5910** (closed): perf(router): cut the kicad-cli invocation count in kct route (52%/41% of wall on boards 02/03)
- **PR #6131**: perf(route): memoise kicad-cli version probe
- **Issue #5761** (closed): via_under_body: case-insensitive SON also matches 'Resonator' (crystal footprints selected)
- **Issue #6119** (closed): update_footprint_position leaves footprint-embedded zones (keepouts) at the old location
- **PR #6127**: fix(pcb): move footprint-embedded zones (keepouts) with their footprint
- **Issue #6117** (closed): router: pure-Python A* fallback commits a via the C++ validator rejected (0.125 mm to a foreign pad, board 03 BTN1)
- **PR #6145**: fix(router): Python fallback validates vias against foreign pads
- **Issue #6090** (closed): kct route --lint-gate fails open on SIGTERM: --output left unjudged
- **PR #6144**: fix(route): stage --lint-gate output so SIGTERM/SIGKILL never leave an unjudged board
- **Issue #6148** (closed): route-auto hierarchical double-marks preserved copper on the C++ grid after #6141
- **PR #6149**: fix(router): stop double-marking preserved copper on the C++ grid in route-auto (#6148)
- **Issue #6139** (closed): route-auto: corridor strategies write tracks through bare NPTH holes (KiCad hole_clearance); gate them by hole clearance (follow-up to #6107)
- **Issue #6122** (closed): route-auto foreign-copper gate: resolve per-net netclass and conditional .kicad_dru clearances (follow-up to #6107)
- **PR #6146**: fix(route-auto): gate copper by hole clearance and per-pair clearance rules
- **Issue #6103** (closed): router: preserved copper (load_existing_routes) never reaches the C++ grid -- C++ A* routes through it, then falls back to Python
- **PR #6141**: fix(router): preserved copper reaches the C++ grid on load
- **Issue #6125** (closed): Panel tiling offsets footprint-local coordinates and leaves zone/keepout polygons behind
- **PR #6142**: fix(panel): make each panel copy a rigid motion of the source board
- **Issue #6107** (closed): route-auto short gate misses other nets' pads, arcs and clearance: silent shorts/DRC errors still written (follow-up to #6001)
- **PR #6118**: fix(route-auto): route around and refuse other nets' pads, arcs and clearance (#6107)
- **Issue #6134** (closed): test(ci): lattice post-pass-gating and route-complete-report CLI tests time out in Test (extended) under load
- **PR #6140**: test(ci): stub post-route kicad-cli in lattice/route-complete CLI tests (#6134)
- **Issue #6137** (closed): ci: assert-no-bot-external red daily -- loom-fleet-reader-{1,2}[bot] issues mislabeled 'external' (trust pin covers only dispatch bot)
- **PR #6138**: ci(label-external): trust loom-fleet-reader-{1,2}[bot] app accounts
- **Issue #6129** (closed): main CI red: ruff UP031 in test_calibrate_fom.py + timeouts (6058, 5521) + board07 refill assertion
- **Issue #6128** (closed): test: fix fleet copper-gap frame regression exceeding the CI timeout
- **PR #6135**: test: fix main CI red -- copper-gap frame timeout, board07 refill premise, plan-gate cost
- **Issue #6098** (closed): perf(router): C++ 'open set exhausted' still falls back to pure-Python A* on every give-up — ~5 min per unroutable net
- **PR #6116**: perf(router): skip the Python fallback once it has proven a net unreachable
- **Issue #6132** (closed): ci: ruff UP031 in tests/test_calibrate_fom.py:277 fails Lint & Format on main
- **Issue #6099** (closed): router: detect_layer_stack silently treats 8+-layer boards as 2-layer (kct route, keepout resolution, route-auto hierarchical cap)
- **PR #6110**: fix(router): build N-layer stacks instead of collapsing 8+ layers to 2
- **Issue #6052** (closed): route: board 03 final pour fill varies run to run with identical copper (two fill signatures)
- **PR #6109**: fix(route): canonicalize routed-board UUIDs so identical copper gives an identical fill
- **Issue #6120** (closed): ci: Lint & Format red on main - optim_fom_cmd.py unformatted after #6114
- **PR #6121**: style: ruff format optim_fom_cmd.py (main Lint & Format red)
- **Issue #6124** (closed): chore(lint): main fails ruff format check in cli/optim_fom_cmd.py (from #6112)
- **Issue #6123** (closed): main red: ruff format fails on src/kicad_tools/cli/optim_fom_cmd.py:110 (from #6112)
- **Issue #6087** (closed): Footprint-embedded keepout rule areas are not covered by the #6039 intersectsArea DRU rules
- **PR #6094**: fix(drc): enforce footprint-embedded keepouts in .kicad_dru and router
- **Issue #6112** (closed): fom-debug composite collapses to ~1e-7 on most boards; :.6f text output hides it and score is not cross-board comparable
- **PR #6114**: fix(optim): readable fom-debug score output and cross-board caveat
- **Issue #6111** (closed): calibrate_fom.py per-board seeds use process-randomized hash(board); not reproducible without PYTHONHASHSEED
- **PR #6113**: fix(research): stable per-board seed in calibrate_fom (no PYTHONHASHSEED dependence)
- **Issue #6021** (closed): Re-run full FOM weight calibration on current fleet boards (default.yaml fitted on May 2026 boards)
- **PR #6108**: feat(optim): re-calibrate FOM default weights on current fleet boards
- **Issue #6093** (closed): test: format-json stdout contract exercises route-auto only on its error path
- **Issue #6001** (closed): route-auto: classify unrouted connections as congested vs blocked (follow-up to #5944)
- **PR #6105**: feat(route-auto): classify unrouted connections as congested/blocked
- **Issue #6102** (closed): ci: update_ci_extended.py can silently move PR-required gates back into ci_extended
- **PR #6104**: ci(tests): pr_required marker keeps merge gates out of ci_extended
- **Issue #6085** (closed): schematic _patch_node: pin-UUID preservation guard never fires; rotate fields with symbol angle
- **PR #6100**: fix(schematic): rotate fields with symbol, fix dead pin-UUID guard
- **Issue #6078** (closed): pour oracle: closer's copper model reads carved saved fills while the oracle refills (GND cluster mismatch); carve fragments board-03 F.Cu GND pour
- **PR #6097**: fix(zones): keep saved pour fills connected; check the saved fill in gates and export
- **Issue #6019** (closed): perf(router): iterated 1-Steiner uses O(n^3) Python Prim per candidate; 44-pad net takes ~2 min (bit-exact 185x fix available)
- **PR #6092**: perf(router): batched bit-identical 1-Steiner candidate scoring
- **Issue #6059** (closed): route-auto: RoutingOrchestrator ignores board keepout rule areas (follow-up to #6008)
- **PR #6096**: fix(route-auto): honour board keepout rule areas in every strategy
- **Issue #6054** (closed): route: --lint-gate rolls back a route that introduces new kct check findings (split from #6006)
- **PR #6084**: feat(route): --lint-gate rolls back a route that introduces new lint findings
- **Issue #5994** (closed): test: format_json_stdout contract [route] exceeds its 50s subprocess budget locally on main
- **PR #6089**: test(cli): speed up format-json contract route and suggest-footprint cases
- **Issue #5992** (closed): perf(cli): cache kicad-cli probe_failed for a short TTL so a hanging kicad-cli doesn't cost ~30s per lookup
- **PR #6086**: perf(cli): cache kicad-cli probe_failed for a short TTL
- **Issue #6039** (closed): Emit an intersectsArea DRU rule per keepout rule area so KiCad DRC actually checks keepouts
- **PR #6080**: fix(drc): emit an intersectsArea disallow rule per keepout rule area
- **Issue #6058** (closed): physical_copper_gap mixes board-relative and sheet frames (arcs from raw sexp; findings never absolutized)
- **PR #6082**: fix(check): physical_copper_gap uses one coordinate frame, reports sheet coords
- **Issue #6057** (closed): Schematic model: edited elements still regenerate from builder defaults (justify, font, field positions)
- **PR #6083**: fix(schematic): edited elements keep their KiCad styling (patch source node)
- **Issue #6061** (closed): router: escape-stub generator is not keepout-rule-area aware (follow-up to #6008)
- **PR #6081**: fix(router): keep escape stubs and vias out of keepout rule areas
- **Issue #6023** (closed): pour oracle: align cluster model with KiCad per-net counts; try/finally restore in fill-strip
- **PR #6079**: fix(fill): restore stripped fills on every failure path; keep oracle closer's original error
- **Issue #5908** (closed): route: --order-method greedy|critical_first|congestion|hybrid are silent no-ops on the auto-layers / escalation paths
- **PR #6046**: fix(route): reject --order-method greedy|critical_first|congestion|hybrid on escalation paths
- **Issue #6009** (closed): router: relaxed blocker search (temporarily_unblock_routed_nets) finds no blockers -- C++ grid and route R-trees are not unblocked
- **PR #6073**: fix(router): relaxed blocker search lifts routes off the C++ grid and R-trees
- **Issue #6060** (closed): router: bus detection misses /DATA0-style (prefixed numeric-suffix) net names
- **PR #6077**: fix(router): parse sheet-prefixed numeric bus names like /DATA0
- **Issue #6070** (closed): schematic: lib_symbols property order in generated schematics depends on PYTHONHASHSEED (set iteration in _flatten_with_parent)
- **PR #6074**: fix(schematic): deterministic property order when flattening extends symbols
- **Issue #6006** (closed): check: detect-mistakes waivers, SARIF output, and route --lint-gate (follow-up to #5946)
- **PR #6056**: feat(check): detect-mistakes evidence-bound waivers and SARIF output
- **Issue #6071** (closed): schematic: removed elements resurrect when saving a deepcopied loaded Schematic (_source_consumed holds stale id()s)
- **PR #6075**: fix(schematic): deepcopy/pickle of loaded Schematic no longer resurrects removed elements
- **Issue #5911** (closed): perf(router): pour-net oracle completion loop is 34.6% of kct route wall time on board 03 (169 s)
- **PR #6053**: perf(router): batch oracle link-route writes and run both oracle DRC projects concurrently
- **Issue #6062** (closed): router: route_all_hierarchical (negotiated) skips keepout rule-area install (follow-up to #6008)
- **PR #6072**: fix(router): install keepout rule areas in route_all_hierarchical
- **Issue #6051** (closed): Schematic model load/save still drops fidelity: label alignment, text font/ID, power-symbol field positions, version, embedded_fonts
- **PR #6064**: fix(schematic): re-emit untouched elements verbatim on load + save
- **Issue #6055** (closed): evidence: ev2 local net evidence compares sheet-coordinate findings to board-relative pads (empty on boards with non-zero origin)
- **PR #6063**: fix(check): put ev2 local net evidence pads in sheet coordinates
- **Issue #6008** (closed): route: grid engine ignores board-file keepout rule areas (only lattice honours them) -- no tracking issue since #4605
- **PR #6034**: fix(route): enforce board-file keepout rule areas in the grid engine
- **Issue #6013** (closed): ci: fit the Test job on GitHub-hosted runners by moving its slowest tests to a non-PR job
- **PR #6014**: ci: run Test on GitHub-hosted runners by moving its slow tail to a main-only job
- **Issue #6004** (closed): pintype annotation: resolve references for sheets placed more than once
- **PR #6049**: fix(pintype): resolve references per sheet placement
- **Issue #6048** (closed): Schematic model round-trip: Arduino_Mega/Arduino_Uno templates unloadable by kicad-cli after load+save
- **PR #6050**: fix(schematic): keep load/save output loadable by kicad-cli
- **Issue #6005** (closed): Schematic model drops (mirror x|y): pin_position wrong for mirrored symbols
- **PR #6038**: fix(schematic): honour (mirror x|y) on placed symbols
- **Issue #6016** (closed): perf(pcb): _NoConnectZone.build is O(R*W*J) - seconds-to-minutes on dense sheets
- **PR #6042**: perf(pcb): index no-connect zone flood fill (O(W) instead of O(R*W*J))
- **Issue #6041** (closed): evidence radius: bare clearance/dimensions/width_consistency/mask_to_copper rule IDs get 5 mm, docs promise 3 mm
- **Issue #6011** (closed): check: limit net-membership evidence to pads near the finding (waiver churn on GND/power nets)
- **PR #6040**: feat(check): limit net-membership evidence to pads near the finding
- **Issue #6027** (closed): Board 09 readiness still lists closed #5061 GND-connectivity blocker
- **PR #6037**: fix(boards): compute board 09 strict-connectivity blocker instead of hardcoding closed #5061
- **Issue #6015** (closed): check waivers: upgrade note for legacy silkscreen fp_line waivers; per-line items for silk_edge_clearance
- **PR #6035**: fix(check): per-line items for silk_edge_clearance; re-waive hint for legacy fp_line waivers
- **Issue #6028** (closed): detect-mistakes power-trace width: reports first (not narrowest) segment and flags no-load regulator bias pins
- **PR #6036**: fix(explain): power-trace width reports narrowest segment, skips no-load VREG nets
- **Issue #6032** (closed): validate --sync: root-sheet /X <-> /Sheet/X name swap is not detected
- **PR #6033**: fix(validate): detect root-sheet /X <-> /Sheet/X net name swaps
- **Issue #5999** (closed): validate --sync: detect name swaps between same-named nets on different sheets (/A/CLK <-> /B/CLK)
- **PR #6031**: fix(validate): detect name swaps between same-leaf nets on different sheets
- **Issue #6029** (closed): is_switch_node_name false positives: SWD debug pin, *_SW load-switched rails and button nets
- **PR #6030**: fix(router): tighten is_switch_node_name false positives
- **Issue #5998** (closed): power_pin_nets treats buck switch node (power_out SW pin) as a rail; then annotate remaining fleet boards
- **PR #6026**: fix(explain): buck switch node is not a power rail; annotate board 09 pin types
- **Issue #5995** (closed): explain: is_ic_footprint misses non-U active parts (oscillators, SOT-223/DPAK regulators, Sensor: library)
- **PR #6024**: fix(explain): recognise non-U active parts in is_ic_footprint
- **Issue #5984** (closed): Re-calibrate FOM decoupling_proximity weight after power-rail classifier change (#5939)
- **PR #6022**: docs(optim): re-check FOM decoupling_proximity weight after #5939 classifier change; keep 0.0181

### 2026-10-06

- **Issue #5934** (closed): Pour-oracle completion (#5785) lands different stitches and pour copper on a byte-identical board
- **PR #6017**: fix(route): make the pour-oracle completion reproducible on identical boards
- **Issue #6012** (closed): test_zones_cmd::TestRunFillZones::test_no_kicad_cli_returns_failure fails on main: asserts old 'kicad-cli not found' text
- **PR #6018**: test(zones): expect 'kicad-cli not installed' after #5932 message change
- **Issue #5985** (closed): Generated boards carry no pad pintype, so detect-mistakes pin-type rail evidence never runs
- **PR #6000**: feat(pcb): write schematic pin types onto generated PCB pads
- **Issue #6010** (closed): route-auto --nets -o: later nets chain from a missing or stale output file when an earlier net wrote nothing
- **Issue #5945** (closed): router: best-so-far checkpointing, --resume, and regressing-pass rollback for kct route / route-auto
- **PR #6003**: feat(router): best-so-far --checkpoint, --resume and regressing-pass rollback for route / route-auto
- **Issue #5946** (closed): check: evidence-hash-bound waivers, check --diff, and explicit 'unknown' coverage states
- **PR #6007**: feat(check): evidence-bound waivers, check --diff, and explicit coverage states
- **Issue #5944** (closed): router: classify each unrouted connection as congested vs blocked in kct route JSON
- **PR #6002**: feat(router): classify each unrouted connection as congested or blocked in kct route JSON
- **Issue #5979** (closed): test(agent-guide): close extractor blind spots in test_agent_guide_commands (fenced blocks, uv run, '...')
- **PR #5996**: fix(agent-guide): close command-extractor blind spots; wire kct audit HV flags
- **Issue #5981** (closed): board03: test_board_03_regression fails locally on main with kicad-cli 10.0.1 (4 annular_width DRC errors), a possible regression of #5191
- **PR #5989**: test(board03): xfail known kicad-cli <10.0.6 J1 oval annular false positive
- **Issue #5980** (closed): validate --sync: warn when two nets' pads are swapped wholesale (names swapped, connectivity intact)
- **PR #5997**: fix(validate): flag wholesale net-name swaps in validate --sync
- **Issue #5970** (closed): fix(explain): BypassCapDistanceCheck treats caps, resistors, connectors and fuses as ICs (65 of 90 bypass findings on board 03)
- **PR #5988**: fix(explain): restrict BypassCapDistanceCheck to real ICs
- **Issue #5935** (closed): test_route_cmd_success_output: board 00 routes with 2 DRC violations and no SUCCESS headline (fails on main)
- **Issue #5932** (closed): fix(cli): find_kicad_cli memoises a transient probe timeout as 'kicad-cli not installed', silently skipping zone refill
- **PR #5990**: fix(cli): never cache a kicad-cli probe timeout as 'not installed'; warn loudly on skipped zone refill
- **PR #5987**: test(route): name the skipped zone fill when board 00 loses its SUCCESS headline
- **Issue #5905** (closed): test(router): board02 jlcpcb-tier1 baseline test_drc_clean_at_jlcpcb_tier1 fails on main (1 error vs ceiling 0)
- **PR #5933**: test(router): make board02 tier1 baseline robust to transient kicad-cli probe failure
- **Issue #5939** (closed): fix(explain): is_power_net substring matching classifies USB_D+, ISENSE_A+, PG_3V3 as power rails (false decoupling errors on board 03)
- **PR #5971**: fix(explain): classify power nets by pin type and whole-token names, not substrings
- **Issue #5982** (closed): fix(mcp): flush libc stdio before restoring fd 1 so unflushed native printf can't trail onto the JSON-RPC stream
- **PR #5983**: fix(mcp): flush libc stdio before restoring fd 1 so unflushed native printf can't trail onto the JSON-RPC stream
- **Issue #5941** (closed): Ecosystem registry: add TraceMaker, fastroute, PCBWorld, kicadmium and broader-search projects; refresh circuit-skills
- **PR #5978**: docs(ecosystem): register TraceMaker, fastroute, PCBWorld, kicadmium and broader-search projects
- **Issue #5964** (closed): [Epic #5952] AGENTS.md parity: single-source contributor guidance rendered into CLAUDE.md and AGENTS.md (+ drift test)
- **PR #5975**: docs: single-source contributor guidance into CLAUDE.md and AGENTS.md (#5964)
- **Issue #5937** (closed): fix(validate): validate --sync pad-net check is a placeholder — swapped pad nets report in_sync: true
- **PR #5974**: fix(validate): finish the validate --sync pad-net check
- **Issue #5940** (closed): fix(explain): detect-mistakes flags all 9 LEDs on board 02 as missing series resistor (shared line resistors not recognized)
- **PR #5972**: fix(explain): recognise shared LED series resistors via path rule
- **Issue #5966** (closed): fix(docs): agent-guide sync step fails as written (--apply needs --confirm) + parse-test every kct invocation in the guide
- **PR #5977**: fix(docs): agent-guide kct commands work as written; parse-test every invocation
- **Issue #5965** (closed): fix(mcp): redirect stdio at the file-descriptor level so native/child-process output can't corrupt JSON-RPC frames
- **PR #5968**: fix(mcp): redirect stdio at the fd level so native/child output can't corrupt frames
- **Issue #5973** (closed): fix(loom-hook): post-worktree timeout ignores leading-zero values; TERM to the hook orphans uv
- **PR #5976**: chore(loom): post-worktree hook follow-ups — decimal timeout, re-raise TERM/INT after killing the sync
- **Issue #5967** (closed): chore(loom): post-worktree hook (uv sync --frozen --extra dev) never exits for background builders; bound it and find the stall
- **PR #5969**: chore(loom): bound and instrument the post-worktree uv sync hook (#5967)
- **Issue #5960** (closed): [Epic #5952] kct agent-guide: harness-neutral agent primer + README harness setup section
- **PR #5963**: feat(agent-guide): harness-neutral agent primer as `kct agent-guide` + README harness section (#5960)
- **Issue #5961** (closed): [Epic #5952] MCP stdio smoke test per client: handshake, tools/list and a tool call from each kct mcp setup launch command
- **PR #5962**: fix(mcp): keep stdio stdout JSON-RPC-only; add per-client stdio smoke test
- **Issue #5951** (closed): feat(agents): make kicad-tools bilingual — Codex CLI and opencode support for MCP setup, skills and usage guide
- **PR #5959**: feat(skills): kct skills install --harness opencode (#5951)
- **Issue #5950** (closed): feat(skills): ship /kct:* skills in the wheel with 'kct skills install' (needed for pcba-bench sandboxed toolkit track)
- **PR #5958**: feat(skills): ship /kct:* skills in the wheel with `kct skills install`
- **Issue #5938** (closed): fix(cli): drc, erc and detect-mistakes print progress text to stdout under --format json, breaking JSON parsing
- **PR #5957**: fix(cli): keep --format json stdout a single JSON document (#5938)
- **Issue #5953** (closed): [Epic #5952] kct mcp setup --client codex|opencode
- **PR #5956**: feat(mcp): kct mcp setup --client codex|opencode (#5953)
- **Issue #5954** (closed): [Epic #5952] Audit /kct:* skills and agent-facing text for Claude-only assumptions (+ lint test)
- **PR #5955**: feat: make kct skills and MCP descriptions harness-neutral (#5954)
- **Issue #5870** (closed): test_board03_pour_fill_is_reproducible flakes under concurrent CPU load
- **PR #5936**: fix(route): add --deterministic-rescue so board-03 pour test is load-independent

### 2026-10-04

- **Issue #5607** (closed): boards: committed board06 output/ artifact is stale relative to the recipe (needs a KiCad-capable refresh)

### 2026-10-03

- **Issue #5903** (closed): find_kicad_cli() should probe before trusting a found binary, with a documented container fallback (split from #5877)
- **PR #5931**: feat: probe find_kicad_cli() before trusting binary; opt-in container fallback
- **Issue #5704** (closed): Manufacturing profiles: project min_silk_clearance is mapped from the solder-mask clearance, not the silkscreen floor
- **PR #5925**: refactor(manufacturers): source .kicad_pro min_silk_clearance from a named constant
- **PR #5930**: ci: cut board-job CI cost (diffpair to main/nightly, per-board filters, benchmark cadence)
- **Issue #5895** (closed): [Epic #5784] Flip the pose-centerline diff-pair search on by default: fix board 06 MIPI_RST corridor regression
- **PR #5929**: feat(router): pose-trunk corridor guard; pose-centerline diff-pair search on by default (#5895)
- **Issue #5801** (closed): Wire up or remove orphaned BayesianOptStrategy: --strategy bayesian is unreachable despite README/ROADMAP claiming it ships
- **PR #5928**: feat: wire --strategy bayesian into CLI and MCP (#5801)
- **Issue #5902** (closed): fix(router): keepout-intrusion guard matches pad.ref instead of the physical component key (#5890 follow-up)
- **PR #5927**: fix(router): match keepout-intrusion pads by physical component key
- **Issue #5901** (closed): flake: test_partial_placement_route_modes[evolutionary] exceeds its 30s deadline on a quiet machine (the #5579 class, reopened in practice)
- **PR #5926**: test: shrink evolutionary GA in partial-placement route modes test
- **Issue #5701** (closed): ci: stacked PRs (base = a feature branch) get no CI at all — 'CLEAN' means 'never ran', not 'passed'
- **PR #5924**: ci: distinguish absent checks from passing for stacked PRs
- **PR #5922**: fix(router): pose-centerline trunks yield sealed corridors; keep the search opt-in (#5895)
- **Issue #5712** (closed): champion-epic.md: 'Epic Passes Evaluation' comments reuse the rejection-only champion:epic-verdict marker, corrupting the unrevised-eval skip/escalate ladder
- **Issue #5673** (closed): Halo-shape switch (#5660/PR #5668) under-blocks via drill-to-drill spacing — board02/06 DRC regressions

### 2026-10-02

- **Issue #5894** (closed): [Parent #5787] Step 3: flag-gated KRT-style N+1 rip-up prototype
- **PR #5919**: feat(router): flag-gated KRT-style sequential N+1 rip-up prototype
- **Issue #5913** (closed): [Epic #5508] Phase 3a: access-loss rip-up targeting via the witness
- **PR #5920**: fix(router): target the copper that sealed a pad's last exit for rip-up
- **Issue #5882** (closed): Hoist a shared arc-bounding helper for core.board_outline and router.fixed_copper
- **PR #5915**: refactor(core): share one arc circumcircle/sweep helper
- **Issue #5787** (closed): [Epic #5784] Phase 3: attribute kct route runtime, then crossing-aware ordering + N+1 rip-up (measure first)
- **PR #5912**: docs(research): re-measure kct route phase profile -- kicad-cli subprocesses are 52%/41% of wall
- **Issue #5786** (closed): [Epic #5784] Phase 2: pose-based centerline search (Dubins heuristic) for coupled diff pairs
- **Issue #5893** (closed): [Parent #5787] Step 2: crossing-aware default net ordering experiment on boards 02/03
- **PR #5909**: feat(router): measure crossing-aware net ordering; add --order-method crossing
- **Issue #5891** (closed): [Epic #5508] Phase 2: commit-time pad-access invariant (default on for the grid engine)
- **PR #5907**: feat(router): commit-time pad-access invariant, default on for the grid engine
- **Issue #5511** (closed): Epic: netlist degrees of freedom — swappable-pin groups and acting placement feedback
- **Issue #5904** (closed): [Epic #5511] Phase 4: Recipe diff surface + docs
- **PR #5906**: docs(routing): document swap groups and recorded deltas (Epic #5511 Phase 4)
- **Issue #5785** (closed): [Epic #5784] Phase 1: KiCad-oracle completion loop for pour nets + one completion verdict
- **PR #5900**: feat(router): KiCad-oracle completion loop for pour nets + one stranded-pad verdict
- **Issue #5890** (closed): [Epic #5511] Phase 3: classifier-driven placement feedback on the default path when infeasible
- **PR #5898**: feat(router): act on classifier placement feedback by default when the plan is infeasible
- **PR #5896**: feat(router): opt-in pose-based centerline search for coupled diff pairs (Dubins heuristic)
- **Issue #5876** (closed): kct route --nets blames a 'loader bug' when the pour-net auto-skip removed the requested nets
- **PR #5897**: fix: distinguish pour-net auto-skip conflicts from loader bugs in --nets guard
- **PR #5892**: docs(research): per-phase kct route runtime profile for boards 02/03 (Part of #5787)

### 2026-10-01

- **Issue #5875** (closed): kct check --mfr does not validate .kicad_pro netclass defaults against the manufacturer floor
- **PR #5889**: feat: validate .kicad_pro netclass defaults against the manufacturer floor
- **PR #5887**: docs(ecosystem): register punkfab/circuit-skills as watch
- **Issue #5857** (closed): [Epic #5509] Phase 4d: switch kct check (and resolve drc/cpp exemption) to the clearance kernel
- **PR #5881**: fix(validate): switch kct check clearance family to the shared kernel
- **Issue #5856** (closed): [Epic #5509] Phase 4c: switch DRC-nudge repair to the clearance kernel
- **PR #5885**: fix(router): switch DRC-nudge repair to the shared clearance kernel
- **Issue #5873** (closed): kct route refuses dual_gmsl_serializer_adapter: duplicate pad number with distinct unconnected nets (J5.MP)
- **PR #5884**: fix(router): load same-numbered no-connect lands as independent terminals
- **Issue #5872** (closed): Router short gate: production code duplicates count_shorting_violations' predicate, and the escalation audit fails open
- **PR #5883**: refactor(router): share short predicate and make short-audit failure loud
- **Issue #5863** (closed): kct route refuses boards with custom-shaped pads (6 of 16 real dataset-srj18 source boards)
- **PR #5880**: fix(router): degrade unsupported pad geometry to obstacle copper instead of refusing the board
- **Issue #5854** (closed): [Epic #5509] Phase 4a: switch optimizer (collision.py + trace.py) to the clearance kernel
- **PR #5874**: fix(router): switch the trace optimizer's collision checkers to the clearance kernel
- **Issue #5847** (closed): Interop gate: run tscircuit-emitted KiCad projects through kct check / LVS / kicad-cli DRC
- **PR #5879**: feat: gate tscircuit's KiCad exports against kct check / LVS / kicad-cli DRC
- **Issue #5864** (closed): kct route aborts with ZonePartitionError under default --auto-pour on a board with authored AGND/GND zones
- **PR #5871**: fix: skip an unsatisfiable auto-pour net instead of aborting kct route
- **Issue #5861** (closed): test(board-04): _FLOOR_TOLERANCE_MM comment mis-cites DRC_TOLERANCE (1e-4, not 1e-6)
- **PR #5869**: test(board-04): fix _FLOOR_TOLERANCE_MM comment citation
- **Issue #5862** (closed): kct route output ships shorting_items while the router self-check reports clearance_viol=0 (Arduino Nano)
- **PR #5868**: fix: router self-check no longer excuses copper shorts as component-inherent
- **Issue #5855** (closed): [Epic #5509] Phase 4b: switch match-group tuning to the clearance kernel
- **PR #5867**: fix(router): match-group tuning measures clearance with the shared kernel (Epic #5509 Phase 4b)
- **Issue #5860** (closed): Board 04's reviewed paid-drill gate reports overall FAILED from 30 strict ERC warnings (DRC leg is clean)
- **PR #5866**: fix: distinguish uninitialized KiCad library tables from real ERC defects
- **Issue #5848** (closed): Benchmark tscircuit autorouter vs kct route on dataset-srj18 source boards
- **PR #5865**: bench(external): sweep all 16 srj18 boards with kct and tscircuit (#5848)
- **Issue #5852** (closed): Board 04 fresh regen is not jlcpcb-tier1 clean: 0.150mm-drill vias fail dimension_via_drill / dimension_annular_ring
- **PR #5859**: test(board-04): gate the fresh regen on its reviewed paid-drill process
- **Issue #5661** (closed): [Epic #5509] Phase 3b: switch search-time physical refinement (route_halo_geometry.py + C++ A*) to the clearance kernel
- **PR #5851**: fix(router): switch search-time halo refinement to the clearance kernel (Epic #5509 Phase 3b)
- **PR #5853**: docs(ecosystem): pin dataset-srj18 boards and benchmark tscircuit autorouter on Arduino Nano (part of #5848)
- **Issue #5700** (closed): Board 06 / diff-pair pours=BROKEN: exact-disc halo lets routing wall in plane-net pad J1.A4's stitch-via site
- **Issue #5660** (closed): [Epic #5509] Phase 3a: switch halo marking (grid.py + grid.cpp) to the clearance kernel
- **PR #5668**: feat(router): halo marking is the clearance kernel's exact dilation (#5660)

### 2026-09-30

- **PR #5850**: fix(router): reserve a stitch-via site per plane-net pad before routing (#5700)
- **PR #5849**: docs(ecosystem): re-evaluate tscircuit, register its autorouter
- **Issue #5843** (closed): Ecosystem registry: CLI silently drops unmapped categories; GitLab SPDX normalization is case-wrong
- **PR #5846**: fix(ecosystem): guard CLI category listing and fix GitLab SPDX casing
- **Issue #5815** (closed): label LVS: preserve sheet-qualified local net identities instead of reporting false mismatches
- **PR #5844**: fix(lvs): walk every placement of a repeated sheet, with its own refs
- **Issue #5839** (closed): feat: ecosystem registry — track related projects and where kicad-tools sits
- **PR #5840**: feat(ecosystem): add tracked registry of related projects with drift detection
- **Issue #5820** (closed): kct mcp serve should name the [mcp] extra when its dependencies are missing
- **PR #5836**: fix: name the mcp extra when kicad_tools.mcp.server import fails
- **Issue #5837** (closed): champion-epic.md Phase Progression's child-issue query misses title-prefix and prose-only phase issues
- **PR #5838**: docs(loom): widen champion-epic phase queries to title-prefix children
- **Issue #5826** (closed): Reconcile PR #5824 vs main's sheet-qualified LVS fix (b96acde2) — both have verified correctness bugs
- **PR #5835**: fix: reconcile sheet-qualified LVS across #5809 and PR #5824 (#5826)
- **Issue #5817** (closed): physical-gap: support filled footprint copper polygons used by standard net ties
- **PR #5834**: fix(physical-gap): model filled and stroked footprint copper polygons
- **Issue #5813** (closed): Feature: readiness should collect legitimate project dependencies outside the PCB directory
- **PR #5830**: feat(readiness): collect project dependencies outside the PCB directory
- **Issue #5818** (closed): native mask geometry: accept geometry-neutral silk_clearance rule context
- **PR #5833**: fix: accept geometry-neutral silk_clearance in native mask rule context
- **Issue #5810** (closed): CLI parser drift: kct check rejects mixed-copper values (--copper outer=X,inner=Y)
- **PR #5831**: fix: kct check accepts keyed --copper outer=X,inner=Y
- **Issue #5812** (closed): readiness: engine fingerprint records consumer repository HEAD for a dependency installed in its venv
- **PR #5832**: fix: bind readiness engine fingerprint to kct's own provenance, not the venv's host repo
- **Issue #5814** (closed): mask-to-copper: source guard rejects freshly loaded KiCad 10 name-only nets
- **PR #5828**: fix: canonicalize both sides of the mask-to-copper source guard
- **Issue #5811** (closed): check: polygonal silkscreen is silently omitted from clearance geometry
- **PR #5829**: fix: model polygon silkscreen in the silk clearance geometry
- **Issue #5816** (closed): readiness: unified CLI omits --generate and --verify supported by standalone runner
- **PR #5827**: fix(cli): forward --generate/--verify to kct readiness
- **Issue #5809** (closed): LVS: root-sheet local net names produce false label mismatches against KiCad-created PCB bindings
- **PR #5823**: fix: sheet-qualify local net identities in label LVS (#5809)
- **Issue #5804** (closed): Move jsonschema from core to mcp extra: only used by MCP server
- **PR #5821**: chore(deps): move jsonschema from core dependencies to the mcp extra
- **Issue #5807** (closed): readiness: HV isolation subcheck accepts unverified text as passing audit evidence
- **PR #5822**: fix(readiness): bind the HV isolation gate to a measured audit, not free text
- **Issue #5808** (closed): create-pcb: ignores project fp-lib-table and exits successfully after omitting required components
- **PR #5819**: fix: create-pcb resolves footprints via project fp-lib-table, fails loudly on drops

### 2026-09-29

- **PR #5806**: chore(loom): pin #5783's lease-guard files so resync stops failing
- **PR #5799**: docs(research): benchmark KRT on boards 04/05/07 and fix the harness timeout leak
- **Issue #5790** (closed): research: extend KRT benchmark to boards 04/05/07 (fanout, length-matching, dense bundles) + fix reproduce command
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

## 2026-10-15 — v0.23.0 release cut

- v0.23.0 release PR opened by hand (manual fallback in RELEASING.md; `AUTO_RELEASE` is unset, so the daily workflow only dry-runs). 451 changelog fragments assembled; release-gate gaps closed in #6752.

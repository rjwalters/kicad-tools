# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-10-18*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

_None._

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

_None._

## Important

`loom:important`; taken before unlabelled work.

- **#6858**: router: coupled pre-pass should keep the better of the joint-search and pose-centreline candidates (compare-and-keep; unblocks #6835)
- **#7093**: router: differential pair gets unequal via counts when a leg falls back to uncoupled routing (board 03 USB_D 2 vs 3; blocks #6394)
- **#7269**: board 07 MIPI_DAT0: recover 0.038 mm skew lost to compare-and-keep (blocks #6858)
- **#7362**: main regression from #7324: board 03 fresh route USB_D 16.3→19.7 mm after 0.22 mm In2.Cu net class; tier-1 common-run test red

## Ready

Human-approved issues ready for implementation (`loom:issue`).

_None._

## In Progress

Issues currently being built (`loom:building`).

- **#5798**: [Epic #5784] Phase 4: deconfound kct's match-group length tuning on board 07 (measure first)
- **#6554**: diffpair: reproduce KiCad's diff_pair_uncoupled primitive pairing exactly (kct is a lower bound)
- **#7062**: LDOBlock internal routing shorts foreign pads and crosses itself on real SOT-23-5 geometry
- **#7093**: router: differential pair gets unequal via counts when a leg falls back to uncoupled routing (board 03 USB_D 2 vs 3; blocks #6394)
- **#7153**: kct export bundle inconsistencies: JLCPCB Gerbers omit paste layers for SMT bundles; board 04 manifest says DRC OK while report says FAIL/NOT_READY; report ERC falsely 'skipped by user request'
- **#7199**: Committed fleet CPLs still in the pre-#7059 frame — regenerate packages + add a frame-consistency test
- **#7224**: hermit: re-consolidate point-in-polygon (8 copies) and point-to-segment distance (2 canonical + 7 private); footprint_outside_board misjudges top/right-edge origins
- **#7241**: Boards 05 and 07: committed CPLs still in the old frame; refresh producers refuse on evidence drift (split from #7199)
- **#7296**: Router: refuse pad-edge vias (mask-dam floor) and move existing ones on boards 02/05/06 (split from #7220, PR3)
- **#7339**: Board 07 seed-42 re-route: DDR_DATA_BYTE_0 match-group skew 16.7 mm (tol 0.100) exceeds MATCHGROUP_VIOLATION_BASELINE once DQ0 routes
- **#7344**: Router: coupled pair trunks do not tighten to the solved intra_pair_clearance (113-125 ohm on boards 06/07)
- **#7353**: Consolidate board evidence producers onto one shared gate pipeline (refresh_bundles copy, 5 fill-identity impls, board-04 one-offs)
- **#7355**: kct readiness --verify doesn't honour board 04's recorded #6269 exception: blocks on 24 paid-drill via errors while the committed record says ready
- **#7356**: Evidence producers: one shared saved-vs-refilled fill-identity gate for every producer (#7353 step 2)
- **#7359**: Replace count-based CI allowances with identity-keyed exceptions (#7270 Q3 ruling): diffpair/matchgroup impedance caps, known_issues warning counts
- **#7365**: Board 02 readiness.json is stale on main: README.md edited by #7342 without re-pin
- **#7367**: Key remaining count-based CI/test allowances by identity (extends #7359): stale board-03/07 allowances, board-05 blocking cap, DRC tolerance counts
- **#7374**: main red after #7317: board-04 tombstoning/pintype tests stale, board-04 repair deepcopy timeouts, order-dependent diffpair shadow spy

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#7340**: ci: bump board 07 match-group baseline to 2 for DDR_DATA_BYTE_0
- **#7350**: feat(router): length-aware choice among via-parity fallback leg pairs (#7093)
- **#7360**: feat(router): opt-in diff-pair gap tightening post-pass (--tighten-diffpair-gap)
- **#7363**: refactor: route refresh_bundle through refresh_readiness.repin_staged
- **#7364**: refactor: share one saved-vs-refilled fill check across readiness producers
- **#7371**: chore(boards): re-pin board 02 readiness evidence after README edit
- **#7375**: fix: visit match-group members by descending deficit

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

_None._

## Proposed

Issues carrying `loom:curated`.

- **#5164**: Stage and validate native zone refill for Board 07 via repairs before restoring CI *(curated)*
- **#5240**: CI runtime: reduce measured Test, Board06 E2E and diff-pair regression job medians *(curated)*
- **#5286**: Board07 full recipe exports seven unexpected signal opens and clearance failures after stage-budget integration *(curated)*
- **#5333**: Board07 coupled pre-pass exhausts all seven pair searches despite valid corridor guides *(curated)*
- **#5410**: router: refine dynamic route halo rejection with physical clearance checks *(curated)*
- **#5508**: Epic: pad-access invariant — no committed route may strand an unrouted pad *(curated)*
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)*
- **#5550**: Reconcile 4 outstanding loom-quarantine stashes left by the worktree reaper (issues 5201, 4982, 5216, 5004) *(curated)*
- **#5798**: [Epic #5784] Phase 4: deconfound kct's match-group length tuning on board 07 (measure first) *(curated)*
- **#6445**: [Epic #6441] Phase 3: via pairs as a single move in the lattice coupled centreline *(curated)*
- **#6446**: [Epic #6441] Phase 4: budget-driven hybrid fallback — longest coupled trunk plus single-ended terminal legs *(curated)*
- **#6527**: router: Router.route_bidirectional has no production caller; wire it into the Python fallback and measure, or remove it *(curated)*
- **#6554**: diffpair: reproduce KiCad's diff_pair_uncoupled primitive pairing exactly (kct is a lower bound) *(curated)*
- **#6858**: router: coupled pre-pass should keep the better of the joint-search and pose-centreline candidates (compare-and-keep; unblocks #6835) *(curated)*
- **#7026**: board 03: fresh class-clean lattice route is not complete under the recipe's tier-1 zone fill (3 GND opens) *(curated)*
- **#7062**: LDOBlock internal routing shorts foreign pads and crosses itself on real SOT-23-5 geometry *(curated)*
- **#7093**: router: differential pair gets unequal via counts when a leg falls back to uncoupled routing (board 03 USB_D 2 vs 3; blocks #6394) *(curated)*
- **#7153**: kct export bundle inconsistencies: JLCPCB Gerbers omit paste layers for SMT bundles; board 04 manifest says DRC OK while report says FAIL/NOT_READY; report ERC falsely 'skipped by user request' *(curated)*
- **#7186**: kct post-route DRC reports 33 shorts on routed board 07m where pinned kicad-cli reports 0 (engine disagreement) *(curated)*
- **#7199**: Committed fleet CPLs still in the pre-#7059 frame — regenerate packages + add a frame-consistency test *(curated)*
- **#7204**: DFA false-PASS: courtyard check silently skips courtyard-less footprints (boards 00/01/04 signed off with 0 courtyards) and AssemblyCapabilities is never enforced (board 07 double-sided CPL unflagged) *(curated)*
- **#7206**: Spec-declared currents never reach the ampacity gate: board 05 motor phase necked to 0.2 mm passes check, audit and readiness *(curated)*
- **#7224**: hermit: re-consolidate point-in-polygon (8 copies) and point-to-segment distance (2 canonical + 7 private); footprint_outside_board misjudges top/right-edge origins *(curated)*
- **#7241**: Boards 05 and 07: committed CPLs still in the old frame; refresh producers refuse on evidence drift (split from #7199) *(curated)*
- **#7256**: board 03: after #7163 a fresh arm C route fails usb_geometry (MCU launch 4.138/4.284 > 4 mm; USB width 0.30 -> 0.275) *(curated)*
- **#7269**: board 07 MIPI_DAT0: recover 0.038 mm skew lost to compare-and-keep (blocks #6858) *(curated)*
- **#7275**: board 03: fix the 6 real thermal_asymmetry findings via recipe and drop the pinned allowance *(curated)*
- **#7296**: Router: refuse pad-edge vias (mask-dam floor) and move existing ones on boards 02/05/06 (split from #7220, PR3) *(curated)*
- **#7339**: Board 07 seed-42 re-route: DDR_DATA_BYTE_0 match-group skew 16.7 mm (tol 0.100) exceeds MATCHGROUP_VIOLATION_BASELINE once DQ0 routes *(curated)*
- **#7344**: Router: coupled pair trunks do not tighten to the solved intra_pair_clearance (113-125 ohm on boards 06/07) *(curated)*
- **#7349**: Boards 06/07: enable diff-pair gap tightening on seed-42 re-routes and drop the impedance caps (#7344 PR 2) *(curated)*
- **#7353**: Consolidate board evidence producers onto one shared gate pipeline (refresh_bundles copy, 5 fill-identity impls, board-04 one-offs) *(curated)*
- **#7355**: kct readiness --verify doesn't honour board 04's recorded #6269 exception: blocks on 24 paid-drill via errors while the committed record says ready *(curated)*
- **#7356**: Evidence producers: one shared saved-vs-refilled fill-identity gate for every producer (#7353 step 2) *(curated)*
- **#7359**: Replace count-based CI allowances with identity-keyed exceptions (#7270 Q3 ruling): diffpair/matchgroup impedance caps, known_issues warning counts *(curated)*
- **#7362**: main regression from #7324: board 03 fresh route USB_D 16.3→19.7 mm after 0.22 mm In2.Cu net class; tier-1 common-run test red *(curated)*
- **#7365**: Board 02 readiness.json is stale on main: README.md edited by #7342 without re-pin *(curated)*
- **#7367**: Key remaining count-based CI/test allowances by identity (extends #7359): stale board-03/07 allowances, board-05 blocking cap, DRC tolerance counts *(curated)*
- **#7374**: main red after #7317: board-04 tombstoning/pintype tests stale, board-04 repair deepcopy timeouts, order-dependent diffpair shadow spy *(curated)*

## Proposed (Architect / Hermit)

- **#6441**: Epic: diff-pair routing redesign — gateways, via pairs, uncoupled budget, per-segment rules *(architect)*
- **#6445**: [Epic #6441] Phase 3: via pairs as a single move in the lattice coupled centreline *(architect)*
- **#6446**: [Epic #6441] Phase 4: budget-driven hybrid fallback — longest coupled trunk plus single-ended terminal legs *(architect)*
- **#6447**: [Epic #6441] Phase 5: coupling-preserving skew tuning from a skew ledger, and pair invariants for post-passes *(architect)*
- **#6448**: [Epic #6441] Phase 6: unify diff-pair engines — grid consumes PairPlan or grid coupled pre-pass retired *(architect)*
- **#7153**: kct export bundle inconsistencies: JLCPCB Gerbers omit paste layers for SMT bundles; board 04 manifest says DRC OK while report says FAIL/NOT_READY; report ERC falsely 'skipped by user request' *(hermit)*
- **#7206**: Spec-declared currents never reach the ampacity gate: board 05 motor phase necked to 0.2 mm passes check, audit and readiness *(hermit)*
- **#7367**: Key remaining count-based CI/test allowances by identity (extends #7359): stale board-03/07 allowances, board-05 blocking cap, DRC tolerance counts *(hermit)*
- **#7374**: main red after #7317: board-04 tombstoning/pintype tests stale, board-04 repair deepcopy timeouts, order-dependent diffpair shadow spy *(hermit)*

## Epics

- **#3438**: router: board 07 stuck at 28/31 — negotiated router cannot complete parallel pad-array bundles (DDR bundle ALONE routes 9/11)
- **#4410**: board-05: generator must produce a manufacturable BLDC board unattended (81% route, 7 ISENSE/PWM open, 2 real shorts)
- **#4431**: route: per-net scalar clearance cannot express HV isolation — need class-pair (net-class × net-class) clearance rules in the router; both workarounds proven inadequate on a mains board
- **#5508**: Epic: pad-access invariant — no committed route may strand an unrouted pad
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC
- **#5784**: Epic: adopt measured KiCadRoutingTools techniques (oracle completion, pose diff-pair search, N+1 rip-up)
- **#5952**: Epic: Harness-neutral kicad-tools (Claude Code, Codex CLI, opencode)
- **#6441**: Epic: diff-pair routing redesign — gateways, via pairs, uncoupled budget, per-segment rules

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 0 |
| Very important | 0 |
| Important | 4 |
| Ready (`loom:issue`) | 0 |
| In Progress (`loom:building`) | 18 |
| PRs awaiting review | 7 |
| Approved PRs awaiting merge | 0 |
| Curated | 39 |
| Architect / Hermit proposals | 9 |
| Active epics | 8 |
<!-- guide:plan-body:end -->

## Release History (static archive)

_Historical release notes below this point are a static archive, not regenerated by Guide — see WORK_LOG.md for the current chronological record of merged PRs and closed issues._

| Release | Highlights |
|---------|-----------|
| **v0.20.0** (2026-08-06) | `kct route --complete` completion pass, route-time HV-isolation enforcement in every engine, `.kct_waivers.json` waiver mechanism, `net-status` strict-by-default, KiCad-10 net-dialect / export-manifest / LVS-identity correctness sweep; shipped as a 13-PR train through a GitHub Actions outage |
| **v0.19.0** (2026-07-20) | HV-isolation design loop (`kct creepage --voltage-map`, `zones hv-keepout`, HV-aware placement, `/kct:hv-isolation-loop` skill) + via-in-pad manufacturability (`kct fix-vias` off-pad relocation), `analyze electrical-rating`, `--emit-dru`/`--emit-drc-constraints` rule-identity sidecars |
| **v0.18.0** (2026-07-20) | HV / analog manufacturing gates — `kct creepage` (surface-path creepage audit vs IEC 60664-1/62368-1) and `kct analyze current-sense` (analog layout lint). Plus real `--nets` route filter, `pcb reinforce` multi-branch anchoring, `route --layers auto` inner-layer advisory |
| **v0.17.0** (2026-07-17) | Experimental alternative routing substrate — adaptive octilinear **lattice** engine + constrained-Delaunay **navmesh/mesh** engine, both default-OFF; routes large mixed-pitch boards the grid can't fit in memory |
| **v0.16.0** (2026-07-15) | Region-bounded routing, ampacity-aware net-class min-width + DRC (IPC-2221), copper dedupe, `pcb reinforce` anchor-PTH rows, `net-status --strict` real-geometry connectivity, off-board preflight |
| **v0.15.0** (2026-07-13) | Router feasibility certificates + constructive escape ordering, coupled diff-pair corridor attractors + C++ joint-state A\* port, copper-LVS gates across boards 01–07, LCSC/EasyEDA + cross-library 3D model resolver tiers |
| **v0.14.0** (2026-06-16) | Demo gallery website (kicad-tools.org), zone-fill foreign-pad clearance fix, PCB `page_fit`, oblique 3D + 2D-SVG renders, gallery LVS status, ERC/LVS/Manifest meta sub-checks for `kct check` |

**LVS Soundness Epic — COMPLETE (2026-06-17).** Independent copper-LVS soundness epic (motivated by #3742), shipped via a `/loom:sweep` run processing 16 PRs: gate + extractor hardening (independent copper-extracted LVS gate, label-free pour extraction, per-zone pour-pad bonding, foreign-net track-segment carve, layer-aware segment chaining), fleet rollout across boards 00–07, and real defects the gate surfaced and fixed on boards 02/03/04 that DRC had missed.

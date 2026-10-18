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

## Ready

Human-approved issues ready for implementation (`loom:issue`).

_None._

## In Progress

Issues currently being built (`loom:building`).

- **#5798**: [Epic #5784] Phase 4: deconfound kct's match-group length tuning on board 07 (measure first)
- **#6554**: diffpair: reproduce KiCad's diff_pair_uncoupled primitive pairing exactly (kct is a lower bound)
- **#7058**: kct creepage overstates creepage around a slot: the detour is computed between the straight-line nearest points, and narrow gaps are not bridged
- **#7062**: LDOBlock internal routing shorts foreign pads and crosses itself on real SOT-23-5 geometry
- **#7064**: PCBLayout.route() draws a straight port-to-port line that crosses foreign pads and never reaches a pad
- **#7075**: transmission_line.py: CPWG impedance reads 53-233 % high, and microstrip/CPWG dielectric loss is too large by a factor of eps_eff
- **#7076**: Microstrip Z0 with copper thickness reads 1.6-3.7 % low (docstring claims 0.2 %)
- **#7079**: ImpedanceRule judges every detected differential pair at a 0.15 mm gap, whatever the routed spacing
- **#7093**: router: differential pair gets unequal via counts when a leg falls back to uncoupled routing (board 03 USB_D 2 vs 3; blocks #6394)
- **#7136**: kct sign-off skills don't refuse bad boards: Gate 1 exits 2 on every board, kicad-cli gate exits 0 on errors, export never gets the DRC report, audit ERC never runs
- **#7150**: board 04 J1 value SWD-6 vs SWD (schematic vs PCB) + kct validate --sync should flag value mismatches (#7147 Part B)
- **#7153**: kct export bundle inconsistencies: JLCPCB Gerbers omit paste layers for SMT bundles; board 04 manifest says DRC OK while report says FAIL/NOT_READY; report ERC falsely 'skipped by user request'
- **#7188**: One DRC blocking-verdict helper: kicad-drc-summary, kct mfr validate and Project.check_drc() pass boards kicad-cli fails
- **#7209**: board 05 ampacity relies on 1 oz inner copper the fab order never requests (JLCPCB default 0.5 oz → 1186 inner segments under-width)
- **#7210**: ampacity: path_ampacity can't resolve board 05 phase paths; zone-only nets read 'checked' vacuously (#7206 follow-ups)
- **#7227**: assembly-tier preflight gaps: 008004 chips pass silently; LFCSP judged as BGA; report vs preflight bottom-side count (#7226 follow-up)
- **#7230**: One sign-off exit-code contract: kct drc --net/--type/--category exits 0 on DRC errors, kct check JSON says passed while exiting 2, exit 2 means four things

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#7154**: fix: seed creepage detour from both shapes and bridge sub-X cutouts
- **#7221**: fix(validate): resolve board 05 phase paths and flag zone-only ampacity nets
- **#7235**: fix(board03): move six refdes out from under their part bodies

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#7217**: feat(export): fail preflight when stackup copper differs from the fab order
- **#7229**: fix(export): fail 008004 chips and judge LFCSP by pin pitch in assembly preflight
- **#7231**: refactor(geometry): shared point_in_polygon; footprint_outside_board accepts boundary origins (PR1 of #7224)
- **#7232**: feat(check): thermal_asymmetry and via_at_pad_edge warnings; detect-mistakes reuses them (#7220)

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
- **#7058**: kct creepage overstates creepage around a slot: the detour is computed between the straight-line nearest points, and narrow gaps are not bridged *(curated)*
- **#7062**: LDOBlock internal routing shorts foreign pads and crosses itself on real SOT-23-5 geometry *(curated)*
- **#7064**: PCBLayout.route() draws a straight port-to-port line that crosses foreign pads and never reaches a pad *(curated)*
- **#7075**: transmission_line.py: CPWG impedance reads 53-233 % high, and microstrip/CPWG dielectric loss is too large by a factor of eps_eff *(curated)*
- **#7076**: Microstrip Z0 with copper thickness reads 1.6-3.7 % low (docstring claims 0.2 %) *(curated)*
- **#7079**: ImpedanceRule judges every detected differential pair at a 0.15 mm gap, whatever the routed spacing *(curated)*
- **#7093**: router: differential pair gets unequal via counts when a leg falls back to uncoupled routing (board 03 USB_D 2 vs 3; blocks #6394) *(curated)*
- **#7136**: kct sign-off skills don't refuse bad boards: Gate 1 exits 2 on every board, kicad-cli gate exits 0 on errors, export never gets the DRC report, audit ERC never runs *(curated)*
- **#7150**: board 04 J1 value SWD-6 vs SWD (schematic vs PCB) + kct validate --sync should flag value mismatches (#7147 Part B) *(curated)*
- **#7153**: kct export bundle inconsistencies: JLCPCB Gerbers omit paste layers for SMT bundles; board 04 manifest says DRC OK while report says FAIL/NOT_READY; report ERC falsely 'skipped by user request' *(curated)*
- **#7186**: kct post-route DRC reports 33 shorts on routed board 07m where pinned kicad-cli reports 0 (engine disagreement) *(curated)*
- **#7188**: One DRC blocking-verdict helper: kicad-drc-summary, kct mfr validate and Project.check_drc() pass boards kicad-cli fails *(curated)*
- **#7190**: Fleet re-verify: no ready claim falsified, but kct export --strict-preflight passes a 6-error board on a stale committed drc_report.json; board 01 lacks kicad_tools_pwr sidecar *(curated)*
- **#7199**: Committed fleet CPLs still in the pre-#7059 frame — regenerate packages + add a frame-consistency test *(curated)*
- **#7204**: DFA false-PASS: courtyard check silently skips courtyard-less footprints (boards 00/01/04 signed off with 0 courtyards) and AssemblyCapabilities is never enforced (board 07 double-sided CPL unflagged) *(curated)*
- **#7206**: Spec-declared currents never reach the ampacity gate: board 05 motor phase necked to 0.2 mm passes check, audit and readiness *(curated)*
- **#7209**: board 05 ampacity relies on 1 oz inner copper the fab order never requests (JLCPCB default 0.5 oz → 1186 inner segments under-width) *(curated)*
- **#7210**: ampacity: path_ampacity can't resolve board 05 phase paths; zone-only nets read 'checked' vacuously (#7206 follow-ups) *(curated)*
- **#7220**: DFM: kct check is blind to thermal asymmetry its own solid-connect repair creates (13 passives, 4 PTH pins) and to via drills at SMD pad edges *(curated)*
- **#7224**: hermit: re-consolidate point-in-polygon (8 copies) and point-to-segment distance (2 canonical + 7 private); footprint_outside_board misjudges top/right-edge origins *(curated)*
- **#7227**: assembly-tier preflight gaps: 008004 chips pass silently; LFCSP judged as BGA; report vs preflight bottom-side count (#7226 follow-up) *(curated)*
- **#7228**: Silkscreen false-PASS: kct misses text stroke width, off-board silk and refdes under body (board 03: 6), and readiness skips text height *(curated)*
- **#7230**: One sign-off exit-code contract: kct drc --net/--type/--category exits 0 on DRC errors, kct check JSON says passed while exiting 2, exit 2 means four things *(curated)*

## Proposed (Architect / Hermit)

- **#6441**: Epic: diff-pair routing redesign — gateways, via pairs, uncoupled budget, per-segment rules *(architect)*
- **#6445**: [Epic #6441] Phase 3: via pairs as a single move in the lattice coupled centreline *(architect)*
- **#6446**: [Epic #6441] Phase 4: budget-driven hybrid fallback — longest coupled trunk plus single-ended terminal legs *(architect)*
- **#6447**: [Epic #6441] Phase 5: coupling-preserving skew tuning from a skew ledger, and pair invariants for post-passes *(architect)*
- **#6448**: [Epic #6441] Phase 6: unify diff-pair engines — grid consumes PairPlan or grid coupled pre-pass retired *(architect)*
- **#7136**: kct sign-off skills don't refuse bad boards: Gate 1 exits 2 on every board, kicad-cli gate exits 0 on errors, export never gets the DRC report, audit ERC never runs *(hermit)*
- **#7153**: kct export bundle inconsistencies: JLCPCB Gerbers omit paste layers for SMT bundles; board 04 manifest says DRC OK while report says FAIL/NOT_READY; report ERC falsely 'skipped by user request' *(hermit)*
- **#7188**: One DRC blocking-verdict helper: kicad-drc-summary, kct mfr validate and Project.check_drc() pass boards kicad-cli fails *(hermit)*
- **#7190**: Fleet re-verify: no ready claim falsified, but kct export --strict-preflight passes a 6-error board on a stale committed drc_report.json; board 01 lacks kicad_tools_pwr sidecar *(hermit)*
- **#7206**: Spec-declared currents never reach the ampacity gate: board 05 motor phase necked to 0.2 mm passes check, audit and readiness *(hermit)*
- **#7220**: DFM: kct check is blind to thermal asymmetry its own solid-connect repair creates (13 passives, 4 PTH pins) and to via drills at SMD pad edges *(hermit)*
- **#7224**: hermit: re-consolidate point-in-polygon (8 copies) and point-to-segment distance (2 canonical + 7 private); footprint_outside_board misjudges top/right-edge origins *(hermit)*
- **#7228**: Silkscreen false-PASS: kct misses text stroke width, off-board silk and refdes under body (board 03: 6), and readiness skips text height *(hermit)*

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
| Important | 1 |
| Ready (`loom:issue`) | 0 |
| In Progress (`loom:building`) | 17 |
| PRs awaiting review | 3 |
| Approved PRs awaiting merge | 4 |
| Curated | 38 |
| Architect / Hermit proposals | 13 |
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

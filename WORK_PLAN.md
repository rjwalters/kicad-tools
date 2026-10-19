# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-10-19*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

_None._

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#7093**: router: differential pair gets unequal via counts when a leg falls back to uncoupled routing (board 03 USB_D 2 vs 3; blocks #6394)

## Important

`loom:important`; taken before unlabelled work.

- **#6858**: router: coupled pre-pass should keep the better of the joint-search and pose-centreline candidates (compare-and-keep; unblocks #6835)
- **#7269**: board 07 MIPI_DAT0: recover 0.038 mm skew lost to compare-and-keep (blocks #6858)
- **#7401**: router: board 06 USB2_D+ uncoupled 12.791 > 12.700 mm after v62 pad-edge floor reorders the single-ended fallback
- **#7478**: `atomic_write_text` fsyncs a read-only handle: every `PCB.save` fails on Windows with `OSError: [Errno 9]` (0.22.0, 0.23.0)

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#7026**: board 03: fresh class-clean lattice route is not complete under the recipe's tier-1 zone fill (3 GND opens)
- **#7093**: router: differential pair gets unequal via counts when a leg falls back to uncoupled routing (board 03 USB_D 2 vs 3; blocks #6394)
- **#7423**: Board 07: enable route-time length matching in the committed map after #7397 (no-worse re-measure; #5798 Q4 ruling)

## In Progress

Issues currently being built (`loom:building`).

- **#6554**: diffpair: reproduce KiCad's diff_pair_uncoupled primitive pairing exactly (kct is a lower bound)
- **#7206**: Spec-declared currents never reach the ampacity gate: board 05 motor phase necked to 0.2 mm passes check, audit and readiness
- **#7349**: Boards 06/07: enable diff-pair gap tightening on seed-42 re-routes and drop the impedance caps (#7344 PR 2)
- **#7463**: board 03 arm C: VBUS U2.5 left unconnected (keepout-constrained escape next to USB_D U2 pads); last gate before re-deriving board 03 under #7093
- **#7478**: `atomic_write_text` fsyncs a read-only handle: every `PCB.save` fails on Windows with `OSError: [Errno 9]` (0.22.0, 0.23.0)
- **#7484**: DSN export: `custom` pads are written as their anchor rectangle only (primitives ignored); round, oval and rotated pads also collapse to an axis-aligned rectangle
- **#7503**: Lattice router: pad-clearance check uses a square around pads, not the true (rounded/rotated) outline, so legal via/track points are rejected (board 03 VBUS, #7463)
- **#7508**: Schematic blocks: fix DebugHeader jtag and UCC27211 defaults, wire USB ESD TVS
- **#7510**: Schematic blocks: ports misplaced at off-grid origins (grid-snap mismatch)
- **#7519**: PCB.save() falsely refuses an unedited board whose pads use nameless numbered nets (net N)
- **#7520**: docs/research: add diff-pair skew-compensation note and a README index for all research notes
- **#7529**: JLC upload ledger uses POSIX-only os.O_DIRECTORY / O_NOFOLLOW; AttributeError on Windows (found in #7527 review)

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#7468**: fix(router): gate the coupled-commit skew trim on combined P+N uncoupled length (#7269)
- **#7514**: fix: make JTAG header, UCC27211 driver and USB ESD blocks build with defaults
- **#7522**: docs: add diff-pair skew compensation note and enforce research index coverage
- **#7524**: fix: skip pad net_name unpersisted-edit check for number-only net nodes
- **#7525**: fix(dsn): export circle, oval, rotated and custom pad copper as real shapes
- **#7530**: fix: guard POSIX-only O_NOFOLLOW/O_DIRECTORY in JLC upload ledger

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#7502**: fix(router): gap-tightening never worsens pair skew or uncoupled length
- **#7527**: fix: fsync atomic-write tmp files through a writable handle (Windows PCB.save EBADF)

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
- **#7093**: router: differential pair gets unequal via counts when a leg falls back to uncoupled routing (board 03 USB_D 2 vs 3; blocks #6394) *(curated)*
- **#7186**: kct post-route DRC reports 33 shorts on routed board 07m where pinned kicad-cli reports 0 (engine disagreement) *(curated)*
- **#7206**: Spec-declared currents never reach the ampacity gate: board 05 motor phase necked to 0.2 mm passes check, audit and readiness *(curated)*
- **#7256**: board 03: after #7163 a fresh arm C route fails usb_geometry (MCU launch 4.138/4.284 > 4 mm; USB width 0.30 -> 0.275) *(curated)*
- **#7269**: board 07 MIPI_DAT0: recover 0.038 mm skew lost to compare-and-keep (blocks #6858) *(curated)*
- **#7349**: Boards 06/07: enable diff-pair gap tightening on seed-42 re-routes and drop the impedance caps (#7344 PR 2) *(curated)*
- **#7384**: Committed board evidence doesn't reproduce: refresh_readiness --dry-run (pinned 10.0.6, committed checked_at) rewrites files on 6 of 7 boards *(curated)*
- **#7401**: router: board 06 USB2_D+ uncoupled 12.791 > 12.700 mm after v62 pad-edge floor reorders the single-ended fallback *(curated)*
- **#7478**: `atomic_write_text` fsyncs a read-only handle: every `PCB.save` fails on Windows with `OSError: [Errno 9]` (0.22.0, 0.23.0) *(curated)*
- **#7484**: DSN export: `custom` pads are written as their anchor rectangle only (primitives ignored); round, oval and rotated pads also collapse to an axis-aligned rectangle *(curated)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)*
- **#7491**: router: C++ search finds no USB3_RX1- path on board 06 once USB2_D routes coupled (blocks #7401) *(curated)*
- **#7503**: Lattice router: pad-clearance check uses a square around pads, not the true (rounded/rotated) outline, so legal via/track points are rejected (board 03 VBUS, #7463) *(curated)*
- **#7508**: Schematic blocks: fix DebugHeader jtag and UCC27211 defaults, wire USB ESD TVS *(curated)*
- **#7510**: Schematic blocks: ports misplaced at off-grid origins (grid-snap mismatch) *(curated)*
- **#7519**: PCB.save() falsely refuses an unedited board whose pads use nameless numbered nets (net N) *(curated)*

## Proposed (Architect / Hermit)

- **#6441**: Epic: diff-pair routing redesign — gateways, via pairs, uncoupled budget, per-segment rules *(architect)*
- **#6445**: [Epic #6441] Phase 3: via pairs as a single move in the lattice coupled centreline *(architect)*
- **#6446**: [Epic #6441] Phase 4: budget-driven hybrid fallback — longest coupled trunk plus single-ended terminal legs *(architect)*
- **#6447**: [Epic #6441] Phase 5: coupling-preserving skew tuning from a skew ledger, and pair invariants for post-passes *(architect)*
- **#6448**: [Epic #6441] Phase 6: unify diff-pair engines — grid consumes PairPlan or grid coupled pre-pass retired *(architect)*
- **#7206**: Spec-declared currents never reach the ampacity gate: board 05 motor phase necked to 0.2 mm passes check, audit and readiness *(hermit)*
- **#7367**: Key remaining count-based CI/test allowances by identity (extends #7359): stale board-03/07 allowances, board-05 blocking cap, DRC tolerance counts *(hermit)*
- **#7377**: route: collapse the 4 copy-pasted post-route tails into one pipeline -- --length-match-diffpairs, --stitch-power-planes, --progressive-clearance, --bus-routing silently no-op on the default --auto-layers path *(hermit)*

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
| Very important | 1 |
| Important | 4 |
| Ready (`loom:issue`) | 3 |
| In Progress (`loom:building`) | 12 |
| PRs awaiting review | 6 |
| Approved PRs awaiting merge | 2 |
| Curated | 31 |
| Architect / Hermit proposals | 8 |
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

# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-10-17*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#6874**: ci(board-07): move copper-safety DRC ratchet to scheduled slow-tests
- **#6900**: ci: add Windows smoke job and skip RSS positivity test without resource

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

_None._

## Important

`loom:important`; taken before unlabelled work.

_None._

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#5798**: [Epic #5784] Phase 4: deconfound kct's match-group length tuning on board 07 (measure first)
- **#6765**: DSN exporter does not represent JLCPCB fab-floor .kicad_dru rules (fastroute stays non-comparable on board 00)

## In Progress

Issues currently being built (`loom:building`).

- **#6554**: diffpair: reproduce KiCad's diff_pair_uncoupled primitive pairing exactly (kct is a lower bound)
- **#6824**: board 03: fresh lattice routes fail routing_plan.usb_geometry (USB_D common run not on In2.Cu, MCU launch > 4 mm) — blocks #6394
- **#6837**: [Epic #6441] Phase 2c: extract engine-free router/pairplan/ package + structured PairOutcome (prereq for P6 #6448)
- **#6894**: ci: add a Windows smoke job; skip test_peak_rss_bytes_positive where resource is unavailable (follow-up #6739)

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#6910**: feat(router): lattice pairs try the gateway on a preferred layer before planar
- **#6926**: refactor(router): pairplan attach and anchor-shift candidate generators (#6837 slice 2c-iii)

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#6874**: ci(board-07): move copper-safety DRC ratchet to scheduled slow-tests
- **#6900**: ci: add Windows smoke job and skip RSS positivity test without resource

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
- **#5743**: ci: 54% of CI time is board jobs with zero failures — one duplicated routing pass, one binary path filter, and an inverted cadence *(curated)*
- **#5878**: File the 6 tscircuit upstream defects found by the interop gate (needs a credential with issue-write outside rjwalters/*) *(curated)*
- **#6394**: board 03: 13 copper violations of the router's own Power class (0.2 mm) exposed by #6390 *(curated)*
- **#6404**: boards 00-07: refill saved zones and refresh hash-bound bundles/readiness after #6390's sidecar update *(curated)*
- **#6445**: [Epic #6441] Phase 3: via pairs as a single move in the lattice coupled centreline *(curated)*
- **#6446**: [Epic #6441] Phase 4: budget-driven hybrid fallback — longest coupled trunk plus single-ended terminal legs *(curated)*
- **#6527**: router: Router.route_bidirectional has no production caller; wire it into the Python fallback and measure, or remove it *(curated)*
- **#6554**: diffpair: reproduce KiCad's diff_pair_uncoupled primitive pairing exactly (kct is a lower bound) *(curated)*
- **#6765**: DSN exporter does not represent JLCPCB fab-floor .kicad_dru rules (fastroute stays non-comparable on board 00) *(curated)*
- **#6824**: board 03: fresh lattice routes fail routing_plan.usb_geometry (USB_D common run not on In2.Cu, MCU launch > 4 mm) — blocks #6394 *(curated)*
- **#6835**: router: raise flag-off coupled iteration budget to 350k/phase (700k total) so Board07 couples 6/7 pairs; soften endpoint-tolerance comment (#6826 follow-up) *(curated)*
- **#6837**: [Epic #6441] Phase 2c: extract engine-free router/pairplan/ package + structured PairOutcome (prereq for P6 #6448) *(curated)*
- **#6850**: Live-verify #6842 gateway offer on boards 03/06b/06rf/07rf *(curated)*
- **#6858**: router: coupled pre-pass should keep the better of the joint-search and pose-centreline candidates (compare-and-keep; unblocks #6835) *(curated)*
- **#6883**: Lattice coupled pairs never leave the pad layer for a preferred, opted-in plane (board 03 USB_D common run stays on F.Cu) *(curated)*
- **#6894**: ci: add a Windows smoke job; skip test_peak_rss_bytes_positive where resource is unavailable (follow-up #6739) *(curated)*

## Proposed (Architect / Hermit)

- **#5858**: [Epic #5509] Phase 4e: hermit pass — delete duplicate clearance arithmetic *(architect)*
- **#5914**: [Epic #5508] Phase 3b: coupled first-leg/departure selection against the access map *(architect)*
- **#6441**: Epic: diff-pair routing redesign — gateways, via pairs, uncoupled budget, per-segment rules *(architect)*
- **#6445**: [Epic #6441] Phase 3: via pairs as a single move in the lattice coupled centreline *(architect)*
- **#6446**: [Epic #6441] Phase 4: budget-driven hybrid fallback — longest coupled trunk plus single-ended terminal legs *(architect)*
- **#6447**: [Epic #6441] Phase 5: coupling-preserving skew tuning from a skew ledger, and pair invariants for post-passes *(architect)*
- **#6448**: [Epic #6441] Phase 6: unify diff-pair engines — grid consumes PairPlan or grid coupled pre-pass retired *(architect)*
- **#5802**: router/core.py: Autorouter god-class has regrown to 20,764 lines (216 methods), 4.6x since last extraction closed *(hermit)*

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
| Operator merge-risk holds | 2 |
| Very important | 0 |
| Important | 0 |
| Ready (`loom:issue`) | 2 |
| In Progress (`loom:building`) | 4 |
| PRs awaiting review | 2 |
| Approved PRs awaiting merge | 2 |
| Curated | 24 |
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

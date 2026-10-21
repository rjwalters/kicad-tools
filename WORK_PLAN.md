# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-10-20*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

_None._

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#7580**: router: finish diff-pair via-count parity (continuation of #7093)

## Important

`loom:important`; taken before unlabelled work.

- **#6858**: router: coupled pre-pass should keep the better of the joint-search and pose-centreline candidates (compare-and-keep; unblocks #6835)
- **#7269**: board 07 MIPI_DAT0: recover 0.038 mm skew lost to compare-and-keep (blocks #6858)

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#7699**: schema: root sheet UUID in HierarchyBuilder._load_schematic uses recursive find("uuid"); a root without a top-level uuid picks up a nested one

## In Progress

Issues currently being built (`loom:building`).

- **#7521**: Diff-pair skew compensation per best practice (Q16): exclude tuning bumps from uncoupled metric, emit KiCad tuning patterns, geometry gates, sourced per-class skew limits
- **#7580**: router: finish diff-pair via-count parity (continuation of #7093)
- **#7612**: Board 07 fixture: 4 diff pairs fail skew with no_suitable_segment (TMDS_D2, MIPI_DAT1, MIPI_DAT0, MIPI_CLK)
- **#7659**: test flake: test_route_entry_path_goldens partial-rule_relaxation times out (C++ gives up on /D, Python fallback >60 s) — follow-up to #7626
- **#7665**: ci: build and test site/ in CI: site-only PRs and Dependabot bumps merge without running vitest, astro check or the build
- **#7669**: bench: re-run PocketBeagle and STRF (zero-touch + tuned) after the #4945/#5302 fixes and add a dispatchable external-bench workflow
- **#7675**: Board 07: remove DRAFT from titles and re-issue readiness
- **#7682**: 3D coverage audit: count footprints, not refs, in `kct check`, `kct render` and `kct fleet status`
- **#7684**: Model insertion that cannot regress: existence-checked `add-3d-models` tiers, generators own their models, and a regeneration guard
- **#7694**: chore: add ecosystem registry entries for the 3D-model neighbours

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#7660**: fix(router): stop counting tied iterations as progress for patience
- **#7671**: ci: run site test/check/build/check-links on site-relevant PRs
- **#7681**: fix(boards): drop DRAFT from board 07 titles and re-issue readiness
- **#7693**: feat: existence-check 3D model refs and add ensure_models for generators
- **#7695**: feat: audit 3D model coverage per footprint with shared classifier
- **#7698**: ci: add dispatch-only external benchmark re-run workflow

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
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)*
- **#5550**: Reconcile 4 outstanding loom-quarantine stashes left by the worktree reaper (issues 5201, 4982, 5216, 5004) *(curated)*
- **#5798**: [Epic #5784] Phase 4: deconfound kct's match-group length tuning on board 07 (measure first) *(curated)*
- **#6445**: [Epic #6441] Phase 3: via pairs as a single move in the lattice coupled centreline *(curated)*
- **#6446**: [Epic #6441] Phase 4: budget-driven hybrid fallback — longest coupled trunk plus single-ended terminal legs *(curated)*
- **#6858**: router: coupled pre-pass should keep the better of the joint-search and pose-centreline candidates (compare-and-keep; unblocks #6835) *(curated)*
- **#7186**: kct post-route DRC reports 33 shorts on routed board 07m where pinned kicad-cli reports 0 (engine disagreement) *(curated)*
- **#7256**: board 03: after #7163 a fresh arm C route fails usb_geometry (MCU launch 4.138/4.284 > 4 mm; USB width 0.30 -> 0.275) *(curated)*
- **#7269**: board 07 MIPI_DAT0: recover 0.038 mm skew lost to compare-and-keep (blocks #6858) *(curated)*
- **#7349**: Boards 06/07: enable diff-pair gap tightening on seed-42 re-routes and drop the impedance caps (#7344 PR 2) *(curated)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)*
- **#7518**: DSN export: verify a Specctra router doesn't double-flip back-side padstack layers (deferred from #7481 / #7496) *(curated)*
- **#7521**: Diff-pair skew compensation per best practice (Q16): exclude tuning bumps from uncoupled metric, emit KiCad tuning patterns, geometry gates, sourced per-class skew limits *(curated)*
- **#7548**: Board 06 synthetic fixture: 9 diff pairs over max skew on the seed-42 re-route (identity-allowed in check_diffpair_coverage.py) *(curated)*
- **#7557**: Board 07: TMDS_D0_N pad U4.B2 has no legal exit; needs placement rework or joint TMDS escape-corridor allocation (#3673) *(curated)*
- **#7573**: router: board 05 +5V/GND pour completeness depends on where nTRIP lands (C10 stitch sites unreserved) *(curated)*
- **#7580**: router: finish diff-pair via-count parity (continuation of #7093) *(curated)*
- **#7611**: Board 07 fixture: DDR_DATA_BYTE_0 and MIPI_CSI_LANES group length skew over tolerance *(curated)*
- **#7612**: Board 07 fixture: 4 diff pairs fail skew with no_suitable_segment (TMDS_D2, MIPI_DAT1, MIPI_DAT0, MIPI_CLK) *(curated)*
- **#7619**: Board 07 fixture: DQS and TMDS_D1 diff pairs routed with unequal via counts (N leg +2 vias), causing multi-mm skew *(curated)*
- **#7624**: router: recover 06rf USB3_RX1/RX2 coupling at J1 after #7517 corridor competition (Q29 tracked regression) *(curated)*
- **#7648**: `sch_set_footprint` corrupts sheets (footprint used as a `re.sub` template, unescaped, universal-newline round trip); S-expression writers emit raw `\r`; `Footprint.to_sexp` escapes nothing *(curated)*
- **#7649**: router: source-side diff-pair skew avoidance; retire the Q35 2× interim bump excursion *(curated)*
- **#7650**: board 03: recover USB_D common-run and MCU-launch margin given up to route VBUS U2.5 (#7475) *(curated)*
- **#7659**: test flake: test_route_entry_path_goldens partial-rule_relaxation times out (C++ gives up on /D, Python fallback >60 s) — follow-up to #7626 *(curated)*
- **#7665**: ci: build and test site/ in CI: site-only PRs and Dependabot bumps merge without running vitest, astro check or the build *(curated)*
- **#7666**: site: gallery shows stale or raw board identity: unchecked board.json age, PCB file stems as titles, and board 07 still titled "DRAFT" *(curated)*
- **#7669**: bench: re-run PocketBeagle and STRF (zero-touch + tuned) after the #4945/#5302 fixes and add a dispatchable external-bench workflow *(curated)*
- **#7675**: Board 07: remove DRAFT from titles and re-issue readiness *(curated)*
- **#7682**: 3D coverage audit: count footprints, not refs, in `kct check`, `kct render` and `kct fleet status` *(curated)*
- **#7684**: Model insertion that cannot regress: existence-checked `add-3d-models` tiers, generators own their models, and a regeneration guard *(curated)*
- **#7690**: Epic: full 3D model coverage, from demo boards to the JLCPCB catalog, reproducible on the pinned toolchain *(curated)*
- **#7694**: chore: add ecosystem registry entries for the 3D-model neighbours *(curated)*
- **#7699**: schema: root sheet UUID in HierarchyBuilder._load_schematic uses recursive find("uuid"); a root without a top-level uuid picks up a nested one *(curated)*

## Proposed (Architect / Hermit)

- **#6441**: Epic: diff-pair routing redesign — gateways, via pairs, uncoupled budget, per-segment rules *(architect)*
- **#6445**: [Epic #6441] Phase 3: via pairs as a single move in the lattice coupled centreline *(architect)*
- **#6446**: [Epic #6441] Phase 4: budget-driven hybrid fallback — longest coupled trunk plus single-ended terminal legs *(architect)*
- **#6447**: [Epic #6441] Phase 5: coupling-preserving skew tuning from a skew ledger, and pair invariants for post-passes *(architect)*
- **#6448**: [Epic #6441] Phase 6: unify diff-pair engines — grid consumes PairPlan or grid coupled pre-pass retired *(architect)*

## Epics

- **#3438**: router: board 07 stuck at 28/31 — negotiated router cannot complete parallel pad-array bundles (DDR bundle ALONE routes 9/11)
- **#4410**: board-05: generator must produce a manufacturable BLDC board unattended (81% route, 7 ISENSE/PWM open, 2 real shorts)
- **#4431**: route: per-net scalar clearance cannot express HV isolation — need class-pair (net-class × net-class) clearance rules in the router; both workarounds proven inadequate on a mains board
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC
- **#5784**: Epic: adopt measured KiCadRoutingTools techniques (oracle completion, pose diff-pair search, N+1 rip-up)
- **#5952**: Epic: Harness-neutral kicad-tools (Claude Code, Codex CLI, opencode)
- **#6441**: Epic: diff-pair routing redesign — gateways, via pairs, uncoupled budget, per-segment rules

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 0 |
| Very important | 1 |
| Important | 2 |
| Ready (`loom:issue`) | 1 |
| In Progress (`loom:building`) | 10 |
| PRs awaiting review | 6 |
| Approved PRs awaiting merge | 0 |
| Curated | 39 |
| Architect / Hermit proposals | 5 |
| Active epics | 7 |
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

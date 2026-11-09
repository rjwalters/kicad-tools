# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-05*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#8623**: chore(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#8626**: chore(deps): bump astral-sh/setup-uv from 10.3.0 to 10.3.2 in the github-actions group across 1 directory

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

_None._

## Important

`loom:important`; taken before unlabelled work.

_None._

## Ready

Human-approved issues ready for implementation (`loom:issue`).

_None._

## In Progress

Issues currently being built (`loom:building`).

- **#8532**: check rules fiducials_missing/board_id_missing and default tapeout/readiness step
- **#8629**: submit-jlcpcb --website: SELECTORS are placeholders that match nothing on the live quote page; CDP attach fails on a Chrome with no open tabs
- **#8639**: optimize-traces: drop dead _infer_board_stack and restore 'undetermined' stack_source
- **#8643**: Warn when a board is checked without its paired .kicad_pro/.kicad_dru (kicad-cli silently uses default rules)
- **#8644**: route: HV mask setup may consume the #8486 5 s back-off floor, so backed-off HV nets never reach the A* search
- **#8645**: route: HV mask setup runs on a 1x deadline before the 3x escape hint, and compute_expanded_blocked ignores the deadline

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#8642**: fix: report undetermined stack_source when board lacks stackup
- **#8646**: fix: raise back-off floor above HV mask setup time so backed-off nets reach A*
- **#8647**: fix: share setup/search deadline budget and bound blocked-mask dilation
- **#8648**: feat: warn on missing or stock-default DRC project rules

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#8623**: chore(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#8626**: chore(deps): bump astral-sh/setup-uv from 10.3.0 to 10.3.2 in the github-actions group across 1 directory

## Proposed

Issues carrying `loom:curated`.

- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)*
- **#5539**: Champion: Merge-Risk Hold Digest *(curated)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)*
- **#8214**: optimize-traces: collision-check each rewrite on finished boards instead of whole-net rollback *(curated)*
- **#8414**: Post-negotiation sweep overruns its announced 60 s bound until the global timeout *(curated)*
- **#8425**: Live-verify covered-track split (#8406) on pinned kicad-cli and #8368 board B *(curated)*
- **#8434**: Changelog Fragment Check re-runs on every label change; Loom role label churn keeps it queued *(curated)*
- **#8486**: route perf: nets proven sealed are re-searched at full budget and re-rescued every iteration (43% of search time, rescue 0/15 for ~1,475 s each pass) *(curated)*
- **#8532**: check rules fiducials_missing/board_id_missing and default tapeout/readiness step *(curated)*
- **#8577**: route: new copper keeps only its own class clearance from preserved copper of a wider-clearance class (fails kicad-cli DRC) *(curated)*
- **#8602**: Board 07: DRC-verified prune and re-derive authored escapes so validate.py passes (part of #8399) *(curated)*
- **#8610**: route --complete chain pass commits ~282 shorts against Net 0 copper on dense mains board (rc 4) *(curated)*
- **#8620**: Find the real cause of the 10-12x pure-Python routing slowdown with all extras on macOS *(curated)*
- **#8629**: submit-jlcpcb --website: SELECTORS are placeholders that match nothing on the live quote page; CDP attach fails on a Chrome with no open tabs *(curated)*
- **#8639**: optimize-traces: drop dead _infer_board_stack and restore 'undetermined' stack_source *(curated)*
- **#8643**: Warn when a board is checked without its paired .kicad_pro/.kicad_dru (kicad-cli silently uses default rules) *(curated)*
- **#8644**: route: HV mask setup may consume the #8486 5 s back-off floor, so backed-off HV nets never reach the A* search *(curated)*
- **#8645**: route: HV mask setup runs on a 1x deadline before the 3x escape hint, and compute_expanded_blocked ignores the deadline *(curated)*

## Proposed (Architect / Hermit)

_None._

## Epics

- **#4431**: route: per-net scalar clearance cannot express HV isolation — need class-pair (net-class × net-class) clearance rules in the router; both workarounds proven inadequate on a mains board
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC
- **#5952**: Epic: Harness-neutral kicad-tools (Claude Code, Codex CLI, opencode)

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 2 |
| Very important | 0 |
| Important | 0 |
| Ready (`loom:issue`) | 0 |
| In Progress (`loom:building`) | 6 |
| PRs awaiting review | 4 |
| Approved PRs awaiting merge | 2 |
| Curated | 18 |
| Architect / Hermit proposals | 0 |
| Active epics | 3 |
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

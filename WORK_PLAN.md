# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-10-29*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#8105**: docs(site): refresh kicad-tools.org homepage for v0.25.0
- **#8119**: chore(loom): resync installed Loom to v0.19.1236
- **#8139**: refactor(ci): move board04 paid-drill dispatch out of vendored routed-DRC gate

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

- **#8134**: install-kct.sh vendors .kct/ci/check_routed_drc.py with board04-specific paid-drill code
- **#8148**: route: size the post-route reserve so one oracle refill+DRC round fits at --timeout 600 (board 03: oracle stage ~144 s vs 90 s reserve)
- **#8149**: study: what bounded extra-length or skew trade would give a via-equal hybrid leg pair on board 03 USB_D (J1.A6→U2.3)? Measure, no tolerance change
- **#8153**: pcb move-via --check does not evaluate hole clearance (passes moves kicad-cli rejects)
- **#8154**: export: bom_lcsc_stock reports OK without checking LCSC numbers merged from an existing BOM
- **#8155**: route --nets: 'blocked by stored-via geometry (blocking net X)' blames a distant net; escape-limited nets fail slowly
- **#8156**: route writes unsuffixed fab_profile.json / net_class_map.json and leaves *_unverified_* sets beside the board
- **#8157**: bom_lcsc_values: RV (varistor) references classified as resistors, so a correct MOV FAILs as 'different component type'
- **#8158**: tapeout: re-running into an existing bundle dir fails sign-off Gate 1 on the previous run's stale manifest.json
- **#8159**: silk_overlap: intra-footprint false positives remain for fp_line vs fp_rect (follow-up to #8090)
- **#8168**: test flake: tests/test_zones_cmd.py::TestFillSuccess::test_fill_calls_run_fill_zones fails on Test job

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#8150**: perf: raise post-route finishing reserve to 27% of --timeout
- **#8152**: chore(research): add hybrid-parity relaxation study driver for board 03 USB_D
- **#8160**: fix: report nearest via-blocker and via-site-aware pin-access advice
- **#8161**: fix: classify RV* references as non-resistors in value parsing
- **#8162**: fix: clear stale manifest.json in sign-off Gate 1
- **#8163**: fix: evaluate CSV-merged LCSC numbers in stock preflight and warn when nothing checked
- **#8164**: fix: exempt same-footprint line-to-rect silk joints from silk_overlap
- **#8165**: fix: check hole_clearance and hole_to_hole in pcb move-via --check
- **#8167**: fix: key kct route sidecars to the output stem and list unverified sets
- **#8169**: test: mock check_saved_fill in mocked zones fill success tests

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#8105**: docs(site): refresh kicad-tools.org homepage for v0.25.0
- **#8119**: chore(loom): resync installed Loom to v0.19.1236
- **#8139**: refactor(ci): move board04 paid-drill dispatch out of vendored routed-DRC gate

## Proposed

Issues carrying `loom:curated`.

- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)*
- **#8134**: install-kct.sh vendors .kct/ci/check_routed_drc.py with board04-specific paid-drill code *(curated)*
- **#8148**: route: size the post-route reserve so one oracle refill+DRC round fits at --timeout 600 (board 03: oracle stage ~144 s vs 90 s reserve) *(curated)*
- **#8149**: study: what bounded extra-length or skew trade would give a via-equal hybrid leg pair on board 03 USB_D (J1.A6→U2.3)? Measure, no tolerance change *(curated)*
- **#8153**: pcb move-via --check does not evaluate hole clearance (passes moves kicad-cli rejects) *(curated)*
- **#8154**: export: bom_lcsc_stock reports OK without checking LCSC numbers merged from an existing BOM *(curated)*
- **#8155**: route --nets: 'blocked by stored-via geometry (blocking net X)' blames a distant net; escape-limited nets fail slowly *(curated)*
- **#8156**: route writes unsuffixed fab_profile.json / net_class_map.json and leaves *_unverified_* sets beside the board *(curated)*
- **#8157**: bom_lcsc_values: RV (varistor) references classified as resistors, so a correct MOV FAILs as 'different component type' *(curated)*
- **#8158**: tapeout: re-running into an existing bundle dir fails sign-off Gate 1 on the previous run's stale manifest.json *(curated)*
- **#8159**: silk_overlap: intra-footprint false positives remain for fp_line vs fp_rect (follow-up to #8090) *(curated)*
- **#8168**: test flake: tests/test_zones_cmd.py::TestFillSuccess::test_fill_calls_run_fill_zones fails on Test job *(curated)*

## Proposed (Architect / Hermit)

_None._

## Epics

- **#4410**: board-05: generator must produce a manufacturable BLDC board unattended (81% route, 7 ISENSE/PWM open, 2 real shorts)
- **#4431**: route: per-net scalar clearance cannot express HV isolation — need class-pair (net-class × net-class) clearance rules in the router; both workarounds proven inadequate on a mains board
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC
- **#5952**: Epic: Harness-neutral kicad-tools (Claude Code, Codex CLI, opencode)

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 3 |
| Very important | 0 |
| Important | 0 |
| Ready (`loom:issue`) | 0 |
| In Progress (`loom:building`) | 11 |
| PRs awaiting review | 10 |
| Approved PRs awaiting merge | 3 |
| Curated | 13 |
| Architect / Hermit proposals | 0 |
| Active epics | 4 |
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

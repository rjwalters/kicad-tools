# Fleet-router verdict: TraceMaker vs fastroute vs `kct route`

Issue #6335 (parent #5943, protocol #6334). Single predeclared run per board/tool on the committed demo fleet, fixed placement, one referee. Machine-readable twin: [`results.json`](results.json). Table below was generated from it row-for-row.

## Setup (all frozen before the run)

- **kct** `9a1fb50494be` (clean tree), C++ backend 1.0.0 (build 56) built in this worktree; `kicad-cli` 10.0.6; Python 3.12.3; Linux x86_64, 16 CPUs, shared host.
- **TraceMaker** `89ff58ca` (CPU-only, g++ 13.3, scratch prefix for nlohmann_json 3.11.3 / CLI11 2.4.2); binary sha256 `d254662ce819f6be...`.
- **fastroute** `e5d52c45` (0.1.7, `cargo build --release --locked -j 2`); binary sha256 `26a02e3e2254540...`. Full hashes: `evidence/binary-hashes.sha256`.
- **Budgets** (manifest `e9d97d53ebcc...`, unmodified; no `--wall-cap` override): wall cap **600 s** (the manifest value, superseding the issue body's 1,200 s proposal), 1 thread, seed 1, TraceMaker `--work 50000000`, fastroute `--multi-start` at its upstream default (no `-mp` value was set). Timeout rows show ~620 s because kill-and-reap time is included.
- Reproduce: `uv run python benchmarks/external/fleet_routers/run.py run --work-dir <scratch> --boards 00,01,02,03,04,05,06,07,08,09 --tracemaker-dir <pin> --tracemaker-build-log <log> --fastroute-dir <pin>`; builds per `README.md`.
- Board 08 has no committed `.kicad_pcb`: three `unsupported` rows. Boards 06, 07 and 09 are stripped of tracks/vias by the protocol's policy; `boards/` is unchanged by the run.

## Results

`Outcome` is the driver outcome: `success` means "ran and scored, no known rule loss", **not** "clean". `Clean pass` requires every referee to pass and 100% completion. `kct check` is the JLCPCB manufacturing profile; DRC columns are `kicad-cli` (total error-level count incl. pre-existing errors; the `input` row shows those: none on any board).

| Board | Tool | Outcome | Connections | DRC errors | DRC warnings | Unconnected | kct check (jlcpcb) errors | Sync | Vias | Wirelength mm | Clean pass | Wall s | Lost constraints |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 00 | input | reference | 0/3 | 0 | 3 | 3 | 3 | in sync | 0 | 0 | False | n/a | 0 |
| 00 | kct | success | 3/3 | 0 | 3 | 0 | 0 | in sync | 0 | 8.71 | True | 23.62 | 0 |
| 00 | tracemaker | success | 3/3 | 0 | 3 | 0 | 0 | in sync | 0 | 29.66 | True | 0.11 | 0 |
| 00 | fastroute | non-comparable | 3/3 | 0 | 3 | 0 | 0 | in sync | 0 | 30.17 | True | 0.03 | 1 |
| 01 | input | reference | 0/5 | 0 | 4 | 5 | 3 | in sync | 0 | 0 | False | n/a | 0 |
| 01 | kct | success | 5/5 | 0 | 4 | 0 | 0 | in sync | 5 | 65.25 | True | 10.07 | 0 |
| 01 | tracemaker | success | 5/5 | 0 | 4 | 0 | 0 | in sync | 0 | 54.02 | True | 0.47 | 0 |
| 01 | fastroute | non-comparable | 5/5 | 0 | 4 | 0 | 0 | in sync | 0 | 54.62 | True | 0.02 | 1 |
| 02 | input | reference | 0/36 | 0 | 0 | 36 | 12 | in sync | 0 | 0 | False | n/a | 0 |
| 02 | kct | success | 36/36 | 0 | 3 | 0 | 0 | in sync | 43 | 406.64 | True | 185.35 | 0 |
| 02 | tracemaker | success | 36/36 | 0 | 0 | 0 | 3 | in sync | 9 | 478.38 | False | 19.66 | 0 |
| 02 | fastroute | non-comparable | 36/36 | 6 | 0 | 0 | 6 | in sync | 6 | 509.78 | False | 2.37 | 2 |
| 03 | input | reference | 43/115 | 0 | 39 | 72 | 25 | in sync | 0 | 0 | False | n/a | 0 |
| 03 | kct | success | 114/115 | 5 | 39 | 1 | 0 | in sync | 69 | 733.18 | False | 212.22 | 0 |
| 03 | tracemaker | success | 115/115 | 1 | 36 | 0 | 30 | in sync | 60 | 949.14 | False | 218.56 | 0 |
| 03 | fastroute | non-comparable | 75/115 | 83 | 36 | 41 | 48 | in sync | 49 | 785.53 | False | 8.54 | 14 |
| 04 | input | reference | 0/43 | 0 | 17 | 43 | 12 | in sync | 0 | 0 | False | n/a | 0 |
| 04 | kct | success | 43/43 | 0 | 17 | 0 | 0 | in sync | 35 | 214.54 | True | 55.82 | 0 |
| 04 | tracemaker | success | 43/43 | 0 | 18 | 0 | 7 | in sync | 22 | 408.56 | False | 33.46 | 0 |
| 04 | fastroute | non-comparable | 43/43 | 0 | 17 | 0 | 10 | in sync | 10 | 455.93 | False | 2.87 | 2 |
| 05 | input | reference | 30/142 | 0 | 7 | 112 | 37 | in sync | 0 | 0 | False | n/a | 0 |
| 05 | kct | timeout | -- | -- | -- | -- | -- | -- | -- | -- | -- | 620.2 (TIMEOUT) | 0 |
| 05 | tracemaker | success | 142/142 | 0 | 7 | 0 | 15 | in sync | 60 | 1143.97 | False | 220.39 | 0 |
| 05 | fastroute | non-comparable | 141/142 | 20 | 12 | 1 | 6 | in sync | 43 | 1297.33 | False | 16.01 | 9 |
| 06 | input | reference | 8/90 | 0 | 0 | 82 | 16 | in sync | 0 | 0 | False | n/a | 0 |
| 06 | kct | success | 90/90 | 0 | 0 | 0 | 0 | in sync | 55 | 507.88 | True | 62.38 | 0 |
| 06 | tracemaker | success | 90/90 | 0 | 0 | 0 | 10 | in sync | 25 | 847.44 | False | 49.36 | 0 |
| 06 | fastroute | non-comparable | 82/90 | 20 | 0 | 8 | 16 | in sync | 21 | 603.04 | False | 1.97 | 10 |
| 07 | input | reference | 2/161 | 0 | 0 | 159 | 53 | in sync | 0 | 0 | False | n/a | 0 |
| 07 | kct | timeout | -- | -- | -- | -- | -- | -- | -- | -- | -- | 620.16 (TIMEOUT) | 0 |
| 07 | tracemaker | timeout | -- | -- | -- | -- | -- | -- | -- | -- | -- | 600.13 (TIMEOUT) | 0 |
| 07 | fastroute | non-comparable | 130/161 | 549 | 0 | 31 | 209 | in sync | 138 | 2325.6 | False | 9.39 | 7 |
| 08 | kct | unsupported | -- | -- | -- | -- | -- | -- | -- | -- | -- | n/a | 0 |
| 08 | tracemaker | unsupported | -- | -- | -- | -- | -- | -- | -- | -- | -- | n/a | 0 |
| 08 | fastroute | unsupported | -- | -- | -- | -- | -- | -- | -- | -- | -- | n/a | 0 |
| 09 | input | reference | 9/124 | 0 | 1 | 115 | 31 | in sync | 0 | 0 | False | n/a | 0 |
| 09 | kct | timeout | -- | -- | -- | -- | -- | -- | -- | -- | -- | 620.15 (TIMEOUT) | 0 |
| 09 | tracemaker | success | 114/124 | 0 | 2 | 10 | 39 | in sync | 27 | 775.62 | False | 589.73 | 0 |
| 09 | fastroute | non-comparable | 117/124 | 20 | 1 | 7 | 17 | in sync | 45 | 1124.24 | False | 12.74 | 1 |

## What was measured

**Completion and DRC (separate from manufacturing and sync).** Every row is `in sync` against the frozen schematic, so no router broke pad nets.

- `kct route`: 100% completion and 0 DRC errors with clean pass on 00, 01, 02, 04, 06. On 03 it left 1 connection unrouted with 5 DRC errors. It **timed out at 600 s** on 05, 07 and 09 (no scored result; partial boards are not scored).
- TraceMaker: 100% completion on 00–06 (06: 90/90) and 0 DRC errors on all of them except 03 (1 `starved_thermal`). It needs 0.1–220 s on those boards. It **timed out on 07** and on **09 finished at 589.7 s with 114/124 connections (10 unconnected)**. Its clean pass is `false` on 02–06 because `kct check` reports `via_in_pad` (3, 30, 6, 15, 10 on 02, 03, 04, 05, 06 respectively) and, on 04, one `hole_to_hole_clearance`; these are manufacturing-profile findings that `kicad-cli` DRC does not flag. On 09 `kct check` also reports 9 `connectivity`, 1 `hole_to_hole_clearance` and 29 `via_in_pad`.
- fastroute: **every row is `non-comparable`** (see below), so none supports a ranking. Diagnostic numbers: 0 DRC errors on 00, 01, 04; 6 `annular_width` on 02; 83 on 03 (75/115 connected); 20 on 05; 20 on 06 (82/90); 549 on 07 (130/161); 20 on 09 (117/124).

**Where each succeeds or fails.** Simple boards (00, 01): all three connect everything with zero DRC errors; kct is shortest on wirelength for 00 (8.71 mm vs 29.66 / 30.17) and TraceMaker on 01 (54.02 vs 65.25 mm, 0 vs 5 vias). On the mid-size boards (02, 04, 06) kct and TraceMaker both complete; kct is clean on the `kct check` profile where TraceMaker is not (a home-referee comparison: `kct check` is our own checker, and its JLCPCB-profile `via_in_pad` rule is one `kicad-cli` does not flag), TraceMaker is faster on 02, 04 and 06 (19.7 s vs 185.4 s; 33.5 s vs 55.8 s; 49.4 s vs 62.4 s). On the densest boards neither is uniformly better: only TraceMaker completes 05; neither completes 07; only TraceMaker returns a (partial, 10 unconnected) result on 09; on 03 TraceMaker completes where kct leaves one connection. These are single runs on one seed, so differences of this size are anecdotes, not rankings.

## Why fastroute rows are non-comparable

The counts below come from each fastroute row's `lost_constraints` in `results.json`. This run's base already includes #6768 (issue #6756). The 9 JLCPCB fab-only `.kicad_dru` rules on 00–07 (silk, hole clearance, annular ring, copper-to-edge and hole-to-edge rules, text size) therefore sit in `not_routing_rules` and are **not** counted as losses. The losses that make these rows non-comparable fall into these categories:

| Board | Losses | Net-class clearances (exemption-scoped) | Native zone clearances | Diff-pair rules | `SMD Pad Clearance - jlcpcb` | Board `min_copper_edge_clearance` 0.3 mm |
|---|---|---|---|---|---|---|
| 00 | 1 | 1 (`Reviewed clearance - Power`) | – | – | – | – |
| 01 | 1 | 1 (`Reviewed clearance - Power`) | – | – | – | – |
| 02 | 2 | 2 (`Reviewed clearance - Clock/Power`) | – | – | – | – |
| 03 | 14 | 2 (`Reviewed clearance - Clock/Power`) | 4 (GND on F.Cu/In1.Cu/B.Cu, VCC on In2.Cu) | 8 (USB_D and USB_MCU_D: `diff_pair_gap`, `diff_pair_uncoupled`, `skew`, `via_count` each) | – | – |
| 04 | 2 | 2 (`kct net class clearance - Clock/Power`) | – | – | – | – |
| 05 | 9 | 7 (`Reviewed clearance - Audio/Debug/Differential/Digital/Clock/HighCurrentSignal/Power`) | – | – | 1 | 1 |
| 06 | 10 | 1 (`Reviewed clearance - Power`) | – | 8 (LVDS1–4: `diff_pair_gap`, `skew` each) | – | 1 |
| 07 | 7 | 6 (`Reviewed clearance - AUX/SDRAM_ADDRESS/SDRAM_BYTE0/SDRAM_BYTE1/SDRAM_COMMAND/SWD_CLOCK`) | – | – | – | 1 |
| 09 | 1 | – | – | – | – | 1 |

What would make these rows comparable, by category:

- **Current-main note (audit re-run at `a34d5a63`, 2026-10-15, issue #6800).** The audit now reports a class-scoped clearance whose named classes have no member nets under the frozen project under `not_routing`, not as a loss. Re-running prepare + `dsn_audit` on boards 00–07 (manifest unchanged) gives 0 losses on 00, 01, 02, 04, 05, 07; 6 on 03 (diff-pair `uncoupled`/`skew`/`via_count`) and 4 on 06 (LVDS `skew`). The per-board counts in the table above predate this change.

- **Net-class clearances (all 22).** Each is a `NetClass == 'X'` clearance whose only other clauses are kct's same-footprint-pad and coupled-diff-pair exemptions. They are counted lost because the frozen `.kicad_pro` (the pre-route one) defines only `Default`, while the frozen `_routed.kicad_dru` names classes that only the routed sibling project defines. No net is in those classes under the graded project, so the exporter emits no class for them and `kicad-cli` applies the rules to no net. Neither #6768 nor PR #6770 (#6765, superseded and empty) changes this; see the current-main note below. Tracked in #6800.
- **Board copper-to-edge clearance (05, 06, 07, 09).** DSN-expressible, for example by insetting the exported boundary. The exporter does not do this today. Tracked in #6784.
- **`diff_pair_gap` (2 on 03, 4 on 06).** Its 0.15 mm minimum equals the pair nets' exported class clearance (`HighSpeed` on 03, `LVDS` on 06), so the DSN already implies it. The audit counts it lost only because it compares nothing but clearance, width, via and hole against the DSN. Tracked in #6784.
- **Native zone clearances (03) and `SMD Pad Clearance` (05).** These are not router-side constraints. The scorer refills zones, so the filler applies the zone clearance, and pad-to-pad spacing between footprints is fixed by placement. Both are candidates for `not_representable`. That classification decision is also recorded in #6784.
- **Diff-pair `skew`, `diff_pair_uncoupled`, `via_count` (6 on 03, 4 on 06).** These are post-route length and coupling checks with no construct in our DSN exporter. No open fix covers them. Unless they are classified as not-representable (#6784 asks for that decision), 03 and 06 stay non-comparable for any DSN-fed router.

### Current main (2026-10-15, kct `847666c8`)

The table above is the measurement at kct `9a1fb50494be` and is unchanged. Re-running only the driver's `prepare_board` + `dsn_audit` on current main (no routing; the manifest verifies clean) gives these losses per board:

| Board | Losses | Net-class clearances | Diff-pair rules |
|---|---|---|---|
| 00 | 1 | 1 | – |
| 01 | 1 | 1 | – |
| 02 | 2 | 2 | – |
| 03 | 8 | 2 | 6 (`diff_pair_uncoupled`, `skew`, `via_count` for USB_D and USB_MCU_D) |
| 04 | 2 | 2 | – |
| 05 | 7 | 7 | – |
| 06 | 5 | 1 | 4 (`skew`, LVDS1–4) |
| 07 | 6 | 6 | – |
| 09 | 0 | – | – |

- **Gone since the table was measured (#6784):** copper-to-edge clearance on 05/06/07/09 (the exporter now insets the boundary), `diff_pair_gap` on 03/06, native zone clearances on 03 and `SMD Pad Clearance` on 05. The last two are now in `not_routing_rules`.
- **Kept on purpose (#6784):** the diff-pair `skew` / `diff_pair_uncoupled` / `via_count` rules on 03 and 06.
- **Still counted: the same 22 net-class clearances.** The DSN exporter does write class clearances when the project defines the classes. Auditing the routed triplets (`*_routed.kicad_{pcb,pro,dru}`) reports none of these 22; for example, board 00 exports `(class Power ... (clearance 200.0))`. That is what the PR #6770 Doctor audit measured. The driver, however, grades against the pre-route `.kicad_pro`, where no net is in those classes. Whether to count these rules as inert or to freeze a project that defines the classes is decided in #6800.

The DRC failures on 02, 03, 05, 06, 07 and 09 (annular ring, hole clearance, shorting, solder-mask bridge) are diagnostic only. This run does not isolate their cause. **Fastroute's routing quality is not established by this run.**

## Evidence, provenance and independence

- Raw `kicad-cli` DRC JSON for all 32 scored rows (gzip) and last 20 KB of each tool log: `evidence/drc/`, `evidence/logs/`. Hashes of every router output (routed boards and SES; the boards themselves are not committed): `evidence/output-hashes.sha256`. Binary hashes: `evidence/binary-hashes.sha256`.
- `results.json` carries the manifest sha256 `e9d97d53ebcc...` and every graded-rules hash per row; paths in it are scratch paths of the run host.
- Independent re-score of one simple, one dense and one non-comparable row against the frozen rules: `evidence/rescore.md` (counts equal the driver's). A referee that did not run is recorded as unavailable, never as pass.

## Hypotheses (not measured causes)

Observed, not explained: TraceMaker's via-in-pad findings (02–06, 09) coincide with it placing vias in pads that kct avoids; TraceMaker completes the dense boards 03/05 that kct runs out of the 600 s budget on (05) or leaves one connection on (03). The portfolio-under-budget and escape/negotiation ideas that may explain this are tracked in #5947; this run does not show that any technique caused any difference. Results are one seed and one run per row on a shared host; timing is wall time including process start.

## Follow-ups

- Re-run the fastroute rows (with a manifest re-freeze) once #6800 is decided. #6784 has landed. After #6800, 00, 01, 02, 04, 05, 07 and 09 should have no losses. 03 and 06 stay non-comparable for DSN routers because of the diff-pair length and coupling rules #6784 keeps as losses.
- #5947 consumes these rows.

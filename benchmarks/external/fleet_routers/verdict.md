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

**Where each succeeds or fails.** Simple boards (00, 01): all three connect everything with zero DRC errors; kct is shortest on wirelength for 00 (8.71 mm vs 29.66 / 30.17) and TraceMaker on 01 (54.02 vs 65.25 mm, 0 vs 5 vias). On the mid-size boards (02, 04, 06) kct and TraceMaker both complete; kct is clean on the `kct check` profile where TraceMaker is not, TraceMaker is faster on 02, 04 and 06 (19.7 s vs 185.4 s; 33.5 s vs 55.8 s; 49.4 s vs 62.4 s). On the densest boards neither is uniformly better: only TraceMaker completes 05; neither completes 07; only TraceMaker returns a (partial, 10 unconnected) result on 09; on 03 TraceMaker completes where kct leaves one connection. These are single runs on one seed, so differences of this size are anecdotes, not rankings.

## Why fastroute rows are non-comparable

`kicad_tools.export.dsn` does not represent the 10 JLCPCB fab-floor `.kicad_dru` rules (tracked in #6765, open and out of scope here) nor, on 05, 06, 07 and 09, the project `min_copper_edge_clearance` (0.3 mm) board rule. The affected counts are 00:1, 01:1, 02:2, 03:14, 04:2, 05:9, 06:10, 07:7, 09:1 lost constraints, each listed per row in `results.json` (`lost_constraints`). The DRC failures on 02, 03, 05, 06, 07, 09 (annular ring, hole clearance, shorting, solder-mask bridge) are consistent with routing against a view that omitted those rules, but this run does not isolate that cause. **Fastroute's routing quality is not established by this run.** The fastroute rows should be re-run after #6765 lands and the manifest is re-frozen.

## Evidence, provenance and independence

- Raw `kicad-cli` DRC JSON for all 32 scored rows (gzip) and last 20 KB of each tool log: `evidence/drc/`, `evidence/logs/`. Hashes of every router output (routed boards and SES; the boards themselves are not committed): `evidence/output-hashes.sha256`. Binary hashes: `evidence/binary-hashes.sha256`.
- `results.json` carries the manifest sha256 `e9d97d53ebcc...` and every graded-rules hash per row; paths in it are scratch paths of the run host.
- Independent re-score of one simple, one dense and one non-comparable row against the frozen rules: `evidence/rescore.md` (counts equal the driver's). A referee that did not run is recorded as unavailable, never as pass.

## Hypotheses (not measured causes)

Observed, not explained: TraceMaker's via-in-pad findings (02–06, 09) coincide with it placing vias in pads that kct avoids; TraceMaker completes the dense boards 03/05 that kct runs out of the 600 s budget on (05) or leaves one connection on (03). The portfolio-under-budget and escape/negotiation ideas that may explain this are tracked in #5947; this run does not show that any technique caused any difference. Results are one seed and one run per row on a shared host; timing is wall time including process start.

## Follow-ups

- Re-run the fastroute rows after #6765 (and a manifest re-freeze).
- #5947 consumes these rows. No duplicate issues filed.


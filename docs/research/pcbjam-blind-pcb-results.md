# PCBJam blind-PCB study: kct on the Antmicro M.2-to-PCIe adapter

**Result: partial.** The zero-touch `kct` protocol reaches 68.4 % completion
(67 of 98 connections) on the M.2-to-PCIe adapter in 29.2 min and fails the
manufacturable bar (100 % nets routed, 0 DRC on both engines) with 146 KiCad
DRC violations and 31 unconnected items; the router itself classifies the
board infeasible under as-shipped rules (13 blocked edges, `blocked_path: 21`
at the connector pads). Every number below is reproducible from the pinned
commands in [Reproducing](#reproducing).

2026-11-19, issue #9595. Point-in-time note: describes the tree as of
`42d80f0b6` (kicad-tools) and `b5a047f3` (PCBJam/blind-pcb-runs).

## The study

[PCBJam's blind PCB study](https://github.com/PCBJam/blind-pcb-runs) (repo
MIT) publishes the inputs and scoring of its November 2026 experiments:
each board with copper stripped, the brief the agent got, the sandbox, the
host-side scoring scripts, and the results of a frontier model (Fable 5.2)
with and without their routing tool. It explicitly invites other tools to
run the same boards. This note is our first like-for-like entry; issue
#9596 tracks the ecosystem-registry side.

Board: **Antmicro M.2 to PCIe x4 adapter** (20 footprints, 25 nets, nine
PCIe differential pairs plus REFCLK, 4 copper layers, 80 × 34 mm). Chosen
per the issue's recommendation. Licence note: the issue body guessed MIT;
the licence read from the pinned tree (`boards/m2-pcie-adapter/LICENSE`) is
**Apache-2.0** (upstream `antmicro/m2-pcie-adapter` at `11844f1`, upgraded
to KiCad 10 by PCBJam) — also permissive, recorded in
`benchmarks/external/boards.toml` with the verified-on date.

## Mode and what "agent-driven" meant here

PCBJam's published run of this board (`m2-place-bt-20261113-1037`) is
**place + route**: an agent in their sandbox, tools = `kicad-cli` + `pcbnew`
(+ their `bt` helper), 22 min, free to iterate and to edit netclasses.
Their route-only mode (used on other boards) hands the agent a stripped
board with placement fixed — that is the mode this issue scopes, and it
maps 1:1 onto our external-benchmark harness: placement preserved, copper
ripped, route, measure. The brief for a route-only run of a 4-layer board
is their generic `sandbox/briefs/route-4layer.md` (`{{STEM}}` =
`m2-pcie-adapter`); its task constraints (fixed placement, 4 copper layers,
save readable output) are exactly the harness's protocol.

For this run the "agent invoking kct" was the harness itself
(`benchmarks/external/`, issue #4932), driving `kct route` in the
**zero-touch** protocol: rules as shipped, no netclass or diff-pair tuning,
no manual iteration, one seeded attempt (seed 9595) under a declared
1800 s wall-clock cap. No external or fallback router was used; the C++
router backend was confirmed before timing was recorded
(`kct build-native --check`: router/placement/DRC all available, version
1.0.0, build 70). Two nets fell back from the C++ pathfinder to kct's
built-in pure-Python A* on blocked via geometry — that is kct's own
internal fallback path, not a different router.

## Results

| | PCBJam reference (Fable 5.2) | kct zero-touch (seed 9595) |
|---|---|---|
| Mode | place + route, agent-driven | route-only, zero-touch harness |
| Time | 22 min (1333 s) | 29.2 min (1751 s, cap 1800 s) |
| Completion | 100 % (0 unconnected) | **68.4 %** (67 of 98 connections) |
| DRC errors (KiCad, zones refilled) | 0 (+ 20 pre-existing lib notices) | **146** (+ the same 20 lib notices) |
| Unconnected | 0 | **31** |
| Track length | 886 mm | 656 mm |
| Vias | 218 | 54 (20 GND, 34 signal) |
| Corners / skew | not scored by PCBJam for this board | n/a (#9594); 0/9 pairs routed coupled, so pair skew is undefined |
| Verdict | clean | **partial** |

Human baseline of the same board (captured pre-rip-up by
`normalize.py`): 571 segments, 92 vias, 1186.3 mm, 37 unrouted pads.
Fable's 218 vias are dominated by ground stitching (its NOTES describe a
ground fence at every connector ground pin and layer change); kct's
54 vias are what its negotiated router needed for the 68 % it reached.
Corners and skew are PCBJam-scorable only via per-role analysis files
they did not publish for this board (`sandbox/roles/` has H4E2 and
macropad only); kct-native layout metrics are #9594 — hence n/a.

kct's own gates on the same routed board (from the benchmark report JSON):
`kct check` **FAIL (160 E / 305 W)**, kicad-cli DRC cross-gate **135
findings** — both engines fail, consistent with PCBJam's scoring above.

### Violation profile (PCBJam `drc.sh`, KiCad 10.0.6, zones refilled)

| Severity:type | Count |
|---|---|
| error:copper_edge_clearance | 78 |
| error:clearance | 29 |
| error:hole_clearance | 15 |
| error:shorting_items | 8 |
| error:track_width | 8 |
| error:solder_mask_bridge | 8 |
| warning:text_height | 17 |
| warning:lib_footprint_issues | 20 (pre-existing on the input board) |

## Why it failed (router's own diagnosis)

- All three escalation attempts (4L SIG-GND-PWR-SIG, 4L ALL-SIG, 6L) ended
  `INSUFFICIENT` with `Failure causes: {'blocked_path': 21}`; the routing
  plan reports **overflow 40 on 13 edges — infeasible** as posed.
- The blocked geometry is the two fine-pitch connectors: via candidates at
  the M.2 edge fingers (J1) and the PCIe socket (J2) are blocked by the
  stored pad/via geometry of neighbouring pads (including NC pads), so
  pairs escape one leg at a time. Net by net: exactly one leg of every
  PCIe pair got a full-length route (e.g. `/Data_1_OUT_P` 31.2 mm,
  `/Data_1_IN_N` 31.6 mm) while the partner legs were the ones the
  pathfinder gave up on; 9 of 25 nets have no segments at all.
- The harness's placement-delta feedback loop (issue #5890) offers
  moves that would unblock the edges, but the route-only brief forbids
  moving footprints, so it was declined (and would also have consumed the
  wall-clock budget — it was skipped with 51 s of the cap left).
- Netclass note: the board ships authored `Clock`/`Differential`
  netclasses; zero-touch routes single-ended, so the nine pairs were never
  routed as coupled pairs (`Diff pairs: 0/9`). A `--tuned`-protocol run
  with a declared diff-pair config is the natural next attempt (that is
  the recipe #4943 added for STRF) and is deliberately out of scope here.

## Honest caveats

- **This is not a capability verdict.** It compares kct's unattended
  zero-touch protocol against an agent with full tool freedom and 22 min
  of iteration that could edit netclasses, add zones and stitching, and
  retry. The like-for-like column is the task (same stripped board, same
  brief constraints, same scoring), not the amount of agency.
- Single board, single seed, one run. PCBJam's own study shows
  run-to-run spread on other boards.
- kicad-cli here is **10.0.6** (host) vs the study's 10.0.8 image; the
  20 `lib_footprint_issues` warnings are pre-existing on the untouched
  input board (Fable's report shows the same notices).
- PCBJam's `lab-metrics.cjs` parses KiCad-written segment field order and
  reports `totalTrackMm: 0` on a kct-written board; their looser
  `stats.cjs` reads it fine (656 mm). Track/via numbers above are from
  their `stats.cjs` + `drc.json`; the harness's own measurement
  (`collect_report`) agrees (655.95 mm, 54 vias).
- No renders were produced: the study's `render.sh` needs `rsvg-convert`,
  not installed on this host. The placement check passes (0 footprints
  moved — route-only), so no sandbox/docker step was needed.
- Board files and run outputs are **not committed** (licence + size
  hygiene, `benchmarks/external/README.md`); they live in the gitignored
  `.cache/kct-benchmarks/external/` tree and can be refetched from the
  pinned SHA.

## Reproducing

All commands run from the repo root at kicad-tools `42d80f0b6` (plus this
issue's manifest entry; no router code changed), after
`uv run kct build-native` (C++ backend available, version 1.0.0):

```sh
# 1. fetch + normalize (PCBJam/blind-pcb-runs @ b5a047f3d8e7963bca46da485b05c6a7041d9cfb)
uv run python benchmarks/external/fetch_boards.py --board pcbjam_m2_pcie_adapter
uv run python benchmarks/external/normalize.py --board pcbjam_m2_pcie_adapter

# 2. route (route-only, zero-touch, seeded, 1800 s cap) — exit 4 = insufficient
uv run kct route \
  .cache/kct-benchmarks/external/normalized/pcbjam_m2_pcie_adapter.kicad_pcb \
  -o .cache/kct-benchmarks/external/results/routed/pcbjam_m2_pcie_adapter.zero-touch.kicad_pcb \
  --seed 9595 --timeout 1800 -v

# 3a. kct-native measurement (kct check + kicad-cli DRC cross-gate, JSON+md report):
#     collect_report on the routed board, wall_clock 1751 s, route exit 4
#     (this note's numbers come from
#      .cache/kct-benchmarks/external/results/pcbjam_m2_pcie_adapter.zero-touch.json)
# 3b. PCBJam scoring, unchanged: their scoring/score.sh steps against a
#     run-dir layout (work/ = routed board, input/ = stripped board,
#     seconds.txt = 1751), KICAD_CLI=kicad-cli (10.0.6), renders skipped
#     (no rsvg-convert on this host); drc.json + stats.cjs + report.cjs.
```

`kct bench external --board pcbjam_m2_pcie_adapter --seed 9595` performs
steps 1–3a in one command but has no route timeout for this suite; the
manual sequence above is the same pipeline with the cap declared.

## Follow-ups (each its own issue)

- `--tuned` diff-pair config for this board (declared netclass map), the
  natural second attempt given the router's diagnosis.
- Place-and-route mode (their `m2-place.md` brief, keep-list and
  placement check), where the placement-delta feedback becomes legal.
- The other boards (CM4 NVMe NAS — MIT; macropad — CERN-OHL-P).
- kct-native corners/ground-via/skew columns: #9594.
- Posting results back to PCBJam: outward-facing, needs an operator call.

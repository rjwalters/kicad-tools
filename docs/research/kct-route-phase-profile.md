# Where `kct route` time goes on boards 02 and 03

Issue #5787 (Epic #5784, Phase 3, step 1). Companion to
[`kicad-routing-tools-comparison.md`](kicad-routing-tools-comparison.md), which
reported `kct route` at ~50 s (board 02) and ~105 s (board 03) against KRT's
1.2 s / 7.8 s without saying where the time goes.

Tool: `scripts/research/route_phase_profile.py` (runs `kct route IN -o OUT`
in-process, the documented default recipe, with exclusive/inclusive timers on
~30 phase entry points; the exclusive column sums to wall time). Raw JSON
(per-phase, stage markers, per-iteration, full timestamped log) is produced
with `--json`.

## Measurement conditions (read before trusting absolute numbers)

- Commit `8fa12b76`, C++ backend built and active, macOS, 28 logical CPUs.
- **The host was heavily loaded** by other concurrent sweep builders
  (load average 24-30 on 28 CPUs for both runs). Wall times are therefore
  roughly 2x (board 02: 111 s vs ~50 s) and ~3x (board 03: 335 s vs ~105 s)
  the figures in the comparison doc. Read the **percentages** as the finding,
  not the seconds. Kicad-cli subprocesses and the single-threaded Python
  phases are the most sensitive to contention. A quiet-host re-run is
  recommended before using absolute numbers; this was not possible here.
- One run per board; no variance estimate.
- Cold start, in-process; the profile harness's own overhead is a handful of
  `perf_counter` calls per phase entry (A* is called ~100 times).

## Board 02 (charlieplex 3x3): wall 111.0 s, 10/10 nets, 37 vias, 2 layers

| Group | Seconds | % of wall |
|---|---|---|
| A* search (C++, 93 calls) + clearance re-validation of results | 40.5 + 2.5 = 43.0 | 38.7 |
| Grid bookkeeping (mark 6.0, unmark 3.1, resync 4.4) | 13.5 | 12.2 |
| Negotiation scans / bookkeeping / rip-up (overuse, via-seg, seg-seg, loop) | ~5.9 | 5.3 |
| Connectivity / route validation (`validate_routes` 6.2, connectivity 2.7) | 8.9 | 8.0 |
| Pour: `kicad-cli` zone fill | 15.5 | 14.0 |
| Trace optimizer (grid-synced) | 8.6 | 7.8 |
| Post-route DRC (kicad-cli geometric 8.0 + wrapper 2.0) | 10.0 | 9.0 |
| Pad-clearance demotion | 2.0 | 1.8 |
| Load PCB, sidecars, writes, probes, CLI glue, global plan | ~4.0 | 3.6 |

Stage markers: layer-escalation (routing proper) 70.8 s; optimization 12.1 s;
native-zone-fill 15.5 s; post-route-drc 11.3 s; serialization 0.5 s.
Negotiation: four iterations of 14.9 / 15.1 / 14.5 / 12.2 s, of which A* is
11.7 / 11.5 / 10.9 / 8.8 s (~77% each). Every iteration reroutes 6 of 10 nets;
overflow went 21, 16, 27, 20 -- it never converged monotonically.

## Board 03 (USB joystick): wall 335.2 s, 24/24 nets, 46 vias, 4 layers

| Group | Seconds | % of wall |
|---|---|---|
| A* search (C++ 124 calls, plus one pure-Python fallback 0.7 s) + re-validation | 81.0 + 0.7 + 7.8 = 89.5 | 26.7 |
| Trace optimizer (grid-synced) | 82.6 | 24.6 |
| Pour: `kicad-cli` zone fill | 64.9 | 19.3 |
| Access-witness sidecar (journal replay) | 38.9 | 11.6 |
| Grid bookkeeping (mark 12.0, unmark 6.0, resync 2.5) | 20.6 | 6.1 |
| Post-route DRC (kicad-cli geometric 9.6 + wrapper 6.3) | 15.9 | 4.7 |
| Global plan (tile graph) | 4.3 | 1.3 |
| Negotiation scans / loop bookkeeping / rip-up | ~7.2 | 2.1 |
| Connectivity / route validation | 3.8 | 1.1 |
| Load, writes, probes, CLI glue | ~6.5 | 1.9 |

Stage markers: layer-escalation (routing proper, incl. escape + two-phase)
119.8 s; optimization 91.3 s; native-zone-fill 64.9 s; post-route-drc 56.1 s
(includes the 38.9 s witness sidecar). Detailed routing finished at ~120 s of
335 s; the remaining ~215 s is post-routing work. Negotiated iterations
(attempt 0): 47.7 / 31.9 / 31.5 s, A* 39.0 / 26.1 / 24.4 s (~80%).

## Findings

1. **Routing search is not the dominant cost.** Time inside the negotiated
   routing loop is 63% (02) and 36% (03) of wall time. The remainder is
   post-routing work: pour fill, trace optimizer, DRC, witness sidecar.
2. **Board 03: 64% of wall time (215 s of 335 s) occurs after routing has
   finished.** Three phases account for it: the trace optimizer (82.6 s,
   pure Python, 1 call), `kicad-cli` zone fill (64.9 s) and the
   access-witness journal replay (38.9 s). None of these is touched by
   ordering or rip-up changes (steps 2 and 3 of #5787).
3. **Inside routing, A* is ~75-80% of each negotiated iteration** and grid
   mark/unmark/resync is the next largest slice (12% on 02, 6% on 03).
   Violation scans and rip-up bookkeeping are small (2-5%).
4. **Whole-set negotiation re-routes most nets every iteration** (6/10 on 02
   per iteration, 4 iterations, ending with overflow no better than iteration
   1). That is where an ordering or N+1 rip-up change could save time; the
   ceiling is bounded by the routing-loop share above (~70 s on 02, ~120 s
   on 03 under this load), not by total wall time.
5. KRT's 1.2 s / 7.8 s presumably does none of the above post-processing
   (the comparison doc's numbers are not like for like on this point); this
   was not verified here.

## Not measured

- Quiet-host timings (see conditions). Absolute seconds are inflated.
- Time inside `kicad-cli` is attributed as a single wall-clock block; its
  internal split was not examined.
- Which individual A* calls dominate within an iteration (per-net A* cost)
  was not broken out; `--cprofile` can drill down but was not run.
- No ordering/rip-up experiments: those are steps 2 and 3.

## Reproduce

```bash
uv run kct build-native --check
uv run python scripts/research/route_phase_profile.py \
    boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb \
    --work-dir /tmp/route-profile/02 --json /tmp/route-profile/02.json
```

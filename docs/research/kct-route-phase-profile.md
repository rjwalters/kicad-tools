# Where `kct route` time goes on boards 02 and 03

Issue #5787 (Epic #5784, Phase 3, step 1). Companion to
[`kicad-routing-tools-comparison.md`](kicad-routing-tools-comparison.md), which
reported `kct route` at ~50 s (board 02) and ~105 s (board 03) against KRT's
1.2 s / 7.8 s without saying where the time goes.

Tool: `scripts/research/route_phase_profile.py` (runs `kct route IN -o OUT`
in-process, the documented default recipe, with exclusive/inclusive timers on
~40 phase entry points; the exclusive column sums to wall time). It also
tallies every `kicad-cli` child process. Raw JSON (per-phase, stage markers,
per-iteration, full timestamped log) is produced with `--json`.

**The answer, in one line: on both boards the largest single cost is
`kicad-cli` subprocesses — 52% of wall on board 02 and 41% on board 03 —
and A\* search is second.** Section
[Re-measurement](#re-measurement-2026-10-02-n3-per-board) is the current
reading; [First pass](#first-pass-2026-10-01-n1-per-board-superseded-absolute-numbers)
is kept because its percentages were what Phase 3's step-2 and step-3 child
issues were scoped against.

## Re-measurement (2026-10-02, N=3 per board)

The first pass (below) had two gaps that made its absolute seconds unusable:
**one run per board** (no variance estimate) and a **phase list that predated
two subsystems merged after it** — the pour-net KiCad-oracle completion loop
(#5785) and the commit-time pad-access invariant (#5891) — whose cost was
pooled into the escalation driver's residual. Both are fixed here:
`--repeat 3` (fresh subprocess per run) plus ten new phase entry points.

### Conditions

- Commit `09be722c5`, C++ router backend built and active (`kct build-native
  --check`: available), macOS, **18 logical CPUs**, load average 5–9 during
  runs. This is **not the same host** as the first pass (28 CPUs), so the two
  sections' absolute seconds are not comparable; see
  [What changed](#what-changed-against-the-first-pass).
- 3 runs per board. Routing *results* were identical across runs and identical
  to the first pass: board 02 10/10 routable nets / 37 vias / exit 0, board 03
  24 nets / 46 vias / exit 3 (incomplete). Only the timing moved.
- **Run 2 of each board is load-polluted** (board 02 load spiked 7.3 → 15.7,
  board 03 → 15.9 by run end) and is the `max` column everywhere. Read the
  **median**; the min/max spread is published so a reader can see which rows
  are load-sensitive (the `kicad-cli` and A\* rows are not; the Python-heavy
  trace optimizer is).

### `kicad-cli` invocations — the dominant cost on both boards

| Board | Invocations | `pcb drc` | `pcb fill-zones` | `version` | Total | % of median wall |
|---|---|---|---|---|---|---|
| 02 | **10** | 8 (79.9–83.6 s) | 1 (5.6–6.3 s) | 1 (5.8–6.2 s) | 91.6 s | **52.1** |
| 03 | **20** | 18 (183.2–257.5 s) | 1 (5.8–10.4 s) | 1 (5.9–6.1 s) | 201.7 s | **41.2** |

Counts were **identical in all three runs of each board** — this is structural,
not noise.

A large part of that is not work at all but **process startup**: on this host
*any* `kicad-cli` invocation costs ~6 s before it does anything, measured
directly against trivial subcommands —

```
$ /usr/bin/time -p .../kicad-cli version    # 6.23, 5.99, 6.00 s real (0.4 s user)
$ /usr/bin/time -p .../kicad-cli --help     # 5.98 s real
$ /usr/bin/time -p .../kicad-cli pcb        # 5.98 s real
```

At ~6 s × 10 and × 20 that is **~60 s of board 02's 175.8 s (34%) and ~120 s of
board 03's 489.0 s (25%) spent launching KiCad, not computing**. The
`version` probe is the clearest case: 1 invocation, ~6 s, 100% startup, zero
work. `pcb fill-zones` at 5.6 s is barely above the floor, so the pour fill
itself is nearly free — the cost is the launch.

This is host-specific in magnitude (a host with a faster `kicad-cli` cold start
pays less), but the **invocation count is a property of our code** and is the
lever. Filed as #5910 (cut the count) and #5911 (the oracle completion loop,
the largest single consumer of the `pcb drc` calls).

The 8 / 18 `pcb drc` calls are only partly attributable from this data: the
wrapped `run_geometric_drc` phase accounts for 3 calls (02) and 5 (03), and
the oracle completion loop is the only other `pcb drc` caller on these runs,
so it accounts for the remaining 5 and 13. The harness tallies subprocesses
globally and does not attribute each child to its enclosing phase, so treat
that split as inferred, not measured.

### Board 02 (charlieplex 3x3): wall median 175.8 s (min 170.3, max 248.9; spread 1.46x)

Grouped from the per-phase medians (full table in the raw JSON):

| Group | Median s | % of median wall |
|---|---|---|
| `kicad-cli` zone fill after route (incl. its own in-Python work) | 33.8 | 19.2 |
| `kicad-cli` geometric DRC (3 calls) | 32.7 | 18.6 |
| Pour-net oracle completion loop (#5785, `kicad-cli pcb drc` rounds) | 25.8 | 14.6 |
| `kicad-cli --version` probe | 6.2 | 3.5 |
| A\* search (C++, 93 calls) + clearance re-validation | 37.7 + 2.0 = 39.7 | 22.6 |
| Grid bookkeeping (mark 5.1, resync 3.2, unmark 2.4) | 10.6 | 6.0 |
| Connectivity / route validation (`validate_routes` 3.0, connectivity 2.2) | 5.2 | 2.9 |
| Trace optimizer (grid-synced) | 5.0 | 2.8 |
| Negotiation scans / bookkeeping / rip-up | 4.9 | 2.8 |
| Pad-clearance demotion | 1.5 | 0.8 |
| Commit-time pad-access invariant (#5891), 93 veto checks | 0.46 | 0.3 |
| Load, writes, sidecars, CLI glue, global plan, grid build | ~2.0 | 1.1 |

Negotiated loop: four iterations, overflow 21 → 16 → 27 → 20 (never
monotone), reroutes 6 of 10 nets each time — unchanged from the first pass.
The oracle completion loop starts at t≈102 s of 176 s, i.e. it owns the tail.

### Board 03 (USB joystick): wall median 489.0 s (min 451.7, max 741.2; spread 1.64x)

| Group | Median s | % of median wall |
|---|---|---|
| **Pour-net oracle completion loop (#5785, `kicad-cli pcb drc` rounds)** | **169.0** | **34.6** |
| `kicad-cli` zone fill after route (incl. its own in-Python work) | 70.8 | 14.5 |
| `kicad-cli` geometric DRC (5 calls) | 52.1 | 10.6 |
| `kicad-cli --version` probe | 5.9 | 1.2 |
| A\* search (C++, 124 calls) + re-validation + 1 Python fallback | 69.9 + 5.8 + 0.6 = 76.3 | 15.6 |
| Trace optimizer (grid-synced) | 43.3 | 8.9 |
| Access-witness sidecar (journal replay) | 27.4 | 5.6 |
| Grid bookkeeping (mark 9.6, unmark 3.6, resync 2.0) | 15.1 | 3.1 |
| Post-route DRC wrapper | 6.3 | 1.3 |
| Global plan, connectivity, two-phase bookkeeping, load, writes | ~12.0 | 2.5 |
| Negotiation scans / rip-up | 3.1 | 0.6 |
| Commit-time pad-access invariant (#5891), 10 veto checks | 0.52 | 0.1 |

Detailed routing finishes at t≈111 s of 489 s; the oracle loop starts at
t≈232 s. Negotiated iterations (attempt 0) are ~80% A\*, as before.

### Findings

1. **`kicad-cli` subprocesses, not search, are the top cost on both boards**
   (52% / 41% of wall). ~6 s of each invocation is fixed startup, so
   **invocation count is a first-class cost driver** — and it is 10 and 20.
   Neither ordering (step 2) nor rip-up (step 3) touches any of it.
2. **The pour-net oracle completion loop (#5785) is the single largest phase
   on board 03** — 169 s, 34.6% of wall, ~13 `kicad-cli pcb drc` rounds — and
   third largest on board 02 (25.8 s). It did not exist when the first pass
   was measured. Follow-up: #5911.
3. **A\* is the most stable thing measured.** Per board its median barely
   moves across hosts, loads and commits (board 02: 40.5 s first pass → 37.7 s
   here; board 03: 81.0 s → 69.9 s), and its min/max within a board is tight
   (board 03: 59.8–76.8 s) even on the load-polluted run. A\* is **22.6%**
   (02) and **15.6%** (03) of wall — the *ceiling* on any step-2/step-3
   improvement is a fraction of that, not of total wall.
4. **The whole-set negotiated loop's own overhead is small**: violation scans,
   rip-up and loop bookkeeping total 4.9 s (02) and 3.1 s (03), ~1–3% of
   wall. What step 3 could save is redundant *A\* work*, not negotiation
   bookkeeping.
5. **The commit-time pad-access invariant (#5891) is not a cost centre**:
   0.46 s over 93 veto checks (02) and 0.52 s over 10 (03). Its own perf fix
   (#5907) holds. The 10-vs-93 gap is expected — the two-phase path board 03
   takes does not opt in to the gate.
6. **Four suspected cost centres measured at 0.00 s** and can be dropped from
   future hypotheses: routing grid construction, plan-gate preflight,
   attempt preserved-copper capture, connectivity-invariant enforcement.
7. **Neither pass reproduces the comparison doc's 50 s / 105 s.** Those
   figures should not be quoted as current without a re-run; this doc's
   numbers supersede them for boards 02/03.

### What changed against the first pass

First pass 111 s / 335 s, this pass 175.8 s / 489.0 s. **Two variables moved at
once** (host: 28 → 18 CPUs; code: `8fa12b76` → `09be722c5`), so the gap cannot
be attributed to either by this data alone. One component *can* be separated by
construction: the oracle completion loop's code did not exist at `8fa12b76`, so
its 25.8 s (02) and 169.0 s (03) are new cost, not host. Netting it out gives
150 s / 320 s — board 03 then lands within 5% of the first pass's 335 s, board
02 remains 35% above its 111 s. That is arithmetic, not a controlled A/B; a
same-host before/after was not run.

The first pass's "Load PCB, sidecars, writes, probes, CLI glue" residual
(~4 s / ~6.5 s) is also now explained: with the oracle loop and auto-pour
wrapped, the escalation driver's own exclusive time collapses to **0.34 s (02)
and 0.40 s (03)** — in the un-refined phase list it had absorbed 24.2 s and
159.6 s of oracle-loop time.

## First pass (2026-10-01, N=1 per board; superseded absolute numbers)

Kept for provenance: this is the profile #5893 (step 2) and #5894 (step 3) were
scoped against. Its **percentages** are what those issues quote; its absolute
seconds are superseded by the section above, and its phase list predates the
oracle loop and the pad-access invariant.

- Commit `8fa12b76`, C++ backend built and active, macOS, 28 logical CPUs.
- **The host was heavily loaded** by other concurrent sweep builders (load
  average 24–30 on 28 CPUs for both runs). One run per board; no variance
  estimate.

### Board 02: wall 111.0 s, 10/10 nets, 37 vias, 2 layers

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

### Board 03: wall 335.2 s, 24/24 nets, 46 vias, 4 layers

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

### First-pass findings (percentages still cited by #5894)

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

- **A same-host before/after across `8fa12b76`…`09be722c5`.** Without it, the
  host-vs-code split in [What changed](#what-changed-against-the-first-pass) is
  arithmetic rather than a controlled result.
- **A genuinely quiet host.** Load was 5–9 on 18 CPUs; run 2 of each board
  spiked past 15. The `kicad-cli` and A\* rows proved load-insensitive, but the
  Python-heavy trace optimizer did not (board 03: 41.5 s min, 110.7 s on the
  polluted run).
- **Inside `kicad-cli`.** Each invocation is one wall-clock block; the ~6 s
  startup floor is measured from trivial subcommands, not instrumented inside
  KiCad.
- **Per-net A\* cost** within an iteration. `--cprofile` can drill down; it was
  not run (it inflates Python relative to the C++ loop).
- **Boards 00/01/06.** This is a two-board attribution, as the issue asked.
- No ordering/rip-up experiments: those are steps 2 (#5893, landed in PR #5909,
  negative result) and 3 (#5894).

## Reproduce

```bash
uv run kct build-native --check     # must report "available"
uv run python scripts/research/route_phase_profile.py \
    boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb \
    --work-dir /tmp/route-profile/02 --json /tmp/route-profile/02.json \
    --repeat 3
```

Swap in `boards/03-usb-joystick/output/usb_joystick.kicad_pcb` for board 03
(~8 min per run). Drop `--repeat` for a single run with the full per-iteration
split and stage timeline; add `--cprofile OUT.prof` to drill into Python.

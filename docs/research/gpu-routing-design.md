# GPU-accelerated / batched routing: profile-first design note (Issue #7053)

Status: research complete. **Recommendation: don't build a GPU routing
backend now.** The cheaper CPU work the data points to is listed in
[section 6](#6-go--no-go-and-recommendation).

Successor to the orphaned `BatchPathfinder` "GPU" kernel removed by #7051.
That kernel was never wired into the router and its GPU paths were
placeholders. This note does not revive it. It asks whether a *real* GPU or
batched routing backend would pay for itself on our boards, using numbers
measured in this run.

## Go / no-go criterion (stated before the recommendation)

> **Build a GPU/batched first slice only if its Amdahl ceiling
> `S_max = 1 / (1 - p)` is at least 2.0x on both board 03 and board 05**,
> where `p` is the share of wall time that slice could remove, measured
> below.

`S_max` is the speedup you would get if the targeted work took **zero**
time, i.e. an infinitely fast GPU with free transfers. Any real kernel lands
strictly below it. Boards 03 and 05 are where wall time is a problem. Board 02
is already about 1 minute. Why 2.0x: a GPU backend brings a CUDA/Metal
toolchain, a second determinism contract (section 5) and a second search
implementation that must stay in parity with the C++ one. A ceiling below 2x
on the slow boards cannot repay that, because the realised gain is always
smaller than the ceiling. **Result: no candidate meets the bar** (best single
slice: 1.33x on 03 and 1.59x on 05; all three candidates together: 1.67x and
1.90x). The decision only changes if the bar is dropped below 1.59x.

## 1. Conditions and Reproduce

- Commit `3830a3f63e7bf7ffba5baca8f9533fce5c52db97` for every run,
  measured 2026-10-18 (UTC).
- Host: macOS 27.0.1 (build 26A434), **8 logical CPUs**, Apple silicon
  (aarch64 CPython 3.12.11). Local KiCad is 10.0.6.
- C++ backend built in this worktree and confirmed:
  `uv run kct build-native --check` printed `C++ backend: available (version 1.0.0)`,
  `build version: 60 (required: 60)`. The harness recorded
  `cpp_backend: True` for every run.
- **N=1 per configuration** (N=2 for the default recipe on 02 and 03, see
  below). There is no variance estimate beyond that.
- **The host was not quiet.** Other sweep agents were running. The 1-minute
  load average at the start and end of each run is recorded in the table.
- **Concurrency disclosure.** Batch A ran three profiles *at the same time*
  (02, 03, 02 under cProfile) to fit the time box. Batch B ran 05 alongside a
  failed region-parallel launch (wrong argument syntax, exited after 0.08 s).
  To get clean figures, 03 and then 02 were re-run **alone** (batch D). **The
  per-board tables below use the batch D runs.** The batch A runs are shown
  only to show that the *shares* stay put under contention.

| Run | Batch | Ran concurrently with | Wall s | Exit | Load start / end | Result |
|---|---|---|---|---|---|---|
| 02 default | A | 03, 02-cProfile | 81.0 | 0 | 3.80 / 5.73 | 10/10 nets, 39 vias |
| 03 default | A | 02, 02-cProfile | 103.1 | 0 | 3.80 / 6.61 | 24/24 nets, 42 vias |
| 02 `--cprofile` | A | 02, 03 | 105.8 | 0 | 3.80 / 6.80 | 10/10 nets, 39 vias |
| 05 default | B | (failed launch only) | 627.2 | **124 (capped)** | 5.32 / 3.44 | best 30/33 (91%) on 4L when killed |
| 03 `--region-parallel` | C | nothing | 88.5 | 0 | 2.90 / 3.80 | 24/24 nets, 42 vias |
| **03 default** | **D** | nothing | **81.1** | 0 | 2.97 / 3.35 | 24/24 nets, 42 vias |
| **02 default** | **D** | nothing | **62.9** | 0 | 3.24 / 2.45 | 10/10 nets, 39 vias |

Profiling wall time, by batch timestamps: A 03:17:54-03:19:41Z (1 m 47 s),
B 03:20:01-03:30:29Z (10 m 28 s), C 03:30:50-03:32:19Z (1 m 29 s),
D 03:32:53-03:35:17Z (2 m 24 s). **Total 16 m 08 s, inside the 30-minute
box.** Board 05 hit its 600 s `timeout` cap (section 2.3). Nothing was
dropped.

Board 02 is now 63 s and board 03 is 81 s, against 175.8 s and 489.0 s in
the #5787 re-measurement (cited prior: `docs/research/kct-route-phase-profile.md`,
18-CPU host, commit `09be722c5`). One reason is visible in this run's data:
each `kicad-cli` invocation now costs about 0.17 s (`version`: 0.167 s on 02,
0.169 s on 03), where the prior doc measured about 6 s of startup per call.
Code and host also changed, so this is not a controlled A/B. Board 03 also
now exits 0 with 24/24 nets, where the prior run had exit 3.

### Reproduce

```bash
uv run kct build-native            # once per worktree
uv run kct build-native --check    # must print "C++ backend: available"
S=$TMPDIR/gpu-design

# Phase attribution, one board at a time (batch D used this order, alone):
timeout 600 uv run python scripts/research/route_phase_profile.py \
    boards/03-usb-joystick/output/usb_joystick.kicad_pcb \
    --work-dir $S/03b --json $S/03b.json
timeout 600 uv run python scripts/research/route_phase_profile.py \
    boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb \
    --work-dir $S/02b --json $S/02b.json
timeout 600 uv run python scripts/research/route_phase_profile.py \
    boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb \
    --work-dir $S/05 --json $S/05.json

# C++ vs Python split inside A* (cProfile fallback, see section 2.4):
timeout 600 uv run python scripts/research/route_phase_profile.py \
    boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb \
    --work-dir $S/02cp --json $S/02cp.json --cprofile $S/02.prof

# Parallelism baseline (note the '=': '--extra --region-parallel' is
# rejected by argparse):
timeout 600 uv run python scripts/research/route_phase_profile.py \
    boards/03-usb-joystick/output/usb_joystick.kicad_pcb \
    --work-dir $S/03rp --json $S/03rp.json --extra=--region-parallel
```

The groups in section 2 add up the harness's per-phase **exclusive**
seconds, which sum to wall:

| Group | Harness phases |
|---|---|
| C++ A* | `A* search (CppPathfinder.route)` (exclusive) |
| Python A* fallback | `pure-Python A* fallback (Router.route)` |
| Cost-map updates | `grid mark_route`, `grid unmark_route`, `grid resync_route_occupancy` |
| Negotiation / rip-up | segment-segment and via-segment violation scans, `find nets through overused cells`, `rip_up_nets`, both negotiated-loop bookkeeping phases |
| Clearance / DRC checks (Python) | `A* result clearance re-validation`, `net connectivity validation`, `io.validate_routes`, `pad-access invariant: per-commit veto check`, `post-route DRC validation`, `pad-clearance demotion` |
| `kicad-cli` phases | geometric DRC, pour-net oracle completion loop, zone fill after route, pre-route auto-pour, `--version` probe |
| Trace optimizer | `trace optimizer (grid-synced)` |
| Python orchestration / other | everything else (escalation driver, CLI glue, load/write, sidecars, global plan, unattributed) |

The harness did not need any new timers. No file under `scripts/` changed.

## 2. Profile tables

### 2.1 Board 02 (charlieplex 3x3): wall 62.9 s, batch D

| Group | Seconds | % of wall |
|---|---|---|
| **C++ A\* search** (203 calls) | **30.97** | **49.2** |
| `kicad-cli` phases (in-Python work around the calls included) | 12.95 | 20.6 |
| Cost-map updates (mark 3.55, unmark, resync) | 6.67 | 10.6 |
| Clearance / DRC checks (Python), incl. A\* re-validation 2.26 | 7.37 | 11.7 |
| Trace optimizer | 2.19 | 3.5 |
| Negotiation scans / rip-up / loop bookkeeping | 1.76 | 2.8 |
| Python orchestration / other (incl. witness 0.22) | 0.99 | 1.6 |
| Python A\* fallback | 0.00 | 0.0 |

`kicad-cli` subprocesses alone: 10 invocations, 10.90 s (17.3% of wall), made
up of 8 `pcb drc` (10.57 s), 1 `fill-zones` (0.17 s) and 1 `version` (0.17 s).

Negotiated iterations (seconds / of which A\*): 3.31 / 2.12, 4.45 / 2.88,
3.80 / 2.33, then **30.64 / 25.89**. Iteration 3 is the perturbation pass
(`Perturbation activated (magnitude=0.10, stagnation=1)`). It costs **48.7%
of wall**, and 25.89 s of the 30.97 s of A\* on this board is spent inside it.
Its result was then thrown away: `Emitting iteration 1 result (4 pass(es)
scored; final pass regressed, rolled back)`. The contended batch A run showed
the same thing: 38.58 s / 33.00 s, rolled back.

### 2.2 Board 03 (USB joystick): wall 81.1 s, batch D

| Group | Seconds | % of wall |
|---|---|---|
| `kicad-cli` phases (in-Python work around the calls included) | 25.44 | 31.4 |
| **C++ A\* search** (125 calls) | **19.95** | **24.6** |
| Trace optimizer (pure Python) | 16.43 | 20.3 |
| Cost-map updates (mark 2.85, unmark, resync) | 5.13 | 6.3 |
| Clearance / DRC checks (Python), incl. A\* re-validation 2.37 | 5.85 | 7.2 |
| Negotiation scans / rip-up / loop bookkeeping | 1.49 | 1.8 |
| Python A\* fallback (2 calls) | 1.23 | 1.5 |
| Python orchestration / other (escalation driver 3.09, global plan, load, witness) | 5.54 | 6.8 |

`kicad-cli` subprocesses alone: 12 invocations, 15.03 s (18.5% of wall): 10
`pcb drc` (14.70 s), 1 `fill-zones`, 1 `version`.

Negotiated iterations (two-phase detailed loop): 13.35 / 10.62, 8.10 / 6.47,
8.31 / 6.45 s. About 79% of each iteration is A\*, the same proportion the
#5787 doc reported ("~80% A\*", cited prior).

### 2.3 Board 05 (BLDC controller): **capped at 600 s, partial**

The run was killed by `timeout 600`. The harness recorded wall 627.2 s and
exit 124, because route's partial-save stage (26.84 s) ran after the signal.
The best result by then was **30/33 nets (91%) on 4 layers**. `kicad-cli` was
never reached (0 invocations), so post-route phases are **missing** from this
board. Shares below are shares of the truncated run, not of a completed route.

| Group | Seconds | % of wall |
|---|---|---|
| **Python A\* fallback** (66 calls) | **282.32** | **45.0** |
| **C++ A\* search** (572 calls) | **233.57** | **37.2** |
| Cost-map updates | 24.12 | 3.8 |
| Clearance / DRC checks (Python), incl. A\* re-validation 10.12 | 23.93 | 3.8 |
| Negotiation scans / rip-up / loop bookkeeping | 15.57 | 2.5 |
| Access-witness sidecar (journal replay, partial save) | 26.81 | 4.3 |
| Python orchestration / other | 20.92 | 3.3 |

stderr carried 24 fallback warnings: 21 `no path (C++ A* open set exhausted)`,
2 `all via candidates blocked by stored-via geometry`, and 1 `post-route
clearance validation failed; exhausted 5 resume attempts`. In other words,
**45% of the board-05 wall is the pure-Python A\* re-searching nets that the
C++ search has already drained**. This run does not record how many of those
re-searches actually found a route (proposal P3).

The layer-escalation attempts repeat similar work. Iteration 0 of attempts
0, 1 and 2 cost 121.29 / 113.56 / 116.76 s, of which A\* was 111.10 / 108.39 /
111.47 s, and the attempts reached 29, 30 and 30 routed nets.

### 2.4 C++ vs Python split inside the A\* share

`py-spy` could not be used on this host. `py-spy 0.4.2` (via `uvx`) printed
`Collecting stack traces from native extensions (--native) is not supported on
your platform` for `--native`. Without `--native` it printed `This program
requires root on OSX`, and no `sudo` was available non-interactively. **The
split below is the documented cProfile fallback.** cProfile inflates Python
time.

cProfile does not list the nanobind `router_cpp` calls as separate built-in
entries. No `router_cpp` function appears in the profile. Native time
therefore shows up as the *tottime* of `_run_fresh_search`
(`src/kicad_tools/router/cpp_backend.py`), whose body is a series of
`self._impl.*` native setter calls followed by `self._impl.route_resumable(...)`.
Its only Python callee is `_sync_stored_routes`.

Board 02, batch A, cProfile run (`CppPathfinder.route` cumulative 46.47 s over
203 calls):

| Component | Seconds | % of A\* phase | Side |
|---|---|---|---|
| `_run_fresh_search` tottime (native search + setters + trivial bytecode) | 39.36 | 84.7 | **C++ (upper bound)** |
| A\* result clearance re-validation (`_validate_route_clearance`) | 6.03 | 13.0 | Python |
| `clear_avoidance_costs` | 0.56 | 1.2 | Python |
| `_convert_result_to_route` | 0.30 | 0.6 | Python |
| `_sync_stored_routes` | 0.19 | 0.4 | Python |
| remainder (`route` / `_route_impl` own time) | 0.03 | 0.1 | Python |

The same split from the uninstrumented harness timers, which do not inflate
Python, is shown next. Here the C++ figure is the exclusive time of
`CppPathfinder.route`. Under cProfile, the Python part of that exclusive time
is at most 1.08 s (the last four rows above).

| Board (run) | A\* phase inclusive s | C++ (exclusive) s | Python (re-validation + fallback) s | C++ % |
|---|---|---|---|---|
| 02 (D) | 33.22 | 30.97 | 2.26 + 0 | 93.2 |
| 03 (D) | 23.54 | 19.95 | 2.37 + 1.23 | 84.7 |
| 05 (B, capped) | 526.01 | 233.57 | 10.12 + 282.32 | 44.4 |

The C++ search is barely affected by cProfile: 39.36 s under cProfile against
39.23 s exclusive in the contended batch A run without it. Python
re-validation went from 2.60 s to 6.03 s, so cProfile inflates it by about
2.3x. **On 02 and 03 the A\* share is at least 85% C++ already. On 05 most of
it is the pure-Python fallback.**

## 3. Parallelism audit

**What exists.**

- **C++ A\***: one search runs on one thread. A grep for `std::thread`,
  `#pragma omp` and `std::async` in `src/kicad_tools/router/cpp/src/` finds
  nothing. `bindings.cpp` (`NB_MODULE(router_cpp, ...)`) never mentions `gil`,
  so no binding releases the GIL. A bound call therefore holds the GIL for
  its full duration, and Python threads cannot overlap two C++ searches.
- **`--region-parallel`** (`RegionBasedNegotiatedRouter`,
  `src/kicad_tools/router/parallel.py`) fans nets out to a
  `ThreadPoolExecutor`. Each worker, however, runs its whole `route_fn` inside
  `with self.router.grid.locked():` (`_route_net_with_lock`), so per-net
  routing is serialised on the grid lock as well as on the GIL. It is also
  auto-disabled below 16 nets per region (`region_parallel_min_nets_per_region`,
  `core.py`). With the default 2x2 grid that means fewer than 64 nets, which
  covers all three boards here (10, 24 and 33 nets).
- **`region_parallel` is only accepted by `route_all_negotiated`**
  (`core.py`). Board 03 takes the `route_with_escape` / two-phase path, so the
  flag never reaches it.

**Measured comparison (board 03, same commit, both runs alone):**

| | Default (D) | `--region-parallel` (C) |
|---|---|---|
| Wall | 81.1 s | 88.5 s |
| C++ A\* calls / exclusive s | 125 / 19.95 | 125 / 21.22 |
| Detailed iterations s | 13.35 / 8.10 / 8.31 | 14.25 / 9.18 / 8.36 |
| Nets routed / vias | 24/24 / 42 | 24/24 / 42 |
| Log lines | 231 | 231 |
| Lines mentioning "parallel" or "partition" | 0 | 0 |

The routing result is the same, the A\* call count is the same, and the log
has the same length. It contains neither the region-parallel auto-disable
message nor any partition output, so **the flag was a silent no-op on this
board**. The 7.4 s wall difference is run-to-run noise: the same default
configuration measured 103.1 s under contention in batch A. It is not an
effect of the flag.

**What this leaves on the table.** Process CPU self-time is 52.0 s for a
62.9 s wall (02) and 66.0 s for 81.1 s (03), so about one core is busy on an
8-core host. **Neither existing mechanism exercises the multi-core CPU we
already have.** Before reaching for a GPU, the parallel headroom is: release
the GIL around `route_resumable` and route independent nets against a
snapshot of the cost map (section 4, candidate 2).

The batch width is small. The negotiated loop reroutes 6 nets per iteration on
board 02 (`Rerouted 6/6 nets`) and 15 on board 05 (`Ripping up 15 nets with
conflicts`). Neither width comes close to filling a GPU, which needs
thousands of independent work items.

## 4. Candidate GPU/batch targets with Amdahl ceilings

Formula: `S_max = 1 / (1 - p)`, with `p` = measured group seconds / measured
wall (sections 2.1-2.3). There is no assumed kernel speedup. These are
ceilings for an infinitely fast, zero-transfer implementation.

| Candidate | 02: p -> S_max | 03: p -> S_max | 05 (capped): p -> S_max |
|---|---|---|---|
| 1. Batched cost-map updates in the negotiated loop (mark/unmark/resync + scans) | 8.43/62.89 = 0.134 -> **1.15x** | 6.62/81.05 = 0.082 -> **1.09x** | 39.69/627.25 = 0.063 -> **1.07x** |
| 2. Parallel independent-net A\* / GPU wavefront (C++ A\* share) | 30.97/62.89 = 0.492 -> **1.97x** | 19.95/81.05 = 0.246 -> **1.33x** | 233.57/627.25 = 0.372 -> **1.59x** |
| 3. Batched clearance queries (re-validation + Python clearance/DRC checks) | 7.37/62.89 = 0.117 -> **1.13x** | 5.85/81.05 = 0.072 -> **1.08x** | 23.93/627.25 = 0.038 -> **1.04x** |
| 1 + 2 + 3 together | 46.77/62.89 = 0.744 -> 3.90x | 32.42/81.05 = 0.400 -> 1.67x | 297.19/627.25 = 0.474 -> 1.90x |

For comparison, here is CPU-side work with no GPU involved:

| CPU target | Board | p -> S_max |
|---|---|---|
| Discarded perturbation pass (iteration 3, rolled back) | 02 | 30.64/62.89 = 0.487 -> 1.95x |
| Pure-Python A\* fallback after C++ drained its open set | 05 (capped) | 282.32/627.25 = 0.450 -> 1.82x |
| `kicad-cli` phases + trace optimizer | 03 | (25.44+16.43)/81.05 = 0.517 -> 2.07x |

Each CPU target matches or exceeds the GPU ceiling on its own board. None of
them needs a GPU, a new toolchain or a new determinism contract.

**Precedents.**

- **OrthoRoute** (`kct ecosystem show orthoroute`: MIT, code reuse permitted
  with attribution, verdict `complementary`, Python + CUDA/Metal). It is the
  closest GPU-router precedent: a GPU PathFinder whose author says it targets
  "very large, dense, highly regular multilayer backplanes and BGA escape
  patterns and does poorly on typical boards". The idea that transfers is
  PathFinder's structure: negotiated present/history costs, all nets
  rerouted against a frozen cost field each iteration, then a cost update.
  That Jacobi-style structure is what makes batching possible at all, and it
  shapes candidates 1 and 2. What does not transfer is the payoff. It depends
  on thousands of nets on a big regular grid, while our boards reroute 6-15
  nets per iteration (section 3). Its regular-lattice assumption also does
  not fit our octilinear, pad-carve-out-heavy C++ search.
- **fastroute** (`kct ecosystem show fastroute`: GPL-3.0, **ideas only, no
  code read or copied**, verdict `watch`). It is a Rust port of freerouting
  that claims "about 4-5x less wall time (2102 s vs 510 s ...)" on CPU (cited
  from the registry entry, not measured here). It is evidence that
  implementation-level CPU work can produce multi-x wall-time gains with no
  GPU. For us, the parts still in Python (the 45% fallback on 05, the 20%
  trace optimizer on 03) are where an implementation-language change has
  room. Our A\* is already C++.
- **KiCadRoutingTools** (`kct ecosystem show kicadroutingtools`: MIT,
  verdict `benchmarked`, Python + Rust). The comparison note
  (`docs/research/kicad-routing-tools-comparison.md`) found it "faster and
  completes more nets on the sparse and medium boards" (registry summary,
  cited). That is a CPU router, and it is the practical benchmark to beat.
- **Prior negative results that constrain the design**, cited and not
  re-derived. `kct-route-net-order-experiment.md` (crossing-aware ordering: no
  gain), `kct-route-ripup-experiment.md` (KRT-style N+1 rip-up: no gain) and
  `kct-route-nogood-cache-experiment.md` (sound nogood cache declined; it
  needs a cheaper validity signal). The last one matters for P3: a "skip the
  fallback when C++ drained" rule has the same soundness question that note
  worked through. The #5787 phase profile
  (`kct-route-phase-profile.md`) already said A\* is "the *ceiling* on any
  step-2/step-3 improvement". This run re-measures that ceiling on today's
  tree: 49.2% on 02 and 24.6% on 03, against the 22.6% and 15.6% cited in
  that doc.

**What a real routing kernel would need from `acceleration/backend.py`.**
`ArrayBackend` is an *array-op* facade over NumPy, CuPy and MLX (`zeros`,
`where`, `sum`, `scatter_add`, ...). Routing state lives in the C++
`router_cpp` grid, not in Python arrays. A kernel would need:

- device-resident grid planes with dirty-region sync from the C++ grid,
  instead of `to_numpy` round trips;
- a custom-kernel dispatch (CUDA / Metal compute) rather than composed
  array ops;
- a deterministic-reduction mode, because `scatter_add` is documented as
  "Atomically add";
- a CPU reference path to diff against.

Nothing in `acceleration/` provides those today. A real slice would hang off
the C++ backend, not off `ArrayBackend`.

## 5. Determinism design

The router must stay seeded and reproducible: the same board, seed and
commit must give the same copper. Any parallel or batched execution has to
keep the following:

- **Fixed net ordering.** Batches are formed from a deterministic order (net
  id, or the existing ordering policy) and **committed in that order**, never
  in completion order.
- **Snapshot semantics.** Every net in a batch searches against the *same*
  frozen cost field (PathFinder/Jacobi). The present/history update is
  applied once per batch, after all of its searches. Because of that, the
  answer does not depend on scheduling. It **does** differ from today's
  sequential Gauss-Seidel loop, where net *k* sees nets *1..k-1*'s new
  usage. Golden outputs would change, and convergence would need
  re-measuring, because the #5893 / #5894 results do not carry over.
- **Deterministic tie-breaks.** The open-set ordering must use the full key
  `(f, g, cell index, layer)` so that equal-cost frontiers expand the same
  way on every run and every thread count.
- **No atomics-dependent order.** Cost accumulation must not rely on atomic
  float adds, because the order of the adds then changes the result. Use
  integer or fixed-point costs, or a per-net partial-sum buffer reduced in
  fixed index order.

**Residual non-determinism a GPU backend would add:**

- float reductions whose association order depends on block/warp schedule;
- FMA contraction and transcendental-math differences between compilers,
  drivers and vendors (CUDA vs Metal), so even a "deterministic" kernel can
  differ bit-for-bit across machines;
- GPU timeouts or watchdogs interacting with the per-net wall-clock deadlines
  (`timeout_s` in `route_resumable`), which already make search results
  depend on wall time.

The mitigations are integer costs, a CPU reference mode that is
cross-checked in CI, and keeping the C++ search the default. These are the
same costs the go/no-go weighs.

## 6. Go / no-go and recommendation

Criterion (stated at the top): build only if `S_max >= 2.0x` on **both**
board 03 and board 05.

| Candidate | 03 S_max | 05 S_max | Meets 2.0x on both? |
|---|---|---|---|
| 1. Batched cost-map updates | 1.09x | 1.07x | no |
| 2. Parallel / GPU A\* | 1.33x | 1.59x | no |
| 3. Batched clearance queries | 1.08x | 1.04x | no |
| All three at once | 1.67x | 1.90x | no |

**Recommendation: don't build** a GPU or batched routing backend now. Even an
infinitely fast GPU doing all three jobs at once could not halve wall time on
the boards that are slow. Board 05's largest cost (45%) is the *Python*
fallback, which a GPU A\* kernel would not touch. On board 03, `kicad-cli` and
the trace optimizer together outweigh A\* (51.7% vs 24.6%).

**What the data points to instead (CPU work). These are proposals, not filed
issues:**

- **P1. Don't run, or budget, the perturbation pass that gets rolled back
  (board 02).** It is 48.7% of wall (`S_max` 1.95x), and its output is
  discarded in both runs. Measure how often the pass is ever kept across the
  fleet before changing it.
- **P2. Trace optimizer and `kicad-cli` round trips (board 03).** The
  optimizer is 20.3% of wall and pure Python, and the `kicad-cli` phases are
  31.4%. 10 `pcb drc` calls cost 14.70 s; the subprocesses themselves are
  18.5% of wall, the rest is in-Python work around them. This continues
  #5910 / #5911 from the #5787 doc.
- **P3. Pure-Python fallback after a drained C++ search (board 05).** It is
  45.0% of the capped wall (`S_max` 1.82x). First measure how many of the 66
  fallback calls produce a route. If drained C++ searches almost never
  succeed in Python, gate the fallback. The nogood-cache note shows how to do
  that soundly. If they do succeed, the gap belongs in the C++ search.
- **P4. Repeated escalation attempts (board 05).** Three attempts each spend
  about 110 s of A\* in iteration 0 to reach 29-30 nets.
- **P5. Make multi-core CPU parallelism real before any GPU.** Release the GIL
  around `route_resumable`, replace the whole-route grid lock with snapshot
  semantics (section 5), and wire `region_parallel` into the two-phase /
  escape path, where it is currently ignored. The ceiling is still candidate
  2's 1.33x / 1.59x, but this needs no new toolchain and keeps the C++ search.
  Make `--region-parallel` say when it is ignored (board 03 gave no log line).
- **P6. Revisit the GPU question** only when a board appears in the fleet
  where the C++ A\* share alone is at least 50% of wall (`S_max >= 2x`), or
  when the net count per iteration reaches the thousands where OrthoRoute's
  approach pays off.

## Not measured

- **Per-board variance.** N=1 per configuration (N=2 for the default 02/03,
  once contended and once alone). The shares were stable across the two
  (02: A\* 48.4% vs 49.2%; 03: 22.5% vs 24.6%). Absolute seconds were not
  (03: 103.1 vs 81.1 s).
- **A completed board-05 route.** It was capped at 600 s, so post-route phases
  are missing and its shares are of a truncated run.
- **A native-frame (py-spy `--native`) split.** It is unsupported on this
  macOS host; the cProfile fallback is used.
- **Whether `--region-parallel` helps on a board that takes
  `route_all_negotiated` with at least 64 nets.** None of 02, 03 or 05
  qualifies.
- **Any GPU kernel.** No GPU code was written or timed. All GPU figures are
  Amdahl ceilings.

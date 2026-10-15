# Issue #5617 — inside the native A\*: where phase 4's C++ time goes

Every earlier profile of the Diff-Pair re-route step
(`phase4-native-split.md`, `phase4-python-fallback-profile.md`) stopped at the
Python/C++ boundary: `CppPathfinder._run_fresh_search` was one opaque frame
holding 60–75 % of phase 4, and the conclusion each time was "further gains
need a `router/cpp` change". This note opens that frame with a native
(C++-symbolised) profile, names the hotspot, and records the paired
before/after measurement of the change it motivated.

## Provenance

| | |
|---|---|
| Source revision (baseline) | `33e98c97` (`origin/main`, 2026-10-15) |
| Change under test | this PR's branch `feature/issue-5617` (on top of `33e98c97`) |
| Host | `loom-worker-3`, AWS EC2 `m7i.4xlarge`, Intel Xeon Platinum 8488C × 16 vCPU, 61 GiB RAM — shared Loom dispatch worker, not a dedicated benchmark box |
| OS / kernel | Ubuntu 24.04.4 LTS, Linux 7.0.0-1014-aws x86_64 |
| Toolchain | CPython 3.12.3 (`uv` project venv), g++ 13.3.0, cmake 3.28.3 |
| Native router | `router_cpp`, backend 1.0.0, **build version 55** (`uv run kct build-native --check`) |
| `kicad-cli` | 10.0.6 |
| Profiler | py-spy 0.4.2 (`uvx py-spy`), `record --native --idle --subprocesses --rate 20 --format speedscope` |
| Environment | `PYTHONHASHSEED=42`, `--seed 42` |
| Host load (1-min avg at launch) | between 1.5 and 6.0 on 16 vCPUs for every run below (never oversubscribed) |

`kct build-native` ships a stripped Release `.so`, which py-spy cannot
symbolise. The native profiles below therefore use a second build of the
**same sources** configured `RelWithDebInfo` with
`CMAKE_CXX_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG -fno-omit-frame-pointer"`,
built outside the worktree and swapped in only for the profiling run. That
build routes the board identically (routed PCB equal to the Release build's
modulo object UUIDs), so it is the same search, just with symbols.

`perf` is unavailable on this host (`perf_event_paranoid = 4`), which is why
py-spy's `--native` unwinder is used rather than a hardware-counter profile.

## Method

As in `phase4-python-fallback-profile.md`: py-spy wraps the whole coverage
script, the `generate_design.py --step route` child is sampled as its own
profile, and the samples are sliced to phase 4 by the `[phase 4 @ …]` /
`[phase 5 @ …]` offsets `check_diffpair_coverage.py` prints. `--idle` keeps
the GIL-released native frames in the sample. Inline frames (from the debug
info) appear as their own entries, so "self" below means "not attributed to a
named callee", and inclusive times are the meaningful column.

## The native split, baseline (`33e98c97`)

Phase 4 under the native profiler: **557.8 s**. Callees of
`Pathfinder::run_astar_loop` (the resumable search `_run_fresh_search` drives),
inclusive:

| frame | s | % of phase 4 |
|---|---:|---:|
| `Pathfinder::run_astar_loop` (all of it) | 426.8 | 76.5 % |
| ↳ **`Pathfinder::is_via_blocked_diag`** | **247.7** | **44.4 %** |
| ↳↳ its own body (cell-kernel scan, stored-via loop) | 102.9 | 18.5 % |
| ↳↳ `via_route_geometry_clear` → `Grid3D::route_via_geometry_clear` | 51.0 | 9.2 % |
| ↳↳ `Grid3D::at` + `Grid3D::is_valid` (kernel scan) | 76.3 | 13.7 % |
| ↳ `Pathfinder::is_trace_blocked` | 77.0 | 13.8 % |
| ↳ `Grid3D::trace_stored_vias_clear` | 42.2 | 7.6 % |
| ↳ open-set heap (`sift_down`/`sift_up`/`push`/`pop`) | 29.0 | 5.2 % |
| ↳ `Grid3D::authored_segment_clear` | 7.1 | 1.3 % |
| ↳ `Pathfinder::get_pad_channel_cost` | 6.3 | 1.1 % |

Cross-cutting (inclusive, whole phase): `Grid3D::route_geometry_candidates`
65.6 s (11.8 %), of which `std::sort` of the gathered index lists 25.4 s;
`std::variant<KSegment, KVia, KEdge, KPad, KZonePoly>` construction/destruction
in `clearance::clear` calls 38.7 s (6.9 %).

### Why `is_via_blocked_diag` was 44 %

Both A\* loops try a via drop to every *other* routable layer:

```cpp
for (int new_layer : routable_layers_) {
    if (new_layer == current.layer) continue;
    if (is_via_blocked_diag(current.x, current.y, net, ...)) { ...; continue; }
```

None of `is_via_blocked_diag`'s inputs is `new_layer` — a through via spans
the stack, so the predicate already walks every layer itself — and nothing in
the loop body mutates the grid it reads. Board 06 is a 4-layer board, so every
expanded node asked the identical full-stack question **three times**.

Two smaller findings from the same table:

* `trace_stored_vias_clear` (run on every neighbour step) called
  `route_geometry_candidates`, which gathers **and sorts** the stored-*segment*
  bins as well as the via bins — then read only the vias.
* `clearance::clear(const KShape&, const KShape&, double)` takes variants, so
  each typed call built two `std::variant` temporaries whose alternatives
  include vectors (non-trivial destructor).

## The change

1. **`Pathfinder::ViaBlockMemo`** (`pathfinder.hpp`) — evaluates
   `is_via_blocked_diag` once per expanded node and replays the verdict *and
   its `out_*` blocker diagnostics* for the remaining layers, so each loop's
   per-layer `via_block_count` / last-blocker bookkeeping runs exactly as
   before. It is disabled while a pairwise (cross-domain HV) matrix is
   installed, because that branch's `note_pairwise_block` increments the
   Python-visible `pairwise_block_count` per call and a replay would
   under-count it.
2. **`Grid3D::gather_route_geometry`** — the per-bin-set half of
   `route_geometry_candidates`; `trace_stored_vias_clear` gathers only the via
   bins. Same sorted, de-duplicated via list as before.
3. **Typed `clearance::clear` overloads** for segment/segment, segment/via,
   via/segment and via/via — each is `copper_gap`'s own branch (same
   layer gate, same canonical argument order, same per-pair function), so the
   verdict is bit-for-bit the dispatcher's. The `router_cpp.clear` binding now
   routes those pair kinds through the overloads, so the existing cross-port
   parity corpus exercises them; `tests/router/test_typed_kernel_clear_5617.py`
   checks `clear(a, b, r) == (copper_gap(a, b) >= r - EPS)` at and one ulp
   either side of the threshold.

No search order, cost, clearance, iteration budget, deadline or resume
semantics changes; no binding surface changes (build version stays 55).

## After: the native split (this branch)

Phase 4 under the native profiler: **368.6 s** (from 557.8 s).

| frame | before | after |
|---|---:|---:|
| `run_astar_loop` | 426.8 s | 231.2 s |
| via-drop check (`is_via_blocked_diag` / `ViaBlockMemo::blocked`) | 247.7 s | 89.7 s |
| ↳ `via_route_geometry_clear` | 51.0 s | 14.0 s |
| `is_trace_blocked` | 77.0 s | 74.1 s |
| `trace_stored_vias_clear` | 42.2 s | 10.2 s |
| `std::variant` temporaries in `clear` | 38.7 s | not in profile |

The via-drop check fell by 2.76× — the expected ~3× for three calls → one.

## Paired wall-clock measurement (Release build, no profiler)

`PYTHONHASHSEED=42 /usr/bin/time -v uv run python scripts/ci/check_diffpair_coverage.py boards/06-diffpair-test --seed 42`,
same host, same day, runs one at a time:

| run | build | phase 4 | re-route child | whole script (wall) | user CPU |
|---|---|---:|---:|---:|---:|
| base2 | `33e98c97` | 541.7 s | 658.5 s | 11:26.8 | 697.3 s |
| after1 | memo + via-only gather | 346.6 s | 465.1 s | 8:15.0 | 505.8 s |
| after2 | + typed `clear` (final) | 355.7 s | 467.0 s | 8:16.0 | 507.3 s |
| after3 | final (repeat) | 351.1 s | 465.4 s | 8:14.6 | 505.5 s |

(A py-spy-wrapped baseline run, `base`, measured 565.5 s / 684.5 s.)

**Phase 4: −35 % (541.7 s → ~351 s). Re-route step: −29 % (658.5 s →
~466 s). Whole script: −28 % (686.8 s → ~495 s).** The typed `clear`
overloads are not separable from noise in wall time (after1 vs after2/3); the
native profile shows their effect inside `trace_stored_vias_clear`.

Every other phase is unchanged within noise (phase 10 8.9–9.3 s, the five
zone refills ~12 s each, trace optimisation ~15 s).

## Routing-output equivalence and determinism

* The routed `diffpair_test_routed.kicad_pcb` of every run above — base,
  base2, both native-profile runs, after1, after2, after3 — is identical
  after masking `(uuid …)` / `(tstamp …)` tokens. (Object UUIDs — e.g. on
  footprint property fields — differ between *any* two runs, baseline vs
  baseline included; that is pre-existing and not routing output.) The other
  nine files in `regression-output/` are byte-identical.
* The routing logs, with timings masked, are identical line for line
  (`Rerouted …`, overflow counts, resume/fallback lines, every net's result).
* Gate verdict in every run: DRC error count 9 (allowed 18), signal reach
  21/21, pour connectivity OK (5 pour nets), all 3 diff-pair rules exercised,
  9 diff-pair error violations within baseline 18.
* after2 and after3 (final code, two runs) produce identical output:
  `PYTHONHASHSEED=42` / `--seed 42` determinism holds.

## What is left in phase 4

After this change `run_astar_loop` is 231 s of 369 s (63 %). Next native
targets, by size:

1. **The via-drop check itself, 89.7 s (24 %)** — now one call per node; its
   body scans a circular kernel on all four layers plus a *linear* walk over
   every stored via (`grid_.stored_vias()`), and the geometry pre-check.
   Memoising across nodes at the same `(x, y)` (a via drop from `(x, y, L0)`
   and from `(x, y, L1)` asks the same question) would need a per-search
   cache invalidated across `resume()`; not attempted here.
2. **`is_trace_blocked`, 74.1 s (20 %)** — the per-neighbour trace-envelope
   kernel scan; unchanged by this PR.
3. Open-set heap maintenance, ~29 s (8 %).

Outside phase 4 the step is now dominated by the five `kicad-cli` zone
refills (~60 s together) and phase 10b's repair round (~16 s).

## Reproducing this

```bash
uv run kct build-native --check            # build 55; rebuild if stale
PYTHONHASHSEED=42 /usr/bin/time -v uv run python \
    scripts/ci/check_diffpair_coverage.py boards/06-diffpair-test --seed 42

# Native profile: build an unstripped twin of the extension and swap it in.
cmake -B /tmp/build-prof -S src/kicad_tools/router/cpp \
    -DPython_EXECUTABLE=$PWD/.venv/bin/python \
    -Dnanobind_DIR=$(.venv/bin/python -m nanobind --cmake_dir) \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    "-DCMAKE_CXX_FLAGS_RELWITHDEBINFO=-O3 -g -DNDEBUG -fno-omit-frame-pointer"
cmake --build /tmp/build-prof -j 2
cp /tmp/build-prof/router_cpp.*.so src/kicad_tools/router/
PYTHONHASHSEED=42 uvx py-spy record --native --idle --subprocesses --rate 20 \
    --format speedscope -o native.json -- \
    .venv/bin/python scripts/ci/check_diffpair_coverage.py boards/06-diffpair-test --seed 42
uv run kct build-native --force            # restore the Release build
```

Slice `native.json` to phase 4 by taking the second-largest profile (the route
child) and accumulating sample weights between the `[phase 4 @ …]` and
`[phase 5 @ …]` offsets in the script's log.

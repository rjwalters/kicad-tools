# Issue #5617 — phase-4 profile of the Diff-Pair re-route step

A retained, reproducible profile of
`scripts/ci/check_diffpair_coverage.py boards/06-diffpair-test --seed 42`
at a finer granularity than the 25-row phase table PR #5637 added, plus the
paired before/after measurement for the optimisation this profile motivated
(`Router._via_halo_clear_cached`, PR for Issue #5617).

This is Acceptance item 1 of Issue #5617 ("a local or containerised profile …
retained with source revision, image, native version and host identity").

**Contents**

- [Provenance](#provenance)
- [Method](#method)
- [What the CI phase table already said](#what-the-ci-phase-table-already-said)
- [Finer-grained attribution inside phase 4](#finer-grained-attribution-inside-phase-4)
- [The negative result on "suggested target 1"](#the-negative-result-on-suggested-target-1)
- [Paired before/after, four runs](#paired-beforeafter-four-runs)
- [Routing-output equivalence and determinism](#routing-output-equivalence-and-determinism)
- [What is left in phase 4](#what-is-left-in-phase-4)
- [Reproducing this](#reproducing-this)

## Provenance

| | |
|---|---|
| Baseline revision | `37a30b8c` (`origin/main`, 2026-09-22) |
| Change under test | `e8951f58` — "memoise the per-layer route-halo via probe" |
| Host | AWS EC2, Intel Xeon Platinum 8488C × 8 vCPU, 15 GiB RAM |
| OS / kernel | Ubuntu 24.04.4 LTS, Linux 7.0.0-1010-aws x86_64 |
| Python | CPython 3.12.3 (`uv` project venv) |
| Native router | `router_cpp.cpython-312-x86_64-linux-gnu.so`, backend 1.0.0, **build version 42** (`uv run kct build-native --check`) |
| `kicad-cli` | 10.0.5 |
| Profiler | py-spy 0.4.2, `record --subprocesses --idle --rate 50 --format speedscope` |
| Environment | `PYTHONHASHSEED=42`, `--seed 42` |
| Host load (1-min avg at launch) | run A 6.96, run C 6.27, run B 8.48, run D 7.57 |

> **Not a CI-equivalent host.** This dev host is ~1.5–2× slower than the
> self-hosted `heavy` CI runner for phase 4, and it shares itself with
> concurrent Loom sweeps — so absolute wall times here are not comparable to
> a CI job. The *ratios* below are, and every comparison in this document is
> either a ratio or a paired same-host measurement.

## Method

`py-spy record --subprocesses` wraps the whole coverage script, so the
`generate_design.py --step route` child (the step's 96 % cost) is sampled as
its own profile. Samples are in time order with per-sample weights, so the
child's profile can be sliced by the `[phase N @ Xs]` offsets that
`check_diffpair_coverage.py`'s phase profiler (PR #5637) already prints. Every
number below labelled "phase 4" is that slice — from `[phase 4 @ …]` to
`[phase 5 @ …]`.

`--idle` is required: the C++ A* releases the GIL, so its Python frame
(`CppPathfinder._run_fresh_search`) is "idle" to py-spy. Including idle samples
makes that frame a **wall-clock measure of native search time**, which is what
makes it usable as the host-speed normaliser below.

**Host-speed normalisation.** Four runs on a shared host cannot be compared by
raw wall time (phase 4's two baseline runs differ by 39 % purely from load).
Because the change under test is verdict-preserving — the routed artifacts are
byte-identical, see below — the *native* search performs exactly the same work
in every run, so `_run_fresh_search`'s wall time is a per-run speed reference.
All deltas are quoted as ratios against it.

## What the CI phase table already said

From CI job `106665540456` on `37a30b8c` (self-hosted `heavy`), the step is
609.8 s and phase 4 is **405.5 s (66.5 %)**. Everything else is small: the five
`kicad-cli` zone re-fills total 98.6 s (16.2 %), 45-degree quantization 38.8 s,
trace optimisation 22.4 s, and phase 10 — the target #5637 investigated and
#5657 then indexed — is down to **8.7 s (1.4 %)** from the 191–327 s the issue
was filed against. Phase 4 is where the remaining cost is.

## Finer-grained attribution inside phase 4

Inclusive (cumulative) wall time inside the phase-4 slice, baseline run A
(831.1 s phase 4 on this host):

| frame | s | % of phase 4 |
|---|---:|---:|
| `CppPathfinder._run_fresh_search` (native A*, GIL released) | 492.0 | 59.2 % |
| `Router.route` — **the pure-Python A\* fallback** | 255.5 | 30.7 % |
| ↳ `Router._route_impl` | 172.5 | 20.8 % |
| ↳↳ `Router._check_via_placement_cached` | **151.3** | **18.2 %** |
| ↳↳↳ `Router._is_via_blocked` | 120.3 | 14.5 % |
| ↳↳↳↳ `Router._via_halo_clear` → `RouteHaloGeometry.clear` | **112.3** | **13.5 %** |
| ↳ `Router._is_trace_blocked` | 33.4 | 4.0 % |
| ↳↳ `Router._trace_halo_clear` | 24.8 | 3.0 % |
| `Autorouter._relief_rescue` | 166.6 | 20.0 % |

Self (leaf) time by file, same slice:

| file | s | % |
|---|---:|---:|
| `cpp_backend.py` (492.0 s of it is the native call) | 517.1 | 62.2 % |
| `route_halo_geometry.py` | 87.0 | 10.5 % |
| `pathfinder.py` | 61.8 | 7.4 % |
| `grid.py` | 51.2 | 6.2 % |
| shapely (`measurement`/`decorators`/`creation`/`base`) | 50.9 | 6.1 % |
| `geometry.py` | 14.3 | 1.7 % |

So phase 4 splits into ~59 % native search, which no Python change can touch,
and ~41 % Python — and **essentially all of that Python time is the
pure-Python A\* fallback**, with a single predicate (`_via_halo_clear`, i.e.
`RouteHaloGeometry.clear` on a probe via) as its largest component.

The 87.0 s of `route_halo_geometry.py` leaf time is concentrated in
`clear`'s per-object loop — lines 215–255, the bin gather and the #5240
bounding-box prune — confirming the cost is *per candidate object per call*,
i.e. driven by call count rather than by any one expensive operation.

## The negative result on "suggested target 1"

Issue #5617's first suggested target was the exhausted-resume cascade
("3 `gave up` lines per job, each burning 200+ seconds of thrown-away work").
The telemetry PR #5653 added answers this directly, and the answer is **no**:

```
Resume-exhaustion: 1 net(s) burned the full resume budget
  (12.0s of discarded C++ search);
  Python fallback spent 150.4s on those nets, 105.7s on other fallback triggers
```

(run A; CI job `106665540456` reports the same shape — 5.8 s discarded C++,
64.1 s + 54.3 s Python fallback.) The discarded *C++ resume* work is 1.4 % of
phase 4. The expensive consequence is the **Python fallback the exhaustion
hands off to**, and that fallback is entered from several triggers, not just
resume exhaustion — 41 % of its time here came from other causes. Shortening
the resume budget would therefore recover ~1 % of phase 4 while risking
exactly the outcome change the issue's constraints forbid. Making the fallback
itself cheaper is the same win without that risk, which is what the change
below does.

## Paired before/after, four runs

Two runs per arm, alternating, on the same host and worktree. `cpp` is
`_run_fresh_search` (the host-speed reference), `py` is phase 4 minus `cpp`,
`fallback` is `Router.route`, `probe` is `_via_halo_clear`.

| run | code | phase 4 s | cpp s | py s | py/cpp | fallback s | fallback/cpp | probe s | probe/cpp |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A | `37a30b8c` | 831.1 | 492.0 | 339.1 | 0.689 | 255.5 | 0.519 | 112.3 | 0.2283 |
| C | `37a30b8c` | 599.5 | 358.3 | 241.2 | 0.673 | 194.7 | 0.543 | 87.3 | 0.2435 |
| B | `e8951f58` | 550.0 | 382.7 | 167.3 | 0.437 | 111.4 | 0.291 | 10.7 | 0.0281 |
| D | `e8951f58` | 536.0 | 371.6 | 164.5 | 0.443 | 107.6 | 0.290 | 11.3 | 0.0305 |

Normalised (mean of each arm):

| quantity | baseline | after | change |
|---|---:|---:|---:|
| phase 4 / cpp | 1.681 | 1.440 | **−14.4 %** of phase 4 |
| fallback / cpp | 0.531 | 0.290 | **−45.3 %** |
| probe / cpp | 0.2359 | 0.0293 | **−87.6 %** |

The two runs within each arm agree to within 2 % on every normalised ratio,
while raw phase-4 wall time varies by up to 39 % across the four runs — which
is precisely why the raw numbers are not the headline.

**Projection to CI** (not a substitute for Acceptance item 3's cohort
remeasurement): phase 4 is 405.5 s of the 609.8 s step on `37a30b8c`, so
−14.4 % of phase 4 is ≈ −58 s, i.e. ≈ **−9.6 % of the re-route step**.
Cross-checked against CI's own in-log telemetry, which reports 118.4 s of
Python fallback: −45.3 % of that is ≈ −54 s, ≈ −8.8 % of the step. The two
independent estimates agree.

A fast, host-portable arm of the same comparison —
`scripts/research/bench_python_astar_via_kernel.py` (added by #5658), which
prints a verdict checksum over 14 400 `_is_via_blocked` queries:

| workload | `37a30b8c` | `e8951f58` | checksum |
|---|---:|---:|---|
| `kernel` | 0.195 s | 0.106 s | `8dd5b4f140557930` (identical) |
| `halo` | 0.405 s | 0.231 s | `bd55ef0b2a510f5d` (identical) |

That sweep queries each position with two radii × two sharing modes, so its
memo hit rate is higher than a real A*'s; treat its 1.8× as an upper bound and
the four-run table above as the measurement.

## Routing-output equivalence and determinism

After each of the four runs, every file under
`boards/06-diffpair-test/regression-output/` was SHA-256'd:

- **A == C** — the baseline is reproducible run-to-run.
- **B == D** — the changed code is reproducible run-to-run
  (`PYTHONHASHSEED=42`, `--seed 42`, issue #3144).
- **A == B == C == D** — the change alters no output at all, including
  `diffpair_test_routed.kicad_pcb` (the routed copper), the `.kicad_dru`, the
  net-class map and `lvs.json`.

All four runs also print the same `7. Final: 23 routes / 6390 segments /
33 vias`, which matches CI job `106665540456` exactly.

> **Local phases 9b–13 do not reproduce CI.** With `kicad-cli` 10.0.5 the zone
> fill produces zero filled polygons on this board, so the local pour audit
> FAILs and phase 10b grinds for ~300 s. That is a local toolchain artifact,
> present identically in all four runs, and it does not touch phase 4 — the
> routed PCB handed to it is byte-identical across arms. Phase 4 is the only
> phase this document draws conclusions about.

## What is left in phase 4

After the change, phase 4 is ~69 % native search. The remaining Python, in
order (run B, phase-4 slice, leaf time):

| file | s | note |
|---|---:|---|
| `pathfinder.py` | 42.4 | A* bookkeeping + `_is_trace_blocked` |
| `grid.py` | 27.5 | `net` / `blocked` / `is_obstacle` / `cell_at` accessors |
| `route_halo_geometry.py` | 24.3 | `clear`'s per-object loop, now mostly from `_trace_halo_clear` |
| shapely | 14.0 | exact `distance` on the objects that survive the prune |

`_trace_halo_clear` (19.6 s inclusive after the change, from 24.8 s) is the
obvious next candidate, but it is *not* the same shape of win: its verdict
depends on `cells` and `from_cell` as well as position, so it has far less
repetition to memoise than the via probe did.

## Reproducing this

```bash
uv run kct build-native --check          # MUST report the backend as available
PYTHONHASHSEED=42 py-spy record --subprocesses --idle --rate 50 \
    --format speedscope --output /tmp/phase4.speedscope -- \
    uv run python scripts/ci/check_diffpair_coverage.py boards/06-diffpair-test --seed 42 \
    | tee /tmp/phase4.log

# phase boundaries for the slice:
grep -o '\[phase [0-9a-z]* @ [0-9.]*s\]' /tmp/phase4.log

# routing-output equivalence between two runs:
( cd boards/06-diffpair-test/regression-output && find . -type f | sort | xargs sha256sum )
```

Slice the speedscope profile by accumulating `profiles[i].weights` in order
(they are per-sample intervals, in time order) and keeping the samples whose
running offset falls between the phase-4 and phase-5 offsets.

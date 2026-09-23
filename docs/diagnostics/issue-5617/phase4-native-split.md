# Issue #5617 — what is left in phase 4 once the native A\* dominates

A retained, reproducible profile of the Diff-Pair regression job's re-route
step **after** the #5637 / #5657 / #5658 / #5669 / #5671 / #5679 increments,
plus the paired before/after measurement for the optimisation it motivated
(the two per-cell via-site memos, the `_is_trace_blocked` disc-mask memo and
`RouteHaloGeometry.cell_known`'s direct-read rewrite).

Sibling of [`phase4-python-fallback-profile.md`](phase4-python-fallback-profile.md)
(the #5669 increment) and [`redundant-remediation-refill.md`](redundant-remediation-refill.md)
(the #5679 increment). This is Acceptance items 1 and 2 of Issue #5617;
item 3 (the hosted cohort remeasurement) remains open.

**Contents**

- [Provenance](#provenance)
- [Method](#method)
- [Phase 4 is now two-thirds native search](#phase-4-is-now-two-thirds-native-search)
- [The four sites this increment addresses](#the-four-sites-this-increment-addresses)
- [Paired before/after, six runs](#paired-beforeafter-six-runs)
- [Routing-output equivalence and determinism](#routing-output-equivalence-and-determinism)
- [A staleness hole the measurement did not catch](#a-staleness-hole-the-measurement-did-not-catch)
- [What is left in phase 4](#what-is-left-in-phase-4)
- [Reproducing this](#reproducing-this)

## Provenance

| | |
|---|---|
| Baseline revision | `5f5280dc` (`origin/main`, 2026-09-23) |
| Change under test | `106a92bd` (arms `b1`/`b2`), `9545482e` (arm `b3`, adds the token guard below) |
| Board | `boards/06-diffpair-test`, `--seed 42` |
| Host | AWS EC2, 8 vCPU, 30 GiB RAM, **shared with concurrent Loom sweeps** |
| OS / kernel | Ubuntu 24.04.4 LTS, Linux 7.0.0-1010-aws x86_64 |
| Python | CPython 3.12.3 (`uv` project venv, `uv sync --frozen --extra dev`) |
| Native router | `router_cpp.cpython-312-x86_64-linux-gnu.so`, backend 1.0.0, **build version 42** (`uv run kct build-native --check`) |
| `kicad-cli` | **not installed on this host** — see the truncation note in Method |
| Profiler | py-spy 0.4.2, `record --subprocesses --idle --rate 50 --format speedscope` |
| Environment | `PYTHONHASHSEED=42`, `--seed 42` |
| 1-min load average at arm launch | `a1` 41.1, `b1` 33.4, `a2` 12.7, `b2` 12.5, `a3` 13.6, `b3` 8.2 |

> **Not a CI-equivalent host, and heavily contended.** Raw wall times here are
> not comparable to a self-hosted `heavy` runner, and the four arms of the main
> measurement span a 3× range in host load. Every conclusion below is either a
> ratio or a paired same-host comparison; the raw seconds are shown only so the
> normalisation can be checked.

## Method

`py-spy record --subprocesses --idle` wraps
`boards/06-diffpair-test/generate_design.py … --step route --seed 42`, which is
the child `scripts/ci/check_diffpair_coverage.py` spends ~96 % of the step in.
Samples carry per-sample weights in time order, so the profile can be sliced by
the numbered phase headers the pipeline prints (`4. Routing nets…` →
`5. Optimizing traces…`). Everything labelled "phase 4" below is that slice.

`--idle` is required: the C++ A\* releases the GIL, so its Python frame
(`CppPathfinder._run_fresh_search`) looks idle to py-spy. Counting idle samples
turns that frame into a **wall-clock measure of native search time**, which is
what makes it usable as the host-speed normaliser.

**Host-speed normalisation.** The change under test is verdict-preserving — the
routed board is byte-identical across every arm (below) — so the native search
does exactly the same work in every run, and `_run_fresh_search`'s wall time is
a per-run speed reference. All deltas are ratios against it. This matters more
here than in the #5669 measurement: `cpp` ranges from 413.2 s to 626.5 s across
the six arms purely from host load.

**Arms are truncated at phase 9.** `kicad-cli` is not installed on this host, so
phases 9b onward cannot do their real work at all (`Error: kicad-cli not found`)
and only add dead time. Each arm is stopped as soon as `9. Generating
copper-pour zones` is printed — i.e. once phase 8 has written
`diffpair_test_routed.kicad_pcb`, the artifact the equivalence check compares.
Phase 4 is the only phase this document draws conclusions about.

## Phase 4 is now two-thirds native search

Baseline slice at `5f5280dc`, phase 4 = 524.2 s of a 574.6 s route (a run taken
at low host load, kept separately from the paired arms):

| frame | s | % of phase 4 |
|---|---:|---:|
| `CppPathfinder._run_fresh_search` (native A\*, GIL released) — **self** | **354.6** | **67.6 %** |
| `Router.route` — the pure-Python A\* fallback | 105.5 | 20.1 % |
| ↳ `Router._check_via_placement_cached` | 44.0 | 8.4 % |
| ↳↳ `Router._is_via_blocked` | 30.4 | 5.8 % |
| ↳ `Router._is_trace_blocked` | 32.8 | 6.2 % |
| ↳↳ `Router._trace_halo_clear` | 29.8 | 5.7 % |
| ↳↳↳ `RouteHaloGeometry.clear` | 28.9 | 5.5 % |
| `RouteHaloGeometry.cell_known` (both call sites) | 15.9 | 3.0 % |
| `Autorouter._relief_rescue` | 124.3 | 23.7 % |

This is the picture #5669 predicted it would leave: the route-halo via probe it
memoised is gone from the top of the list (`_via_halo_clear` is down to 11.5 s /
2.2 %), and the native search — which no Python change can touch — is now more
than twice the size of everything else in the phase combined.

The Python that remains is still worth attacking, because it is *all* fallback:
`Router.route` is 20.1 % of phase 4 and `_check_via_placement_cached` +
`_is_trace_blocked` are 14.6 points of that 20.1.

## The four sites this increment addresses

Line-level attribution inside the same slice (topmost-frame inclusive, so
children are counted against the line that calls them):

| site | `pathfinder.py` / `route_halo_geometry.py` lines at `5f5280dc` | s | % of phase 4 |
|---|---|---:|---:|
| `ComponentHoleIndex.clear` inside `_is_via_blocked` | 2154–2158 | 3.26 | 0.62 % |
| `ComponentHoleIndex.clear` inside `_check_via_placement_cached` | 3120–3124 | 1.86 | 0.35 % |
| the #5240 non-through-hole pad-drill sweep | 3136–3157 | 10.30 | 1.96 % |
| the #3229 Euclidean-disc kernel | 1781–1787 | 1.60 | 0.31 % |
| `cell_known`'s `cell_at` + the four `_CellView` property reads | 165, 166, 173 | 10.28 | 1.96 % |
| **total addressed** | | **27.30** | **5.21 %** |

Why each repeats work it does not have to:

1. **`ComponentHoleIndex.clear` is a predicate of the candidate cell alone.**
   Its arguments are `grid_to_world(gx, gy)`, `rules.via_drill` and
   `rules.min_hole_to_hole` — none of which depend on `net`, `layer`, `radius`
   or `allow_sharing`. It was nevertheless evaluated once in
   `_check_via_placement_cached` and then once more per non-plane layer inside
   `_is_via_blocked`, i.e. 3–5 identical evaluations per via candidate on this
   four-layer board. (`test_hole_memo_collapses_the_per_layer_repeat` pins the
   collapse to one.)
2. **The pad-drill sweep is likewise cell-only**, but the sibling `_via_cache`
   that would have absorbed it is bypassed whenever `allow_sharing` is set —
   exactly the negotiated mode the fallback runs in — so it ran on *every*
   call. It is the single largest of the four at 1.96 %.
3. **The disc kernel is a pure function of the radius and the region offsets
   relative to the centre.** Away from the board edge those offsets are always
   `(-radius, radius+1, -radius, radius+1)`, so every interior A\* expansion at
   a given radius rebuilt one identical pair of `np.arange`-derived arrays.
   Memoising it hands out a *shared* array, which is only sound because every
   use site is read-only (`&`, `>`, boolean indexing all allocate fresh
   results) — `test_disc_kernel_arrays_are_never_mutated_by_is_trace_blocked`
   pins that.
4. **`cell_known` is a hot leaf**, reached from `_trace_halo_clear`'s
   `all(cell_known(...) for ...)` guard and from `_is_via_blocked`'s known-cell
   filter. It read the four occupancy planes through a freshly allocated
   `_CellView` and four bound-property calls; the rewrite indexes the same four
   arrays directly, with the operand order and short-circuit points unchanged.
   (`_CellView.blocked` / `.net` / `.is_obstacle` / `.pad_blocked` are each
   *defined* as the indexed read that replaces them, in `grid.py`.)

None of the four changes what any predicate returns, which is the property
`tests/test_router_site_memo_5617.py` exists to pin: each new form is compared
against the exact expression it replaced, over sweeps asserted to be
non-degenerate (both verdicts present).

## Paired before/after, six runs

Arms alternate on the same host and worktree, swapping only
`src/kicad_tools/router/pathfinder.py` and
`src/kicad_tools/router/route_halo_geometry.py` between `origin/main` and the
branch. `cpp` is `_run_fresh_search` (the speed reference), `py` is phase 4
minus `cpp`, `fallback` is `Router.route`, `via chk` is
`_check_via_placement_cached`, `known` is `cell_known`.

| run | arm | phase 4 s | cpp s | py s | py/cpp | fallback/cpp | via chk/cpp | `_is_trace_blocked`/cpp | known/cpp |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `a1` | base `5f5280dc` | 901.3 | 626.5 | 274.8 | 0.4386 | 0.2689 | 0.1173 | 0.0829 | 0.0396 |
| `a2` | base `5f5280dc` | 628.2 | 433.8 | 194.4 | 0.4480 | 0.2842 | 0.1232 | 0.0874 | 0.0420 |
| `a3` | base `5f5280dc` | 674.5 | 456.6 | 217.9 | 0.4771 | 0.3139 | 0.1349 | 0.0985 | 0.0444 |
| `b1` | after `106a92bd` | 711.6 | 520.8 | 190.8 | 0.3663 | 0.2250 | 0.0824 | 0.0725 | 0.0270 |
| `b2` | after `106a92bd` | 680.1 | 482.6 | 197.5 | 0.4092 | 0.2371 | 0.0858 | 0.0785 | 0.0267 |
| `b3` | after `9545482e` | 576.3 | 413.2 | 163.2 | 0.3949 | 0.2235 | 0.0791 | 0.0729 | 0.0274 |

`b3` is the confirmation arm at the **final** HEAD, which adds the token guard
described in the next section — a tuple build and compare on every memo call,
so it can only cost time relative to `b1`/`b2`. It did not erode the result:
its adjacent pairing against `a3` is the *largest* of the three
(−5.6 % of phase 4).

Normalised, mean of each arm:

| quantity | baseline (n=3) | after (n=3) | change |
|---|---:|---:|---:|
| phase 4 / cpp | 1.4546 | 1.3901 | **−4.4 % of phase 4** |
| Python / cpp | 0.4546 | 0.3901 | −14.2 % |
| fallback / cpp | 0.2890 | 0.2285 | −20.9 % |
| `_check_via_placement_cached` / cpp | 0.1251 | 0.0824 | **−34.1 %** |
| `cell_known` / cpp | 0.0420 | 0.0270 | **−35.6 %** |
| `_is_trace_blocked` / cpp | 0.0896 | 0.0746 | −16.7 % |

**The spread is wider than #5669's, so read the bound as well as the mean.**
Every one of the four component ratios separates completely — each `after` run
is below *every* `base` run on all four. The phase-4 aggregate does not quite:

| pairing | phase 4 / cpp | change |
|---|---|---:|
| `a2` → `b1` (adjacent) | 1.4480 → 1.3663 | −5.6 % |
| `a2` → `b2` (adjacent) | 1.4480 → 1.4092 | −2.7 % |
| `a3` → `b3` (adjacent) | 1.4771 → 1.3949 | −5.6 % |
| worst of any 3×3 pairing (`a1` vs `b2`) | 1.4386 → 1.4092 | −2.0 % |

**−2.0 % of phase 4 is the floor this measurement supports; −4.4 % is the
mean.** The residual spread is `_relief_rescue` work, which varies run to run
independently of this change and is 24 % of the phase.

**Projection to CI** (explicitly *not* a substitute for Acceptance item 3):
in PR #5679's Diff-Pair job the step was 502.6 s with phase 4 at 330.9 s
(65.8 %), so −4.4 % of phase 4 is ≈ −14.6 s, ≈ **−2.9 % of the re-route step**
(floor: ≈ −6.6 s, ≈ −1.3 %). This is a small increment by design — after #5669
and #5679 the remaining Python in phase 4 is only ~20 % of the phase, and this
change takes about a third of that.

## Routing-output equivalence and determinism

Each arm SHA-256s `boards/06-diffpair-test/regression-output/diffpair_test_routed.kicad_pcb`
— the phase-8 artifact, i.e. all of the routed copper — immediately after the
run:

```
3a98e7820eae22c28262f30cb09d235524094b584f3b9df66919ab7b9c4a93c9   a1  base
3a98e7820eae22c28262f30cb09d235524094b584f3b9df66919ab7b9c4a93c9   a2  base
3a98e7820eae22c28262f30cb09d235524094b584f3b9df66919ab7b9c4a93c9   a3  base
3a98e7820eae22c28262f30cb09d235524094b584f3b9df66919ab7b9c4a93c9   b1  after
3a98e7820eae22c28262f30cb09d235524094b584f3b9df66919ab7b9c4a93c9   b2  after
3a98e7820eae22c28262f30cb09d235524094b584f3b9df66919ab7b9c4a93c9   b3  after
```

- `a1 == a2 == a3` — the baseline is reproducible run to run.
- `b1 == b2 == b3` — the changed code is reproducible run to run
  (`PYTHONHASHSEED=42`, `--seed 42`, issue #3144), across both of its
  revisions.
- **All six are equal — the change alters no routed copper at all.**

All six arms also print the same `7. Final: 23 routes / 6390 segments /
33 vias`, which matches the CI job cited above.

## A staleness hole the measurement did not catch

Byte-identical output on one board is evidence, not proof, and the first
version of this change was in fact unsound in a way board 06 never exercised:
`RoutingGrid.add_component_hole` / `add_pad` append to the **live**
`_component_hole_index` in place, mid-route, with no router-side hook. Hanging
the hole memo off `invalidate_pad_geometry_cache` alone therefore let a
previously-positive via site survive a newly added drill —
`tests/test_component_hole_search.py::test_python_new_hole_invalidates_prior_positive_via_answer`
failed, and would have shipped a *wrong route* on a board that adds a hole
mid-pass.

The fix is a token rather than a stronger entry-point convention, because the
mutator has no entry point to hook:

- the hole memo is tokened on `(index object, len(index.holes), index.known)` —
  `_index` only ever appends, so the length is an exact per-instance version;
  `refreshed()` installs a new object; `known` covers
  `install_component_hole_census(None)`, which negates every verdict without
  touching `holes`. Holding the index reference inside the token also keeps the
  compared object alive, so its identity cannot be recycled underneath it.
- the pad-drill memo is tokened on the **identity of the arrays tuple**
  `_non_th_pad_geometry` returns. That tuple is rebuilt on exactly the events
  that invalidate the sweep's inputs (a pad-count change, and PR #5330's
  explicit drop for in-place `Pad` mutation), so a new tuple object *is* the
  invalidation signal.

Three further pins were added for the two router-hook-free mutators and the
census-becomes-unknown transition. **The lesson for the next increment on this
issue: an output-equivalence run on board 06 does not substitute for the
repository's existing cache-invalidation tests — run both.**

## What is left in phase 4

After the change, phase 4 is ~72 % native search on this host. The remaining
Python, in the order I would look:

1. **The native A\* itself — ~68 % of phase 4, ~45 % of the step.** Unchanged
   and untouched since this issue was filed. Every Python-side increment so far
   (#5658, #5669, this one) has made it a *larger* share. Any further phase-4
   work of consequence is a `router/cpp/` change, and it needs a plan for
   measuring a GIL-released frame that py-spy can only see as wall time.
2. **`RouteHaloGeometry.clear`'s per-object loop — 28.9 s / 5.5 % of phase 4**,
   now reached almost entirely from `_trace_halo_clear` rather than the via
   probe #5669 memoised. Its verdict depends on `cells` and `from_cell` as well
   as position, so it has far less repetition to memoise; #5657's spatial index
   is the more likely shape of win.
3. **`Autorouter._relief_rescue` — 124.3 s / 23.7 % of phase 4** (overlapping
   the frames above, since the fallback runs inside it). It is the noisiest
   term across the six arms here and has never been profiled in its own right.

## Reproducing this

```bash
uv run kct build-native --check          # MUST report the backend as available

python3 - <<'PY'                          # prepare regression-output as CI does
import sys; sys.path.insert(0, "scripts/ci")
from pathlib import Path
from board_recipe_artifacts import recipe_output_dir
print(recipe_output_dir(Path("boards/06-diffpair-test"), prepare=True))
PY

PYTHONHASHSEED=42 py-spy record --subprocesses --idle --rate 50 \
    --format speedscope --output /tmp/phase4.speedscope -- \
    uv run python -u boards/06-diffpair-test/generate_design.py \
        boards/06-diffpair-test/regression-output --step route --seed 42 \
  | ts -s '%.s' | tee /tmp/phase4.log     # any line-timestamping filter will do

sha256sum boards/06-diffpair-test/regression-output/diffpair_test_routed.kicad_pcb
```

Slice the speedscope profile by accumulating `profiles[i].weights` in order
(they are per-sample intervals, in time order) and keeping samples whose
running offset falls between the phase-4 and phase-5 header timestamps. The
`cpp` normaliser is the **self** time of the `_run_fresh_search` frame; every
other quantity in the tables is inclusive time for the named frame.

On a host running concurrent work, alternate the arms (`base`, `after`,
`base`, `after`) rather than running each arm twice in a row — the load on this
host moved by 5× over the 95 minutes the six arms took.

# Validity-aware nogood cache for negotiated routing retries (Issue #6353)

Status: experiment complete. **Verdict: refine** (not adopt, not decline).

TraceMaker describes a "nogood" cache at the algorithm level only. Its GPL
source was not read, copied or adapted; the design below is derived from this
repository's own grid/negotiation structures.

## Design

- **Boundary:** the negotiated retry (per-net route attempt), not inside A*.
- **Key:** `(net, endpoints, layer-set/rule context)` via `NogoodCache.make_key`.
- **Failure window:** bounding box of the net's endpoints on the grid. `full`
  uses the whole grid; `m<N>` pads the endpoint box by N cells (bounded, may
  false-hit on copper outside the window).
- **Validity signal:** a local occupancy fingerprint of the window, compared on
  lookup. Any occupancy change inside the window is a miss (conservative
  invalidation); no per-attempt whole-board hashing.
- **Bounded:** `max_entries` (default 256) with deterministic eviction. Eviction
  only turns a future hit into a miss, so it cannot change correctness.
- **Opt-in:** `kct route --nogood-cache [--nogood-cache-margin-cells N]`;
  default behaviour unchanged. Diagnostics: hit/miss/insertion/invalidation/eviction.

## Tests

`tests/router/test_nogood_cache.py` (11 tests, `--no-cov`): key stability,
unchanged-window hit, relevant-change invalidation, eviction safety, per-attempt
reset, deterministic counters, and a safety oracle in which a blocker is removed
between attempts and the treatment retries rather than serving the stale failure.

## Experiment

Runner: `scripts/research/nogood_cache_experiment.py`, native backend
(`kct build-native --check` OK), fixed seed, boards 00, 01, 06a, 03, 02 copied
to scratch (tracked board artifacts untouched), 1500 s per-row timeout, arms
baseline / nogood-full / nogood-m20. 15 rows, no timeouts or errors.

| Board | Arm | Nets | Conn | Vias | WL mm | A* calls | Wall s | hit/miss/ins/inv/evict |
|---|---|---|---|---|---|---|---|---|
| 00 | baseline | 3/3 | 3/3 | 0 | 8.71 | 2 | 10.1 | -- |
| 00 | full / m20 | 3/3 | 3/3 | 0 | 8.71 | 2 | 10.1 / 10.0 | 0/1/0/0/0 |
| 01 | baseline | 3/3 | 5/5 | 3 | 59.61 | 72 | 5.9 | -- |
| 01 | full / m20 | 3/3 | 5/5 | 3 | 59.61 | 72 | 6.0 / 6.0 | 0/11/0/0/0 |
| 06a | baseline | 18/18 | 90/90 | 55 | 489.4 | 56 | 21.4 | -- |
| 06a | full / m20 | 18/18 | 90/90 | 55 | 489.4 | 56 | 20.9 / 21.2 | none (cache never consulted) |
| 03 | baseline | 26/27 | 114/115 | 73 | 708.55 | 283 | 275.3 | -- |
| 03 | full / m20 | 26/27 | 114/115 | 73 | 708.55 | **263** | 231.5 / 231.3 | 2/6/1/0/0 |
| 02 | baseline | 12/12 | 36/36 | 43 | 408.41 | 415 | 179.9 | -- |
| 02 | full / m20 | 12/12 | 36/36 | 43 | 408.41 | 415 | 186.3 / 176.8 | 0/36/0/0/0 |

The routed-copper SHA-256 is identical across all three arms on every board.

## Findings

1. **No soundness failure observed.** Completion, connectivity, vias and
   wirelength match baseline on all boards, and copper is byte-identical, so no
   cached failure suppressed a feasible route (including the m20 bounded window).
2. **Benefit is small and narrow.** Only board 03 produced hits: 2 hits cut A*
   calls 283 -> 263 (-7.1%) and wall time ~16% (275 -> 231 s). Wall time is load
   dependent on this shared host; A* calls are the load-independent figure and
   the wall delta is not claimed beyond it. Board 02 has 36 misses and zero hits.
3. **Boards 06a/00/01 rarely reach a repeat attempt**, so there is nothing to
   suppress. This is a correct cache with little to do, not a broken experiment.
4. **Window margin made no difference** (full vs m20 identical outputs).

## Limits

Five boards, one seed, one run per row; no variance estimate. The stagnation /
overflow-heavy subjects where retries repeat most were not available as
separate frozen inputs beyond 03 and 02. The fingerprint is occupancy-only; a
change in net-class/clearance rules mid-run is covered only by the key context.

## Verdict: refine

Correct and conservative, but ~7% A* savings on one of five boards does not
justify adoption. Refine before reconsidering: (a) find subjects with genuine
repeated-failure loops (heavy rip-up/stagnation) and (b) key on failure
*reason*/window shrinkage to raise hit rate on boards like 02. Adoption (default
on) is a separate issue.

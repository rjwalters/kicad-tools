# Validity-aware nogood cache for negotiated routing retries (Issue #6353)

Status: experiment complete. **Verdict: decline as a sound cache.** Revisit
only together with a cheaper validity signal (see "Verdict").

TraceMaker describes a "nogood" cache at the algorithm level only. Its GPL
source was not read, copied or adapted; the design below is derived from this
repository's own grid/negotiation structures.

## Design

- **Boundary:** the negotiated retry (per-net route attempt), not inside A*.
- **Key:** `NogoodCache.make_key(net, terminals, reverse_search, pf, context)`:
  net id, every terminal's coordinates/layer/net in order, the reverse-search
  flag, the present-cost factor, and a budget context (effective iteration
  cap, whether a tuned per-net cap is active, the Python iteration override,
  relief mode, whether a wall-clock budget is set). Layer sets and design
  rules are **not** in the key; they are fixed for a router instance and the
  cache lives on that instance.
- **What may be cached:** only a complete failure (no route committed) in
  which every failed sub-search **drained its open set**. On the pure-Python
  router that is `Router.last_route_exhausted`. On the C++ backend the C++
  reason must be NO_PATH, VIA_VIA_BLOCKED or PAIRWISE_BLOCKED **and** the
  Python fallback must have run and drained too. Wall-clock timeouts,
  iteration-cap give-ups, post-route-validation resume exhaustion, skipped
  fallbacks and refused commits are never inserted: under a budget, a
  different cost field can reach the goal, so such a failure says nothing
  about the next attempt.
- **Failure window:** `full` (default) is the whole grid. `m<N>` slices the
  per-cell planes to the terminals' bounding box padded by N cells.
- **Validity signal:** a BLAKE2b digest, recomputed on **every** lookup that
  finds an entry and on every insertion, of every numpy plane on the grid
  (`_blocked`, `_net`, `_usage_count`, `_history_cost`, `_present_cost_ema`,
  `_is_obstacle`, `_is_zone`, pad copper/provenance, `_congestion`, ...;
  `_saved*` rip-up snapshots excluded), the stored-route halo registry, and on
  the C++ backend the #6098 grid stamp (reservations, fixed fills, keepouts,
  router net maps). Cost planes are included on purpose: they decide which
  candidate paths the search validates first. There is no
  `occupancy_generation` fast path, because usage and history updates do not
  bump it. In `full` mode this is whole-board hashing on every consulted
  lookup.
- **Hit replay:** a hit re-fires the failure callback for the stored pairs and
  restores the #6369 per-net failure code, the only persistent side effect of
  a failed search that routing reads. Fallback statistics counters are not
  replayed.
- **Bounded:** `max_entries` (default 256), LRU. Eviction only turns a future
  hit into a miss, so it cannot change the answer.
- **Opt-in:** `kct route --nogood-cache [--nogood-cache-margin-cells N]`;
  default behaviour unchanged (the default path makes exactly the pre-#6353
  call). Diagnostics: hit/miss/insertion/invalidation/eviction.

### Soundness

`full` mode: a hit is served only when the key and the whole grid state the
search reads are unchanged and the stored failure was a drained search, so a
re-run would read the same inputs and fail the same way. Two knobs are
deliberately **unsound** and exist only to measure what the sound mode gives
up:

- `m<N>` (bounded window) can miss a change outside the window that opens a
  detour and serve a stale failure.
- `KCT_NOGOOD_CACHE_IGNORE_PF=1` drops the present-cost factor from the key.

The first version of this experiment (PR #6605 head `c4d5cfab4`) hashed only
`_blocked`/`_net`, used the occupancy generation as a fast path, and cached
iteration-capped failures. The Judge showed that usage, history, obstacle and
zone changes then still hit. `test_oracle_iteration_capped_failure_not_served_after_history_change`
reproduces that: only history changes between two attempts under a 100-
iteration cap, and the old code served the stale failure.

## Tests

`tests/router/test_nogood_cache.py` (20 tests, `--no-cov`): key stability and
budget context, bounded-window hit after an out-of-window change,
relevant-change invalidation, full fingerprint covering `_usage_count`,
`_history_cost`, `_is_obstacle`, `_is_zone` and `_pad_blocked` with the
generation unchanged, `_saved*` snapshots ignored, the drained-search gate
(NO_PATH vs ITERATION_LIMIT / TIMEOUT / skipped fallback / refused commit),
eviction safety, deterministic counters, env default-off, and two safety
oracles: a wall removed between attempts, and history removed under an
iteration cap. Both oracles assert that the retry routes instead of serving
the stale failure.

## Experiment

Runner: `scripts/research/nogood_cache_experiment.py`, native backend
(`kct build-native --check` OK), fixed seed, boards 00, 01, 06a, 03, 02 copied
to scratch (tracked board artifacts untouched), 1500 s per-row timeout, arms
baseline / nogood-full / nogood-m20. 15 rows, no timeouts or errors. Re-run on
the corrected cache.

| Board | Arm | Nets | Conn | Vias | WL mm | A* calls | Wall s | hit/miss/ins/inv/evict |
|---|---|---|---|---|---|---|---|---|
| 00 | baseline | 3/3 | 3/3 | 0 | 8.71 | 2 | 5.3 | -- |
| 00 | full / m20 | 3/3 | 3/3 | 0 | 8.71 | 2 | 4.4 / 4.4 | 0/1/0/0/0 |
| 01 | baseline | 3/3 | 5/5 | 3 | 59.61 | 72 | 2.5 | -- |
| 01 | full / m20 | 3/3 | 5/5 | 3 | 59.61 | 72 | 2.4 / 2.5 | 0/11/0/0/0 |
| 06a | baseline | 18/18 | 90/90 | 55 | 489.4 | 56 | 9.1 | -- |
| 06a | full / m20 | 18/18 | 90/90 | 55 | 489.4 | 56 | 8.4 / 8.5 | none (cache never consulted) |
| 03 | baseline | 26/27 | 114/115 | 74 | 709.54 | 283 | 133.0 | -- |
| 03 | full / m20 | 26/27 | 114/115 | 74 | 709.54 | 283 | 132.0 / 132.2 | 0/14/0/0/0 |
| 02 | baseline | 12/12 | 36/36 | 43 | 408.41 | 421 | 62.6 | -- |
| 02 | full / m20 | 12/12 | 36/36 | 43 | 408.41 | 421 | 62.7 / 64.8 | 0/36/0/0/0 |

The routed-copper SHA-256 is identical across all three arms on every board.

## Findings

1. **The sound cache never inserts on these boards.** 62 lookups across five
   boards, 0 insertions, 0 hits. No failed attempt was a drained search on
   both the C++ and the Python side, so there was never a proof to cache.
2. **The earlier board-03 saving came from an unsound entry.** The first
   version reported 1 insertion and 2 hits on board 03 (A* calls 283 -> 263).
   With the drained-search gate and full fingerprint that entry is not
   created, and A* calls stay at the baseline 283. The saving came from
   reusing a failure that was not a proof.
3. **Output is unchanged**, as it must be when nothing is cached: completion,
   connectivity, vias, wirelength and copper hash match baseline everywhere.
4. **Window margin makes no difference** here, since nothing is inserted in
   either arm.

## Limits

Five boards, one seed, one run per row; no variance estimate. Wall seconds are
from a shared host, so compare A* calls instead. Boards with genuine
repeated-failure loops (heavy rip-up / stagnation) were not available as
frozen inputs beyond 03 and 02. The bounded window and `IGNORE_PF` are unsound
by construction (see "Soundness"). The full fingerprint hashes the whole board
on every consulted lookup; that cost was not separately profiled because no
entry was ever consulted.

## Verdict: decline as a sound cache

Made sound, the cache has nothing to store on the measured boards: real
negotiated failures here are budget-limited, not proofs of unreachability.
The only savings measured came from caching budget-limited failures, which
can hide a route that a later cost field would find. Revisit only with (a)
subjects that produce drained-search failures repeatedly, or (b) a budget-aware
validity model, i.e. one that proves the *same budget* fails under the *same
costs*. That needs cost planes in the key, and history changes every
negotiation iteration, so hits would stay rare. Adopting it (default on) would
be a separate issue.

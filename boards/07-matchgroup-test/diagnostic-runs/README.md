# Board 07 -- Determinism Diagnostic Runs

This directory commits the AC1 / AC4 evidence for Issue #3146 (matchgroup-
routing non-determinism under CI load) as scoped by the issue's curator
analysis.

## Summary

Five consecutive local re-routes of `boards/07-matchgroup-test` at
`--seed 42` with `PYTHONHASHSEED=42` produced **identical** results
after the determinism fixes landed in the PR closing #3146.

| Run | DRC errors | Main pass | Orchestrator                                                | Normalized PCB md5 |
|-----|------------|-----------|-------------------------------------------------------------|--------------------|
| 1   | 18         | 25/31     | 2 tuned, 5 clean, 5 rolled back, 8 budget-exhausted          | 3b7688e5...        |
| 2   | 18         | 25/31     | 2 tuned, 5 clean, 5 rolled back, 8 budget-exhausted          | 3b7688e5...        |
| 3   | 18         | 25/31     | 2 tuned, 5 clean, 5 rolled back, 8 budget-exhausted          | 3b7688e5...        |
| 4   | 18         | 25/31     | 2 tuned, 5 clean, 5 rolled back, 8 budget-exhausted          | 3b7688e5...        |
| 5   | 18         | 25/31     | 2 tuned, 5 clean, 5 rolled back, 8 budget-exhausted          | 3b7688e5...        |

The "Normalized PCB md5" column hashes the PCB text after stripping
`(uuid ...)` lines (KiCad UUIDs are seeded from `os.urandom` and are
intentionally not deterministic) and sorting the remaining lines (so
emit-order differences in the writer don't mask geometric identity).
All five runs hash to `3b7688e5f63a9716a71160115432ad7a`.

This is the strongest possible local determinism evidence short of
re-running the gate under the actual CI runner; AC4 of #3146 is met by
the 5x identical DRC count + identical orchestrator verdicts + identical
normalized PCB content.

## Reproduction

```bash
# In a fresh worktree.
uv run kct build-native --force

mkdir -p /tmp/board07-determinism
for i in 1 2 3 4 5; do
  PYTHONHASHSEED=42 uv run python boards/07-matchgroup-test/generate_design.py \
    --step route --seed 42 > /tmp/board07-determinism/run-$i.log 2>&1
  cp boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb \
     /tmp/board07-determinism/run-$i.kicad_pcb
done

# Compare normalized PCB content across runs.
for i in 1 2 3 4 5; do
  md5 -q <(grep -v "(uuid " /tmp/board07-determinism/run-$i.kicad_pcb | sort)
done | sort -u | wc -l   # expected: 1

# Compare DRC counts.
for i in 1 2 3 4 5; do
  uv run python scripts/ci/check_routed_drc.py \
    /tmp/board07-determinism/run-$i.kicad_pcb 2>&1 | grep -oE "[0-9]+ errors"
done   # expected: "18 errors" x 5
```

Each run takes ~7-10 minutes locally on a fast multi-core box; the
full 5-run sequence is ~40 minutes.

## Root cause and fix

See the PR closing #3146 and `.github/routed-drc-tolerance.yml`'s
extended commentary above the board 07 floor entry for the full
root-cause analysis. Summary:

1. **Tie-break defect.** The Python `AStarNode` dataclass had only
   `f_score` as a `compare=True` field, and the C++ `AStarNode`
   `operator>` compared only `f_score`. On `f_score` ties heap pop
   order was unspecified (Python: fell through to insertion order
   that depended on PYTHONHASHSEED and `random.shuffle` upstream;
   C++: `std::priority_queue` makes no stability guarantee).
2. **PYTHONHASHSEED unpinned.** CPython per-process hash randomization
   made string-keyed dict/set iteration non-deterministic between
   processes.
3. **CI Python fallback.** The `matchgroup-routing-regression` CI job
   was missing `uv run kct build-native`, so board 07 routed in pure
   Python -- 10-100x slower on a 2-core ubuntu-latest runner than on
   a local multi-core box with the C++ backend. The dramatic
   slowdown made the `per_net_timeout` wall-clock budget classify a
   different set of nets as completing-vs-bailing under CI vs local
   load.

Fix:

1. A* tie-break already deterministic on `main`: both Python
   `AStarNode.__lt__` and C++ `AStarNode::operator>` compare on
   `(f_score, seq)` where `seq` is a monotonic counter set at
   heap-push time. This was landed by PR #3192 (closing #3144 --
   the CoupledPathfinder / board 06 sibling) and inherited by this
   board 07 closure. The C++ `Pathfinder` used as the negotiated
   fallback when `CoupledPathfinder` pairs exceed their budget is
   the same code path the negotiated main pass uses for board 07,
   so the tie-break fix from #3192 already applies here -- this
   PR does not re-touch it.
2. `PYTHONHASHSEED=42` pinned in `boards/07-matchgroup-test/`
   `generate_design.py` for its `kct route` subprocess, and pinned
   at the CI workflow step env for the outer `check_matchgroup_coverage.py`.
3. `uv run kct build-native` step added to the
   `matchgroup-routing-regression` CI job (mirroring
   `diffpair-routing-regression`).

The C++ binding version was bumped 5 -> 6 (`types.hpp` +
`cpp_backend.py`) so a stale `.so` from before this PR is rejected
at import time with an actionable `kct build-native` error.

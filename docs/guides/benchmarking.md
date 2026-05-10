# Routing benchmark suite

The routing benchmark suite measures router performance and quality on
real and synthetic PCBs, and detects regressions against committed
baselines.  It lives in `src/kicad_tools/benchmark/` and is driven by
the `kct benchmark` CLI.

## What it measures

For each registered case, `BenchmarkRunner.run_single` produces a
`BenchmarkResult` with:

| Field                | Meaning                                                                  |
| -------------------- | ------------------------------------------------------------------------ |
| `nets_total`         | Multi-pad signal nets the router targeted                                |
| `nets_fully_routed`  | Nets where every pad is in the same connected component                  |
| `nets_partial`       | Nets where >=1 pad but not all pads are connected                        |
| `nets_unrouted`      | Nets with zero connected pads -- the structural-floor candidates         |
| `unrouteable_nets`   | Net names of the `nets_unrouted` set (for diffing in regression reports) |
| `total_segments`     | Sum of segment counts across all routes                                  |
| `total_vias`         | Sum of via counts across all routes                                      |
| `total_length_mm`    | Aggregate trace length                                                   |
| `drc_violations`     | `error_count` from `DRCChecker.check_all()` on the source PCB            |
| `routing_time_sec`   | Wall-clock time spent inside `route_*`                                   |
| `memory_peak_mb`     | Peak resident memory measured via `tracemalloc`                          |

`nets_total = nets_fully_routed + nets_partial + nets_unrouted` for any
case with pad data available.

## Listing and running cases

```bash
# Show what's registered
kct benchmark list

# Run a single case
kct benchmark run --cases chorus_test_revA --strategies negotiated \
  --output benchmarks/chorus_latest.json --verbose

# Compare against the committed baseline
kct benchmark compare --baseline tests/baselines/chorus_test_revA.json
```

When the source PCB for a case is not available on disk (e.g., the
chorus-test-revA board lives in a separate hardware repo), the runner
emits a "SKIP" line and continues -- no exception, no failure.  This
lets CI gracefully no-op when the optional fixture is not configured.

## chorus-test-revA case

`chorus_test_revA` is the densest benchmark case: a 4-layer RPi DAC hat
with a PCM5122 SSOP-28 0.65mm-pitch escape cluster.  It is the source
of nearly every routing-issue insight in #2515, #2517, #2518, #2540,
#2589, #2595, #2604, #2605.

The PCB is **not** checked into kicad-tools.  It lives in a separate
hardware repository because the kicad-tools repo does not ship
real-world PCBs.  Two paths exist for making it available on CI:

1. **`CHORUS_TEST_LOCAL_PATH`** -- filesystem path on the runner.
   Self-hosted runners or local workstations can pre-stage the board
   and set this env var.
2. **`CHORUS_TEST_GIT_URL` + `CHORUS_TEST_GIT_REF`** -- a private git
   URL the workflow can shallow-clone.  Stored as repo secrets so the
   PR build never has access.

The CI workflow `.github/workflows/benchmark-routing.yml` invokes
`scripts/ci/fetch_chorus_test.py`, which tries both sources in order
and skips gracefully when neither is set.

## Regression thresholds

Defined in `src/kicad_tools/benchmark/regression.py`:

### Relative thresholds (`REGRESSION_THRESHOLDS`)

`(warning, error, higher_is_worse)`:

| Metric              | Warning | Error | Direction        |
| ------------------- | ------- | ----- | ---------------- |
| `completion_rate`   | -5%     | -10%  | lower is worse   |
| `nets_fully_routed` | -5%     | -10%  | lower is worse   |
| `total_vias`        | +20%    | +50%  | higher is worse  |
| `total_length_mm`   | +15%    | +30%  | higher is worse  |
| `routing_time_sec`  | +100%   | +200% | higher is worse  |
| `drc_violations`    | +20%    | +50%  | higher is worse  |

### Absolute thresholds (`ABSOLUTE_THRESHOLDS`)

These metrics use the baseline value itself as the bar; *any* increase
above baseline is a regression of the configured severity.  Decreases
are silently celebrated.

| Metric          | Severity on increase | Why absolute?                                  |
| --------------- | -------------------- | ---------------------------------------------- |
| `nets_unrouted` | `error`              | Structural floor: 8 today; a 9th = real regression |

The chorus-test-revA structural floor is 8 nets (DAC_CLK + 7 small
nets in the U5/U7/U9 cluster, per #2595).  Until #2604/#2605 and the
fine-pitch escape work fix the root cause, this is the unavoidable
floor.  But a *9th* unrouteable net means we broke something.

## Baselines

Committed under `tests/baselines/`:

- `tests/baselines/chorus_test_revA.json` -- seeded from
  curator-aggregated numbers in issues #2295, #2517, #2595, #2604, and
  `tests/benchmark_results.json` (PR #2296).  Issue #2611.

### Updating a baseline

Baselines drift forward as the router improves.  To update:

1. Run the nightly job (`Actions -> Routing Benchmark -> Run workflow`)
   or run locally with the fixture present:
   ```bash
   uv run kct benchmark run --cases chorus_test_revA \
     --output benchmarks/new_baseline.json --verbose
   ```
2. Download the artifact (or copy the local JSON) and inspect the new
   metrics.
3. Open a PR replacing `tests/baselines/chorus_test_revA.json` with the
   measured values.  **The PR description MUST justify**:
   - Any *decrease* in `nets_fully_routed` (why did we get worse?).
   - Any *increase* in `nets_unrouted` (a new net joined the floor --
     what new constraint or regression caused this?).
   - Any *decrease* in `nets_unrouted` (which net got routed?  This is
     good news but a baseline-update reminder for downstream consumers).
   - Significant time/memory swings.

Baseline changes are reviewed like any other code change.  The
floor-decrease case is not auto-applied because a downward shift in
`nets_unrouted` followed by a regression to the old floor would *not*
trigger an error -- so the baseline must be moved deliberately.

## Why this is not in PR-time CI

A chorus-test route on a typical GitHub runner takes 25-55 minutes.
Adding that to every PR would:

- Push CI wall time past the 1-hour PR-author patience budget.
- Burn ~50 CPU-minutes per push, including draft pushes.
- Make refactor PRs that don't touch routing wait on irrelevant runs.

Instead, the workflow runs weekly and on `workflow_dispatch`.  When a
PR is suspected of regressing the router (e.g., changes to
`src/kicad_tools/router/`), a maintainer can manually dispatch the
benchmark against the PR branch.

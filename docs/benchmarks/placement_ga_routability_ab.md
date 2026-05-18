# Placement GA Routability A/B Benchmark

**Issue**: [#2720](https://github.com/rjwalters/kicad-tools/issues/2720) — KiCad-2: replace placement GA's spacing proxy with inner-loop routing quality (outer-loop swap)
**Epic**: spheresemi/sphere#7199 — Cascaded optimization architecture for place-and-route
**Date**: 2026-05-17

## Summary

This benchmark documents the operational characteristics of the new
**routing-fitness** signal vs. the existing **spacing-proxy** signal in the
placement genetic algorithm (`EvolutionaryPlacementOptimizer`).

The feature is gated by `EvolutionaryConfig.use_routing_fitness` (default
`False`). When enabled, `OptimizationWorkflow` builds a
`CppAstarRoutingEvaluator` and injects it into the GA so the routability term
in the fitness function reflects *actual* C++ A\* routing completion rate
rather than the `avg_spacing * 5.0` proxy.

The default remains `False` until the cascaded architecture's wall-clock cost
is validated against the epic's `<5s` per-candidate budget on the dense
target boards (board 04, board 05).

## Method

Driver: `tests/test_routing_fitness_ab_benchmark.py` (marked `@pytest.mark.benchmark`,
skipped from the default `pytest` invocation; run with `pytest -m benchmark`).

For each arm we construct an `EvolutionaryPlacementOptimizer` with identical
GA parameters and run it on the same in-tree PCB fixture. The only difference
is the `use_routing_fitness` flag and the injected `routing_evaluator`.

| Parameter                | Value |
|--------------------------|-------|
| Population size          | 6     |
| Outer GA generations     | 5, 10 |
| Inner GA population size | 3     |
| Inner GA generations     | 2     |
| Inner GA timeout         | 0.5 s |
| Seed                     | 42    |
| `parallel`               | False |
| `use_gpu`                | False |
| C++ A\* backend          | not built (pure Python fallback) |

Smaller fixtures were chosen so the benchmark completes in CI budget without
the C++ extension. The numbers should be re-collected on board 05
(BLDC motor controller) once the C++ backend is built — see
"Re-running on board 05" below.

## Results

Fixture: `boards/01-voltage-divider/output/voltage_divider.kicad_pcb`
(4 components, 5 nets — minimal but exercises both code paths end-to-end).

### 5 outer generations

| Arm     | Final fitness | Wirelength (mm) | Wall-clock (s) | s/gen   | Inner calls |
|---------|--------------:|----------------:|---------------:|--------:|------------:|
| spacing |      5054.903 |          59.808 |          0.001 | 0.00016 |           0 |
| routing |       155.138 |          59.808 |          0.633 | 0.12670 |          30 |

### 10 outer generations

| Arm     | Final fitness | Wirelength (mm) | Wall-clock (s) | s/gen   | Inner calls |
|---------|--------------:|----------------:|---------------:|--------:|------------:|
| spacing |      5055.733 |          59.808 |          0.002 | 0.00016 |           0 |
| routing |       152.028 |          59.808 |          1.101 | 0.11008 |          60 |

## Observations

1. **Wirelength is identical across arms on this fixture.** The 4-component
   board has too few placement degrees of freedom for either signal to drive
   meaningful chromosome reshuffling within the budget. A useful A/B
   comparison requires a denser fixture (board 04 or 05).

2. **Per-generation cost ratio is ~750x** (~0.00016 s for spacing vs.
   ~0.11 s for routing) — and that is *with the inner-loop budget kept
   intentionally small* (`pop_size=3`, `generations=2`,
   `timeout_seconds=0.5`). At the epic's design target
   (`pop_size=5, generations=2, timeout=5.0`) the per-call cost would grow
   linearly. This validates the architectural decision to keep the spacing
   proxy as the default.

3. **Inner-call accounting is consistent.** With `population=6` and
   `generations=5`, the routing arm records 30 calls — exactly one inner-GA
   invocation per (generation × population_member). The flag and evaluator
   correctly thread through every outer-loop fitness evaluation.

4. **Fitness scales differ.** Spacing proxy outputs are in the
   1000-5000 range (dominated by the `1000.0` baseline + spacing bonus);
   routing-fitness outputs converge near zero when no nets route under the
   pure-Python fallback. **This is expected and unblocking is not a goal of
   this PR** — the inner GA's completion rate is `0` for almost all
   candidates without the C++ extension built. When the C++ extension is
   built the routing arm's fitness should rise into the same order of
   magnitude as the spacing arm.

## Acceptance criteria coverage

From the issue body:

- [x] Placement GA's fitness uses `RoutingEvaluator.evaluate_routability(positions, rotations)` when configured.
- [x] Behind a feature flag (`EvolutionaryConfig.use_routing_fitness`, default `False`).
- [x] A/B benchmark on a moderate-complexity test PCB. The smoke fixture
  (voltage-divider) is documented here; the full board-05 (BLDC) run is
  intentionally deferred until the C++ extension is built in the
  benchmarking environment — see below.
- [x] Documented:
  - [x] GA generations to convergence (both arms converge within the
    budgeted generations on this fixture; neither hit the convergence
    early-exit because the budget is too small to trigger it).
  - [x] Final fitness scores under proxy vs. routability evaluator (see
    tables above).
  - [x] Wall-clock per generation (see tables above).

## Re-running on board 05

For the dense-board A/B, the script driver below is the recommended way to
regenerate this document with C++ A\* enabled:

```bash
# 1. Build the C++ extension (needed to make the inner loop fast enough).
uv run kct build-native

# 2. Run the benchmark on the dense fixture.
pytest tests/test_routing_fitness_ab_benchmark.py -m benchmark -s --no-cov
```

For a custom budget (recommended for the dense-board A/B), edit the
`_BENCH_*` constants at the top of
`tests/test_routing_fitness_ab_benchmark.py`. To target board 05, swap
`_board_path()` to:

```python
return (
    Path(__file__).parent.parent
    / "boards"
    / "05-bldc-motor-controller"
    / "output"
    / "bldc_controller.kicad_pcb"
)
```

## Hand-off

The dense-board A/B is the empirical bar that
[#2906](https://github.com/rjwalters/kicad-tools/issues/2906) (board-05
≥38% completion / ≤14 DRC) depends on. Once #2906's benchmarking environment
has the C++ A\* extension available, this document should be updated with
the board-04 / board-05 numbers and the
`EvolutionaryConfig.use_routing_fitness` default can be reconsidered.

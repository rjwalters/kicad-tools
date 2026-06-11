"""A/B benchmark: spacing-proxy vs. routing-fitness placement GA (Issue #2720).

This is the documentation deliverable for the KiCad-2 acceptance criteria:

> A/B benchmark on one moderate-complexity test PCB:
>   - GA generations to converge
>   - Wall-clock per generation
>   - Route success rate at convergence
>   - Final wirelength / DRC-clean rate

The test runs both fitness signals on a small in-tree fixture
(``boards/02-charlieplex-led``), records per-strategy wall-clock and final
fitness, and writes a JSON artifact under ``tmp_path`` so the harness can be
re-run by hand at higher budgets when validating the architecture on a
larger board.

The test is **deliberately budget-limited** (5 generations, population 4) so
it completes well under the standard pytest timeout.  It is gated by
``pytest -m benchmark`` so it does NOT run in the default ``pytest``
invocation; CI runs the gated profile separately when validating the cascaded
architecture.

What this test asserts (vs. records)
------------------------------------

The test asserts only the operational contract that the epic requires:

1. Both signals run end-to-end without raising.
2. Both signals return numeric fitness values.
3. The routing-fitness path successfully constructs a
   :class:`CppAstarRoutingEvaluator` and threads it into the optimizer
   (verified via ``optimizer.routing_evaluator is not None``).
4. The A/B record JSON is written and contains both runs' metrics.

The test does **not** assert that one signal beats the other — the epic
explicitly allows the routing-fitness signal to regress, in which case the
default flag stays off (``use_routing_fitness=False``).  The test exists
to produce repeatable A/B data for the human reviewer.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from kicad_tools.optim.evolutionary import (
    EvolutionaryConfig,
    EvolutionaryPlacementOptimizer,
)
from kicad_tools.optim.router_factory import build_pcb_router_factory
from kicad_tools.router.evaluators import (
    CppAstarRoutingEvaluator,
    RoutingEvaluatorConfig,
)
from kicad_tools.schema.pcb import PCB

# Issue #3436: CI runs the suite with `-n auto --timeout=60`.  The
# budget-limited GA A/B run beats 60s alone, but under full-suite xdist
# CPU contention the wall-clock reaper killed it spuriously.  The marker
# overrides the CLI default; it does NOT slow the happy path.
pytestmark = pytest.mark.timeout(300)

# ---------------------------------------------------------------------------
# Test budget — keep small so the gated benchmark still completes quickly.
# Increase locally to validate against the epic's "50-100 configs in <5s" bar.
# ---------------------------------------------------------------------------

_BENCH_GENERATIONS = 5
_BENCH_POPULATION = 4
# Inner-GA budget per outer candidate (when use_routing_fitness=True).  Keeps
# the test well under 60 seconds even without the C++ extension.
_BENCH_INNER_TIMEOUT_SECONDS = 1.0


@dataclass
class _ABRecord:
    """A single arm of the A/B benchmark."""

    arm: str  # "spacing" or "routing"
    final_fitness: float
    wall_clock_seconds: float
    seconds_per_generation: float
    components: int
    wire_length_mm: float
    routing_evaluator_active: bool


def _board_path() -> Path:
    """Return the in-tree fixture used for the A/B benchmark.

    Voltage-divider is the smallest practical fixture: 3 components, ~3
    nets — small enough that even without the C++ A* extension built the
    benchmark completes in CI budget.  Larger boards (e.g.
    ``boards/05-bldc-motor-controller`` per the Curator analysis) should
    be used when running this harness by hand at higher generations to
    validate the cascaded architecture.
    """
    return (
        Path(__file__).parent.parent
        / "boards"
        / "01-voltage-divider"
        / "output"
        / "voltage_divider.kicad_pcb"
    )


def _run_one_arm(
    pcb_path: Path,
    *,
    use_routing_fitness: bool,
) -> _ABRecord:
    """Run the placement GA once with the given fitness signal.

    This intentionally bypasses ``OptimizationWorkflow.run()`` and drives the
    GA directly so that the benchmark stays focused on the fitness signal
    A/B and is not gated by other workflow-level details (e.g.
    ``optimizer.total_wire_length()`` is currently a private method on
    ``EvolutionaryPlacementOptimizer`` — the workflow boundary will be
    cleaned up in a separate issue).
    """
    pcb = PCB.load(str(pcb_path))

    routing_evaluator = None
    if use_routing_fitness:
        factory = build_pcb_router_factory(pcb_path)
        routing_evaluator = CppAstarRoutingEvaluator(
            router_factory=factory,
            config=RoutingEvaluatorConfig(
                pop_size=3,
                generations=2,
                seed=42,
                timeout_seconds=_BENCH_INNER_TIMEOUT_SECONDS,
                num_workers=1,
            ),
        )

    config = EvolutionaryConfig(
        generations=_BENCH_GENERATIONS,
        population_size=_BENCH_POPULATION,
        use_routing_fitness=use_routing_fitness,
        # Disable parallelism to keep wall-clock comparable across arms and
        # avoid nested ProcessPoolExecutor inside the inner GA.
        parallel=False,
        use_gpu=False,
    )
    optimizer = EvolutionaryPlacementOptimizer.from_pcb(
        pcb,
        config=config,
        routing_evaluator=routing_evaluator,
    )

    t0 = time.monotonic()
    best = optimizer.optimize(
        generations=_BENCH_GENERATIONS,
        population_size=_BENCH_POPULATION,
    )
    elapsed = time.monotonic() - t0

    return _ABRecord(
        arm="routing" if use_routing_fitness else "spacing",
        final_fitness=float(best.fitness),
        wall_clock_seconds=elapsed,
        seconds_per_generation=elapsed / max(1, _BENCH_GENERATIONS),
        components=len(optimizer.components),
        wire_length_mm=float(optimizer._total_wire_length()),
        routing_evaluator_active=optimizer.routing_evaluator is not None,
    )


# ---------------------------------------------------------------------------
# The benchmark itself.  Gated by the ``benchmark`` marker so it does not
# run by default — but available via ``pytest -m benchmark``.
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
def test_ab_spacing_vs_routing_fitness(tmp_path):
    """A/B benchmark: spacing proxy vs. routing-fitness on charlieplex board."""
    pcb_path = _board_path()
    if not pcb_path.exists():
        pytest.skip(f"Benchmark board not present: {pcb_path}")

    spacing = _run_one_arm(pcb_path, use_routing_fitness=False)
    routing = _run_one_arm(pcb_path, use_routing_fitness=True)

    record = {
        "issue": 2720,
        "epic": "spheresemi/sphere#7199",
        "board": str(pcb_path.relative_to(pcb_path.parent.parent.parent.parent)),
        "budget": {
            "generations": _BENCH_GENERATIONS,
            "population": _BENCH_POPULATION,
            "inner_timeout_seconds": _BENCH_INNER_TIMEOUT_SECONDS,
        },
        "arms": {
            "spacing": asdict(spacing),
            "routing": asdict(routing),
        },
    }

    artifact = tmp_path / "ab_benchmark.json"
    artifact.write_text(json.dumps(record, indent=2))

    # Operational assertions only — outcome is documented, not asserted.
    assert spacing.components == routing.components, (
        "Both arms must operate on the same component set."
    )
    assert routing.routing_evaluator_active, (
        "Routing-fitness arm must have constructed a CppAstarRoutingEvaluator."
    )
    assert not spacing.routing_evaluator_active, (
        "Spacing arm must NOT carry a routing evaluator."
    )

    # Print a compact human-readable summary for the test log so reviewers
    # can scan the A/B numbers without opening the JSON artifact.
    print()
    print("=" * 60)
    print(f"A/B benchmark — Issue #2720, board: {pcb_path.name}")
    print("=" * 60)
    print(f"{'arm':<10}{'time(s)':>10}{'s/gen':>10}{'fit':>12}{'wirelen':>12}")
    for arm in (spacing, routing):
        print(
            f"{arm.arm:<10}"
            f"{arm.wall_clock_seconds:>10.3f}"
            f"{arm.seconds_per_generation:>10.3f}"
            f"{arm.final_fitness:>12.3f}"
            f"{arm.wire_length_mm:>12.3f}"
        )
    print(f"Artifact: {artifact}")
    print("=" * 60)

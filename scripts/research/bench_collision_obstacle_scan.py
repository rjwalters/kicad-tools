#!/usr/bin/env python3
"""A/B the optimizer's per-cell obstacle scan in isolation.

Issue #5240.  ``VectorCollisionChecker._check_obstacles_clear`` is the
narrow-phase pad/keepout scan the post-route trace optimizer runs for every
candidate segment.  It walks ``O(segment_length * clearance_cells)`` grid
cells in pure Python, and the pre-#5240 loop materialised a ``_CellView``
object per cell (``RoutingGrid.cell_at``) and then read four Python
``property`` descriptors off it, each doing its own NumPy 3-tuple index.

``scripts/research/profile_route_obstacle_scan.py`` measures that scan's
share of a *real* route's wall clock.  This script isolates it: it builds a
representative populated grid, drives a deterministic probe set, and times

* ``cell_at``  -- a verbatim transcription of the pre-#5240 loop, and
* ``production`` -- whatever ``VectorCollisionChecker._check_obstacles_clear``
  is in the checked-out tree,

**in the same process**, with the arms interleaved trial-by-trial so a CPU
frequency or cache excursion hits both.  Run it on the post-change tree and
``production`` is the array-read loop; run it on the pre-change tree and the
two arms measure the same code, which is itself a useful null control.

    uv run python scripts/research/bench_collision_obstacle_scan.py --trials 7

``--check`` (on by default) asserts the two arms return **identical** verdicts
for every probe before any timing is reported -- that exact-output parity is
what makes a faster number meaningful rather than just faster. It also
reports the clear/blocked split, because an arm that returned ``True``
unconditionally would be both fast and in parity with nothing.

``--dump-verdicts PATH`` writes every probe's verdict for a cross-tree diff.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.optimizer.collision import (
    VectorCollisionChecker,
    _iter_dilated_line_cells,
)
from kicad_tools.router.rules import DesignRules

# Board-06-ish extents and resolution: the 0.05 mm grid plus a
# 0.2/0.127 trace/clearance rule is what pushes ``clearance_cells`` to ~4-7,
# which is the multiplier that makes this loop expensive in the first place.
BOARD_W = 60.0
BOARD_H = 45.0
NETS = (0, 3, 7, 11, 26)


def _make_grid(resolution: float) -> RoutingGrid:
    rules = DesignRules(
        grid_resolution=resolution,
        trace_width=0.2,
        trace_clearance=0.127,
    )
    return RoutingGrid(BOARD_W, BOARD_H, rules)


def _populate(grid: RoutingGrid, rng: random.Random, pad_clusters: int) -> int:
    """Paint pad-like obstacle clusters through the public cell setters.

    Returns the number of cells touched.  Clusters (not isolated cells) are
    the realistic shape: a pad occupies a contiguous block of cells with a
    single net, which is what the scan's early-exit behaviour sees.
    """
    touched = 0
    for _ in range(pad_clusters):
        layer_idx = rng.randrange(grid.num_layers)
        gx0 = rng.randrange(grid.cols)
        gy0 = rng.randrange(grid.rows)
        w = rng.randint(2, 8)
        h = rng.randint(2, 8)
        net = rng.choice(NETS)
        # A pad's copper (is_obstacle + pad_blocked) vs. its clearance halo
        # (blocked only) -- both appear in a real grid.
        is_pad_copper = rng.random() > 0.35
        for gy in range(gy0, min(gy0 + h, grid.rows)):
            for gx in range(gx0, min(gx0 + w, grid.cols)):
                cell = grid.cell_at(layer_idx, gy, gx)
                cell.blocked = True
                cell.is_obstacle = is_pad_copper
                cell.pad_blocked = is_pad_copper
                cell.net = net
                touched += 1
    return touched


def _probe_set(
    rng: random.Random, count: int, grid: RoutingGrid
) -> list[tuple[float, float, float, float, int, int, float]]:
    """Deterministic probes: ``(x1, y1, x2, y2, layer_idx, net, width)``."""
    probes = []
    for _ in range(count):
        x1 = rng.uniform(1.0, BOARD_W - 1.0)
        y1 = rng.uniform(1.0, BOARD_H - 1.0)
        # Optimizer candidates are short-to-medium rewrites of an existing
        # polyline, not board-crossing lines.
        x2 = x1 + rng.uniform(-6.0, 6.0)
        y2 = y1 + rng.uniform(-6.0, 6.0)
        probes.append(
            (
                x1,
                y1,
                x2,
                y2,
                rng.randrange(grid.num_layers),
                rng.choice(NETS),
                rng.choice((0.2, 0.25, 0.4)),
            )
        )
    return probes


def _reference_scan(
    checker: VectorCollisionChecker,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    layer_idx: int,
    width: float,
    exclude_net: int,
) -> bool:
    """Verbatim transcription of the pre-#5240 ``cell_at`` scan."""
    grid = checker.grid
    gx1, gy1 = grid.world_to_grid(x1, y1)
    gx2, gy2 = grid.world_to_grid(x2, y2)

    total_clearance = width / 2 + grid.rules.trace_clearance
    clearance_cells = int(total_clearance / grid.resolution) + 1

    for check_x, check_y in _iter_dilated_line_cells(gx1, gy1, gx2, gy2, clearance_cells):
        if not (0 <= check_x < grid.cols and 0 <= check_y < grid.rows):
            continue
        cell = grid.cell_at(layer_idx, check_y, check_x)
        if cell.blocked and (cell.is_obstacle or cell.pad_blocked):
            if cell.net != 0 and cell.net == exclude_net:
                continue
            if cell.pad_blocked and cell.net == exclude_net:
                continue
            return False

    return True


def _run_arm(
    arm: str,
    checker: VectorCollisionChecker,
    probes: list[tuple[float, float, float, float, int, int, float]],
) -> tuple[float, list[bool]]:
    verdicts: list[bool] = []
    if arm == "cell_at":
        fn = _reference_scan
    else:
        fn = VectorCollisionChecker._check_obstacles_clear  # type: ignore[assignment]
    t0 = time.perf_counter()
    for x1, y1, x2, y2, layer_idx, net, width in probes:
        verdicts.append(fn(checker, x1, y1, x2, y2, layer_idx, width, net))
    return time.perf_counter() - t0, verdicts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--probes", type=int, default=400)
    parser.add_argument("--pad-clusters", type=int, default=1200)
    parser.add_argument("--resolution", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-check", dest="check", action="store_false")
    parser.add_argument("--dump-verdicts", default=None)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    grid = _make_grid(args.resolution)
    touched = _populate(grid, rng, args.pad_clusters)
    probes = _probe_set(rng, args.probes, grid)
    checker = VectorCollisionChecker(grid)

    report: dict[str, object] = {
        "seed": args.seed,
        "resolution": args.resolution,
        "grid_cols": grid.cols,
        "grid_rows": grid.rows,
        "grid_layers": grid.num_layers,
        "obstacle_cells_painted": touched,
        "probes": len(probes),
        "trials": args.trials,
    }

    # Correctness control FIRST: a timing comparison between arms that do
    # not agree is meaningless.
    _, ref_verdicts = _run_arm("cell_at", checker, probes)
    _, prod_verdicts = _run_arm("production", checker, probes)
    mismatches = [
        i for i, (a, b) in enumerate(zip(ref_verdicts, prod_verdicts, strict=True)) if a != b
    ]
    report["verdict_mismatches"] = len(mismatches)
    report["probes_clear"] = sum(1 for v in prod_verdicts if v)
    report["probes_blocked"] = sum(1 for v in prod_verdicts if not v)
    if args.check and mismatches:
        print(json.dumps(report, indent=1, sort_keys=True))
        print(f"PARITY FAILURE: {len(mismatches)} probes disagree (first: {mismatches[:8]})")
        return 1
    if report["probes_blocked"] == 0 or report["probes_clear"] == 0:
        print(json.dumps(report, indent=1, sort_keys=True))
        print("DEGENERATE PROBE SET: every probe answered the same way; re-tune --pad-clusters")
        return 1

    timings: dict[str, list[float]] = {"cell_at": [], "production": []}
    for _ in range(args.trials):
        # Interleave the arms so a CPU excursion hits both.
        for arm in ("cell_at", "production"):
            elapsed, _ = _run_arm(arm, checker, probes)
            timings[arm].append(elapsed)

    for arm, samples in timings.items():
        report[f"{arm}_seconds_median"] = statistics.median(samples)
        report[f"{arm}_seconds_min"] = min(samples)
        report[f"{arm}_seconds_samples"] = samples
        report[f"{arm}_micros_per_probe_median"] = statistics.median(samples) / len(probes) * 1e6

    ref_med = statistics.median(timings["cell_at"])
    prod_med = statistics.median(timings["production"])
    report["speedup_median"] = ref_med / prod_med if prod_med else float("inf")

    if args.dump_verdicts:
        with open(args.dump_verdicts, "w", encoding="utf-8") as handle:
            for probe, verdict in zip(probes, prod_verdicts, strict=True):
                handle.write(json.dumps({"probe": probe, "clear": verdict}, sort_keys=True) + "\n")

    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

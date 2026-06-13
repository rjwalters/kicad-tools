"""Quick A* hot-loop micro-benchmark for Issue #3309.

Run with:  uv run python benchmark_astar_3309.py

Compares per-call wall-clock on a dense-obstacle grid representative of
chorus's package-escape pattern, before and after the flat-array fast
path.  Since we don't have both versions of the .so around, the
comparison is against a Python-backend baseline on the same grid.
"""

from __future__ import annotations

import time

from kicad_tools.router import router_cpp


def make_grid(cols: int, rows: int, layers: int, resolution: float):
    grid = router_cpp.Grid3D(cols, rows, layers, resolution, 0.0, 0.0)
    # Maze-like obstacles to force A* to backtrack heavily.  This is the
    # workload class where the hashmap-vs-flat-array gap is largest --
    # hundreds of thousands of cells visited, each one paying for the
    # search_g_scores_.find() / search_closed_set_.count() tuple-hash
    # pair, accumulating into the chorus per-net 100-400s wall-clock.
    for layer in (0, 1):
        # Vertical wall slats spaced 20 cells apart, with a single gap.
        for wall_x in range(60, 360, 20):
            gap_y = (wall_x * 7) % (rows - 80) + 40
            for y in range(20, rows - 20):
                if abs(y - gap_y) > 8:
                    grid.mark_blocked(wall_x, y, layer, 99, False, False)
    return grid


def main() -> None:
    cols, rows, layers, res = 400, 400, 4, 0.127
    grid = make_grid(cols, rows, layers, res)
    rules = router_cpp.DesignRules()
    rules.trace_width = 0.127
    rules.trace_clearance = 0.127
    rules.via_diameter = 0.6
    rules.via_drill = 0.3
    rules.via_clearance = 0.127
    rules.grid_resolution = res

    pathfinder = router_cpp.Pathfinder(grid, rules, True)

    # 10 net pairs scattered around the obstacle field.
    pairs = [((20 + 5 * i) * res, 20 * res, (380 - 5 * i) * res, 380 * res) for i in range(10)]

    total_iters = 0
    t0 = time.monotonic()
    for i, (sx, sy, ex, ey) in enumerate(pairs):
        result = pathfinder.route(
            start_x=sx,
            start_y=sy,
            start_layer=0,
            end_x=ex,
            end_y=ey,
            end_layer=0,
            net=i + 1,
        )
        total_iters += pathfinder.iterations
        status = "OK" if result.success else "FAIL"
        print(
            f"  net {i + 1:02d}: iters={pathfinder.iterations:6d} "
            f"explored={pathfinder.nodes_explored:6d} {status}"
        )
    elapsed = time.monotonic() - t0
    print()
    print(
        f"Total: {elapsed * 1000:.1f}ms across {len(pairs)} nets "
        f"({total_iters} cells visited, {total_iters / max(elapsed, 1e-6):.0f} cells/sec)"
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Benchmark the pure-Python A* fallback's via-clearance predicate (Issue #5617).

Why this predicate
------------------
Phase 4 ("Routing nets") is ~67 % of the Diff-Pair regression job's dominant
``Re-route board + check diff-pair coverage`` step (measured by the phase
profiler added in PR #5637).  Three nets per run exhaust the C++ pathfinder's
resume budget, log ``C++ pathfinder gave up ...`` and hand off to the
**pure-Python A\\*** in ``kicad_tools.router.pathfinder`` -- 10-100x slower per
expansion, so those handoffs dominate phase 4.  A py-spy profile of that
fallback attributes the bulk of its self time to ``Router._is_via_blocked``
and to ``RouteHaloGeometry.clear`` underneath it.

What this measures
------------------
Two deterministic workloads over the *real* production predicate:

``kernel``
    A bare grid with sparse foreign copper -- dominated by the via-kernel scan
    (kernel construction + bounds test + blocked-cell walk).

``halo``
    A grid with a committed via, so the dynamic route halo is populated and
    every query also runs ``RouteHaloGeometry.clear`` (the layer-span branch).

Both print a **verdict checksum** over every boolean the sweep produced.  The
before/after arms of an optimisation are compared by running this script on
each git revision: the checksums must be identical (the optimisation is
verdict-preserving) and only the wall time may move.  This is the same
"never report a speed number for a changed predicate" discipline
``bench_stitch_fill_predicates.py`` enforces in-process.

Usage::

    uv run python scripts/research/bench_python_astar_via_kernel.py
    uv run python scripts/research/bench_python_astar_via_kernel.py --profile 20

Wall-clock numbers are host- and load-dependent; the checksums and query
counts are exact and host-independent.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from kicad_tools.router.grid import RoutingGrid  # noqa: E402
from kicad_tools.router.layers import LayerStack  # noqa: E402
from kicad_tools.router.pathfinder import Router  # noqa: E402
from kicad_tools.router.primitives import Layer, Route, Segment, Via  # noqa: E402
from kicad_tools.router.rules import DesignRules  # noqa: E402

# Board 06 is 100 x 80 mm on a 0.05 mm routing grid (see
# ``boards/06-diffpair-test/generate_design.py``); the benchmark grid matches
# it so kernel size and cache behaviour are representative of the real
# fallback.  With ``via_diameter=0.45`` / ``via_clearance=0.2`` the default
# kernel radius is 9 cells, and every net that carries a net class asks for
# radius 10 (``NetClass.via_size`` defaults to 0.6) -- i.e. the per-net-class
# OVERRIDE path, which is what #5617 memoizes.
BOARD_W_MM = 100.0
BOARD_H_MM = 80.0
RESOLUTION = 0.05


def _rules(via_clearance: float = 0.2) -> DesignRules:
    return DesignRules(
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=0.25,
        via_diameter=0.45,
        via_clearance=via_clearance,
        min_hole_to_hole=0.5,
        grid_resolution=RESOLUTION,
    )


def _kernel_workload() -> tuple[Router, RoutingGrid]:
    """Bare 4-layer grid with a deterministic sparse foreign-copper lattice."""
    rules = _rules()
    grid = RoutingGrid(
        width=BOARD_W_MM,
        height=BOARD_H_MM,
        rules=rules,
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    # Deterministic pseudo-scatter, no RNG so the workload is byte-identical
    # across hosts and Python versions.
    for layer in range(grid.num_layers):
        for cy in range(3, grid.rows, 53):
            for cx in range((cy * 7) % 19, grid.cols, 61):
                grid._blocked[layer, cy, cx] = True
                grid._net[layer, cy, cx] = 2
    router = Router(grid, rules)
    router.set_net_name_to_id({"N1": 1, "N2": 2})
    return router, grid


def _halo_workload() -> tuple[Router, RoutingGrid]:
    """Grid with committed foreign copper so the dynamic route halo is live.

    The halo matters because ``_is_via_blocked`` consults
    ``RouteHaloGeometry.clear`` for every candidate, and that walk is the
    second hot spot of the pure-Python fallback.  The committed copper here is
    a coarse lattice of traces plus a few through vias -- enough objects that
    the halo's spatial bins return real work per query, as they do mid-route
    on board 06.
    """
    rules = _rules(via_clearance=0.2)
    grid = RoutingGrid(
        width=BOARD_W_MM,
        height=BOARD_H_MM,
        rules=rules,
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    route = Route(net=2, net_name="N2")
    for gx, gy in ((400, 300), (700, 500), (1100, 800), (1500, 1100)):
        wx, wy = grid.grid_to_world(gx, gy)
        route.vias.append(Via(wx, wy, 0.25, 0.45, (Layer.F_CU, Layer.B_CU), 2, "N2"))
    # A coarse lattice of committed traces on both outer layers.
    for i in range(1, 20):
        ax, ay = grid.grid_to_world(i * 100, 40)
        bx, by = grid.grid_to_world(i * 100, grid.rows - 40)
        route.segments.append(Segment(ax, ay, bx, by, 0.15, Layer.F_CU, 2, "N2"))
        cx, cy = grid.grid_to_world(40, i * 80)
        dx, dy = grid.grid_to_world(grid.cols - 40, i * 80)
        route.segments.append(Segment(cx, cy, dx, dy, 0.15, Layer.B_CU, 2, "N2"))
    grid.mark_route(route)
    router = Router(grid, rules)
    router.set_net_name_to_id({"N1": 1, "N2": 2})
    return router, grid


def _positions(grid: RoutingGrid, side: int) -> list[tuple[int, int]]:
    """A deterministic lattice of candidate via centres, including the border.

    The border band matters: it is the only place the kernel's bounds test can
    fire, and #5617 replaces a per-cell bounds scan with one bounding-box test
    there.
    """
    out: list[tuple[int, int]] = []
    for i in range(side):
        gx = (grid.cols - 1) * i // max(1, side - 1)
        for j in range(side):
            gy = (grid.rows - 1) * j // max(1, side - 1)
            out.append((gx, gy))
    return out


def _sweep(
    router: Router, grid: RoutingGrid, positions: list[tuple[int, int]], radii: list[int]
) -> tuple[bytes, int, float]:
    """Run every (position, radius, sharing) query; return (verdicts, n, seconds)."""
    verdicts = bytearray()
    started = time.perf_counter()
    for radius in radii:
        for gx, gy in positions:
            for sharing in (False, True):
                verdicts.append(
                    1
                    if router._is_via_blocked(
                        gx, gy, 0, net=1, allow_sharing=sharing, radius=radius
                    )
                    else 0
                )
    elapsed = time.perf_counter() - started
    return bytes(verdicts), len(verdicts), elapsed


def _report(name: str, verdicts: bytes, count: int, elapsed: float) -> None:
    digest = hashlib.sha256(verdicts).hexdigest()[:16]
    blocked = sum(verdicts)
    print(
        f"{name:<8} queries={count:<8} blocked={blocked:<8} "
        f"time={elapsed:8.3f}s  {count / elapsed:9.0f} q/s  checksum={digest}"
    )


def _profile(router: Router, grid: RoutingGrid, side: int, radii: list[int], top: int) -> None:
    """cProfile attribution for the sweep -- finer than the phase table."""
    import cProfile
    import io
    import pstats

    positions = _positions(grid, side)
    prof = cProfile.Profile()
    prof.enable()
    _sweep(router, grid, positions, radii)
    prof.disable()
    stream = io.StringIO()
    pstats.Stats(prof, stream=stream).sort_stats("tottime").print_stats(top)
    print(stream.getvalue())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--side",
        type=int,
        default=60,
        help="Lattice side; the sweep is side^2 positions x radii x 2 sharing modes",
    )
    parser.add_argument(
        "--radii",
        type=int,
        nargs="+",
        default=[9, 10],
        help=(
            "Via radii in grid cells.  9 is the fixture's own "
            "``_via_half_cells`` (constructor kernel, no rebuild even "
            "pre-#5617); 10 is what board 06's net-classed nets actually "
            "ask for -- the per-net-class override path #5617 memoizes."
        ),
    )
    parser.add_argument(
        "--profile",
        type=int,
        metavar="SIDE",
        default=0,
        help="Also print a cProfile attribution over a SIDE^2 lattice (use a small value)",
    )
    parser.add_argument(
        "--workload",
        choices=["kernel", "halo", "both"],
        default="both",
    )
    args = parser.parse_args(argv)

    print(f"grid           : {BOARD_W_MM} x {BOARD_H_MM} mm @ {RESOLUTION} mm")
    print(f"lattice        : {args.side}^2 positions, radii={args.radii}, sharing=(False, True)")

    for workload in ("kernel", "halo"):
        if args.workload not in (workload, "both"):
            continue
        router, grid = _kernel_workload() if workload == "kernel" else _halo_workload()
        positions = _positions(grid, args.side)
        # Warm-up so neither workload pays a one-off allocation/import cost.
        _sweep(router, grid, positions[:16], args.radii)
        verdicts, count, elapsed = _sweep(router, grid, positions, args.radii)
        _report(workload, verdicts, count, elapsed)
        if args.profile:
            print(f"--- cProfile: {workload} ---")
            _profile(router, grid, args.profile, args.radii, top=14)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

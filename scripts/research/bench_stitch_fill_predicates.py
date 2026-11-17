#!/usr/bin/env python3
"""Benchmark the stitch pass's filled-polygon clearance predicates (Issue #5617).

The ``Re-route board + check diff-pair coverage`` CI job's dominant step spends
18-30 % of its wall time in phase 10 (pad-aware post-route plane stitching), and
essentially all of *that* is four predicates in ``kicad_tools.cli.stitch_cmd``
that walk every edge of every ``filled_polygon`` for every candidate via
position:

* ``_check_point_filled_polygon_clearance``  (via pad vs. foreign pour)
* ``_check_segment_filled_polygon_clearance``  (pad-to-via trace vs. foreign pour)
* ``_point_has_edge_margin``  (same-net fill containment margin, #4432)
* ``point_in_polygon``  (called from all of the above)

Issue #5617 bins those edges into a uniform grid (``_PolygonIndex``).  This
script measures the effect **on real board geometry** without paying for a
~17-minute re-route: it reads the fill polygons straight out of a routed PCB
and replays a deterministic sweep of query points over them.

Because the index can be disabled at runtime by raising
``_POLYGON_INDEX_MIN_POINTS`` above the polygon size, the same process measures
both arms back-to-back on identical inputs -- and asserts they return identical
answers, so a speed number can never be reported for a changed predicate.

Usage::

    uv run python scripts/research/bench_stitch_fill_predicates.py \\
        boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb

Add ``--samples N`` to trade runtime for stability (default 4000).  Wall-clock
numbers are host- and load-dependent; the ``speedup`` column is the comparable
figure, and the reported query counts are exact and host-independent.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from kicad_tools.cli import stitch_cmd  # noqa: E402
from kicad_tools.core.sexp_file import load_pcb  # noqa: E402


def _query_points(polys, samples: int) -> list[tuple[float, float]]:
    """A deterministic lattice of query points over the fills' bounding box.

    A lattice (not an RNG) keeps the benchmark reproducible across hosts and
    Python versions without depending on ``random``'s stream stability.
    """
    min_x = min(p.min_x for p in polys)
    max_x = max(p.max_x for p in polys)
    min_y = min(p.min_y for p in polys)
    max_y = max(p.max_y for p in polys)
    side = max(2, int(samples**0.5))
    out: list[tuple[float, float]] = []
    for i in range(side):
        for j in range(side):
            # The 0.5 offsets keep queries off exact vertex coordinates.
            out.append(
                (
                    min_x + (max_x - min_x) * (i + 0.5) / side,
                    min_y + (max_y - min_y) * (j + 0.5) / side,
                )
            )
    return out


def _run_arm(polys, points, indexed: bool) -> tuple[list, float]:
    """Evaluate every predicate over every query point; return (answers, seconds)."""
    original = stitch_cmd._POLYGON_INDEX_MIN_POINTS
    stitch_cmd._POLYGON_INDEX_MIN_POINTS = original if indexed else 10**9
    try:
        answers: list = []
        started = time.perf_counter()
        for px, py in points:
            answers.append(
                stitch_cmd._check_point_filled_polygon_clearance(px, py, 0.225, polys, 0.2)
            )
            answers.append(
                stitch_cmd._check_segment_filled_polygon_clearance(
                    px, py, px + 0.6, py + 0.6, 0.1, polys, 0.2
                )
            )
            answers.append(stitch_cmd._point_has_edge_margin(px, py, polys[0].points, 0.225))
            answers.append(stitch_cmd._point_on_same_net_fill(px, py, 0.225, polys, None))
        elapsed = time.perf_counter() - started
        return answers, elapsed
    finally:
        stitch_cmd._POLYGON_INDEX_MIN_POINTS = original


def _profile_arm(polys, points, indexed: bool, top: int) -> None:
    """Print a ``tottime``-sorted cProfile attribution for one arm.

    This is the finer-than-phase-level attribution Issue #5617's acceptance
    item 1 asks for: it names the *functions* the stitch pass's fill-geometry
    work lands in, rather than the recipe phase they sit under.
    """
    import cProfile
    import io
    import pstats

    original = stitch_cmd._POLYGON_INDEX_MIN_POINTS
    stitch_cmd._POLYGON_INDEX_MIN_POINTS = original if indexed else 10**9
    prof = cProfile.Profile()
    try:
        prof.enable()
        _run_arm(polys, points, indexed=indexed)
        prof.disable()
    finally:
        stitch_cmd._POLYGON_INDEX_MIN_POINTS = original

    stream = io.StringIO()
    pstats.Stats(prof, stream=stream).sort_stats("tottime").print_stats(top)
    arm = "indexed (#5617)" if indexed else "linear (pre-#5617)"
    print(f"\n--- cProfile: {arm}, {len(points) * 4} queries ---")
    print(stream.getvalue())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcb", type=Path, help="A routed PCB with filled zones")
    parser.add_argument("--samples", type=int, default=4000)
    parser.add_argument(
        "--profile",
        type=int,
        metavar="SAMPLES",
        default=0,
        help=(
            "Also print a cProfile function-level attribution for both arms, "
            "over SAMPLES query points (use a small value -- cProfile's "
            "per-call overhead makes the wall times incomparable to the "
            "untraced run above)."
        ),
    )
    args = parser.parse_args(argv)

    sexp = load_pcb(args.pcb)
    polys = stitch_cmd.find_all_filled_polygons(sexp)
    if not polys:
        print(f"error: {args.pcb} has no filled_polygon geometry", file=sys.stderr)
        return 2

    total_vertices = sum(len(p.points) for p in polys)
    points = _query_points(polys, args.samples)
    print(f"pcb            : {args.pcb}")
    print(f"fill polygons  : {len(polys)} ({total_vertices} vertices total)")
    print(f"query points   : {len(points)} x 4 predicates = {len(points) * 4} queries")

    # Warm both arms once so neither pays a one-off import/alloc cost.  The
    # index build is warmed here on purpose: it happens once per fill polygon
    # per process and is amortised over the tens of thousands of queries a
    # real stitch run makes, so charging it to a 10k-query benchmark would
    # overstate it.
    _run_arm(polys, points[:8], indexed=False)
    _run_arm(polys, points[:8], indexed=True)

    linear_answers, linear_s = _run_arm(polys, points, indexed=False)
    indexed_answers, indexed_s = _run_arm(polys, points, indexed=True)

    if linear_answers != indexed_answers:
        differing = sum(1 for a, b in zip(linear_answers, indexed_answers, strict=True) if a != b)
        print(f"FAIL: indexed answers differ from linear in {differing} queries", file=sys.stderr)
        return 1

    print(f"linear  (pre-#5617) : {linear_s:8.3f}s")
    print(f"indexed (#5617)     : {indexed_s:8.3f}s")
    if indexed_s > 0:
        print(f"speedup             : {linear_s / indexed_s:8.2f}x")
    print(f"answers identical   : {len(linear_answers)}/{len(linear_answers)}")

    if args.profile:
        profile_points = _query_points(polys, args.profile)
        _profile_arm(polys, profile_points, indexed=False, top=12)
        _profile_arm(polys, profile_points, indexed=True, top=12)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Measure the router's R-tree segment index under rip-up/re-route traffic.

Issue #5240.  ``RoutingGrid`` keeps a per-layer ``rtree`` index of committed
segments so clearance validation can narrow candidates by envelope.  The
router's rip-up/re-route cycle removes and re-adds segments in bulk, and
``rtree.Index.delete`` searches the index for the matching ``(id, envelope)``
pair -- i.e. each removal is O(index size), and a rip-up-heavy route pays that
per removed segment.

This is an **isolated reproduction** of that traffic, not a board route: it
drives the public ``mark_route`` / ``unmark_route`` API (and, separately, the
index-mutation helpers alone) with a deterministic segment population and
rip-up trace, so the index cost can be measured without a 15-minute route in
the loop.

It is arm-agnostic on purpose -- run it once per git state to A/B a change:

    uv run python scripts/research/bench_rtree_ripup.py --trials 5

Reported numbers are per-phase wall clock.  ``--check`` (on by default) also
runs two correctness controls, which is what makes a *faster* result
meaningful:

1. **Rebuilt-index reference.** Every clearance verdict and reported
   clearance value from the benchmarked grid must match a reference grid
   that replayed the identical trace and then rebuilt its index from scratch
   (``_rebuild_segment_index``) -- the tombstone-free construction.  It
   fails loudly if the index ever lost or gained an answer.
2. **Indexed-vs-brute-force branch comparison** (``indexed_vs_bruteforce``).
   ``validate_segment_clearance`` has two branches, and they are required to
   agree bit-for-bit on ``is_valid`` and ``actual_clearance`` (Issue #3522)
   but already disagree on ``violation_loc``, which is assigned by whichever
   violating candidate is visited last.  Reporting that count makes the
   distinction explicit, so a cross-arm ``--dump-verdicts`` diff that moves
   only locations can be read correctly rather than as a regression.

``--dump-verdicts PATH`` writes every probe's full ``(is_valid, clearance,
location)`` answer with ``repr``-precision floats.  Run it once per git state
with the same seed and diff the two files for exact-output parity.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from typing import Any

from kicad_tools.router.grid import RTREE_AVAILABLE, RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

# Board-06-ish extents and rules; the absolute numbers matter less than the
# indexed-object count, which is what the delete walk scales with.
BOARD_W = 60.0
BOARD_H = 45.0
LAYERS = (Layer.F_CU, Layer.B_CU)


def _make_rules() -> DesignRules:
    return DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.127)


def _random_route(rng: random.Random, net: int, seg_count: int) -> Route:
    """A short multi-segment polyline route, as the router commits them."""
    x = rng.uniform(2.0, BOARD_W - 8.0)
    y = rng.uniform(2.0, BOARD_H - 8.0)
    layer = LAYERS[rng.randrange(len(LAYERS))]
    segments: list[Segment] = []
    for _ in range(seg_count):
        dx, dy = rng.choice([(1.5, 0.0), (0.0, 1.5), (1.2, 1.2), (-1.2, 1.2)])
        segments.append(
            Segment(
                x1=x,
                y1=y,
                x2=x + dx,
                y2=y + dy,
                width=0.2,
                layer=layer,
                net=net,
                net_name=f"NET{net}",
            )
        )
        x += dx
        y += dy
    return Route(net=net, net_name=f"NET{net}", segments=segments, vias=[])


def _build_trace(
    rng: random.Random, live_target: int, ripups: int, seg_count: int
) -> tuple[list[Route], list[tuple[str, int]]]:
    """Deterministic (routes, operations) trace.

    ``routes`` is the pool of route objects; ``operations`` is a list of
    ``("mark" | "unmark", route_index)`` pairs.  The trace first fills the
    index to ``live_target`` routes, then performs ``ripups`` rip-up
    (unmark + re-mark of a fresh route) cycles, which is the shape of the
    router's own traffic.
    """
    routes: list[Route] = []
    operations: list[tuple[str, int]] = []
    live: list[int] = []

    for _ in range(live_target):
        idx = len(routes)
        routes.append(_random_route(rng, rng.randrange(1, 24), seg_count))
        operations.append(("mark", idx))
        live.append(idx)

    for _ in range(ripups):
        victim_pos = rng.randrange(len(live))
        operations.append(("unmark", live.pop(victim_pos)))
        idx = len(routes)
        routes.append(_random_route(rng, rng.randrange(1, 24), seg_count))
        operations.append(("mark", idx))
        live.append(idx)

    return routes, operations


def _replay(grid: RoutingGrid, routes: list[Route], operations: list[tuple[str, int]]) -> None:
    for op, idx in operations:
        if op == "mark":
            grid.mark_route(routes[idx])
        else:
            grid.unmark_route(routes[idx])


def _replay_index_only(
    grid: RoutingGrid, routes: list[Route], operations: list[tuple[str, int]]
) -> None:
    """Same trace, index mutation only -- no cell marking, no via bookkeeping."""
    for op, idx in operations:
        if op == "mark":
            grid._rtree_insert_route(routes[idx])
        else:
            grid._rtree_remove_route(routes[idx])


def _probe_segments(rng: random.Random, count: int) -> list[Segment]:
    probes = []
    for _ in range(count):
        x = rng.uniform(2.0, BOARD_W - 4.0)
        y = rng.uniform(2.0, BOARD_H - 4.0)
        dx = rng.choice([-2.0, -1.0, 1.0, 2.0])
        probes.append(
            Segment(
                x1=x,
                y1=y,
                x2=x + dx,
                y2=y,
                width=0.2,
                layer=LAYERS[rng.randrange(len(LAYERS))],
                net=rng.randrange(1, 24),
                net_name="PROBE",
            )
        )
    return probes


def _exactness_control(
    routes: list[Route], operations: list[tuple[str, int]], probes: list[Segment]
) -> tuple[int, int, list[list[Any]]]:
    """Compare benchmarked-grid verdicts against a rebuilt-index reference.

    Returns ``(compared, mismatches, verdicts)``.  The reference replays the
    identical trace and then calls ``_rebuild_segment_index()``, which
    re-derives the index from ``self.routes`` -- tombstone-free by
    construction on any arm.

    ``verdicts`` is the subject grid's full per-probe answer, serialised with
    ``repr`` so that the *whole* list can be written out with
    ``--dump-verdicts`` and diffed byte-for-byte between two git states.  The
    in-run reference catches an index that lost an answer on this arm; the
    cross-arm dump is what proves the optimisation did not change any answer
    at all.
    """
    rules = _make_rules()
    subject = RoutingGrid(width=BOARD_W, height=BOARD_H, rules=rules)
    reference = RoutingGrid(width=BOARD_W, height=BOARD_H, rules=rules)
    _replay(subject, routes, operations)
    _replay(reference, routes, operations)
    reference._rebuild_segment_index()

    mismatches = 0
    verdicts: list[list[Any]] = []
    for probe in probes:
        got = subject.validate_segment_clearance(probe, exclude_net=probe.net)
        want = reference.validate_segment_clearance(probe, exclude_net=probe.net)
        # ``repr`` on the float keeps full precision, so a dump diff is exact
        # rather than rounded.
        verdicts.append([bool(got[0]), repr(got[1]), repr(got[2])])
        same_verdict = got[0] == want[0]
        same_value = got[1] == want[1] or (got[1] != got[1] and want[1] != want[1])  # NaN
        if not (same_verdict and same_value):
            mismatches += 1
    return len(probes), mismatches, verdicts


def _location_is_path_dependent(
    routes: list[Route], operations: list[tuple[str, int]], probes: list[Segment]
) -> dict[str, int]:
    """Quantify how arbitrary ``violation_loc`` already is on THIS git state.

    ``validate_segment_clearance`` returns ``(is_valid, clearance, loc)``.
    ``loc`` is assigned by whichever violating candidate is visited *last*,
    in both the R-tree branch and the brute-force branch -- so it is a
    representative violation location, not a determined one, and the two
    branches disagree about it for the same board on unmodified code.

    This control makes that concrete: replay the trace once, then answer
    every probe twice on the SAME grid -- once through the R-tree branch,
    once with ``_rtree_available`` forced off so the brute-force branch runs
    -- and count how the two disagree.  Any ``verdict``/``clearance``
    disagreement would be a real bug (Issue #3522 pins those bit-identical);
    a ``location`` disagreement is the pre-existing arbitrariness.
    """
    rules = _make_rules()
    grid = RoutingGrid(width=BOARD_W, height=BOARD_H, rules=rules)
    _replay(grid, routes, operations)

    counts = {"compared": 0, "verdict_differs": 0, "clearance_differs": 0, "location_differs": 0}
    for probe in probes:
        indexed = grid.validate_segment_clearance(probe, exclude_net=probe.net)
        grid._rtree_available = False
        try:
            brute = grid.validate_segment_clearance(probe, exclude_net=probe.net)
        finally:
            grid._rtree_available = RTREE_AVAILABLE
        counts["compared"] += 1
        if indexed[0] != brute[0]:
            counts["verdict_differs"] += 1
        if indexed[1] != brute[1] and not (indexed[1] != indexed[1] and brute[1] != brute[1]):
            counts["clearance_differs"] += 1
        if indexed[2] != brute[2]:
            counts["location_differs"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--live-routes", type=int, default=300)
    parser.add_argument("--segments-per-route", type=int, default=5)
    parser.add_argument("--ripups", type=int, default=600)
    parser.add_argument("--probes", type=int, default=400)
    parser.add_argument("--seed", type=int, default=5240)
    parser.add_argument("--no-check", dest="check", action="store_false")
    parser.add_argument("--json", action="store_true", help="emit a JSON record")
    parser.add_argument(
        "--dump-verdicts",
        metavar="PATH",
        help=(
            "write every probe's (violation, clearance, location) answer as JSON. "
            "Run once per git state with the same seed and diff the two files to "
            "prove the change is answer-for-answer identical."
        ),
    )
    args = parser.parse_args()

    if not RTREE_AVAILABLE:
        print("rtree is not installed -- the indexed path cannot be measured.")
        return 2

    rng = random.Random(args.seed)
    routes, operations = _build_trace(rng, args.live_routes, args.ripups, args.segments_per_route)
    probes = _probe_segments(random.Random(args.seed + 1), args.probes)
    indexed_segments = args.live_routes * args.segments_per_route
    removals = args.ripups * args.segments_per_route

    control: tuple[int, int, list[list[Any]]] | None = None
    path_dependence: dict[str, int] | None = None
    if args.check:
        control = _exactness_control(routes, operations, probes)
        path_dependence = _location_is_path_dependent(routes, operations, probes)
        if args.dump_verdicts:
            with open(args.dump_verdicts, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "seed": args.seed,
                        "live_routes": args.live_routes,
                        "segments_per_route": args.segments_per_route,
                        "ripups": args.ripups,
                        "probes": args.probes,
                        "verdicts": control[2],
                        "indexed_vs_bruteforce": path_dependence,
                    },
                    handle,
                    indent=1,
                    sort_keys=True,
                )
                handle.write("\n")
    elif args.dump_verdicts:
        parser.error("--dump-verdicts requires the exactness control (drop --no-check)")

    full: list[float] = []
    index_only: list[float] = []
    for _ in range(args.trials):
        rules = _make_rules()
        grid = RoutingGrid(width=BOARD_W, height=BOARD_H, rules=rules)
        start = time.perf_counter()
        _replay(grid, routes, operations)
        full.append(time.perf_counter() - start)

        rules = _make_rules()
        grid = RoutingGrid(width=BOARD_W, height=BOARD_H, rules=rules)
        start = time.perf_counter()
        _replay_index_only(grid, routes, operations)
        index_only.append(time.perf_counter() - start)

    record: dict[str, Any] = {
        "trials": args.trials,
        "live_routes": args.live_routes,
        "segments_per_route": args.segments_per_route,
        "indexed_segments": indexed_segments,
        "ripup_cycles": args.ripups,
        "segment_removals": removals,
        "mark_unmark_median_s": statistics.median(full),
        "mark_unmark_trials_s": full,
        "index_only_median_s": statistics.median(index_only),
        "index_only_trials_s": index_only,
    }
    if control is not None:
        record["probes_compared"], record["probe_mismatches"] = control[0], control[1]
        record["indexed_vs_bruteforce"] = path_dependence

    if args.json:
        print(json.dumps(record, indent=2))
    else:
        print(f"indexed segments (steady state): {indexed_segments}")
        print(f"segment removals replayed:       {removals}")
        if control is not None:
            print(f"exactness control:               {control[1]} mismatch(es) / {control[0]}")
        if path_dependence is not None:
            print(f"indexed vs brute-force branch:   {path_dependence}")
        print(f"mark/unmark  median: {record['mark_unmark_median_s']:.6f} s   {full}")
        print(f"index-only   median: {record['index_only_median_s']:.6f} s   {index_only}")

    if control is not None and control[1]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

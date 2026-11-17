#!/usr/bin/env python3
"""Benchmark the stitch pass's foreign-net track clearance scans (Issue #5240).

Phase 10 of the board recipes (``pad-aware post-route plane stitching``) is one
of the two dominant phases of the Diff-Pair regression job's ``Re-route board +
check diff-pair coverage`` step and of Board 06 E2E's ``Regenerate board`` step.
Issue #5617's profiler attributed the fill-geometry half of that phase, which
#5637 indexed.  What is left is the *track* half: every candidate via position
tried by

* :func:`kicad_tools.cli.stitch_cmd.calculate_via_position`
* :func:`~kicad_tools.cli.stitch_cmd.calculate_dogleg_via_position`
* :func:`~kicad_tools.cli.stitch_cmd.calculate_extended_escape_position`

walks **every** foreign-net track segment on the board -- up to 24 candidate
positions per pad for the straight ladder alone, and hundreds more for the
dog-leg / extended-escape ladders, each rescanning the whole pool.

Issue #5240 bins those segments into a uniform grid
(:class:`kicad_tools.router.track_index.TrackSpatialIndex`), a pure superset
pre-filter in front of the unchanged exact distance tests.

This script measures the effect **on real board geometry** without paying for a
~17-minute re-route: it reads the obstacle pools straight out of a routed PCB,
exactly as :func:`~kicad_tools.cli.stitch_cmd.run_stitch` assembles them, and
replays the straight-ladder placement for every pad on the target nets.

Both arms run back-to-back in one process on identical inputs, interleaved
across trials so host load drifts hit both arms, and the script **asserts the
two arms return identical placements** -- a speed number can never be reported
for a changed placement.  The linear arm is obtained by monkeypatching
``stitch_cmd.build_track_index`` to return ``None``, which is precisely the
"no index -- keep the original full scan" branch every call site retains.

Usage::

    uv run python scripts/research/bench_stitch_track_index.py \\
        boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb

Wall-clock numbers are host- and load-dependent; the per-trial spread is
printed so a noisy host is visible rather than hidden behind a mean.  The
reported scan counts are exact and host-independent.
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

DEFAULT_PCB = Path("boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb")
DEFAULT_NETS = ("GND", "VBUS_USB")


class World:
    """The obstacle pools ``run_stitch`` builds for one stitch invocation."""

    def __init__(self, pcb: Path, nets: list[str]) -> None:
        sexp = load_pcb(pcb)
        net_map = stitch_cmd.get_net_map(sexp)
        net_numbers = {num for num, name in net_map.items() if name in nets}
        self.tracks = stitch_cmd.find_all_track_segments(sexp, exclude_nets=net_numbers)
        self.vias = stitch_cmd.find_all_board_vias(sexp, exclude_nets=net_numbers)
        self.pads = stitch_cmd.find_all_pads(sexp, exclude_nets=net_numbers)
        self.polys = stitch_cmd.find_all_filled_polygons(sexp, exclude_nets=net_numbers)
        self.bboxes = stitch_cmd.find_all_pad_bboxes(sexp, exclude_nets=net_numbers)
        self.drills = stitch_cmd.find_all_drills(sexp, exclude_nets=net_numbers)
        self.same_net_polys = [
            fp for fp in stitch_cmd.find_all_filled_polygons(sexp) if fp.net_name in nets
        ]
        self.targets = stitch_cmd.find_pads_on_nets(sexp, set(nets))


def _place_all(world: World, *, indexed: bool) -> tuple[list, float]:
    """Run the straight ladder for every target pad; return (answers, seconds).

    ``indexed=False`` restores the pre-#5240 behaviour by making
    ``build_track_index`` return ``None`` at every call site.
    """
    real_build = stitch_cmd.build_track_index
    shared = None
    if indexed:
        # run_stitch builds the pre-existing foreign-net pool's index once and
        # shares it across pads; mirror that here rather than charging one
        # build per pad.
        shared = real_build(world.tracks)
    else:
        stitch_cmd.build_track_index = lambda *a, **k: None  # type: ignore[assignment]
    try:
        answers: list = []
        started = time.perf_counter()
        for pad in world.targets:
            answers.append(
                stitch_cmd.calculate_via_position(
                    pad,
                    offset=0.5,
                    via_size=0.45,
                    existing_vias=[],
                    clearance=0.2,
                    other_net_tracks=world.tracks,
                    other_net_vias=world.vias,
                    other_net_pads=world.pads,
                    trace_width=0.2,
                    other_net_filled_polygons=world.polys,
                    other_net_pad_bboxes=world.bboxes,
                    other_net_drills=world.drills,
                    via_drill=0.2,
                    same_net_filled_polygons=world.same_net_polys,
                    same_net_fill_layer="B.Cu",
                    other_net_track_index=shared,
                )
            )
        elapsed = time.perf_counter() - started
        return answers, elapsed
    finally:
        stitch_cmd.build_track_index = real_build  # type: ignore[assignment]


def _count_segment_visits(world: World, *, indexed: bool) -> int:
    """Exact, host-independent count of ``point_to_segment_distance`` calls.

    The count covers both the track scans this change indexes and the
    fill-edge checks #5637 already indexed; the fill-edge index is active in
    *both* arms, so the difference between the arms is the track work.
    """
    visits = 0
    real = stitch_cmd.point_to_segment_distance

    def counting(*args: float) -> float:
        nonlocal visits
        visits += 1
        return real(*args)

    stitch_cmd.point_to_segment_distance = counting  # type: ignore[assignment]
    try:
        _place_all(world, indexed=indexed)
    finally:
        stitch_cmd.point_to_segment_distance = real  # type: ignore[assignment]
    return visits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "pcb",
        type=Path,
        nargs="?",
        default=REPO_ROOT / DEFAULT_PCB,
        help="A routed PCB with plane-net pads (default: board 06's fixture)",
    )
    parser.add_argument(
        "--net",
        action="append",
        dest="nets",
        help=f"Stitch target net (repeatable; default: {' '.join(DEFAULT_NETS)})",
    )
    parser.add_argument("--trials", type=int, default=3, help="Interleaved A/B trials (default 3)")
    parser.add_argument(
        "--pads",
        type=int,
        default=0,
        help="Limit to the first N target pads (0 = all; useful on a loaded host)",
    )
    parser.add_argument(
        "--count-visits",
        action="store_true",
        help=(
            "Also report the exact number of point_to_segment_distance calls "
            "each arm issues (adds one traced run per arm)"
        ),
    )
    args = parser.parse_args(argv)

    nets = list(args.nets or DEFAULT_NETS)
    world = World(args.pcb, nets)
    if args.pads:
        world.targets = world.targets[: args.pads]
    if not world.targets:
        print(f"error: {args.pcb} has no pads on {nets}", file=sys.stderr)
        return 2

    print(f"pcb                : {args.pcb}")
    print(f"stitch nets        : {', '.join(nets)}")
    print(f"target pads        : {len(world.targets)}")
    print(f"foreign-net tracks : {len(world.tracks)}")
    print(f"foreign-net fills  : {len(world.polys)}")

    # Warm both arms so neither pays a one-off import/alloc cost.
    _place_all(world, indexed=False)
    _place_all(world, indexed=True)

    linear_times: list[float] = []
    indexed_times: list[float] = []
    linear_answers: list | None = None
    indexed_answers: list | None = None
    for _ in range(max(1, args.trials)):
        linear_answers, elapsed = _place_all(world, indexed=False)
        linear_times.append(elapsed)
        indexed_answers, elapsed = _place_all(world, indexed=True)
        indexed_times.append(elapsed)

    assert linear_answers is not None and indexed_answers is not None
    if linear_answers != indexed_answers:
        differing = sum(1 for a, b in zip(linear_answers, indexed_answers, strict=True) if a != b)
        print(f"FAIL: indexed placements differ from linear for {differing} pads", file=sys.stderr)
        return 1

    def _median(values: list[float]) -> float:
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2

    linear_median = _median(linear_times)
    indexed_median = _median(indexed_times)
    print(f"linear  trials (s) : {', '.join(f'{t:.3f}' for t in sorted(linear_times))}")
    print(f"indexed trials (s) : {', '.join(f'{t:.3f}' for t in sorted(indexed_times))}")
    print(f"linear  median     : {linear_median:8.3f}s")
    print(f"indexed median     : {indexed_median:8.3f}s")
    if indexed_median > 0:
        print(f"speedup            : {linear_median / indexed_median:8.2f}x")
    placed = sum(1 for a in linear_answers if a is not None)
    print(f"placements identical: {len(linear_answers)}/{len(linear_answers)} ({placed} placed)")

    if args.count_visits:
        linear_visits = _count_segment_visits(world, indexed=False)
        indexed_visits = _count_segment_visits(world, indexed=True)
        print(f"point_to_segment_distance, linear  : {linear_visits}")
        print(f"point_to_segment_distance, indexed : {indexed_visits}")
        if indexed_visits:
            print(f"reduction                       : {linear_visits / indexed_visits:.1f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

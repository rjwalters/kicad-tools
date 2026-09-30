#!/usr/bin/env python3
"""Measure the optimizer's per-cell obstacle scan inside a *real* ``kct route``.

Issue #5240.  ``VectorCollisionChecker._check_obstacles_clear`` is the
narrow-phase "does this candidate trace cross pad copper?" scan the post-route
trace optimizer runs for every candidate segment it considers.  It walks
``O(segment_length * clearance_cells)`` grid cells per candidate in pure
Python, so its cost scales with the product of board size, grid resolution and
optimizer candidate count -- exactly the three things the slow CI jobs
maximise.

``scripts/research/bench_collision_obstacle_scan.py`` A/Bs that scan in
isolation; this script answers the prior question -- *how much of a real
route's wall clock does the scan actually consume?* -- by wrapping the two
optimizer entry points and accumulating call counts plus inclusive wall time
around a genuine route:

* ``VectorCollisionChecker.path_is_clear``           -- the optimizer's query
* ``VectorCollisionChecker._check_obstacles_clear``  -- the per-cell scan
* ``GridCollisionChecker.path_is_clear``             -- the non-R-tree fallback

Two ways to A/B it, both from a single checkout:

``--arm cell_at`` swaps ``_check_obstacles_clear`` for the reference
transcription of the pre-#5240 ``cell_at`` + ``_CellView`` loop that
``scripts/research/bench_collision_obstacle_scan.py`` and
``tests/router/test_collision_obstacle_scan_parity_5240.py`` both certify as
verdict-identical, so both arms run against the *same* tree, the same native
extension build and the same interpreter.  That matters: a stale
``router_cpp*.so`` changes the route's wall clock (and, in the wrong
direction, whether route-shaped tests even pass), so an A/B across two git
states silently mixes in whatever else moved between them.

    for arm in cell_at production; do
      uv run python scripts/research/profile_route_obstacle_scan.py \\
          boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb \\
          --arm "$arm" --out "/tmp/b02-$arm.kicad_pcb" \\
          --stats "/tmp/b02-$arm.json" --label "$arm" \\
          --route-arg=--skip-drc --route-arg=-q
    done

The routed PCBs the two arms write are directly comparable: they must be
identical once the randomly-generated UUIDs are normalised
(``sed -E 's/[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}/UUID/g'``).  That is
the end-to-end exact-output control for the whole route+optimize pipeline,
not just for the scan.

``--arm production`` alone also works as a plain per-git-state measurement.

**Install ``rtree`` first** (``uv sync --extra dev``, which every CI job
does).  Without it ``make_collision_checker`` never selects
``VectorCollisionChecker`` at all, the scan under measurement is never
reached, and the run reports an all-zero measurement of the wrong code path.
The script prints ``rtree_available`` in its dump so that mistake is visible
rather than silent.

Stats are written to ``--stats`` every ``--interval`` seconds and once more at
exit, so a run killed part-way still reports the cost accumulated over a
genuine prefix of the route (with ``route_wall_seconds`` as the denominator).
A prefix of a route is a prefix, not a whole route: use a partial dump to size
the bottleneck, not to claim an end-to-end speedup.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import pathlib
import sys
import threading
import time
from typing import Any

from kicad_tools.router.optimizer import collision as collision_mod

STATS: dict[str, Any] = {
    "vector_path_is_clear_calls": 0,
    "vector_path_is_clear_seconds": 0.0,
    "check_obstacles_clear_calls": 0,
    "check_obstacles_clear_seconds": 0.0,
    "grid_path_is_clear_calls": 0,
    "grid_path_is_clear_seconds": 0.0,
}

_WALL0 = time.perf_counter()
_ARM_KIND = "production"


def _install_cell_at_arm() -> None:
    """Replace ``_check_obstacles_clear`` with the pre-#5240 ``cell_at`` loop.

    Imported from the isolated bench rather than duplicated here so the two
    harnesses can never drift apart (and so the parity test, which pins an
    identical transcription, keeps certifying the arm this script measures).
    """
    global _ARM_KIND

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from bench_collision_obstacle_scan import _reference_scan

    collision_mod.VectorCollisionChecker._check_obstacles_clear = (  # type: ignore[method-assign]
        _reference_scan
    )
    _ARM_KIND = "cell_at"


def _install_probes() -> None:
    """Wrap the three collision entry points with inclusive-time accounting.

    ``_check_obstacles_clear`` is called *from* ``path_is_clear``, so its
    seconds are a subset of ``vector_path_is_clear_seconds`` -- the two are
    deliberately not additive.
    """
    vector_cls = collision_mod.VectorCollisionChecker
    grid_cls = collision_mod.GridCollisionChecker

    orig_vector_clear = vector_cls.path_is_clear

    def vector_path_is_clear(self: Any, *a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return orig_vector_clear(self, *a, **kw)
        finally:
            STATS["vector_path_is_clear_calls"] += 1
            STATS["vector_path_is_clear_seconds"] += time.perf_counter() - t0

    vector_cls.path_is_clear = vector_path_is_clear  # type: ignore[method-assign]

    orig_obstacles = vector_cls._check_obstacles_clear

    def check_obstacles_clear(self: Any, *a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return orig_obstacles(self, *a, **kw)
        finally:
            STATS["check_obstacles_clear_calls"] += 1
            STATS["check_obstacles_clear_seconds"] += time.perf_counter() - t0

    vector_cls._check_obstacles_clear = check_obstacles_clear  # type: ignore[method-assign]

    orig_grid_clear = grid_cls.path_is_clear

    def grid_path_is_clear(self: Any, *a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return orig_grid_clear(self, *a, **kw)
        finally:
            STATS["grid_path_is_clear_calls"] += 1
            STATS["grid_path_is_clear_seconds"] += time.perf_counter() - t0

    grid_cls.path_is_clear = grid_path_is_clear  # type: ignore[method-assign]


def _rtree_available() -> bool:
    try:
        import rtree  # noqa: F401
    except Exception:
        return False
    return True


def _snapshot(label: str, exit_code: int | None = None) -> dict[str, Any]:
    record = dict(STATS)
    record["arm"] = label
    record["route_wall_seconds"] = time.perf_counter() - _WALL0
    record["rtree_available"] = _rtree_available()
    record["arm_kind"] = _ARM_KIND
    wall = record["route_wall_seconds"] or 1.0
    record["check_obstacles_share_of_wall"] = record["check_obstacles_clear_seconds"] / wall
    record["vector_path_is_clear_share_of_wall"] = record["vector_path_is_clear_seconds"] / wall
    calls = record["check_obstacles_clear_calls"] or 1
    record["check_obstacles_mean_micros"] = record["check_obstacles_clear_seconds"] / calls * 1e6
    if exit_code is not None:
        record["exit_code"] = exit_code
    return record


def _write(path: str, label: str, exit_code: int | None = None) -> None:
    record = _snapshot(label, exit_code)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=1, sort_keys=True)
        handle.write("\n")
    # Atomic-ish replace so a concurrent reader never sees a half file.
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcb", help="input .kicad_pcb to route")
    parser.add_argument("--out", required=True, help="routed output .kicad_pcb path")
    parser.add_argument("--stats", required=True, help="path for the periodic JSON dump")
    parser.add_argument("--label", default="unlabelled", help="arm label recorded in the dump")
    parser.add_argument(
        "--arm",
        choices=("production", "cell_at"),
        default="production",
        help="which obstacle scan to run: the checked-out one, or the "
        "pre-#5240 cell_at reference transcription",
    )
    parser.add_argument("--interval", type=float, default=30.0, help="dump period, seconds")
    parser.add_argument(
        "--route-arg",
        action="append",
        default=[],
        help="extra argument forwarded verbatim to `kct route` (repeatable)",
    )
    args = parser.parse_args()

    # Swap the arm BEFORE wrapping, so the probe measures the arm.
    if args.arm == "cell_at":
        _install_cell_at_arm()
    _install_probes()

    stop = threading.Event()

    def pump() -> None:
        while not stop.wait(args.interval):
            _write(args.stats, args.label)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    atexit.register(_write, args.stats, args.label)

    # Import after the probes are installed so the CLI's own lazy imports
    # resolve to the wrapped methods.
    from kicad_tools.cli import main as cli_main

    argv = ["route", args.pcb, "-o", args.out, *args.route_arg]
    rc = 0
    try:
        rc = int(cli_main(argv) or 0)
    except SystemExit as exc:
        rc = int(exc.code or 0)
    finally:
        stop.set()

    _write(args.stats, args.label, rc)
    print("I5240_SCAN_PROBE " + json.dumps(_snapshot(args.label, rc), sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

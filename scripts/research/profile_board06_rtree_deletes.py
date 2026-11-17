#!/usr/bin/env python3
"""Measure segment-index mutation cost inside a *real* board-06 route.

Issue #5240.  ``scripts/research/bench_rtree_ripup.py`` A/Bs the segment
index in isolation; this script answers the prior question -- *how much of a
real route's wall clock does that index mutation actually consume?* -- by
instrumenting the exact command the CI "Re-route + coverage" step drives:

    python boards/06-diffpair-test/generate_design.py <outdir> --step route --seed 42

It wraps three call sites and accumulates call counts plus inclusive wall
time:

* ``rtree.index.Index.delete``          -- the eager-delete cost being removed
* ``RoutingGrid._rtree_remove_segment`` -- total segment-removal traffic
* ``RoutingGrid._compact_segment_index`` -- tombstone rebuild cost (absent on
  the pre-#5240 baseline, in which case it is simply not wrapped)

A full board-06 route runs far longer than a convenient measurement window,
so the stats are written to ``--stats <path>`` every ``--interval`` seconds
(and once more at exit).  That makes a **bounded** run informative: kill it
after N minutes and the last dump still reports the accumulated cost over a
genuine prefix of the route, with ``route_wall_seconds`` as the denominator.

    PYTHONHASHSEED=42 uv run python \
        scripts/research/profile_board06_rtree_deletes.py \
        /tmp/probe-out --stats /tmp/probe-stats.json --label baseline

``PYTHONHASHSEED=42`` is **mandatory**, not decoration: board 06's
``_reexec_with_pinned_hash_seed`` (Issue #4536) calls ``os.execv`` when the
variable is unset, which replaces the process image and silently discards
every probe installed here -- the run then completes having measured nothing.
The script asserts the variable up front rather than letting that happen.
The route also needs ``--step pcb`` to have been run into ``outdir`` first.

A prefix of a route is a prefix, not a whole route: use this to size the
bottleneck, not to claim an end-to-end speedup.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import runpy
import sys
import threading
import time
from typing import Any

import rtree.index as rtree_index

from kicad_tools.router import grid as grid_mod

BOARD_SCRIPT = "boards/06-diffpair-test/generate_design.py"

STATS: dict[str, Any] = {
    "delete_calls": 0,
    "delete_seconds": 0.0,
    "remove_segment_calls": 0,
    "remove_segment_seconds": 0.0,
    "compact_calls": 0,
    "compact_seconds": 0.0,
    "index_ctor_calls": 0,
    "index_ctor_seconds": 0.0,
}

_WALL0 = time.perf_counter()


def _install_probes() -> None:
    orig_delete = rtree_index.Index.delete

    def delete(self: Any, *a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return orig_delete(self, *a, **kw)
        finally:
            STATS["delete_calls"] += 1
            STATS["delete_seconds"] += time.perf_counter() - t0

    rtree_index.Index.delete = delete  # type: ignore[method-assign]

    orig_ctor = rtree_index.Index.__init__

    def ctor(self: Any, *a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return orig_ctor(self, *a, **kw)
        finally:
            STATS["index_ctor_calls"] += 1
            STATS["index_ctor_seconds"] += time.perf_counter() - t0

    rtree_index.Index.__init__ = ctor  # type: ignore[method-assign]

    orig_remove = grid_mod.RoutingGrid._rtree_remove_segment

    def remove(self: Any, seg: Any, layer_idx: int) -> Any:
        t0 = time.perf_counter()
        try:
            return orig_remove(self, seg, layer_idx)
        finally:
            STATS["remove_segment_calls"] += 1
            STATS["remove_segment_seconds"] += time.perf_counter() - t0

    grid_mod.RoutingGrid._rtree_remove_segment = remove  # type: ignore[method-assign]

    # Absent on the pre-#5240 baseline; only the tombstone arm compacts.
    orig_compact = getattr(grid_mod.RoutingGrid, "_compact_segment_index", None)
    if orig_compact is not None:

        def compact(self: Any, layer_idx: int) -> Any:
            t0 = time.perf_counter()
            try:
                return orig_compact(self, layer_idx)
            finally:
                STATS["compact_calls"] += 1
                STATS["compact_seconds"] += time.perf_counter() - t0

        grid_mod.RoutingGrid._compact_segment_index = compact  # type: ignore[method-assign]


def _snapshot(label: str, exit_code: int | None = None) -> dict[str, Any]:
    record = dict(STATS)
    record["arm"] = label
    record["route_wall_seconds"] = time.perf_counter() - _WALL0
    record["compaction_available"] = hasattr(grid_mod.RoutingGrid, "_compact_segment_index")
    wall = record["route_wall_seconds"] or 1.0
    record["delete_share_of_wall"] = record["delete_seconds"] / wall
    record["index_mutation_share_of_wall"] = (
        record["remove_segment_seconds"] + record["compact_seconds"]
    ) / wall
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
    parser.add_argument("outdir", help="board generate_design.py output directory")
    parser.add_argument("--stats", required=True, help="path for the periodic JSON dump")
    parser.add_argument("--label", default="unlabelled", help="arm label recorded in the dump")
    parser.add_argument("--interval", type=float, default=30.0, help="dump period, seconds")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if os.environ.get("PYTHONHASHSEED") != "42":
        parser.error(
            "run with PYTHONHASHSEED=42 -- board 06 re-execs itself via os.execv to "
            "pin it (Issue #4536), which would discard every probe installed here "
            "and produce an all-zero measurement."
        )

    _install_probes()

    stop = threading.Event()

    def pump() -> None:
        while not stop.wait(args.interval):
            _write(args.stats, args.label)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    atexit.register(_write, args.stats, args.label)

    sys.argv = [BOARD_SCRIPT, args.outdir, "--step", "route", "--seed", str(args.seed)]
    rc = 0
    try:
        runpy.run_path(BOARD_SCRIPT, run_name="__main__")
    except SystemExit as exc:
        rc = int(exc.code or 0)
    finally:
        stop.set()

    _write(args.stats, args.label, rc)
    print("I5240_PROBE " + json.dumps(_snapshot(args.label, rc), sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

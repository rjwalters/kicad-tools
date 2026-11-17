#!/usr/bin/env python3
"""Benchmark the SExp tree-traversal rewrite (Issue #5240).

``SExp.iter_all`` / ``find`` / ``find_all`` sit underneath essentially every
board- or schematic-touching test, recipe step and ``kct check`` / LVS / DRC
invocation -- 287 call sites across ``schema/``, ``drc/``, ``zones/``,
``router/`` and ``lvs/``. Profiling 5x ``PCB.load()`` of the committed routed
board 03 fixture (``cProfile``, sorted by ``tottime``) attributed roughly a
sixth of the 19.76s total wall time to these three methods combined
(``find_all`` 1.33s / 1,795 calls, ``find`` 0.86s / 92,010 calls, ``iter_all``
0.52s / 361,180 calls), because the original implementation was a recursive
generator (``yield self; for child: yield from child.iter_all()``) that
allocates one generator frame per node per tree level, so a document with
real nesting depth pays O(depth) interpreter resumes per node instead of
O(1).

This script measures the effect on the realistic call pattern directly --
repeated ``PCB.load()`` of committed routed board fixtures -- rather than an
isolated synthetic traversal workload, since ``find``/``find_all``/
``iter_all`` are a minority of ``PCB.load()``'s own work (the S-expression
lexer dominates) and their relative call-count mix across schema parsing is
what actually matters.

Both arms run back-to-back in one process on identical inputs, interleaved
across trials so host load drifts hit both arms equally, and the script
**asserts both arms produce the same parsed board** (footprint/pad/net/
segment counts) -- a speed number can never be reported for a changed parse.
The "baseline" arm is obtained by monkeypatching ``SExp.iter_all`` / ``find``
/ ``find_all`` back to the pre-#5240 recursive implementation; the "patched"
arm is whatever is installed in the target ``src`` tree.

Usage::

    uv run python scripts/research/bench_sexp_traversal.py --trials 5

Wall-clock numbers are host- and load-dependent; report the median across
several interleaved trials, not a single measurement.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_FIXTURES = (
    "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb",
    "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb",
    "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb",
)


def _install_reference_arm(SExp: Any) -> dict[str, Any]:
    """Monkeypatch SExp.iter_all/find/find_all to the pre-#5240 recursive
    implementation. Returns the original (current) bound functions so the
    caller can restore them."""

    def ref_iter_all(self):
        yield self
        for child in self.children:
            yield from ref_iter_all(child)

    def ref_find(self, name, **attrs):
        for child in self.children:
            for node in ref_iter_all(child):
                if node.name == name:
                    if all(self._match_attr(node, k, v) for k, v in attrs.items()):
                        return node
        return None

    def ref_find_all(self, name, **attrs):
        results = []
        for child in self.children:
            for node in ref_iter_all(child):
                if node.name == name:
                    if all(self._match_attr(node, k, v) for k, v in attrs.items()):
                        results.append(node)
        return results

    originals = {
        "iter_all": SExp.iter_all,
        "find": SExp.find,
        "find_all": SExp.find_all,
    }
    SExp.iter_all = ref_iter_all
    SExp.find = ref_find
    SExp.find_all = ref_find_all
    return originals


def _restore_arm(SExp: Any, originals: dict[str, Any]) -> None:
    SExp.iter_all = originals["iter_all"]
    SExp.find = originals["find"]
    SExp.find_all = originals["find_all"]


def _digest(pcb: Any) -> tuple:
    """Structural fingerprint of a loaded PCB: counts, not object identity,
    since fresh objects are constructed on every load either way."""
    return (
        len(pcb.footprints),
        sum(len(fp.pads) for fp in pcb.footprints),
        len(pcb.segments),
        len(pcb.vias),
        len(pcb.zones),
    )


def _workload(PCB: Any, paths: list[Path]) -> tuple[float, list]:
    start = time.perf_counter()
    digests = [_digest(PCB.load(path)) for path in paths]
    elapsed = time.perf_counter() - start
    return elapsed, digests


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--fixture", action="append", default=None)
    args = ap.parse_args()

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp.parser import SExp

    fixtures = [Path(f) for f in (args.fixture or DEFAULT_FIXTURES)]
    paths = [f if f.is_absolute() else REPO_ROOT / f for f in fixtures]

    patched_totals: list[float] = []
    baseline_totals: list[float] = []
    patched_digests = None
    baseline_digests = None

    for _ in range(args.trials):
        # Patched arm first (whatever SExp currently has installed).
        elapsed, digests = _workload(PCB, paths)
        patched_totals.append(elapsed)
        patched_digests = digests

        # Baseline arm: swap in the pre-#5240 recursive implementation.
        originals = _install_reference_arm(SExp)
        try:
            elapsed, digests = _workload(PCB, paths)
        finally:
            _restore_arm(SExp, originals)
        baseline_totals.append(elapsed)
        baseline_digests = digests

    assert patched_digests == baseline_digests, (
        f"parsed boards disagree between arms: {patched_digests} != {baseline_digests}"
    )

    print(
        json.dumps(
            {
                "trials": args.trials,
                "fixtures": [str(f) for f in fixtures],
                "board_digests_footprints_pads_tracks_vias_zones": patched_digests,
                "median_patched_s": statistics.median(patched_totals),
                "median_baseline_s": statistics.median(baseline_totals),
                "patched_totals_s": patched_totals,
                "baseline_totals_s": baseline_totals,
                "speedup": statistics.median(baseline_totals) / statistics.median(patched_totals),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

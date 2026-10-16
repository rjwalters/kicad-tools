#!/usr/bin/env python3
"""A/B for the opt-in negotiated nogood cache (Issue #6353).

Arms (all share ``--seed``; only the cache differs):

``baseline``      default ``kct route`` recipe, cache off.
``nogood-full``   ``--nogood-cache`` with the whole-grid failure window
                  (every grid plane fingerprinted, drained failures only; the
                  arm that must be output-identical).
``nogood-m<N>``   ``--nogood-cache --nogood-cache-margin-cells N`` (bounded
                  window; heuristic that may false-hit, which shows up as a quality
                  delta).

Reuses ``net_order_experiment``'s board recipes, subprocess profiling and
referee (``measure_completion`` / ``measure_copper``), then adds the cache
diagnostics the CLI dumps at process exit via ``KCT_NOGOOD_CACHE_STATS_FILE``
and a SHA-256 of the routed copper listing so "identical output" is checkable.
Every row is recorded, including timeouts and errors.  Read A* calls and the
overflow trajectory before seconds: the host is shared.

Usage::

    uv run kct build-native --check
    uv run python scripts/research/nogood_cache_experiment.py \\
        --boards 00,01,02,03,06a --arms baseline,nogood-full,nogood-m20 \\
        --work-dir /tmp/nogood --json /tmp/nogood/summary.json --timeout 1500

Research-only; ``boards/`` is never written (inputs are copied by the runner).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import net_order_experiment as noe  # noqa: E402


def arm_env(arm: str) -> dict[str, str]:
    """Env that selects the arm.

    Env (not ``--nogood-cache`` via ``--extra``): the profiler's ``--extra``
    argparse option rejects values that start with ``--``.  The CLI flag is
    just sugar that sets these same variables (``route_cmd._main_impl``).
    """
    if arm == "baseline":
        return {}
    if arm == "nogood-full":
        return {"KCT_NOGOOD_CACHE": "1", "KCT_NOGOOD_CACHE_MARGIN_CELLS": "full"}
    m = re.fullmatch(r"nogood-m(\d+)", arm)
    if m:
        return {"KCT_NOGOOD_CACHE": "1", "KCT_NOGOOD_CACHE_MARGIN_CELLS": m.group(1)}
    raise SystemExit(f"unknown arm {arm!r}")


def copper_digest(routed: Path) -> str:
    """Hash segment/via/arc lines only (ignores timestamps/uuids)."""
    keep = []
    for line in routed.read_text().splitlines():
        t = line.strip()
        if t.startswith(("(segment", "(via", "(arc")):
            keep.append(re.sub(r"\(uuid [^)]*\)|\(tstamp [^)]*\)", "", t))
    return hashlib.sha256("\n".join(sorted(keep)).encode()).hexdigest()[:16]


def run_arm(board_key: str, arm: str, work: Path, seed: int, timeout: float) -> dict:
    name = f"nogood-{arm}"
    noe.VARIANTS[name] = ()
    for k in ("KCT_NOGOOD_CACHE", "KCT_NOGOOD_CACHE_MARGIN_CELLS"):
        os.environ.pop(k, None)
    os.environ.update(arm_env(arm))
    stats = work / "stats" / f"{board_key}-{arm}.jsonl"
    stats.parent.mkdir(parents=True, exist_ok=True)
    stats.unlink(missing_ok=True)
    os.environ["KCT_NOGOOD_CACHE_STATS_FILE"] = str(stats)
    try:
        row = noe.run_one(noe.BOARDS[board_key], name, work, seed, timeout)
    except subprocess.TimeoutExpired:
        row = {"board": board_key, "variant": name, "error": f"timeout after {timeout}s"}
    row["arm"] = arm
    if stats.exists():
        lines = [json.loads(x) for x in stats.read_text().splitlines() if x.strip()]
        agg: dict[str, int] = {}
        for d in lines:
            for k, v in d.items():
                agg[k] = agg.get(k, 0) + v
        row["nogood"] = agg
    routed = (
        work / f"{board_key}-{name}" / f"{Path(noe.BOARDS[board_key].src).stem}_routed.kicad_pcb"
    )
    if routed.exists():
        row["copper_sha"] = copper_digest(routed)
    return row


def render(rows: list[dict]) -> str:
    out = [
        "| Board | Arm | Nets | Conn | Vias | WL mm | Iters | A* calls | Loop s | Wall s "
        "| hit/miss/ins/inv/evict | Copper SHA |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        ng = r.get("nogood")
        ngs = (
            "/".join(
                str(ng.get(k, 0))
                for k in ("hits", "misses", "insertions", "invalidations", "evictions")
            )
            if ng
            else "--"
        )
        if "nets_complete" not in r:
            out.append(
                f"| {r['board']} | {r['arm']} | ERROR/TIMEOUT: {str(r.get('error', r.get('subprocess_rc')))[:60]!r} |||||||||| {ngs} |"
            )
            continue
        out.append(
            f"| {r['board']} | {r['arm']} | {r['nets_complete']}/{r['nets_total']} "
            f"| {r['connections_routed']}/{r['connections_total']} | {r['via_count']} "
            f"| {r['wirelength_mm']} | {r.get('negotiated_iterations', '--')} "
            f"| {r.get('astar_calls', '--')} | {r['routing_loop_s']:.1f} | {r['wall_s']:.1f} "
            f"| {ngs} | {r.get('copper_sha', '--')} |"
        )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--boards", default="00,01,02,03,06a")
    ap.add_argument("--arms", default="baseline,nogood-full,nogood-m20")
    ap.add_argument("--work-dir", type=Path, required=True)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--timeout", type=float, default=1500.0)
    args = ap.parse_args()
    rows = []
    for b in [x.strip() for x in args.boards.split(",") if x.strip()]:
        for arm in [x.strip() for x in args.arms.split(",") if x.strip()]:
            row = run_arm(b, arm, args.work_dir, args.seed, args.timeout)
            rows.append(row)
            print(render([row]).splitlines()[-1], flush=True)
            if args.json:
                args.json.parent.mkdir(parents=True, exist_ok=True)
                args.json.write_text(json.dumps(rows, indent=2) + "\n")
    print()
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

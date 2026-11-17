#!/usr/bin/env python3
"""Replay the router's own pairwise (HV-isolation) gate over a routed board.

This is the after-the-fact scorer for issue #4507's T4 criterion: it answers
"of the copper on this board, what does the ROUTER's gate say is bad?" -- which
is a different question from ``kct creepage``.  The census is net-pair keyed
and measures pads, vias and pour fills as well as traces, and it has no
concept of the #4506 rated-footprint attach-zone exemption.  The router's gate
(``board_pairwise_violations``) now covers trace-vs-trace, trace-vs-via,
via-vs-via and (unless ``--no-pad-geometry``) trace/via-vs-pad -- issue #4507
widened it past its original trace-vs-trace-only scope, which is what left 17
residual fails invisible on the softstart rev-C T4 proof board (all of them
trace/via-vs-pad or via-vs-trace) -- and honours the #4506 exemption
throughout, so only this replay can attribute a census fail to *routed
copper* versus placement/pour geometry.  Pour fills remain out of scope
(#3901).

Usage::

    uv run python scripts/replay_pairwise_gate.py ROUTED.kicad_pcb \\
        --voltage-map vmap.json [--dru 0.15] [--hv-threshold 30] \\
        [--no-attach-zones] [--no-pad-geometry] [--json]

The board file is read-only.  Exit code is 1 when the gate reports at least one
shortfall (with the #4506 exemption applied), 0 otherwise.

**Frame note.**  The copper, the attach zones AND the pad geometry are all
resolved through ``router.pairwise_clearance.board_pairwise_violations``,
which reads them from the same file in the same sheet-absolute frame.  Do NOT
hand-roll this with ``build_attach_zones(PCB.load(path).footprints)`` or
``PCB.load(path).footprints[i].pads``: ``PCB.load`` reports footprint (and pad)
positions *board-relative*, so unshifted geometry lands ``board_origin`` away
from the copper it is meant to describe and the replay mis-reports (that
mistake is what put two phantom "genuine router leaks" into the #4507 T4
proof).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kicad_tools.router.pairwise_clearance import (  # noqa: E402
    board_pairwise_violations,
    build_pairwise_clearance_table,
    load_signed_voltage_map,
    violation_pair_key,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="replay_pairwise_gate.py",
        description="Replay the router's pairwise HV gate over a routed board file.",
    )
    parser.add_argument("board", type=Path, help="routed .kicad_pcb to score")
    parser.add_argument(
        "--voltage-map",
        type=Path,
        required=True,
        help="signed per-net voltage map (the same sidecar the run used)",
    )
    parser.add_argument(
        "--dru",
        type=float,
        default=0.2,
        help="scalar DRU clearance floor in mm (default: 0.2); the run prints its own",
    )
    parser.add_argument("--standard", default="iec60664", help="creepage standard id")
    parser.add_argument("--pollution-degree", type=int, default=2)
    parser.add_argument("--material-group", default="IIIa")
    parser.add_argument(
        "--hv-threshold",
        type=float,
        default=30.0,
        help="minimum |dV| for a pair to be widened (default: 30, as the router)",
    )
    parser.add_argument(
        "--no-attach-zones",
        action="store_true",
        help="score without the #4506 rated-footprint exemption (the census' view)",
    )
    parser.add_argument(
        "--no-pad-geometry",
        action="store_true",
        help=(
            "score trace/via copper only, without foreign pad geometry -- the "
            "pre-#4507 scope, kept for A/B comparison against this widening"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    table = build_pairwise_clearance_table(
        load_signed_voltage_map(args.voltage_map),
        dru=args.dru,
        standard_id=args.standard,
        pollution_degree=args.pollution_degree,
        material_group=args.material_group,
        hv_threshold=args.hv_threshold,
    )
    zones = () if args.no_attach_zones else None
    pads = () if args.no_pad_geometry else None
    violations = board_pairwise_violations(args.board, table, attach_zones=zones, foreign_pads=pads)
    pair_keys = sorted({violation_pair_key(v) for v in violations})

    if args.json:
        print(
            json.dumps(
                {
                    "board": str(args.board),
                    "mapped_nets": len(table.net_voltages),
                    "cross_pairs": len(table.required_by_pair),
                    "attach_zones_applied": not args.no_attach_zones,
                    "pad_geometry_applied": not args.no_pad_geometry,
                    "violation_count": len(violations),
                    "net_pairs": [list(pair) for pair in pair_keys],
                    "violations": [
                        {
                            "net_a": v.net_a,
                            "net_b": v.net_b,
                            "actual_mm": round(v.actual_mm, 4),
                            "required_mm": round(v.required_mm, 4),
                            "x": round(v.x, 4),
                            "y": round(v.y, 4),
                        }
                        for v in violations
                    ],
                },
                indent=2,
            )
        )
    else:
        scope = "without" if args.no_attach_zones else "with"
        pad_scope = "trace/via only" if args.no_pad_geometry else "trace/via/pad"
        print(
            f"{args.board}: {len(table.net_voltages)} mapped nets, "
            f"{len(table.required_by_pair)} cross-pairs, DRU floor {args.dru:.3f} mm"
        )
        print(
            f"  pairwise violations ({pad_scope}, {scope} #4506 attach zones): "
            f"{len(violations)}  ({len(pair_keys)} net pairs)"
        )
        seen: set[tuple[str, str]] = set()
        for v in sorted(violations, key=lambda v: v.actual_mm):
            key = violation_pair_key(v)
            if key in seen:
                continue
            seen.add(key)
            print(
                f"    {v.net_a} <-> {v.net_b}: {v.actual_mm:.3f} mm against "
                f"{v.required_mm:.3f} mm at ({v.x:.3f}, {v.y:.3f})"
            )

    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())

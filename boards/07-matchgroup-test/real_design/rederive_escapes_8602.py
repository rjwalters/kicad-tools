#!/usr/bin/env python3
"""Re-derive board 07's authored escape copper after the verified prune (Issue #8602).

``kct pcb dedupe`` + the DRC-verified ``kct pcb prune`` merged collinear runs in
``reviewed-routing/sdram_demo.kicad_pcb``, so the authored escape segment
identities that ``validate.py`` matches (uuid, net, layer, endpoints, width)
no longer existed in the reviewed copper.  The reviewed routing is a superset
of ``authored-source/sdram_demo.kicad_pcb`` (see ``repair_class_clearance_6396.py``),
so the authored source's copper is re-derived from the pruned reviewed copper:

    owned segment = a pruned reviewed segment that lies entirely inside the
                    union of the *pre-prune* authored segments of the same
                    net, layer and width.

Trunk copper (not covered by authored copper) is never claimed by the
authored source.  Everything except the ``(segment ...)`` blocks of the
authored file is kept byte for byte.

Usage (rewrites ``authored-source/sdram_demo.kicad_pcb`` in place)::

    uv run python boards/07-matchgroup-test/real_design/rederive_escapes_8602.py \\
        --pre-prune-authored <git-show of the pre-prune authored file>

Afterwards re-pin ``authored-source/manifest.json`` and
``reviewed-routing/manifest.json`` and rerun ``build.py``.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from kicad_tools.schema.pcb import PCB

ROOT = Path(__file__).resolve().parent
TOL = 1e-4


def _covered(seg, others) -> bool:
    """True when ``seg`` lies inside the union of collinear ``others``."""
    (x0, y0), (x1, y1) = seg.start, seg.end
    length = math.hypot(x1 - x0, y1 - y0)
    if length < TOL:
        return any(
            math.hypot(o.start[0] - x0, o.start[1] - y0) < TOL
            or math.hypot(o.end[0] - x0, o.end[1] - y0) < TOL
            for o in others
        )
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    spans = []
    for o in others:
        ts = []
        for px, py in (o.start, o.end):
            if abs((px - x0) * uy - (py - y0) * ux) > TOL:
                break
            ts.append((px - x0) * ux + (py - y0) * uy)
        else:
            spans.append((min(ts), max(ts)))
    reach = 0.0
    for lo, hi in sorted(spans):
        if lo > reach + TOL:
            break
        reach = max(reach, hi)
    return reach >= length - TOL


def owned_segments(pre_authored: PCB, pruned: PCB):
    index: dict[tuple, list] = {}
    for s in pre_authored.segments:
        index.setdefault((s.net_name, s.layer, round(s.width, 6)), []).append(s)
    return [
        s
        for s in pruned.segments
        if _covered(s, index.get((s.net_name, s.layer, round(s.width, 6)), []))
    ]


def block(s, origin=(0.0, 0.0)) -> str:
    ox, oy = origin

    def f(v: float) -> str:
        return f"{round(v, 6):.6f}".rstrip("0").rstrip(".")

    return (
        "\t(segment\n"
        f"\t\t(start {f(s.start[0] + ox)} {f(s.start[1] + oy)})\n"
        f"\t\t(end {f(s.end[0] + ox)} {f(s.end[1] + oy)})\n"
        f"\t\t(width {f(s.width)})\n"
        f'\t\t(layer "{s.layer}")\n'
        f'\t\t(net "{s.net_name}")\n'
        f'\t\t(uuid "{s.uuid}")\n'
        "\t)\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pre-prune-authored", type=Path, required=True)
    args = parser.parse_args()
    target = ROOT / "authored-source/sdram_demo.kicad_pcb"
    pre = PCB.load(str(args.pre_prune_authored))
    pruned = PCB.load(str(ROOT / "reviewed-routing/sdram_demo.kicad_pcb"))
    owned = owned_segments(pre, pruned)
    lines = args.pre_prune_authored.read_text(encoding="utf-8").split("\n")
    out, i, first = [], 0, None
    while i < len(lines):
        if lines[i] == "\t(segment":
            if first is None:
                first = len(out)
            while lines[i] != "\t)":
                i += 1
            i += 1
            continue
        out.append(lines[i])
        i += 1
    out[first:first] = "".join(block(s, pre.board_origin) for s in owned).rstrip("\n").split("\n")
    target.write_text("\n".join(out), encoding="utf-8")
    print(f"authored segments {len(pre.segments)} -> {len(owned)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

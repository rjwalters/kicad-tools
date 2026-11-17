"""Write tscircuit-autorouter traces back into a normalized ``.kicad_pcb`` (Issue #5848).

The autorouter consumes/produces ``SimpleRouteJson`` (SRJ). SRJ coordinates
come from ``kicad-to-circuit-json``: y is flipped and the board is centred on
its bounding box, so ``kicad = (x + dx, -y + dy)``. ``dx, dy`` are fitted from
the SRJ obstacle centres against the board's pad positions (median residual
of nearest-neighbour matches), not assumed.

Each SRJ connection is mapped to a KiCad net by snapping its first port to the
nearest pad. Wires/vias are written with the *SRJ's* width and a default via
size, which is exactly the information SRJ carries (see the evaluation doc).

Usage:
    uv run python benchmarks/external/tscircuit/srj_to_kicad.py \
        BOARD.kicad_pcb SRJ.json AUTOROUTER_OUT.json OUT.kicad_pcb
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from kicad_tools.schema.pcb import PCB

# The autorouter's own default via diameter is 0.3 mm (`viaDiameter ?? 0.3` in
# @tscircuit/capacity-autorouter) and SRJ carries no via drill, so the drill is
# our choice: 0.15 mm gives the 0.075 mm annular ring the 0.3 mm pad leaves.
VIA_DIAMETER = 0.3
VIA_DRILL = 0.15


class _Layers(dict):
    """SRJ layer names -> KiCad copper layers (``inner1`` -> ``In1.Cu``)."""

    def __missing__(self, key):
        if key.startswith("inner") and key[5:].isdigit():
            return f"In{key[5:]}.Cu"
        raise KeyError(key)


LAYER = _Layers({"top": "F.Cu", "bottom": "B.Cu"})


def fit_offset(pads, obstacles):
    # coarse: vote on rounded offsets, then refine by median residual
    import collections

    votes = collections.Counter()
    for ox, oy in obstacles[:60]:
        for px, py, _ in pads:
            votes[(round(px - ox, 1), round(py + oy, 1))] += 1
    (dx, dy), _ = votes.most_common(1)[0]
    for _ in range(3):
        rx, ry = [], []
        for ox, oy in obstacles:
            tx, ty = ox + dx, -oy + dy
            p = min(pads, key=lambda q: (q[0] - tx) ** 2 + (q[1] - ty) ** 2)
            if (p[0] - tx) ** 2 + (p[1] - ty) ** 2 < 0.25:
                rx.append(p[0] - ox)
                ry.append(p[1] + oy)
        dx, dy = statistics.median(rx), statistics.median(ry)
    return dx, dy, len(rx)


def main(board, srj_path, out_path, dest):
    pcb = PCB.load(board)
    srj = json.loads(Path(srj_path).read_text())
    res = json.loads(Path(out_path).read_text())
    pads = []
    for fp in pcb.footprints:
        for p in fp.pads:
            pos = pcb.get_pad_position(fp.reference, p.number)
            if pos:
                pads.append((pos[0], pos[1], p.net_name))
    obstacles = [(o["center"]["x"], o["center"]["y"]) for o in srj["obstacles"]]
    dx, dy, matched = fit_offset(pads, obstacles)
    print(f"fit dx={dx:.4f} dy={dy:.4f} matched {matched}/{len(obstacles)} obstacles")

    conn_net = {}
    for c in srj["connections"]:
        pt = c["pointsToConnect"][0]
        x, y = pt["x"] + dx, -pt["y"] + dy
        p = min(pads, key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)
        conn_net[c["name"]] = p[2] or None

    nsegs = nvias = 0
    for tr in res["traces"]:
        net = conn_net.get(tr.get("connection_name")) or None
        route = tr["route"]
        for a, b in zip(route, route[1:], strict=False):
            if a["route_type"] == "wire" and b["route_type"] == "wire" and a["layer"] == b["layer"]:
                pcb.add_trace(
                    (a["x"] + dx, -a["y"] + dy),
                    (b["x"] + dx, -b["y"] + dy),
                    width=a.get("width", 0.1),
                    layer=LAYER[a["layer"]],
                    net=net,
                    dedupe=False,
                )
                nsegs += 1
        for r in route:
            if r["route_type"] == "via":
                pcb.add_via(
                    r["x"] + dx,
                    -r["y"] + dy,
                    size=VIA_DIAMETER,
                    drill=VIA_DRILL,
                    net=net,
                    dedupe=False,
                )
                nvias += 1
    pcb.save(dest)
    print(f"wrote {nsegs} segments, {nvias} vias -> {dest}")


if __name__ == "__main__":
    main(*sys.argv[1:5])

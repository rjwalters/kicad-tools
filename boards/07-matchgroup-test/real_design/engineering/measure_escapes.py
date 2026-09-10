"""Measure source bus escape spacing; this is not a transient SI validator."""

import json
import math
from pathlib import Path

from shapely.geometry import LineString

from kicad_tools.schema.pcb import PCB

ROOT = Path(__file__).resolve().parents[1]
board = PCB.load(ROOT / "authored-source/sdram_demo.kicad_pcb")
rules = json.loads((ROOT / "authored-source/net_class_map.json").read_text())
bus = {name for name, rule in rules.items() if rule.get("length_match_group")}
data = {name for name in bus if name.startswith("DQ") or name in ["LDQM", "UDQM"]}


def key(point):
    return tuple(round(v, 5) for v in point)


paths = []
for fp in board.footprints:
    for pad in fp.pads:
        if pad.net_name not in bus:
            continue
        edges = [s for s in board.segments if s.net_name == pad.net_name and s.layer == "F.Cu"]
        point = key(board.get_pad_position(fp.reference, pad.number))
        endpoints = [key(q) for s in edges for q in [s.start, s.end]]
        nearest = min(endpoints, key=lambda q: math.dist(q, point))
        if math.dist(nearest, point) > 0.001:
            raise ValueError(f"Missing pad attachment: {fp.reference}.{pad.number}")
        point = nearest
        points = [point]
        visited = set()
        while True:
            available = [
                (i, s)
                for i, s in enumerate(edges)
                if i not in visited and point in [key(s.start), key(s.end)]
            ]
            if not available:
                break
            if len(available) != 1:
                raise ValueError(f"Branched source escape: {fp.reference}.{pad.number}")
            i, segment = available[0]
            visited.add(i)
            point = key(segment.end) if point == key(segment.start) else key(segment.start)
            points.append(point)
        if len(points) < 2:
            raise ValueError(f"Missing source escape: {fp.reference}.{pad.number}")
        paths.append({"ref": fp.reference, "pin": pad.number, "net": pad.net_name, "path": points})
pairs = []
for i, a in enumerate(paths):
    for b in paths[i + 1 :]:
        if a["net"] == b["net"]:
            continue
        mixed = (a["net"] in data) != (b["net"] in data)
        clock = "SDCLK" in [a["net"], b["net"]]
        if not (mixed or clock):
            continue
        gap = LineString(a["path"]).distance(LineString(b["path"])) - 0.18
        if gap < (0.54 if clock else 5):
            pairs.append(
                {
                    "a": a["ref"] + "." + a["pin"] + ":" + a["net"],
                    "b": b["ref"] + "." + b["pin"] + ":" + b["net"],
                    "edge_gap_mm": round(gap, 6),
                    "clock": clock,
                    "mixed_group": mixed,
                }
            )
report = {
    "scope": "Authored front-side package escape paths only; this measures deviations, not a transient SI pass.",
    "max_escape_length_mm": max(LineString(p["path"]).length for p in paths),
    "clock_escapes": [
        {"ref": p["ref"], "length_mm": LineString(p["path"]).length}
        for p in paths
        if p["net"] == "SDCLK"
    ],
    "spacing_deviations": pairs,
}
Path(__file__).with_name("package-escape-spacing.json").write_text(
    json.dumps(report, indent=2) + "\n"
)
print(json.dumps({k: v for k, v in report.items() if k != "spacing_deviations"}, indent=2))
print("Spacing deviations:", len(pairs))

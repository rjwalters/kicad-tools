#!/usr/bin/env python3
"""Check every track segment against every foreign-net zone filled_polygon.

This is the verification the standard DRC gate cannot do yet (#3527):
segment copper vs. zone *fill* copper of a different net. For each segment
it samples points along the centerline and reports

  - point-in-polygon hits (centerline inside a foreign fill = hard short)
  - minimum edge-to-edge clearance (fill edge distance minus half trace width)

Usage:
    uv run python scripts/check_trace_vs_zone_fills.py <pcb> [--net NETNAME]
                                                              [--min-clearance MM]

With --net, only segments on that net are checked (faster, focused).
Exit code 1 if any short or sub-minimum clearance is found.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kicad_tools.sexp import parse_file  # noqa: E402


def point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


def dist_point_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    seg_len2 = dx * dx + dy * dy
    if seg_len2 == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg_len2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def dist_point_polygon_edge(x: float, y: float, poly: list[tuple[float, float]]) -> float:
    best = math.inf
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        best = min(best, dist_point_segment(x, y, x1, y1, x2, y2))
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcb")
    ap.add_argument("--net", help="only check segments on this net name")
    ap.add_argument(
        "--min-clearance",
        type=float,
        default=0.127,
        help="required edge-to-edge clearance in mm (default 0.127, JLC tier1)",
    )
    ap.add_argument("--step", type=float, default=0.05, help="sampling step along segments (mm)")
    args = ap.parse_args()

    doc = parse_file(args.pcb)

    # Net number -> name map
    net_names: dict[int, str] = {}
    for net in doc.find_all("net"):
        atoms = net.get_atoms()
        if len(atoms) >= 1:
            try:
                num = int(atoms[0])
            except (TypeError, ValueError):
                continue
            if len(atoms) > 1 and str(atoms[1]):
                net_names[num] = str(atoms[1])
            else:
                net_names.setdefault(num, "")

    # Collect zone fills per layer: (zone_net_num, zone_net_name, layer, polygon points)
    fills: list[tuple[int, str, str, list[tuple[float, float]]]] = []
    for zone in doc.find_all("zone"):
        znet_node = zone.find("net")
        if znet_node is None:
            continue
        zatoms = znet_node.get_atoms()
        try:
            znet = int(zatoms[0])
        except (TypeError, ValueError):
            # zone net given by name
            zname = str(zatoms[0])
            znet = next((k for k, v in net_names.items() if v == zname), -1)
        zname = net_names.get(znet, str(zatoms[0]))
        for fp in zone.find_all("filled_polygon"):
            layer_node = fp.find("layer")
            layer = str(layer_node.get_atoms()[0]) if layer_node is not None else "?"
            pts_node = fp.find("pts")
            if pts_node is None:
                continue
            poly = []
            for xy in pts_node.find_all("xy"):
                a = xy.get_atoms()
                poly.append((float(a[0]), float(a[1])))
            if len(poly) >= 3:
                fills.append((znet, zname, layer, poly))

    print(f"zone filled_polygons found: {len(fills)}")

    violations = 0
    checked = 0
    for seg in doc.find_all("segment"):
        net_node = seg.find("net")
        layer_node = seg.find("layer")
        start = seg.find("start")
        end = seg.find("end")
        width_node = seg.find("width")
        if any(n is None for n in (net_node, layer_node, start, end, width_node)):
            continue
        snet = int(net_node.get_atoms()[0])
        sname = net_names.get(snet, "?")
        if args.net and sname != args.net:
            continue
        layer = str(layer_node.get_atoms()[0])
        x1, y1 = (float(a) for a in start.get_atoms()[:2])
        x2, y2 = (float(a) for a in end.get_atoms()[:2])
        half_w = float(width_node.get_atoms()[0]) / 2.0
        seg_len = math.hypot(x2 - x1, y2 - y1)
        nsteps = max(2, int(seg_len / args.step) + 1)
        checked += 1

        for znet, zname, zlayer, poly in fills:
            if zlayer != layer or znet == snet:
                continue
            worst_inside = None
            min_clear = math.inf
            for i in range(nsteps + 1):
                t = i / nsteps
                px, py = x1 + t * (x2 - x1), y1 + t * (y2 - y1)
                edge_d = dist_point_polygon_edge(px, py, poly)
                if point_in_polygon(px, py, poly):
                    worst_inside = (px, py)
                    min_clear = -edge_d - half_w
                    break
                min_clear = min(min_clear, edge_d - half_w)
            if worst_inside is not None:
                violations += 1
                print(
                    f"SHORT: segment net '{sname}' ({x1},{y1})->({x2},{y2}) {layer} "
                    f"centerline INSIDE '{zname}' fill at ({worst_inside[0]:.3f},"
                    f"{worst_inside[1]:.3f}); overlap depth >= {-min_clear:.3f}mm"
                )
            elif min_clear < args.min_clearance:
                violations += 1
                print(
                    f"CLEARANCE: segment net '{sname}' ({x1},{y1})->({x2},{y2}) {layer} "
                    f"edge gap {min_clear:.4f}mm to '{zname}' fill "
                    f"(< {args.min_clearance}mm)"
                )

    print(f"segments checked: {checked}")
    if violations:
        print(f"FAIL: {violations} trace-vs-foreign-fill violation(s)")
        return 1
    print("PASS: no trace centerline inside any foreign fill; all edge gaps >= minimum")
    return 0


if __name__ == "__main__":
    sys.exit(main())

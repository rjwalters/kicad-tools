"""Measure symmetric clock spacing to foreign signal copper on routed layers.

Pads are reported separately because the application note states trace spacing;
package pin geometry must receive explicit review, not a blanket exemption.
Reference planes and their ground/power connections are outside this signal
spacing census. They are the transmission line's intended return structure.
"""

import argparse
import json
from pathlib import Path

from shapely.geometry import Point

from kicad_tools.geometry.copper import segment_copper_polygon
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.clearance import _pad_polygon

SIGNAL_LAYERS = {"F.Cu", "In2.Cu", "In3.Cu", "B.Cu"}
POWER = {"GND", "+3V3", "+3V3_A", "+5V", "VCAP1", "VCAP2"}


def audit(path):
    board = PCB.load(path)
    copper = []
    for i, segment in enumerate(board.segments):
        if segment.layer in SIGNAL_LAYERS and segment.net_name and segment.net_name not in POWER:
            copper.append(
                (
                    f"track:{i}",
                    "track",
                    segment.net_name,
                    {segment.layer},
                    segment_copper_polygon(segment.start, segment.end, segment.width),
                )
            )
    for i, via in enumerate(board.vias):
        if via.net_name and via.net_name not in POWER:
            if set(via.layers) != {"F.Cu", "B.Cu"}:
                raise ValueError("This full-span census does not support partial-span vias")
            copper.append(
                (
                    f"via:{i}",
                    "via",
                    via.net_name,
                    SIGNAL_LAYERS,
                    Point(via.position).buffer(via.size / 2, quad_segs=64),
                )
            )
    for footprint in board.footprints:
        for pad in footprint.pads:
            if not pad.net_name or pad.net_name in POWER:
                continue
            layers = SIGNAL_LAYERS if "*.Cu" in pad.layers else set(pad.layers) & SIGNAL_LAYERS
            shape = _pad_polygon(pad, footprint)
            if shape is not None and layers:
                copper.append(
                    (f"{footprint.reference}.{pad.number}", "pad", pad.net_name, layers, shape)
                )
    clock = [item for item in copper if item[2] == "SDCLK"]
    others = [item for item in copper if item[2] != "SDCLK"]
    findings = []
    minimum = float("inf")
    for a in clock:
        for b in others:
            layers = a[3] & b[3]
            if not layers:
                continue
            gap = a[4].distance(b[4])
            if "pad" not in [a[1], b[1]]:
                minimum = min(minimum, gap)
            if gap < 0.54 - 1e-6:
                findings.append(
                    {
                        "clock_item": a[0],
                        "foreign_item": b[0],
                        "foreign_net": b[2],
                        "clock_kind": a[1],
                        "foreign_kind": b[1],
                        "layers": sorted(layers),
                        "edge_gap_mm": round(gap, 6),
                        "scope": "package/pad review"
                        if "pad" in [a[1], b[1]]
                        else "trace/via spacing",
                    }
                )
    return {
        "required_edge_gap_mm": 0.54,
        "minimum_nonpad_gap_mm": minimum,
        "scope": "All signal tracks/vias on F/In2/In3/B; pad findings reported separately. Power/return structures excluded.",
        "findings": findings,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcb", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = audit(args.pcb)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))

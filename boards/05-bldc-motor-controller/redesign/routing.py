"""Apply reviewed physical copper only to the exact revision-B circuit geometry.

The reference route combines automatic routing and explicit physical corrections.
It is not an autorouter success claim. Fresh native refill/DRC is mandatory after
application; changing a pad, net, footprint or placement invalidates the recipe.
"""

import hashlib
import json
from pathlib import Path

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_file, parse_string, serialize_sexp

ROOT = Path(__file__).resolve().parent

# Reference-designator silkscreen placement, as (x, y) offsets from the
# footprint origin.  Library defaults collide with neighbouring copper on this
# layout; each entry below is a legibility/DFM correction that moves only
# F.SilkS text and never pads, nets or placement.
#
# R4 (issue #5204): the library default for R_0805_2012Metric puts the
# reference 1.65 mm ABOVE the part, straight into R1's pad 2 mask aperture
# (R1 is a 2512 at (127.5, 110) whose pad 2 copper reaches y = 111.675 and
# whose mask aperture is identical -- the board sets pad_to_mask_clearance 0).
# Native KiCad 10.0.6 measured the rendered glyphs at 0.0548 mm from that
# aperture, under JLCPCB's 0.15 mm silk-to-pad floor.  Mirroring the same
# standard offset BELOW the part clears R1 entirely: nothing is placed below
# R4 within 8 mm, and native DRC now measures its reference field at
# 0.8631 mm from R4's own pad 1 (5.8x the floor).  The tightest remaining
# silk-to-pad pair on the board is U2's library outline against its own
# pad 29 at 0.2073 mm, so the 0.15 mm floor holds board-wide.
REFERENCE_OFFSETS = {
    "R4": (0, 1.65),
    "R11": (0, 1.8),
    "RV1": (0, 6.0),
}


def geometry_fingerprint(path):
    pcb = PCB.load(path)
    geometry = {
        "outline": [pcb.board_origin, pcb.board_size],
        "layers": [layer.name for layer in pcb.copper_layers],
        "parts": [],
    }
    for fp in sorted(pcb.footprints, key=lambda item: item.reference):
        pads = [
            {
                "number": pad.number,
                "net": pad.net_name,
                "position": pad.position,
                "size": pad.size,
                "rotation": pad.rotation,
                "shape": pad.shape,
                "type": pad.type,
                "layers": pad.layers,
                "drill": pad.drill,
            }
            for pad in fp.pads
        ]
        pads.sort(key=lambda pad: json.dumps(pad, sort_keys=True))
        geometry["parts"].append(
            {
                "reference": fp.reference,
                "value": fp.value,
                "footprint": fp.name,
                "position": fp.position,
                "rotation": fp.rotation,
                "pads": pads,
            }
        )

    # KiCad native serialization rounds positions to micrometres.
    def stable(value):
        if isinstance(value, float):
            return round(value, 5)
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [stable(item) for item in value]
        return value

    payload = json.dumps(stable(geometry), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def apply_routing(path):
    path = Path(path)
    recipe = json.loads((ROOT / "routing.json").read_text())
    if geometry_fingerprint(path) != recipe["geometry_sha256"]:
        raise ValueError("Revision-B pad/placement/net geometry changed; reroute and validate")
    board = parse_file(path)
    if any(board.find_all(kind) for kind in ("segment", "via", "zone")):
        raise ValueError("Reference copper requires a freshly generated unrouted board")
    nets = {node.get_string(1): node.get_int(0) for node in board.find_all("net")}
    for source in recipe["copper"]:
        node = parse_string(source)
        net = node.find("net")
        if net:
            name = net.get_string(0)
            net.set_value(0, nets.get(name, 0))
            if node.name == "zone":
                old = node.find("net_name")
                if old:
                    old.set_value(0, name)
                else:
                    node.append(parse_string(f"(net_name {json.dumps(name)})"))
        board.append(node)
    # Drawing edits never replace source pads or net assignments.
    for fp in board.find_all("footprint"):
        props = {p.get_string(0): p for p in fp.find_all("property")}
        ref = props["Reference"].get_string(1)
        if ref in REFERENCE_OFFSETS:
            at = props["Reference"].find("at")
            x, y = REFERENCE_OFFSETS[ref]
            at.set_value(0, x)
            at.set_value(1, y)
        for line in fp.children:
            if line.name in {"fp_line", "fp_poly", "fp_arc", "fp_rect", "fp_circle"} and line.find(
                "layer"
            ).get_string(0) in {"F.SilkS", "B.SilkS"}:
                width = line.find("stroke").find("width")
                width.set_value(0, max(0.15, width.get_float(0)))
    path.write_text(serialize_sexp(board))
    return recipe["geometry_sha256"]

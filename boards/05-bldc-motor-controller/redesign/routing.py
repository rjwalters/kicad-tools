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
        if ref in {"R11", "RV1"}:
            at = props["Reference"].find("at")
            at.set_value(0, 0)
            at.set_value(1, 1.8 if ref == "R11" else 6.0)
        for line in fp.children:
            if line.name in {"fp_line", "fp_poly", "fp_arc", "fp_rect", "fp_circle"} and line.find(
                "layer"
            ).get_string(0) in {"F.SilkS", "B.SilkS"}:
                width = line.find("stroke").find("width")
                width.set_value(0, max(0.15, width.get_float(0)))
    path.write_text(serialize_sexp(board))
    return recipe["geometry_sha256"]

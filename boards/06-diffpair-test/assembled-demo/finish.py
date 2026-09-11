#!/usr/bin/env python3
"""Transfer routed LVTTL nets onto the reviewed LVDS layout and refill planes.

Usage: uv run python .../finish.py OUTPUT_DIR ROUTED_INPUT
The generator must have run for OUTPUT_DIR first. Only IN/OUT tracks and vias
are taken from ROUTED_INPUT; footprints, stackup and differential geometry come
from the current source generator. Native DRC remains an independent gate.
"""

import argparse
import copy
import json
import math
from pathlib import Path

from kicad_tools.cli.runner import (
    _restore_net_declarations,
    _run_fill_zones_via_drc,
    _snapshot_element_nets,
    _snapshot_net_declarations,
)
from kicad_tools.router.quantize import quantize_pcb_file
from kicad_tools.sexp import parse_file, parse_string


def repair_drill_overlaps(pcb):
    """Replay four reviewed endpoint moves from the deterministic outer-layer route."""
    moves = json.loads((Path(__file__).parent / "pad-drill-repairs.json").read_text())
    net_numbers = {n.get_string(1): n.get_int(0) for n in pcb.find_children("net")}
    for move in moves:
        old, new = move["absolute_from"], move["absolute_to"]
        number = net_numbers[move["net"]]
        for item in pcb.children:
            if item.name not in ("segment", "via"):
                continue
            net = item.find_child("net")
            if net is None or net.get_int(0) != number:
                continue
            for key in ("at",) if item.name == "via" else ("start", "end"):
                point = item.find_child(key)
                xy = [point.get_float(0), point.get_float(1)]
                if math.dist(xy, old) < 0.001:
                    point.set_value(0, new[0])
                    point.set_value(1, new[1])


def check_process_geometry(path):
    """Inner planes remain continuous references; ordinary through vias stay off lands."""
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

    board = PCB.load(path)
    assert all(s.layer in {"F.Cu", "B.Cu"} for s in board.segments), "Signal on reference plane"
    assert all(
        v.via_type in {None, "through"} and set(v.layers) == {"F.Cu", "B.Cu"} for v in board.vias
    ), "Unexpected blind, buried or microvia process"
    assert not ViaInPadRule().check(board, None).violations, "Drill overlaps an SMT land"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("routed", type=Path)
    args = parser.parse_args()
    base = args.output / "diffpair_test.kicad_pcb"
    # KiCad 10's name-only save format needs the tool compatibility adapter.
    _restore_net_declarations(
        args.routed, _snapshot_net_declarations(base), _snapshot_element_nets(base)
    )
    pcb, routed = parse_file(base), parse_file(args.routed)
    names = {n.get_int(0): n.get_string(1) for n in routed.find_children("net")}
    target = {n.get_string(1): n.get_int(0) for n in pcb.find_children("net")}
    transferred = 0
    for item in routed.children:
        if item.name not in ("segment", "via"):
            continue
        node = item.find_child("net")
        if node is None:
            continue
        name = names.get(node.get_int(0), "")
        if not name.startswith(("IN", "OUT")):
            continue
        if (
            item.name == "segment"
            and item.find_child("start").get_atoms() == item.find_child("end").get_atoms()
        ):
            continue
        item = copy.deepcopy(item)
        item.remove_child("net")
        item.append(parse_string(f"(net {target[name]})"))
        pcb.append(item)
        transferred += 1
    repair_drill_overlaps(pcb)
    path = args.output / "diffpair_test_routed.kicad_pcb"
    path.write_text(pcb.to_string() + "\n")
    # The off-land drill repairs drag attached endpoints. Quantize their
    # resulting chords before refill and all native/process checks.
    quantize_pcb_file(path)
    result = _run_fill_zones_via_drc(path, None, "kicad-cli")
    check_process_geometry(path)
    print(f"Transferred {transferred} signal primitives; refill: {result}")


if __name__ == "__main__":
    main()

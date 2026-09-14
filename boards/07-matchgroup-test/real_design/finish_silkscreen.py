"""Add the reviewed operator legend without modifying electrical geometry."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string

LABELS = [
    ("SDRAM EXERCISER", 55, 8, 2, False),
    ("STM32F429 + IS42S16400J | Rev B", 55, 11, 1, False),
    ("J1: 1 +5V / 2 GND", 12, 22, 1, True),
    ("SWD", 10, 41, 1, True),
    ("3V3 SENSE", 8, 45, 1, True),
    ("SWDIO", 8, 47.54, 1, True),
    ("GND", 8, 50.08, 1, True),
    ("SWCLK", 8, 52.62, 1, True),
    ("NRST", 8, 55.16, 1, True),
    ("SWO", 8, 57.7, 1, True),
    ("UART 3V3", 10, 64, 1, True),
    ("GND", 8, 68, 1, True),
    ("TX", 8, 70.54, 1, True),
    ("RX", 8, 73.08, 1, True),
    ("PASS", 40, 73, 1, False),
    ("FAIL", 46, 73, 1, False),
]


def finish(path: Path) -> None:
    board = PCB.load(path)
    ox, oy = board.board_origin
    # Stock KiCad graphics commonly use 0.12 mm strokes; the selected six-layer
    # factory profile requires at least 0.15 mm. Recheck native clipping after
    # widening rather than waiving this manufacturing requirement.
    nodes = list(board._sexp.children)
    nodes += [node for fp in board.footprints for node in fp._sexp_node.children]
    for node in nodes:
        if not hasattr(node, "find_child"):
            continue
        layer = node.find_child("layer")
        if layer is None or layer.get_string(0) not in {"F.SilkS", "B.SilkS"}:
            continue
        stroke = node.find_child("stroke")
        width = stroke.find_child("width") if stroke is not None else node.find_child("width")
        if width is not None and width.get_float(0) < 0.15:
            width.set_atom(0, 0.15)
    for text, x, y, size, left in LABELS:
        identity = uuid.uuid5(
            uuid.NAMESPACE_URL, f"kicad-tools/board07/operator-legend/{x}/{y}/{text}"
        )
        # Stable IDs make repeated finishing idempotent.
        for node in list(board._sexp.find_children("gr_text")):
            if node.find_child("uuid") and node.find_child("uuid").get_string(0) == str(identity):
                board._sexp.children.remove(node)
        board._sexp.append(
            parse_string(
                f'(gr_text {json.dumps(text)} (at {ox + x} {oy + y}) (layer "F.SilkS") '
                f'(uuid "{identity}") (effects (font (size {size} {size}) (thickness .15)) '
                f"{'(justify left)' if left else ''}))"
            )
        )
    board.save(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcb", type=Path)
    finish(parser.parse_args().pcb)

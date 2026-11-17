"""``--complete`` keeps preserved copper of the nets it lists (Issue #8066).

A net that already carries copper but still has an open link is *listed* for
completion.  Its preserved copper used to be treated as stale: other listed
nets routed straight across it, and it vanished from the output once the net
got a new route.  It must instead be a fixed obstacle for every other net and
be re-emitted unchanged.

Board: NET1 has a preserved F.Cu "wall" (x=112) plus R1 (joined to the wall)
and a stranded R3.  NET2 is fully unrouted between R2 and R4 and its shortest
path crosses the wall.
"""

from __future__ import annotations

import re
from pathlib import Path

from kicad_tools.cli.route_cmd import main as route_main

_WALL = '(segment (start 112 101) (end 112 119) (width 0.4) (layer "F.Cu") (net 1))'


def _res(ref: str, x: float, y: float, net_a: tuple[int, str], net_b: tuple[int, str], n: int):
    return f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{n:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net {net_a[0]} "{net_a[1]}"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net {net_b[0]} "{net_b[1]}"))
  )
"""


def _board() -> str:
    n1, n2 = (1, "NET1"), (2, "NET2")
    return (
        '(kicad_pcb\n  (version 20240108)\n  (generator "test")\n'
        '  (generator_version "8.0")\n  (general (thickness 1.6))\n  (paper "A4")\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        "  (setup (pad_to_mask_clearance 0))\n"
        '  (net 0 "")\n  (net 1 "NET1")\n  (net 2 "NET2")\n'
        "  (gr_rect (start 100 100) (end 130 120) (stroke (width 0.1) (type default))"
        ' (fill none) (layer "Edge.Cuts"))\n'
        + _res("R1", 108, 110, n1, n1, 10)
        + _res("R3", 122, 105, n1, n1, 11)
        + _res("R2", 105, 110, n2, n2, 12)
        + _res("R4", 120, 114, n2, n2, 13)
        + f"  {_WALL}\n"
        # Tie R1 (both pads) to the wall so the wall is NET1's connected copper.
        + '  (segment (start 107.49 110) (end 108.51 110) (width 0.2) (layer "F.Cu") (net 1))\n'
        + '  (segment (start 108.51 110) (end 112 110) (width 0.2) (layer "F.Cu") (net 1))\n'
        + ")\n"
    )


def _segments(text: str):
    out = []
    pat = (
        r"\(segment\s+\(start ([\d.]+) ([\d.]+)\)\s+\(end ([\d.]+) ([\d.]+)\)"
        r".*?\(layer \"([^\"]+)\"\).*?\(net (\d+)\)"
    )
    for m in re.finditer(pat, text, re.S):
        x1, y1, x2, y2 = (float(m.group(i)) for i in range(1, 5))
        out.append((x1, y1, x2, y2, m.group(5), int(m.group(6))))
    return out


def _crosses_wall(seg) -> bool:
    x1, y1, x2, y2, layer, _ = seg
    if layer != "F.Cu" or (x1 - 112) * (x2 - 112) > 0:
        return False
    if x1 == x2:
        return x1 == 112
    t = (112 - x1) / (x2 - x1)
    return 101 <= y1 + t * (y2 - y1) <= 119


def test_listed_net_preserved_copper_is_obstacle_and_kept(tmp_path: Path):
    board = tmp_path / "in.kicad_pcb"
    board.write_text(_board())
    out = tmp_path / "out.kicad_pcb"

    rc = route_main([str(board), "-o", str(out), "--complete", "--backend", "cpp", "--quiet"])
    assert rc == 0

    segs = _segments(out.read_text())
    net1 = [s for s in segs if s[5] == 1]
    net2 = [s for s in segs if s[5] == 2]

    # AC2: NET1 got a new route for R3, yet its preserved copper is still there.
    assert (112.0, 101.0, 112.0, 119.0, "F.Cu", 1) in net1
    assert (107.49, 110.0, 108.51, 110.0, "F.Cu", 1) in net1
    assert len(net1) > 3, "NET1's open link must be completed"
    # AC1: NET2's new copper never crosses NET1's preserved wall.
    assert net2, "NET2 must be routed"
    assert not [s for s in net2 if _crosses_wall(s)]

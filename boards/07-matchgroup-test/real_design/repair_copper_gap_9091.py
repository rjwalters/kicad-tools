#!/usr/bin/env python3
"""Scripted repair of board 07's same-net copper slits (Issue #9091).

Issue #8938 stopped grading same-net ``physical_copper_gap`` findings as
warnings, and board 07's hand-reviewed copper failed the routed-DRC gate with
29 same-net slits (25 on the pruned reviewed artifact, which merged collinear
pieces).  The rule and its JLCPCB bands are unchanged.  The copper changes
instead.  This script is the complete, reviewable record of that change.  It
applies fixed tables to the pre-repair artifacts and nothing else.

Via-to-own-pad slits (9 sites)
    ``VIA_MOVES``: each via moves 53-87 um straight away from its own pad, so
    the via clears the pad by the fab's ``pad_spacing`` danger edge plus 5 um.
    The edge is read from the JLCPCB ``6layer_1oz`` DFM entry, the same table
    the rule grades with, and was 0.15 mm when the table below was derived.
    Eight moves are collinear with the via's stub, so the stub only
    lengthens.  The DQ7 via (U2.13) also takes one short 0/45/90 jog on its
    In2 stub, built by ``repair_class_clearance_6396.move_via``; DQ7 gains
    0.145 mm, inside ``SDRAM_BYTE0``'s 5 mm skew (spread 0.105 -> 0.180 mm).  Other-net
    copper stays at least ``class + 5 um`` (0.18 mm) away.  The table came
    from a minimal-move search like #6396's: displacement plus 10 um per jog.

    The GND via beside C26 at (106.654, 81.3786) had no such move.  A +5V
    staircase (0.15 mm steps) ran diagonally from the +5V pad to the
    y = 80.75 horizontal and boxed it in.  The same pad already has a straight
    vertical branch through y = 80.75, so the ``+5V`` rows of ``DELETES``,
    ``RESHAPES`` and ``ADDS`` delete the staircase, split the vertical at
    y = 80.75 and start the horizontal there.  The
    result is a plain 90 degree T.  The via is then free to move.

Trace loops at U2.50 (DQ13) and U2.39 (UDQM)
    An F.Cu trunk cannot cross the TSOP-II pad row.  Pads are 0.342 mm apart,
    and a 0.18 mm trace needs 0.53 mm at the 0.175 mm class.  So both trunks
    ran *through* their own pad, then on past it, and doubled back along the
    pad's escape to the escape's end.  The authored source puts a fanout via
    at that end, but the reviewed routing has none.  Everything beyond the
    pad was therefore a closed same-net loop.  The signal never travelled it,
    yet ``validate.py`` counted it as matched length.  ``DELETES`` removes each
    loop.  The trunk ends on the pad's long axis at the pad centre, the way
    every other trunk on this board enters a pad.

    The loops counted 9-10 mm of copper, so removing them would break
    ``SDRAM_BYTE1``'s 5 mm group skew.  ``TROMBONES`` restores that length as
    real series length on each trunk, with one trombone from the repo's
    ``SerpentineGenerator.generate_trombone``.  That is the generator the
    board's original tuning used (``engineering/experiments/tune-local.py``).
    The trombones use 0.72 mm spacing on free F.Cu, and their geometry is
    recorded here.

Trace junction at U2.53 (DQ15)
    DQ15 is a genuine single path: its trunk comes round the package end and
    meets the escape at the escape's end.  The trunk approached that end as a
    0.05 mm-step staircase at about 25 degrees to the escape, which left a
    narrow same-net wedge.  The ``DQ15`` rows replace the staircase and the last
    0.75 mm of escape with one 45 degree leg.  The leg meets the escape's own
    45 degree leg at a right angle, which is not a slit.  The 1.5 mm this
    removes goes back into the existing DQ15 meander: its six tops are raised
    by 0.125 mm (``POINT_MOVES``).

The authored physical source (``authored-source/sdram_demo.kicad_pcb``) gets
the edits whose copper it owns (``authored=True``): the eight power vias and
the +5V branch.  The reviewed routing artifact gets all of them.  Both inputs
are pinned to their pre-repair SHA-256, so the tables can never be replayed
onto different copper.

Usage::

    # rewrite both artifacts in place (refuses unless they are pre-repair)
    uv run python boards/07-matchgroup-test/real_design/repair_copper_gap_9091.py

    # reproduce from git history and compare against the committed files
    uv run python boards/07-matchgroup-test/real_design/repair_copper_gap_9091.py \\
        --verify-from-git 89c2ee058

Afterwards re-pin ``authored-source/manifest.json`` and
``reviewed-routing/manifest.json`` and rerun ``build.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AUTHORED = ROOT / "authored-source/sdram_demo.kicad_pcb"
REVIEWED = ROOT / "reviewed-routing/sdram_demo.kicad_pcb"

#: SHA-256 of the pre-repair inputs (main 89c2ee058).
PRE_REPAIR_SHA256 = {
    AUTHORED: "180a55b19e5c7117fc97e9599debb81d119e61fa14d3f7513928f4657f1577f5",
    REVIEWED: "4d2378a2ba7714c3c6d9f3b4be7852423cf2e2a1b4b6f82996c1c45e30e31879",
}

Point = tuple[float, float]

#: ``(net, old (x, y), new (x, y), authored)``, applied in this order.
VIA_MOVES: tuple[tuple[str, Point, Point, bool], ...] = (
    ("GND", (127.53, 111.787), (127.5927, 111.787), True),
    ("GND", (183.639, 91.3636), (183.5924, 91.317), True),
    ("GND", (150.639, 88.3636), (150.5924, 88.317), True),
    ("GND", (108.362, 80.0), (108.295, 80.0), True),
    ("+3V3", (111.001, 78.4136), (110.9581, 78.3707), True),
    ("GND", (114.639, 81.3636), (114.5924, 81.317), True),
    ("GND", (115.639, 97.3636), (115.5924, 97.317), True),
    ("DQ7", (165.85, 104.25), (165.91, 104.25), False),
    # Free only once the +5V staircase below is gone.
    ("GND", (106.654, 81.3786), (106.5924, 81.317), True),
)

#: ``(net, layer, (a, b), authored)``: segments to delete (either direction).
DELETES: tuple[tuple[str, str, tuple[Point, Point], bool], ...] = (
    # +5V: the 0.15 mm staircase branch that boxed in the GND via at (106.654, 81.3786).
    ("+5V", "F.Cu", ((105.725, 81.775), (106.0, 81.5)), True),
    ("+5V", "F.Cu", ((106.0, 81.35), (106.15, 81.2)), True),
    ("+5V", "F.Cu", ((106.0, 81.5), (106.0, 81.35)), True),
    ("+5V", "F.Cu", ((106.15, 81.05), (106.3, 80.9)), True),
    ("+5V", "F.Cu", ((106.15, 81.2), (106.15, 81.05)), True),
    ("+5V", "F.Cu", ((106.3, 80.9), (106.45, 80.9)), True),
    ("+5V", "F.Cu", ((106.45, 80.9), (106.6, 80.75)), True),
    # DQ13 (U2.50): the pass-by trunk, its staircase and the escape beyond the pad.
    ("DQ13", "F.Cu", ((170.15, 97.55), (178.95, 97.55)), False),
    ("DQ13", "F.Cu", ((176.25, 97.8), (177.67, 97.8)), False),
    ("DQ13", "F.Cu", ((177.67, 97.8), (178.75, 96.72)), False),
    ("DQ13", "F.Cu", ((178.75, 96.72), (180.75, 96.72)), False),
    ("DQ13", "F.Cu", ((178.95, 97.55), (179.0, 97.5)), False),
    ("DQ13", "F.Cu", ((179.0, 97.5), (179.05, 97.5)), False),
    ("DQ13", "F.Cu", ((179.05, 97.5), (179.1, 97.45)), False),
    ("DQ13", "F.Cu", ((179.1, 97.45), (179.2, 97.45)), False),
    ("DQ13", "F.Cu", ((179.2, 97.45), (179.25, 97.4)), False),
    ("DQ13", "F.Cu", ((179.25, 97.4), (179.3, 97.4)), False),
    ("DQ13", "F.Cu", ((179.3, 97.4), (179.35, 97.35)), False),
    ("DQ13", "F.Cu", ((179.35, 97.35), (179.4, 97.35)), False),
    ("DQ13", "F.Cu", ((179.4, 97.35), (179.45, 97.3)), False),
    ("DQ13", "F.Cu", ((179.45, 97.3), (179.5, 97.3)), False),
    ("DQ13", "F.Cu", ((179.5, 97.3), (179.55, 97.25)), False),
    ("DQ13", "F.Cu", ((179.55, 97.25), (179.6, 97.25)), False),
    ("DQ13", "F.Cu", ((179.6, 97.25), (179.65, 97.2)), False),
    ("DQ13", "F.Cu", ((179.65, 97.2), (179.75, 97.2)), False),
    ("DQ13", "F.Cu", ((179.75, 97.2), (179.8, 97.15)), False),
    ("DQ13", "F.Cu", ((179.8, 97.15), (179.85, 97.15)), False),
    ("DQ13", "F.Cu", ((179.85, 97.15), (179.9, 97.1)), False),
    ("DQ13", "F.Cu", ((179.9, 97.1), (179.95, 97.1)), False),
    ("DQ13", "F.Cu", ((179.95, 97.1), (180.0, 97.05)), False),
    ("DQ13", "F.Cu", ((180.0, 97.05), (180.05, 97.05)), False),
    ("DQ13", "F.Cu", ((180.05, 97.05), (180.1, 97.0)), False),
    ("DQ13", "F.Cu", ((180.1, 97.0), (180.15, 97.0)), False),
    ("DQ13", "F.Cu", ((180.15, 97.0), (180.2, 96.95)), False),
    ("DQ13", "F.Cu", ((180.2, 96.95), (180.3, 96.95)), False),
    ("DQ13", "F.Cu", ((180.3, 96.95), (180.35, 96.9)), False),
    ("DQ13", "F.Cu", ((180.35, 96.9), (180.4, 96.9)), False),
    ("DQ13", "F.Cu", ((180.4, 96.9), (180.45, 96.85)), False),
    ("DQ13", "F.Cu", ((180.45, 96.85), (180.5, 96.85)), False),
    ("DQ13", "F.Cu", ((180.5, 96.85), (180.55, 96.8)), False),
    ("DQ13", "F.Cu", ((180.55, 96.8), (180.67, 96.8)), False),
    ("DQ13", "F.Cu", ((180.67, 96.8), (180.75, 96.72)), False),
    # UDQM (U2.39): the pass-by trunk, its staircase and the escape beyond the pad.
    ("UDQM", "F.Cu", ((175.35, 106.3), (175.4, 106.3)), False),
    ("UDQM", "F.Cu", ((175.4, 106.3), (175.45, 106.35)), False),
    ("UDQM", "F.Cu", ((175.45, 106.35), (178.45, 106.35)), False),
    ("UDQM", "F.Cu", ((176.25, 106.6), (177.64, 106.6)), False),
    ("UDQM", "F.Cu", ((177.64, 106.6), (177.7, 106.66)), False),
    ("UDQM", "F.Cu", ((177.7, 106.66), (177.99, 106.66)), False),
    ("UDQM", "F.Cu", ((177.99, 106.66), (178.05, 106.72)), False),
    ("UDQM", "F.Cu", ((178.05, 106.72), (178.34, 106.72)), False),
    ("UDQM", "F.Cu", ((178.34, 106.72), (178.4, 106.78)), False),
    ("UDQM", "F.Cu", ((178.4, 106.78), (178.69, 106.78)), False),
    ("UDQM", "F.Cu", ((178.45, 106.35), (178.5, 106.4)), False),
    ("UDQM", "F.Cu", ((178.5, 106.4), (178.55, 106.4)), False),
    ("UDQM", "F.Cu", ((178.55, 106.4), (178.6, 106.45)), False),
    ("UDQM", "F.Cu", ((178.6, 106.45), (178.65, 106.45)), False),
    ("UDQM", "F.Cu", ((178.65, 106.45), (178.7, 106.5)), False),
    ("UDQM", "F.Cu", ((178.69, 106.78), (178.75, 106.84)), False),
    ("UDQM", "F.Cu", ((178.7, 106.5), (178.8, 106.5)), False),
    ("UDQM", "F.Cu", ((178.75, 106.84), (179.5, 106.84)), False),
    ("UDQM", "F.Cu", ((178.8, 106.5), (178.85, 106.55)), False),
    ("UDQM", "F.Cu", ((178.85, 106.55), (178.9, 106.55)), False),
    ("UDQM", "F.Cu", ((178.9, 106.55), (178.95, 106.6)), False),
    ("UDQM", "F.Cu", ((178.95, 106.6), (179.0, 106.6)), False),
    ("UDQM", "F.Cu", ((179.0, 106.6), (179.05, 106.65)), False),
    ("UDQM", "F.Cu", ((179.05, 106.65), (179.1, 106.65)), False),
    ("UDQM", "F.Cu", ((179.1, 106.65), (179.15, 106.7)), False),
    ("UDQM", "F.Cu", ((179.15, 106.7), (179.2, 106.7)), False),
    ("UDQM", "F.Cu", ((179.2, 106.7), (179.25, 106.75)), False),
    ("UDQM", "F.Cu", ((179.25, 106.75), (179.41, 106.75)), False),
    ("UDQM", "F.Cu", ((179.41, 106.75), (179.5, 106.84)), False),
    # UDQM: the diagonal that carries the trombone (TROMBONES re-lays it).
    ("UDQM", "F.Cu", ((168.2, 99.8), (171.25, 102.85)), False),
    # DQ15 (U2.53): the shallow staircase and the last 0.75 mm of escape.
    ("DQ15", "F.Cu", ((178.2, 93.35), (178.25, 93.4)), False),
    ("DQ15", "F.Cu", ((178.25, 93.4), (178.3, 93.4)), False),
    ("DQ15", "F.Cu", ((178.3, 93.4), (178.35, 93.45)), False),
    ("DQ15", "F.Cu", ((178.35, 93.45), (178.4, 93.45)), False),
    ("DQ15", "F.Cu", ((178.4, 93.45), (178.45, 93.5)), False),
    ("DQ15", "F.Cu", ((178.45, 93.5), (178.5, 93.5)), False),
    ("DQ15", "F.Cu", ((178.5, 93.5), (178.55, 93.55)), False),
    ("DQ15", "F.Cu", ((178.55, 93.55), (178.65, 93.55)), False),
    ("DQ15", "F.Cu", ((178.65, 93.55), (178.7, 93.6)), False),
    ("DQ15", "F.Cu", ((178.7, 93.6), (178.75, 93.6)), False),
    ("DQ15", "F.Cu", ((178.75, 93.6), (178.8, 93.65)), False),
    ("DQ15", "F.Cu", ((178.75, 93.96), (179.5, 93.96)), False),
    ("DQ15", "F.Cu", ((178.8, 93.65), (178.85, 93.65)), False),
    ("DQ15", "F.Cu", ((178.85, 93.65), (178.9, 93.7)), False),
    ("DQ15", "F.Cu", ((178.9, 93.7), (178.95, 93.7)), False),
    ("DQ15", "F.Cu", ((178.95, 93.7), (179.0, 93.75)), False),
    ("DQ15", "F.Cu", ((179.0, 93.75), (179.05, 93.75)), False),
    ("DQ15", "F.Cu", ((179.05, 93.75), (179.1, 93.8)), False),
    ("DQ15", "F.Cu", ((179.1, 93.8), (179.2, 93.8)), False),
    ("DQ15", "F.Cu", ((179.2, 93.8), (179.25, 93.85)), False),
    ("DQ15", "F.Cu", ((179.25, 93.85), (179.39, 93.85)), False),
    ("DQ15", "F.Cu", ((179.39, 93.85), (179.5, 93.96)), False),
)

#: ``(net, layer, (a, b), (a2, b2), authored)``: a segment keeps its UUID and
#: width but gets new endpoints.
RESHAPES: tuple[tuple[str, str, tuple[Point, Point], tuple[Point, Point], bool], ...] = (
    # +5V: the vertical branch stops at the T; the horizontal starts there.
    (
        "+5V",
        "F.Cu",
        ((105.725, 79.05), (105.725, 81.775)),
        ((105.725, 79.05), (105.725, 80.75)),
        True,
    ),
    ("+5V", "F.Cu", ((106.6, 80.75), (109.162, 80.75)), ((105.725, 80.75), (109.162, 80.75)), True),
    # DQ13: the trunk's 45 degree approach now lands on the pad's axis (y 97.8).
    ("DQ13", "F.Cu", ((169.05, 98.65), (170.15, 97.55)), ((169.05, 98.65), (169.9, 97.8)), False),
    # UDQM: the 45 degree approach continues to the pad's axis (y 106.6).
    ("UDQM", "F.Cu", ((172.85, 103.8), (175.35, 106.3)), ((172.85, 103.8), (175.65, 106.6)), False),
    # DQ15: the trunk stops 0.06 mm short and turns 45 degrees onto the escape's corner.
    ("DQ15", "F.Cu", ((176.39, 93.35), (178.2, 93.35)), ((176.39, 93.35), (178.14, 93.35)), False),
)

#: ``(net, layer, (a, b), authored)``: new 0.18 mm segments with deterministic
#: UUIDs.  They are inserted after the last existing segment.
ADDS: tuple[tuple[str, str, tuple[Point, Point], bool], ...] = (
    ("+5V", "F.Cu", ((105.725, 80.75), (105.725, 81.775)), True),
    ("UDQM", "F.Cu", ((175.65, 106.6), (176.25, 106.6)), False),
    ("DQ15", "F.Cu", ((178.14, 93.35), (178.75, 93.96)), False),
)

#: ``(net, layer, points, authored)``: trombones, laid as consecutive 0.18 mm
#: segments.  Loop stations and amplitude come from
#: ``SerpentineGenerator.generate_trombone`` (``min_spacing=0.72``,
#: ``gap_factor=1``, ``side="outer"``), sized to restore each net's reviewed
#: length.  DQ13 has three loops of 1.6492 mm across its new y = 97.8 trunk
#: (9.895 mm).  UDQM has two loops of 1.6747 mm on its 45 degree diagonal
#: (6.699 mm).  The diagonal's points are written as ``(x0 + a + b, y0 + a - b)``
#: with ``a`` and ``b`` on the 0.1 um grid, so every leg is exactly 0/45/90.
TROMBONES: tuple[tuple[str, str, tuple[Point, ...], bool], ...] = (
    (
        "DQ13",
        "F.Cu",
        (
            (169.9, 97.8),
            (170.62, 97.8),
            (170.62, 96.1508),
            (171.34, 96.1508),
            (171.34, 97.8),
            (172.06, 97.8),
            (172.06, 96.1508),
            (172.78, 96.1508),
            (172.78, 97.8),
            (173.5, 97.8),
            (173.5, 96.1508),
            (174.22, 96.1508),
            (174.22, 97.8),
            (176.25, 97.8),
        ),
        False,
    ),
    (
        "UDQM",
        "F.Cu",
        (
            (168.2, 99.8),
            (168.7091, 100.3091),
            (169.8933, 99.1249),
            (170.4024, 99.634),
            (169.2182, 100.8182),
            (169.7274, 101.3274),
            (170.9116, 100.1432),
            (171.4207, 100.6523),
            (170.2365, 101.8365),
            (171.25, 102.85),
        ),
        False,
    ),
)

#: ``(net, layer, old, new, authored)``: a corner shared by exactly two
#: segments moves (``repair_class_clearance_6396.move_point``).
POINT_MOVES: tuple[tuple[str, str, Point, Point, bool], ...] = (
    # DQ15: raise the six meander tops 0.125 mm (12 legs x 0.125 = 1.5 mm).
    ("DQ15", "F.Cu", (168.47, 91.8222), (168.47, 91.6972), False),
    ("DQ15", "F.Cu", (169.19, 91.8222), (169.19, 91.6972), False),
    ("DQ15", "F.Cu", (169.91, 91.8222), (169.91, 91.6972), False),
    ("DQ15", "F.Cu", (170.63, 91.8222), (170.63, 91.6972), False),
    ("DQ15", "F.Cu", (171.35, 91.8222), (171.35, 91.6972), False),
    ("DQ15", "F.Cu", (172.07, 91.8222), (172.07, 91.6972), False),
    ("DQ15", "F.Cu", (172.79, 91.8222), (172.79, 91.6972), False),
    ("DQ15", "F.Cu", (173.51, 91.8222), (173.51, 91.6972), False),
    ("DQ15", "F.Cu", (174.23, 91.8222), (174.23, 91.6972), False),
    ("DQ15", "F.Cu", (174.95, 91.8222), (174.95, 91.6972), False),
    ("DQ15", "F.Cu", (175.67, 91.8222), (175.67, 91.6972), False),
    ("DQ15", "F.Cu", (176.39, 91.8222), (176.39, 91.6972), False),
)

WIDTH = "0.18"


def _helpers():
    """The #6396 record's parser/serializer, reused so both records agree."""
    spec = importlib.util.spec_from_file_location(
        "board07_repair_6396", ROOT / "repair_class_clearance_6396.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


H = _helpers()


def _q(p: Point) -> tuple[int, int]:
    return H.qi(p[0]), H.qi(p[1])


def _segments(text: str, net: str, layer: str, ends: tuple[Point, Point]):
    want = {_q(ends[0]), _q(ends[1])}
    hits = []
    for m in H.SEGMENT_RE.finditer(text):
        block = m[0]
        if H._field(block, "net") != net or H._field(block, "layer") != layer:
            continue
        if {H._point(block, "start"), H._point(block, "end")} == want:
            hits.append(m)
    if len(hits) != 1:
        raise ValueError(f"expected one {net} {layer} segment {ends}, found {len(hits)}")
    return hits[0]


def delete_segment(text: str, net: str, layer: str, ends) -> str:
    m = _segments(text, net, layer, ends)
    return text[: m.start()] + text[m.end() :]


def reshape_segment(text: str, net: str, layer: str, old, new) -> str:
    m = _segments(text, net, layer, old)
    block = m[0]
    forward = H._point(block, "start") == _q(old[0])
    a, b = (new[0], new[1]) if forward else (new[1], new[0])
    block = H._set_point(H._set_point(block, "start", _q(a)), "end", _q(b))
    return text[: m.start()] + block + text[m.end() :]


def segment_block(net: str, layer: str, ends) -> str:
    a, b = _q(ends[0]), _q(ends[1])
    key = f"kicad-tools/issue-9091/{net}/{layer}/{H.fmt(a[0])},{H.fmt(a[1])}/{H.fmt(b[0])},{H.fmt(b[1])}"
    return (
        "\t(segment\n"
        f"\t\t(start {H.fmt(a[0])} {H.fmt(a[1])})\n"
        f"\t\t(end {H.fmt(b[0])} {H.fmt(b[1])})\n"
        f"\t\t(width {WIDTH})\n"
        f'\t\t(layer "{layer}")\n'
        f'\t\t(net "{net}")\n'
        f'\t\t(uuid "{uuid.uuid5(uuid.NAMESPACE_URL, key)}")\n'
        "\t)\n"
    )


def add_segments(text: str, blocks: list[str]) -> str:
    last = None
    for last in H.SEGMENT_RE.finditer(text):
        pass
    if last is None:
        raise ValueError("no segment to anchor additions")
    return text[: last.end()] + "".join(blocks) + text[last.end() :]


def repair(text: str, *, authored: bool, log=None) -> str:
    def owned(flag: bool) -> bool:
        return flag or not authored

    for net, old, new, flag in VIA_MOVES:
        if owned(flag):
            text, jogs = H.move_via(text, net, old, new)
            if log:
                log(f"via    {net:6s} {old} -> {new}  jogs={jogs}")
    for net, layer, ends, flag in DELETES:
        if owned(flag):
            text = delete_segment(text, net, layer, ends)
            if log:
                log(f"delete {net:6s} {layer} {ends}")
    for net, layer, old, new, flag in RESHAPES:
        if owned(flag):
            text = reshape_segment(text, net, layer, old, new)
            if log:
                log(f"shape  {net:6s} {layer} {old} -> {new}")
    for net, layer, old, new, flag in POINT_MOVES:
        if owned(flag):
            text = H.move_point(text, net, layer, old, new)
            if log:
                log(f"point  {net:6s} {layer} {old} -> {new}")
    blocks = [segment_block(net, layer, ends) for net, layer, ends, flag in ADDS if owned(flag)]
    for net, layer, points, flag in TROMBONES:
        if owned(flag):
            blocks += [
                segment_block(net, layer, pair) for pair in zip(points, points[1:], strict=False)
            ]
    if blocks:
        text = add_segments(text, blocks)
        if log:
            log(f"add    {len(blocks)} segment(s)")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--verify-from-git",
        metavar="REV",
        help="Repair the pre-repair files from REV and compare with the working tree",
    )
    args = parser.parse_args()
    ok = True
    for path in (AUTHORED, REVIEWED):
        if args.verify_from_git:
            rel = path.relative_to(ROOT.parents[2]).as_posix()
            source = subprocess.run(
                ["git", "show", f"{args.verify_from_git}:{rel}"],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
        else:
            source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != PRE_REPAIR_SHA256[path]:
            print(f"{path.name}: input is not the pre-repair artifact", file=sys.stderr)
            return 1
        print(f"== {path.relative_to(ROOT)}")
        repaired = repair(source.decode(), authored=path == AUTHORED, log=print).encode()
        if args.verify_from_git:
            same = repaired == path.read_bytes()
            ok &= same
            print(f"   reproduces committed file: {same}")
        else:
            path.write_bytes(repaired)
            print(f"   sha256 {hashlib.sha256(repaired).hexdigest()}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

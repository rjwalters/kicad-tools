#!/usr/bin/env python3
"""One-off geometric repair of board 07's reviewed copper (Issue #6396).

Issue #6390 made both DRC engines hold board 07 to its own 0.175 mm
``AUX`` / ``SDRAM_*`` net classes (``authored-source/net_class_map.json``).
The hand-reviewed copper had only been validated against the 0.15 mm native
floor, and 42 class findings surfaced: 41 track-vs-via, 1 track-vs-pad.  The
classes stay at 0.175 mm; the copper moves instead.

This script is the complete, reviewable record of that change.  It applies a
fixed table of moves to the pre-repair artifacts and nothing else:

* ``VIA_MOVES`` -- 24 through-vias, each nudged by a few tens of microns
  (max 46.5 um) away from the class copper it was too close to.  Every move
  is on the 1 um grid.  The table was derived by a minimal-move search: the
  smallest displacement (plus a 10 um penalty per jog) that clears every
  other-net segment, via and pad on every copper layer by ``class + 5 um``, or
  ``class + 2 um`` for the three vias (``+3V3`` 142.863/96.25, ``A0``
  156.8/105.8, ``A2`` 149.5/99.25) boxed in on both sides, where no 5 um move
  exists. No pre-existing gap may drop below ``min(original, class + margin)``.
* Each via drags its attached same-net stubs with it and keeps them on
  0/45/90 degrees (``router/quantize.py::segment_angle_census``): a stub
  collinear with the move simply lengthens or shortens; otherwise the stub is
  cut on its own axis and closed with a short 0/45/90 jog (<= 0.06 mm, mostly
  inside the via's own 0.5 mm pad). Jog segments get deterministic UUIDs.
* ``POINT_MOVES`` -- the one track-vs-pad finding (a ``GND`` F.Cu escape over
  ``U1-37``/``UART_RX``, 2 um short): the horizontal run moves 7 um away from
  the pad, and the 45 degree leg to its via keeps its angle.

The authored physical source (``authored-source/sdram_demo.kicad_pcb``)
receives the moves whose copper it owns (``authored=True``); the reviewed
routing artifact, a superset of it, receives all of them.  Both inputs are
pinned to their pre-repair SHA-256 so the table can never be replayed onto
different copper.

Usage::

    # rewrite both artifacts in place (refuses unless they are pre-repair)
    uv run python boards/07-matchgroup-test/real_design/repair_class_clearance_6396.py

    # reproduce from git history and compare against the committed files
    uv run python boards/07-matchgroup-test/real_design/repair_class_clearance_6396.py \\
        --verify-from-git 810f833f

Afterwards re-pin ``authored-source/manifest.json`` and
``reviewed-routing/manifest.json`` and rerun ``build.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AUTHORED = ROOT / "authored-source/sdram_demo.kicad_pcb"
REVIEWED = ROOT / "reviewed-routing/sdram_demo.kicad_pcb"

#: SHA-256 of the pre-repair inputs (main 810f833f).
PRE_REPAIR_SHA256 = {
    AUTHORED: "d13e663fa64f0d1e6bec05a5fa5fb25cc9201266ac064adf8d93d81d6501b7e0",
    REVIEWED: "bd6ab53eef8014405caf46d0afb77e8bbd2e99e2a51b5b10b5c82260c24467d9",
}

#: ``(net, old (x, y), new (x, y), authored)`` in application order.
VIA_MOVES: tuple[tuple[str, tuple[float, float], tuple[float, float], bool], ...] = (
    ("+3V3", (124.138, 110.75), (124.158, 110.73), True),
    ("+3V3_A", (124.138, 111.75), (124.138, 111.77), True),
    ("SWDIO", (125.9, 99.5), (125.92, 99.48), False),
    ("A3", (127.8, 109.1), (127.78, 109.08), False),
    ("A1", (130.75, 100.75), (130.73, 100.73), False),
    ("+3V3", (142.863, 96.25), (142.863, 96.233), True),
    ("VCAP2", (142.863, 97.25), (142.843, 97.27), True),
    ("A0", (156.8, 105.8), (156.8, 105.783), False),
    ("DQ15", (161.3, 95.3), (161.32, 95.28), False),
    ("DQ13", (166.65, 99.2), (166.644, 99.18), False),
    ("UDQM", (167.4, 99.2), (167.406, 99.18), False),
    ("A8", (168.15, 118.4), (168.13, 118.42), False),
    ("A9", (170.1, 116.45), (170.08, 116.47), False),
    ("GND", (106.639, 81.3636), (106.654, 81.3786), True),
    ("SDNRAS", (160.1, 109.75), (160.104, 109.768), False),
    ("A0", (148.9, 98.7), (148.913, 98.687), False),
    ("A1", (156.5, 109.25), (156.542, 109.23), False),
    ("A3", (121.65, 103.75), (121.63, 103.77), False),
    ("SDNRAS", (152.0, 111.3), (151.982, 111.3), False),
    ("SDNWE", (155.7, 107.6), (155.691, 107.591), False),
    ("GND", (127.5, 111.787), (127.53, 111.787), True),
    ("DQ15", (156.8, 104.45), (156.804, 104.459), False),
    ("A2", (149.5, 99.25), (149.503, 99.238), False),
    ("DQ12", (158.75, 106.95), (158.739, 106.95), False),
)

#: ``(net, layer, old (x, y), new (x, y), authored)``: segment endpoints that
#: move together (the GND escape beside ``U1-37``).
POINT_MOVES: tuple[tuple[str, str, tuple[float, float], tuple[float, float], bool], ...] = (
    ("GND", "F.Cu", (125.25, 114.662), (125.25, 114.655), True),
    ("GND", "F.Cu", (124.549, 114.662), (124.542, 114.655), True),
)

Q = 10_000  # serialization quantum: 0.1 um
DIRS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
NUM = r"(-?[\d.]+)"
SEGMENT_RE = re.compile(r"^([ \t]*)\(segment\n.*?^\1\)\n", re.M | re.S)
VIA_RE = re.compile(r"^([ \t]*)\(via\n.*?^\1\)\n", re.M | re.S)


def qi(value: float) -> int:
    return round(value * Q)


def fmt(value: int) -> str:
    text = f"{value / Q:.4f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _point(block: str, key: str) -> tuple[int, int]:
    m = re.search(rf"\({key} {NUM} {NUM}\)", block)
    return qi(float(m[1])), qi(float(m[2]))


def _set_point(block: str, key: str, p: tuple[int, int]) -> str:
    new, n = re.subn(rf"\({key} {NUM} {NUM}\)", f"({key} {fmt(p[0])} {fmt(p[1])})", block, count=1)
    assert n == 1
    return new


def _field(block: str, key: str) -> str:
    return re.search(rf'\({key} "([^"]*)"\)', block)[1]


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


def reroute_stub(far: tuple[int, int], via: tuple[int, int], new: tuple[int, int]):
    """Pieces from ``far`` to ``new`` keeping ``far``->``via``'s axis.

    Returns ``[(far, new)]`` when the move is collinear with the stub, else
    ``[(far, q), (q, new)]`` with ``q`` on the original axis and the jog
    ``q->new`` on the shortest 0/45/90 direction.
    """
    dx, dy = via[0] - far[0], via[1] - far[1]
    if not (dx == 0 or dy == 0 or abs(dx) == abs(dy)):
        raise ValueError(f"stub {far}->{via} is not on 0/45/90")
    u = (_sign(dx), _sign(dy))
    ex, ey = new[0] - far[0], new[1] - far[1]
    if ex * u[1] - ey * u[0] == 0 and ex * u[0] + ey * u[1] > 0:
        return [(far, new)]
    best = None
    for e in DIRS:
        det = u[0] * e[1] - u[1] * e[0]
        if det == 0:
            continue
        t_num, r_num = ex * e[1] - ey * e[0], u[0] * ey - u[1] * ex
        if t_num % det or r_num % det:
            continue
        t, r = t_num // det, r_num // det
        if t <= 0 or r <= 0:
            continue
        length = r * math.hypot(*e)
        if best is None or length < best[0]:
            best = (length, (far[0] + t * u[0], far[1] + t * u[1]))
    if best is None:
        raise ValueError(f"no 0/45/90 jog from {far}->{via} to {new}")
    return [(far, best[1]), (best[1], new)]


def move_via(text: str, net: str, old: tuple[float, float], new: tuple[float, float]):
    """Move one via and re-attach its stubs; return ``(text, jog_count)``."""
    o, n = (qi(old[0]), qi(old[1])), (qi(new[0]), qi(new[1]))
    hits = [
        m for m in VIA_RE.finditer(text) if _field(m[0], "net") == net and _point(m[0], "at") == o
    ]
    if len(hits) != 1:
        raise ValueError(f"expected one {net} via at {old}, found {len(hits)}")
    m = hits[0]
    text = text[: m.start()] + _set_point(m[0], "at", n) + text[m.end() :]
    jogs = 0
    edits = []
    for s in SEGMENT_RE.finditer(text):
        block = s[0]
        if _field(block, "net") != net:
            continue
        ends = {"start": _point(block, "start"), "end": _point(block, "end")}
        at_via = [k for k, p in ends.items() if p == o]
        if not at_via:
            continue
        if len(at_via) != 1:
            raise ValueError(f"degenerate stub at {old}")
        key = at_via[0]
        far = ends["end" if key == "start" else "start"]
        pieces = reroute_stub(far, o, n)
        if len(pieces) == 1:
            edits.append((s.start(), s.end(), _set_point(block, key, n)))
            continue
        q = pieces[0][1]
        jogs += 1
        stub_uuid = _field(block, "uuid")
        jog_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kicad-tools/issue-6396/{stub_uuid}/jog"))
        jog = _set_point(_set_point(block, "start", q), "end", n).replace(stub_uuid, jog_uuid)
        edits.append((s.start(), s.end(), _set_point(block, key, q) + jog))
    if not edits:
        raise ValueError(f"{net} via at {old} has no attached stub")
    for start, end, repl in sorted(edits, reverse=True):
        text = text[:start] + repl + text[end:]
    return text, jogs


def move_point(text: str, net: str, layer: str, old, new) -> str:
    o, n = (qi(old[0]), qi(old[1])), (qi(new[0]), qi(new[1]))
    edits = []
    for s in SEGMENT_RE.finditer(text):
        block = s[0]
        if _field(block, "net") != net or _field(block, "layer") != layer:
            continue
        for key in ("start", "end"):
            if _point(block, key) == o:
                block = _set_point(block, key, n)
        if block != s[0]:
            edits.append((s.start(), s.end(), block))
    if len(edits) != 2:
        raise ValueError(f"expected two {net} {layer} segments at {old}, found {len(edits)}")
    for start, end, repl in sorted(edits, reverse=True):
        text = text[:start] + repl + text[end:]
    return text


def repair(text: str, *, authored: bool, log=None) -> str:
    for net, old, new, owned in VIA_MOVES:
        if authored and not owned:
            continue
        text, jogs = move_via(text, net, old, new)
        if log:
            dist = math.dist(old, new) * 1000
            log(f"{net:8s} {old} -> {new}  {dist:5.1f} um  jogs={jogs}")
    for net, layer, old, new, owned in POINT_MOVES:
        if authored and not owned:
            continue
        text = move_point(text, net, layer, old, new)
        if log:
            log(f"{net:8s} {layer} track point {old} -> {new}")
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

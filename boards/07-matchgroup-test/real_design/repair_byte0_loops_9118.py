#!/usr/bin/env python3
"""Scripted repair of board 07's SDRAM_BYTE0 same-net loops (Issue #9118).

PR #9102 (Issue #9091) removed the loops at U2.50 (DQ13) and U2.39 (UDQM) and
put their length back as real series length.  ``SDRAM_BYTE0`` has the same
defect on two nets.  This script is the complete, reviewable record of that
repair.  It applies fixed tables to the post-#9102 reviewed routing and nothing
else.  ``repair_copper_gap_9091.py`` stays the historical record of the byte-1
repair; its inputs are the pre-repair copper, so it cannot be re-run.

Loops
    DQ7 at U2.13 (164.75, 104.2).  The fan-out via at (165.91, 104.25) is
    joined to the pad by a straight F.Cu stub.  The stub carried on *through*
    the pad to x = 161.85, and a second branch (the authored escape) ran from
    the pad centre west along y = 104.2 and 104.08 and then a staircase joined
    the two 0.05 mm apart.  The via end of the stub is the whole signal path
    (1.16 mm).  Everything west of the pad is off-path copper: about 6.3 mm.

    LDQM at U1.141 (126.25, 94.3375).  The trunk climbs x = 126.3 from y = 95.1
    *through* the pad to y = 92.35, then follows the authored escape back down
    through (125.162, 91.09) to the pad centre.  The signal enters the pad at
    its south end.  The climb beyond the pad and the whole escape are
    off-path: about 7.2 mm.

    The signal never travels either loop, yet ``validate.py`` sums it as
    matched length (Issue #9117).  ``DELETES`` removes the off-path copper and
    ``RESHAPES`` ends each trunk inside its own pad on the pad's long axis.

Series length
    Measured as the shortest path through the copper
    (``physical_path.py``), the loop-free nets are 6-7 mm shorter than their
    byte-0 siblings: DQ7 57.07 mm and LDQM 56.02 mm against 62.60-63.52 mm,
    a spread of 7.51 mm against ``group_skew_mm`` 5.0.  ``TROMBONES`` restores
    that length as real series length, two loops each, laid by the repo's
    ``SerpentineGenerator.generate_trombone`` (the generator the board's
    original tuning used) with ``min_spacing=0.72``, ``gap_factor=1`` and
    ``side="outer"``, amplitude = added length / 4:

    * DQ7 adds 5.93 mm (amplitude 1.4825 mm) on the 6.05 mm F.Cu leg
      (138.8, 120.7) -> (144.85, 120.7), bulging south.
    * LDQM adds 6.98 mm (amplitude 1.745 mm) on the 6.27 mm F.Cu leg
      (132.59, 102.474) -> (132.59, 96.2), bulging east.

    The bulges sit in free F.Cu (at least 0.19 mm to any other-net copper).

Nothing here touches a tolerance.  The authored source is unchanged: the loop
copper exists only in the reviewed routing (the authored escape segments stay
in ``authored-source``; the reviewed routing never kept the authored fan-out
vias).

Usage::

    # rewrite the reviewed routing in place (refuses unless post-#9102)
    uv run python boards/07-matchgroup-test/real_design/repair_byte0_loops_9118.py

    # reproduce from git history and compare against the committed file
    uv run python boards/07-matchgroup-test/real_design/repair_byte0_loops_9118.py \\
        --verify-from-git c694302cd

Afterwards re-pin ``reviewed-routing/manifest.json`` and rerun ``build.py``.
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

#: SHA-256 of the inputs this table applies to (post-#9102 main, c694302cd).
INPUT_SHA256 = {
    AUTHORED: "51663e0156b541c9602bef2bf55266bbfde02545fb5264a2909fdd4cf4b7163a",
    REVIEWED: "b6585732be6793a1f6d290bfe725284e2bcf4a5b314c6de3f8ce1d8aa1ca24bc",
}

Point = tuple[float, float]

#: ``(net, layer, (a, b))``: segments to delete (either direction).
DELETES: tuple[tuple[str, str, tuple[Point, Point]], ...] = (
    # DQ7 (U2.13): the authored escape branch and the staircase that joins it.
    ("DQ7", "F.Cu", ((164.75, 104.2), (162.37, 104.2))),
    ("DQ7", "F.Cu", ((162.37, 104.2), (162.25, 104.08))),
    ("DQ7", "F.Cu", ((162.25, 104.08), (161.5, 104.08))),
    ("DQ7", "F.Cu", ((161.85, 104.25), (161.8, 104.2))),
    ("DQ7", "F.Cu", ((161.8, 104.2), (161.75, 104.2))),
    ("DQ7", "F.Cu", ((161.75, 104.2), (161.7, 104.15))),
    ("DQ7", "F.Cu", ((161.7, 104.15), (161.57, 104.15))),
    ("DQ7", "F.Cu", ((161.57, 104.15), (161.5, 104.08))),
    # DQ7: the trombone host (TROMBONES re-lays it with two loops).
    ("DQ7", "F.Cu", ((138.8, 120.7), (144.85, 120.7))),
    # LDQM (U1.141): the authored escape, which the trunk loops back along.
    ("LDQM", "F.Cu", ((126.25, 94.3375), (126.25, 92.9255))),
    ("LDQM", "F.Cu", ((126.25, 92.9255), (125.162, 91.8375))),
    ("LDQM", "F.Cu", ((125.162, 91.8375), (125.162, 91.0875))),
    ("LDQM", "F.Cu", ((125.162, 91.0875), (125.162, 91.212))),
    ("LDQM", "F.Cu", ((125.162, 91.212), (126.3, 92.35))),
    # LDQM: the trombone host.
    ("LDQM", "F.Cu", ((132.59, 102.474), (132.59, 96.2))),
)

#: ``(net, layer, (a, b), (a2, b2))``: a segment keeps its UUID and width but
#: gets new endpoints.
RESHAPES: tuple[tuple[str, str, tuple[Point, Point], tuple[Point, Point]], ...] = (
    # DQ7: the via stub ends inside U2.13 (pad x 163.995-165.505).
    ("DQ7", "F.Cu", ((165.91, 104.25), (161.85, 104.25)), ((165.91, 104.25), (164.75, 104.25))),
    # LDQM: the trunk's vertical ends at the pad centre's y, still on x = 126.3.
    ("LDQM", "F.Cu", ((126.3, 92.35), (126.3, 95.1)), ((126.3, 94.3375), (126.3, 95.1))),
)

#: ``(net, layer, points)``: trombones, laid as consecutive 0.18 mm segments.
#: Generated by ``SerpentineGenerator.generate_trombone`` (see the module
#: docstring) on the host leg each ``DELETES`` row removes.
TROMBONES: tuple[tuple[str, str, tuple[Point, ...]], ...] = (
    (
        "DQ7",
        "F.Cu",
        (
            (138.8, 120.7),
            (139.52, 120.7),
            (139.52, 122.1825),
            (140.24, 122.1825),
            (140.24, 120.7),
            (140.96, 120.7),
            (140.96, 122.1825),
            (141.68, 122.1825),
            (141.68, 120.7),
            (144.85, 120.7),
        ),
    ),
    (
        "LDQM",
        "F.Cu",
        (
            (132.59, 102.474),
            (132.59, 101.754),
            (134.335, 101.754),
            (134.335, 101.034),
            (132.59, 101.034),
            (132.59, 100.314),
            (134.335, 100.314),
            (134.335, 99.594),
            (132.59, 99.594),
            (132.59, 96.2),
        ),
    ),
)

WIDTH = "0.18"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The #9091 record's segment surgery, reused so both records agree.
R = _load("board07_repair_9091", "repair_copper_gap_9091.py")
H = R.H


def segment_block(net: str, layer: str, ends) -> str:
    a, b = R._q(ends[0]), R._q(ends[1])
    key = f"kicad-tools/issue-9118/{net}/{layer}/{H.fmt(a[0])},{H.fmt(a[1])}/{H.fmt(b[0])},{H.fmt(b[1])}"
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


def repair(text: str, *, log=None) -> str:
    for net, layer, ends in DELETES:
        text = R.delete_segment(text, net, layer, ends)
        if log:
            log(f"delete {net:5s} {layer} {ends}")
    for net, layer, old, new in RESHAPES:
        text = R.reshape_segment(text, net, layer, old, new)
        if log:
            log(f"shape  {net:5s} {layer} {old} -> {new}")
    blocks = []
    for net, layer, points in TROMBONES:
        blocks += [
            segment_block(net, layer, pair) for pair in zip(points, points[1:], strict=False)
        ]
    text = R.add_segments(text, blocks)
    if log:
        log(f"add    {len(blocks)} segment(s)")
    return text


def _source(rev: str | None, path: Path) -> bytes:
    if not rev:
        return path.read_bytes()
    rel = path.relative_to(ROOT.parents[2]).as_posix()
    return subprocess.run(
        ["git", "show", f"{rev}:{rel}"], cwd=ROOT, check=True, capture_output=True
    ).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--verify-from-git",
        metavar="REV",
        help="Repair the post-#9102 file from REV and compare with the working tree",
    )
    args = parser.parse_args()
    for path in (AUTHORED, REVIEWED):
        if hashlib.sha256(_source(args.verify_from_git, path)).hexdigest() != INPUT_SHA256[path]:
            print(f"{path.name}: input is not the post-#9102 artifact", file=sys.stderr)
            return 1
    if (
        args.verify_from_git
        and hashlib.sha256(AUTHORED.read_bytes()).hexdigest() != INPUT_SHA256[AUTHORED]
    ):
        print("authored-source changed: this repair does not own it", file=sys.stderr)
        return 1
    print(f"== {REVIEWED.relative_to(ROOT)}")
    repaired = repair(_source(args.verify_from_git, REVIEWED).decode(), log=print).encode()
    if args.verify_from_git:
        same = repaired == REVIEWED.read_bytes()
        print(f"   reproduces committed file: {same}")
        return 0 if same else 1
    REVIEWED.write_bytes(repaired)
    print(f"   sha256 {hashlib.sha256(repaired).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

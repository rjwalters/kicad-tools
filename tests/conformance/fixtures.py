"""The four hand-authored named fixtures, and how to regenerate them.

Each fixture reproduces a **specific, already-observed** disagreement between
``kicad-cli pcb drc`` and one of today's in-tree clearance consumers.  They are
not random cases that happened to fail: they are the evidence from #5398,
#5410, the search-vs-commit asymmetry found while curating Epic #5509, and the
router's rectangle-bounded pad model.

Every fixture is committed under ``tests/fixtures/conformance/`` as
``<name>-seed<N>.kicad_pcb`` + ``.kicad_pro``, so the boards under test are
reviewable artefacts rather than something regenerated at test time.  Regenerate
with::

    uv run python -m tests.conformance.fixtures --write

The seeds are the issue numbers the cases come from (5398, 5410, 5425, 5229)
rather than an arbitrary counter -- the filename then says where the case came
from without a lookup table.

Consumer expectations are recorded in each case's ``notes`` and asserted (as
report-only ``xfail``) by the adapter PR.  This module and
``test_named_fixtures.py`` assert **only** kicad-cli's verdict.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from tests.conformance.board import FIXTURES_DIR, write_case
from tests.conformance.generator import (
    CaseRules,
    CopperCase,
    PadSpec,
    PairIntent,
    PairKind,
    SegmentSpec,
    ViaSpec,
    pad_shape,
)

__all__ = [
    "FIXTURE_BUILDERS",
    "FIXTURE_RULES",
    "NAMED_FIXTURES",
    "build_named_fixture",
    "fixture_pcb_path",
    "roundrect_corner_support",
    "write_named_fixtures",
]

# ---------------------------------------------------------------------------
# Rule values, pinned
# ---------------------------------------------------------------------------
#
# These constants are the fixtures' whole point: a later "fix" that silences a
# disagreement by editing the fixture's clearance instead of the consumer must
# fail loudly.  ``test_named_fixtures.py`` asserts each fixture still carries
# exactly these numbers.

TRACE_CLEARANCE_MM = 0.15
"""Router-side trace clearance -- ``DEFAULT_ROUTE_CLEARANCE_MM`` (#5398)."""

VIA_CLEARANCE_MM = 0.20
"""Router-side via clearance -- the ``DesignRules`` default the CLI never sets."""

PROJECT_CLEARANCE_MM = 0.20
"""The ``.kicad_pro`` ``Default`` netclass clearance kicad-cli actually applies."""

HOLE_TO_HOLE_MM = 0.50
"""Drill-to-drill floor (#5410's 0.513 mm measurement is against this)."""

GRID_RESOLUTION_MM = 0.127
"""Routing grid pitch that produced #5410's six-cell via halo."""

FIXTURE_RULES = CaseRules(
    project_clearance=PROJECT_CLEARANCE_MM,
    trace_clearance=TRACE_CLEARANCE_MM,
    via_clearance=VIA_CLEARANCE_MM,
    min_hole_to_hole=HOLE_TO_HOLE_MM,
    min_drill_clearance=HOLE_TO_HOLE_MM,
    grid_resolution=GRID_RESOLUTION_MM,
)

_TRACE_WIDTH_MM = 0.15
_VIA_DIAMETER_MM = 0.6
_VIA_DRILL_MM = 0.3


# ---------------------------------------------------------------------------
# Fixture 1 -- #5398: segment vs. via at 0.18 mm, order-dependent verdict
# ---------------------------------------------------------------------------


def build_issue5398_seg_via_order() -> CopperCase:
    """0.18 mm copper gap between a 0.15 mm track and a 0.6/0.3 via.

    The gap sits in the window ``trace_clearance (0.15) < 0.18 <
    via_clearance (0.20) == project Default (0.20)``, which makes the answer
    depend on *insertion order* inside a single backend:

    * via committed first, then the segment -- ``validate_segment_clearance``
      compares against ``trace_clearance`` and **accepts**
      (``grid.py:4041-4055``; C++ twin ``grid.cpp:1394``);
    * segment first, then the via -- ``validate_via_clearance`` compares
      against ``via_clearance`` and **rejects** (``grid.py:4083-4084``;
      ``grid.cpp:1481``).

    kicad-cli has no such notion of order: the project's ``Default`` class is
    0.20 mm, so it reports one clearance violation either way.  The board is
    therefore order-free; the adapters replay both orders against it.
    """
    gap = 0.18
    seg_y = 15.0
    via_y = seg_y + _TRACE_WIDTH_MM / 2.0 + gap + _VIA_DIAMETER_MM / 2.0

    segment = SegmentSpec(
        net="SENSE_TRACK",
        start=(10.0, seg_y),
        end=(30.0, seg_y),
        width=_TRACE_WIDTH_MM,
        layer="F.Cu",
    )
    via = ViaSpec(
        net="SENSE_VIA",
        x=20.0,
        y=round(via_y, 6),
        diameter=_VIA_DIAMETER_MM,
        drill=_VIA_DRILL_MM,
    )
    return CopperCase(
        seed=5398,
        name="issue5398-seg-via-0p18-order",
        width=40.0,
        height=30.0,
        layers=2,
        rules=FIXTURE_RULES,
        segments=(segment,),
        vias=(via,),
        pairs=(
            PairIntent(
                kind=PairKind.SEG_VIA,
                net_a=segment.net,
                net_b=via.net,
                target_gap_mm=gap,
                required_mm=PROJECT_CLEARANCE_MM,
                expect_violation=True,
            ),
        ),
        notes=(
            "#5398: 0.18 mm gap between a 0.15 mm track and a 0.6/0.3 via. "
            "kicad-cli flags it against the project Default class (0.20 mm). "
            "Expected today: Python/C++ grid commit gates ACCEPT when the via "
            "is inserted first and REJECT when the segment is inserted first."
        ),
    )


# ---------------------------------------------------------------------------
# Fixture 2 -- #5410: a legal via the grid halo refused
# ---------------------------------------------------------------------------


def build_issue5410_dqs_n_halo() -> CopperCase:
    """The DQ3 via that board-07's A* refused next to a DQS_N via.

    Coordinates are #5410's verbatim: DQS_N at ``(143.777, 123.800)``, the
    candidate DQ3 via at ``(143.142, 123.292)``.  That is a 0.213 mm copper
    gap against a 0.20 mm requirement and a 0.513 mm drill gap against a
    0.50 mm floor -- **legal on both counts**, and kicad-cli says so.

    The grid marked DQS_N's halo as a Chebyshev *square* of
    ``int((0.3 + 0.2 + 0.075) / 0.127) + 1 + 1 = 6`` cells
    (``grid.py:_mark_via``), which swallows this candidate.  A* burned
    1,000,000 expansions and reported the net unroutable.  The board is sized
    160x140 mm and written with ``center=False`` so these coordinates can be
    quoted exactly as the issue recorded them.
    """
    a = ViaSpec(
        net="DQS_N",
        x=143.777,
        y=123.800,
        diameter=_VIA_DIAMETER_MM,
        drill=_VIA_DRILL_MM,
    )
    b = ViaSpec(
        net="DQ3",
        x=143.142,
        y=123.292,
        diameter=_VIA_DIAMETER_MM,
        drill=_VIA_DRILL_MM,
    )
    centre_distance = math.dist((a.x, a.y), (b.x, b.y))
    copper_gap = centre_distance - _VIA_DIAMETER_MM
    return CopperCase(
        seed=5410,
        name="issue5410-dqs-n-halo-vs-legal-via",
        width=160.0,
        height=140.0,
        layers=4,
        rules=FIXTURE_RULES,
        vias=(a, b),
        pairs=(
            PairIntent(
                kind=PairKind.VIA_VIA,
                net_a=a.net,
                net_b=b.net,
                target_gap_mm=round(copper_gap, 6),
                required_mm=PROJECT_CLEARANCE_MM,
                expect_violation=False,
            ),
        ),
        notes=(
            "#5410: DQ3 via 0.213 mm copper / 0.513 mm drill from DQS_N, "
            "against 0.20 / 0.50 requirements -- legal, and kicad-cli agrees. "
            "Expected today: the grid-occupancy consumer REJECTS it (six-cell "
            "Chebyshev square halo on a 0.127 mm grid) while the commit "
            "validators and the halo-geometry refinement accept it."
        ),
    )


# ---------------------------------------------------------------------------
# Fixture 3 -- search rejects what commit accepts, same backend, same pair
# ---------------------------------------------------------------------------


def build_search_vs_commit_seg_via_max() -> CopperCase:
    """A 0.18 mm segment/via gap that search refuses and commit accepts.

    Here the project's ``Default`` class is **0.15 mm**, so kicad-cli is
    clean.  The router disagrees with itself:

    * ``RouteHaloGeometry.clear`` raises the requirement to
      ``max(required, rules.via_clearance)`` for a trace-vs-via pair
      (``route_halo_geometry.py:235``; C++ twin ``grid.cpp:957``), i.e. 0.20 --
      so A* **refuses** this candidate;
    * the commit validators compare the same pair against ``trace_clearance``
      (0.15) and **accept** it (``grid.cpp:1394``).

    Same run, same backend, same two objects, opposite answers -- which is why
    a net can look unroutable and still pass validation once routed.
    """
    rules = CaseRules(
        project_clearance=TRACE_CLEARANCE_MM,  # 0.15 -- deliberately below the gap
        trace_clearance=TRACE_CLEARANCE_MM,
        via_clearance=VIA_CLEARANCE_MM,
        min_hole_to_hole=HOLE_TO_HOLE_MM,
        min_drill_clearance=HOLE_TO_HOLE_MM,
        grid_resolution=GRID_RESOLUTION_MM,
    )
    gap = 0.18
    seg_y = 15.0
    via_y = seg_y + _TRACE_WIDTH_MM / 2.0 + gap + _VIA_DIAMETER_MM / 2.0
    segment = SegmentSpec(
        net="SEARCH_TRACK",
        start=(10.0, seg_y),
        end=(30.0, seg_y),
        width=_TRACE_WIDTH_MM,
        layer="F.Cu",
    )
    via = ViaSpec(
        net="COMMIT_VIA",
        x=20.0,
        y=round(via_y, 6),
        diameter=_VIA_DIAMETER_MM,
        drill=_VIA_DRILL_MM,
    )
    return CopperCase(
        seed=5425,
        name="search-vs-commit-seg-via-max",
        width=40.0,
        height=30.0,
        layers=2,
        rules=rules,
        segments=(segment,),
        vias=(via,),
        pairs=(
            PairIntent(
                kind=PairKind.SEG_VIA,
                net_a=segment.net,
                net_b=via.net,
                target_gap_mm=gap,
                required_mm=TRACE_CLEARANCE_MM,
                expect_violation=False,
            ),
        ),
        notes=(
            "Search-vs-commit asymmetry: 0.18 mm gap with the project Default "
            "class at 0.15 mm, so kicad-cli is clean. Expected today: the "
            "route-halo geometry consumer REJECTS (max(required, "
            "via_clearance) = 0.20) while the Python and C++ commit gates "
            "ACCEPT (trace_clearance = 0.15)."
        ),
    )


# ---------------------------------------------------------------------------
# Fixture 4 -- roundrect corner: exact outline vs. rectangle bounds
# ---------------------------------------------------------------------------


def roundrect_corner_support(width: float, height: float, rratio: float) -> float:
    """Distance from a roundrect's centre to its outline along the diagonal.

    The corner radius is ``rratio * min(width, height)``.  The arc's centre
    sits at ``(w/2 - r, h/2 - r)`` in the pad frame, so the outline point on
    the corner diagonal is ``hypot(w/2 - r, h/2 - r) + r`` from the pad
    centre -- strictly less than the bare rectangle's ``hypot(w/2, h/2)``.
    That difference is the entire fixture.
    """
    r = rratio * min(width, height)
    return math.hypot(width / 2.0 - r, height / 2.0 - r) + r


def build_roundrect_corner_gap() -> CopperCase:
    """A track at a rotated roundrect's corner: legal outline, illegal bbox.

    A 1.0x1.0 mm roundrect pad (``rratio`` 0.25, so a 0.25 mm corner radius)
    rotated 45 degrees.  Its outline reaches 0.6036 mm along the corner
    diagonal; the *rectangle* the router models it as reaches 0.7071 mm.  The
    probe track is placed to leave a 0.22 mm gap to the true outline:

    * exact polygon model (``kct check``'s ``_pad_polygon``, and kicad-cli):
      0.22 mm >= 0.20 mm -- **clean**;
    * rectangle-bounded model (``grid.cpp:537 pad_rect_distance``; "Oval and
      roundrect pads retain conservative rectangle bounds",
      ``grid.cpp:1221-1223``): 0.1164 mm < 0.20 mm -- **rejected**.

    Because the pad is square and rotated 45 degrees, one corner diagonal
    points along the board's ``+X`` axis under either rotation sign, so the
    construction does not depend on the pad-rotation convention.
    """
    shape = pad_shape("Pad_RoundRect")
    assert shape.roundrect_rratio is not None
    pad_w, pad_h = shape.size
    support = roundrect_corner_support(pad_w, pad_h, shape.roundrect_rratio)
    exact_gap = 0.22

    pad = PadSpec(
        net="PAD_ROUNDRECT",
        reference="P1",
        footprint=shape.name,
        x=20.0,
        y=15.0,
        rotation=45.0,
        layer="F.Cu",
    )
    track_x = pad.x + support + exact_gap + _TRACE_WIDTH_MM / 2.0
    segment = SegmentSpec(
        net="CORNER_TRACK",
        start=(round(track_x, 6), 13.0),
        end=(round(track_x, 6), 17.0),
        width=_TRACE_WIDTH_MM,
        layer="F.Cu",
    )
    return CopperCase(
        seed=5229,
        name="roundrect-corner-gap",
        width=40.0,
        height=30.0,
        layers=2,
        rules=FIXTURE_RULES,
        segments=(segment,),
        pads=(pad,),
        pairs=(
            PairIntent(
                kind=PairKind.PAD_SEG,
                net_a=pad.net,
                net_b=segment.net,
                target_gap_mm=exact_gap,
                required_mm=PROJECT_CLEARANCE_MM,
                expect_violation=False,
            ),
        ),
        notes=(
            "Roundrect corner: 0.22 mm to the exact outline (clean for "
            "kicad-cli and for kct check's polygon pad model) but 0.1164 mm "
            "to the rectangle bounds the router uses. Expected today: the C++ "
            "grid REJECTS, the kct check ClearanceRule ACCEPTS."
        ),
    )


FIXTURE_BUILDERS = {
    "issue5398-seg-via-0p18-order": build_issue5398_seg_via_order,
    "issue5410-dqs-n-halo-vs-legal-via": build_issue5410_dqs_n_halo,
    "search-vs-commit-seg-via-max": build_search_vs_commit_seg_via_max,
    "roundrect-corner-gap": build_roundrect_corner_gap,
}

NAMED_FIXTURES = tuple(FIXTURE_BUILDERS)


def build_named_fixture(name: str) -> CopperCase:
    """Build one named fixture's :class:`CopperCase` by name."""
    try:
        builder = FIXTURE_BUILDERS[name]
    except KeyError:
        raise KeyError(
            f"Unknown conformance fixture {name!r}; known: {sorted(FIXTURE_BUILDERS)}"
        ) from None
    return builder()


def fixture_stem(case: CopperCase) -> str:
    """``<name>-seed<N>`` -- the committed filename stem."""
    return f"{case.name}-seed{case.seed}"


def fixture_pcb_path(name: str, directory: Path | None = None) -> Path:
    """Path of a committed fixture board."""
    case = build_named_fixture(name)
    return (directory or FIXTURES_DIR) / f"{fixture_stem(case)}.kicad_pcb"


def write_named_fixtures(directory: Path | None = None) -> list[Path]:
    """Write every named fixture; returns the ``.kicad_pcb`` paths written."""
    target = Path(directory) if directory is not None else FIXTURES_DIR
    written: list[Path] = []
    for name in NAMED_FIXTURES:
        case = build_named_fixture(name)
        board = write_case(case, target, stem=fixture_stem(case))
        written.append(board.pcb_path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the committed conformance fixture boards."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the boards (without it, only the destination is printed).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"Destination directory (default: {FIXTURES_DIR}).",
    )
    args = parser.parse_args(argv)

    target = args.out or FIXTURES_DIR
    if not args.write:
        print(f"Would write {len(NAMED_FIXTURES)} fixtures to {target}")
        for name in NAMED_FIXTURES:
            print(f"  {fixture_stem(build_named_fixture(name))}")
        return 0

    for path in write_named_fixtures(target):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())

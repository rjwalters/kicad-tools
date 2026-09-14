"""Native-parity regression for same-zone fill-fragment bonding (Issue #5362).

Background
----------
#5358's Board03 repair produced a board where native KiCad reported one
``unconnected_items`` result between the R13/R14 VCC supply copper while this
repo's connectivity analyzers reported the board fully connected.  The
responsible defect was in how both analyzers bonded a zone's
``filled_polygon`` fragments to each other:

* :meth:`NetStatusAnalyzer._build_fill_island_groups` accumulated every
  fragment of a ``zone`` object into one pad group, and
* :meth:`ConnectivityValidator._connect_pour_pads_label_free` unioned every
  pad bonded to any fragment of a ``zone``,

both on the premise (stated verbatim in the latter's docstring) that "all
fragments of one ``zone`` are the same net ... DRC guarantees retained
fragments are electrically bonded".  That premise is false.

Native ground truth
-------------------
Measured with ``kicad-cli pcb drc --format json`` under
``kicad/kicad:10.0`` digest ``sha256:182c8005cb77...`` (KiCad **10.0.5**, the
image digest recorded in #5358/#5362), with **no** ``--refill-zones`` and no
``--save-board``; the board SHA256 was identical before and after every run.
Two same-zone fill fragments carrying one pad each report:

===============================  =====================  ==================
zone encoding                    fragment gap (mm)      unconnected_items
===============================  =====================  ==================
``filled_areas_thickness`` absent  0.0 / 0.05 / 0.1 /
(format default = ``yes``,         0.2 / 0.24 / 0.25    0
``min_thickness 0.25``)            0.26 / 0.3 / 0.5 /
                                   2.0                  1
``(filled_areas_thickness no)``    every gap above,
                                   including 0.0        1
===============================  =====================  ==================

i.e. the stored outlines are centre-lines of ``min_thickness``-wide copper
unless ``filled_areas_thickness`` is explicitly ``no``, and two fragments are
one piece of metal exactly when their stored outlines are within
``min_thickness`` of each other.  With the ``no`` encoding the stored outline
*is* the copper and native KiCad never bonds two fill outlines of one zone to
each other -- not at a shared corner, not along a shared edge, and not across
a genuinely overlapping band.  In every encoding a real conductor (a pad, a
via or a track whose copper reaches into both fragments) does bond them.

The fixtures below are that measurement series, one board per row, asserted
against both analyzers.  Each case's ``native`` field records the measured
``unconnected_items`` count for the identical bytes this module generates.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.connectivity import ConnectivityValidator

pytest.importorskip("shapely")


_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup)
  (net 0 "")
  (net 1 "GND")
  (gr_line (start 5 5) (end 45 5) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 45 5) (end 45 45) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 45 45) (end 5 45) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 5 45) (end 5 5) (layer "Edge.Cuts") (width 0.05))
  (footprint "R" (layer "F.Cu") (uuid "10000000-0000-0000-0000-000000000001")
    (at {p1x} {p1y})
    (property "Reference" "R13" (at 0 0) (layer "F.Cu")
      (uuid "20000000-0000-0000-0000-000000000001")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND")
      (uuid "30000000-0000-0000-0000-000000000001"))
  )
  (footprint "R" (layer "F.Cu") (uuid "10000000-0000-0000-0000-000000000002")
    (at {p2x} {p2y})
    (property "Reference" "R14" (at 0 0) (layer "F.Cu")
      (uuid "20000000-0000-0000-0000-000000000002")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND")
      (uuid "30000000-0000-0000-0000-000000000002"))
  )
{extra}  (zone
    (net 1)
    (net_name "GND")
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (hatch edge 0.5)
    (connect_pads (clearance 0))
    (min_thickness {min_thickness})
{fat}    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))
    (polygon (pts (xy 8 8) (xy 42 8) (xy 42 42) (xy 8 42)))
{fills}  )
)
"""

_FAT_NO = "    (filled_areas_thickness no)\n"


def _fill(points: list[tuple[float, float]]) -> str:
    pts = " ".join(f"(xy {x} {y})" for x, y in points)
    return f'    (filled_polygon (layer "F.Cu") (pts {pts}))\n'


def _rect(x0: float, x1: float, y0: float = 20, y1: float = 30) -> list[tuple[float, float]]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


@dataclass(frozen=True)
class Case:
    """One measured board: geometry plus its native ``unconnected_items`` count."""

    name: str
    fills: str
    native_unconnected: int
    min_thickness: float = 0.25
    stroke_encoded: bool = False  # True => omit (filled_areas_thickness no)
    p1: tuple[float, float] = (12, 25)
    p2: tuple[float, float] = (28, 25)
    extra: str = ""

    @property
    def connected(self) -> bool:
        """Native verdict: the two supply pads share one copper component."""
        return self.native_unconnected == 0

    def board(self) -> str:
        return _BOARD.format(
            p1x=self.p1[0],
            p1y=self.p1[1],
            p2x=self.p2[0],
            p2y=self.p2[1],
            min_thickness=self.min_thickness,
            fat="" if self.stroke_encoded else _FAT_NO,
            fills=self.fills,
            extra=self.extra,
        )


_TWO_RECTS_GAP = {
    gap: _fill(_rect(10, 20)) + _fill(_rect(20 + gap, 30))
    for gap in (0.0, 0.05, 0.1, 0.2, 0.24, 0.25, 0.26, 0.3, 0.5, 2.0)
}

# --- the measured series (see module docstring for provenance) ---------------
CASES: list[Case] = [
    # Stroke-encoded fills (KiCad format default): fragments bond iff their
    # gap is within min_thickness (0.25 mm here).
    *[
        Case(f"stroke_gap{gap}", _TWO_RECTS_GAP[gap], 0, stroke_encoded=True)
        for gap in (0.0, 0.05, 0.1, 0.2, 0.24, 0.25)
    ],
    *[
        Case(f"stroke_gap{gap}", _TWO_RECTS_GAP[gap], 1, stroke_encoded=True)
        for gap in (0.26, 0.3, 0.5, 2.0)
    ],
    # (filled_areas_thickness no): the stored outline IS the copper, and no
    # amount of fragment-to-fragment adjacency bonds them.
    *[
        Case(f"solid_gap{gap}", _TWO_RECTS_GAP[gap], 1)
        for gap in (0.0, 0.05, 0.1, 0.2, 0.24, 0.25, 0.26, 0.3, 0.5, 2.0)
    ],
    # Degenerate contacts, solid-encoded: a shared corner and a shared edge
    # are not copper, and neither is a genuinely overlapping band.
    Case(
        "solid_point_touch",
        _fill(_rect(10, 18, 20, 25)) + _fill(_rect(18, 30, 25, 30)),
        1,
        p1=(14, 22),
        p2=(26, 28),
    ),
    Case("solid_edge_touch", _fill(_rect(10, 18)) + _fill(_rect(18, 30)), 1),
    Case("solid_overlap_band", _fill(_rect(10, 19)) + _fill(_rect(17, 30)), 1),
    # Genuinely-connected controls that must keep working.
    Case("single_fragment", _fill(_rect(10, 30)), 0, p1=(14, 25), p2=(26, 25)),
    Case("identical_fragments", _fill(_rect(10, 30)) * 2, 0, p1=(14, 25), p2=(26, 25)),
    Case(
        "bridged_by_track",
        _fill(_rect(10, 18)) + _fill(_rect(20, 30)),
        0,
        p1=(14, 25),
        p2=(26, 25),
        extra=(
            '  (segment (start 17 25) (end 21 25) (width 0.3) (layer "F.Cu") (net 1)\n'
            '    (uuid "40000000-0000-0000-0000-000000000001"))\n'
        ),
    ),
    Case(
        "pad_bridges_fragments",
        _fill(_rect(10, 20)) + _fill(_rect(22, 32)),
        0,
        p1=(14, 25),
        p2=(26, 25),
        extra=(
            '  (footprint "TP" (layer "F.Cu")\n'
            '    (uuid "10000000-0000-0000-0000-000000000003") (at 21 25)\n'
            '    (property "Reference" "TP1" (at 0 0) (layer "F.Cu")\n'
            '      (uuid "20000000-0000-0000-0000-000000000003")\n'
            "      (effects (font (size 1 1) (thickness 0.15))))\n"
            '    (pad "1" smd rect (at 0 0) (size 3 3) (layers "F.Cu") (net 1 "GND")\n'
            '      (uuid "30000000-0000-0000-0000-000000000003"))\n'
            "  )\n"
        ),
    ),
]


@pytest.fixture(params=CASES, ids=lambda case: case.name)
def case(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Case, Path]:
    board_case: Case = request.param
    path = tmp_path / f"{board_case.name}.kicad_pcb"
    path.write_text(board_case.board())
    return board_case, path


def test_net_status_matches_native_fill_fragment_bonding(
    case: tuple[Case, Path],
) -> None:
    """``NetStatusAnalyzer`` agrees with native KiCad on every measured board.

    Before #5362 every ``solid_*`` and ``stroke_gap>=0.26`` row reported
    ``complete`` (one island) because a zone's fragments were unioned by zone
    identity alone -- the net-status half of the R13/R14 VCC false negative.
    """
    board_case, path = case
    status = NetStatusAnalyzer(path, strict=True).analyze().get_net("GND")
    assert status is not None
    expected_islands = 1 if board_case.connected else 2
    assert status.island_count == expected_islands, (
        f"{board_case.name}: native kicad-cli reported "
        f"{board_case.native_unconnected} unconnected_items "
        f"(=> {expected_islands} copper islands) but net-status found "
        f"{status.island_count} (status={status.status})"
    )


def test_pad_partition_matches_native_fill_fragment_bonding(
    case: tuple[Case, Path],
) -> None:
    """``extract_pad_partition`` agrees with native KiCad on every measured board.

    This is the copper-LVS half: the same fragment-bonding question decides
    whether the two supply pads land in one physical component.
    """
    board_case, path = case
    partition = ConnectivityValidator(path).extract_pad_partition()
    component = next(c for c in partition if "R13.1" in c)
    assert ("R14.1" in component) is board_case.connected, (
        f"{board_case.name}: native kicad-cli reported "
        f"{board_case.native_unconnected} unconnected_items but the copper "
        f"partition gave {sorted(component)} (full partition={partition})"
    )


def test_zone_reports_stroke_inflation_from_filled_areas_thickness(
    tmp_path: Path,
) -> None:
    """``filled_areas_thickness`` drives ``Zone.fill_inflation`` (default yes)."""
    solid = tmp_path / "solid.kicad_pcb"
    solid.write_text(CASES[-1].board())  # carries (filled_areas_thickness no)
    stroke = tmp_path / "stroke.kicad_pcb"
    stroke.write_text(Case("stroke", _fill(_rect(10, 30)), 0, stroke_encoded=True).board())

    solid_zone = PCB.load(solid).zones[0]
    assert solid_zone.filled_areas_thickness is False
    assert solid_zone.fill_inflation() == 0.0

    stroke_zone = PCB.load(stroke).zones[0]
    assert stroke_zone.filled_areas_thickness is True
    assert stroke_zone.fill_inflation() == pytest.approx(0.125)

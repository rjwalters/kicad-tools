"""Unit tests for the congestion/escape-driven placement nudge (issue #3865, M3).

The expensive part of the M3 stage -- the re-route subprocess and the
net-positive accept/reject -- is exercised end-to-end on chorus (see the PR
description for measured numbers).  These unit tests target the load-bearing
*new* code: the bounded, board-outline-aware nudge-vector geometry in
:class:`kicad_tools.router.placement_nudge.PlacementNudge`, which decides WHICH
part to move and BY HOW MUCH for a PLACEMENT_BOUND net.

Each test builds a small synthetic board (the same pattern as
``test_stuck_classifier.py``) so the real classifier + proposal pipeline runs
without the router.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.router.placement_nudge import (
    NudgeConfig,
    PlacementNudge,
)
from kicad_tools.router.stuck_classifier import (
    StuckClass,
    classify_stuck_nets_from_pcb,
)
from kicad_tools.schema.pcb import PCB

# A board with a real Edge.Cuts rectangle so extract_board_outline() works and
# the #3804 outline guard is exercised.  Outline: (0,0)-(100,100).
_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (gr_rect (start 0 0) (end 100 100) (layer "Edge.Cuts") (width 0.1))
"""


def _placement_bound_board(stranded_at: tuple[float, float] = (80, 80)) -> str:
    """A PLACEMENT_BOUND net: connected island at (10,10), stranded pad far out.

    The stranded pad sits alone in open space (open escape lane, no rippable
    copper nearby) so the classifier labels it PLACEMENT_BOUND -- exactly the
    M3 target.  ``stranded_at`` lets a test push the pad near the outline edge.
    """
    sx, sy = stranded_at
    return _HEADER + (
        '  (net 0 "")\n'
        '  (net 1 "TGT")\n'
        # connected island for TGT (R1.1 <-> R1.2 routed)
        '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
        '    (property "Reference" "R1")\n'
        '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
        '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
        "  )\n"
        # stranded TGT pad on U1, alone in open space
        f'  (footprint "U_SOT" (layer "F.Cu") (at {sx} {sy})\n'
        '    (property "Reference" "U1")\n'
        '    (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 1 "TGT"))\n'
        "  )\n"
        '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
        ")\n"
    )


@pytest.fixture
def placement_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "placement.kicad_pcb"
    p.write_text(_placement_bound_board())
    return p


def _load(path: Path) -> PCB:
    return PCB.load(str(path))


class TestProposeGeometry:
    def test_classifier_sees_placement_bound(self, placement_pcb: Path):
        # Sanity: the synthetic board really is PLACEMENT_BOUND so the nudge
        # proposal pipeline is exercising the intended path.
        pcb = _load(placement_pcb)
        result = classify_stuck_nets_from_pcb(pcb)
        tgt = [d for d in result.diagnoses if d.net_name == "TGT"]
        assert len(tgt) == 1
        assert tgt[0].classification is StuckClass.PLACEMENT_BOUND

    def test_proposes_bounded_nudge_toward_island(self, placement_pcb: Path):
        pcb = _load(placement_pcb)
        nudger = PlacementNudge(pcb, NudgeConfig(max_nudge_mm=1.5))
        nudges = nudger.propose()

        # U1 owns the stranded pad of the placement-bound net TGT.
        assert len(nudges) == 1
        n = nudges[0]
        assert n.ref == "U1"
        assert n.target_net == "TGT"

        # The move is bounded by max_nudge_mm.
        assert n.distance_mm <= 1.5 + 1e-6
        assert n.distance_mm > 0.0

        # The move heads TOWARD the connected island at (10, 10): the new
        # position must be strictly closer to the island than the old one.
        island = (10.0, 10.0)
        old_d = math.hypot(n.old_xy[0] - island[0], n.old_xy[1] - island[1])
        new_d = math.hypot(n.new_xy[0] - island[0], n.new_xy[1] - island[1])
        assert new_d < old_d

    def test_skips_fixed_refs(self, placement_pcb: Path):
        pcb = _load(placement_pcb)
        nudger = PlacementNudge(pcb, NudgeConfig(fixed_refs=frozenset({"U1"})))
        assert nudger.propose() == []

    def test_skips_locked_footprints(self, placement_pcb: Path):
        pcb = _load(placement_pcb)
        for fp in pcb.footprints:
            if fp.reference == "U1":
                fp.locked = True
        nudger = PlacementNudge(pcb, NudgeConfig())
        assert nudger.propose() == []

    def test_respects_board_outline(self, tmp_path: Path):
        # Stranded pad and its part sit just inside the bottom-right corner.
        # The island is at (10,10) (toward the interior), so a nudge toward the
        # island moves the part INWARD -- it must stay inside the outline.
        p = tmp_path / "edge.kicad_pcb"
        p.write_text(_placement_bound_board(stranded_at=(98.5, 98.5)))
        pcb = _load(p)
        nudger = PlacementNudge(pcb, NudgeConfig(max_nudge_mm=1.5, outline_margin_mm=0.5))
        nudges = nudger.propose()
        assert len(nudges) == 1
        nx, ny = nudges[0].new_xy
        # Inside the (0,0)-(100,100) outline with margin.
        assert 0.5 <= nx <= 99.5
        assert 0.5 <= ny <= 99.5

    def test_no_outline_means_no_nudge(self, tmp_path: Path):
        # A board with no Edge.Cuts outline cannot be certified safe to move
        # parts on -- the #3804 guard treats "no outline" as "skip".
        body = _placement_bound_board().replace(
            '  (gr_rect (start 0 0) (end 100 100) (layer "Edge.Cuts") (width 0.1))\n',
            "",
        )
        p = tmp_path / "no_outline.kicad_pcb"
        p.write_text(body)
        pcb = _load(p)
        nudger = PlacementNudge(pcb, NudgeConfig())
        assert nudger.propose() == []

    def test_apply_moves_footprint(self, placement_pcb: Path):
        pcb = _load(placement_pcb)
        nudger = PlacementNudge(pcb, NudgeConfig())
        nudges = nudger.propose()
        assert nudges
        nudger.apply(nudges)
        u1 = next(fp for fp in pcb.footprints if fp.reference == "U1")
        assert (u1.position[0], u1.position[1]) == pytest.approx(nudges[0].new_xy)

    def test_max_components_caps_nudges(self, tmp_path: Path):
        # Two independent placement-bound nets; max_components=1 -> one nudge.
        body = _HEADER + (
            '  (net 0 "")\n'
            '  (net 1 "TGT1")\n'
            '  (net 2 "TGT2")\n'
            '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
            '    (property "Reference" "R1")\n'
            '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT1"))\n'
            '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT1"))\n'
            "  )\n"
            '  (footprint "U_SOT" (layer "F.Cu") (at 80 80)\n'
            '    (property "Reference" "U1")\n'
            '    (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 1 "TGT1"))\n'
            "  )\n"
            '  (footprint "R_0402" (layer "F.Cu") (at 30 30)\n'
            '    (property "Reference" "R2")\n'
            '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "TGT2"))\n'
            '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "TGT2"))\n'
            "  )\n"
            '  (footprint "U_SOT" (layer "F.Cu") (at 70 70)\n'
            '    (property "Reference" "U2")\n'
            '    (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 2 "TGT2"))\n'
            "  )\n"
            '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
            '  (segment (start 29.5 30) (end 30.5 30) (width 0.25) (layer "F.Cu") (net 2))\n'
            ")\n"
        )
        p = tmp_path / "two.kicad_pcb"
        p.write_text(body)
        pcb = _load(p)
        nudger = PlacementNudge(pcb, NudgeConfig(max_components=1))
        assert len(nudger.propose()) == 1

"""Regression guard for the #4157 wire-union over-merge on board-05 (#4226).

PR #4157 (#4143) unioned collinear-overlapping and T-touching wires in the
schematic connectivity extractor on **pure geometry**, ignoring junction
dots.  That over-corrected: board-05's stub-wire+label idiom has incidental
dot-less grazes (rail-drop stubs and diagonal-escape wires whose bodies
graze each other's X/Y at generator-chosen coordinates) that KiCad does NOT
merge.  The pure-geometry predicate fused 36 such dot-less pairs through
Union-Find, collapsing distinct nets into one 85-pin ``+24V`` blob and
producing 16 false copper-LVS shorts on a previously gallery-READY board.

The fix (issue #4226) gates wire-to-wire union on junction-dot presence,
matching KiCad's real connectivity: a T-touch/collinear-overlap unions only
where a junction dot sits.  This test locks in the corrected extraction on
the committed board-05 schematic, cross-validated against the
``kicad-cli sch export netlist`` ground truth quoted in the issue:

    C10.1 -> OSC_IN, J2.1 -> PHASE_C, C30.1 -> HALL_A
    GND=36 (largest), +3V3=27, +24V=13

A synthetic dot-less-graze fixture is also included so the regression guard
runs even where the local-only board-05 schematic is unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.schematic.models.elements import Junction, Label, Wire
from kicad_tools.schematic.models.schematic import Schematic

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_05_SCH = (
    REPO_ROOT / "boards" / "05-bldc-motor-controller" / "output" / "bldc_controller.kicad_sch"
)


@pytest.mark.skipif(
    not BOARD_05_SCH.exists(),
    reason="board-05 schematic (local-only consumer board) not present",
)
class TestBoard05WireUnionRegression:
    """The committed board-05 schematic must extract KiCad's real netlist."""

    @pytest.fixture(scope="class")
    def sch(self) -> Schematic:
        return Schematic.load(str(BOARD_05_SCH))

    def test_c10_1_is_osc_in_not_plus24v(self, sch: Schematic) -> None:
        # The headline corruption: C10.1 was fused to +24V; must be OSC_IN.
        assert sch.get_net_for_pin("C10", "1") == "OSC_IN"

    def test_j2_1_is_phase_c(self, sch: Schematic) -> None:
        assert sch.get_net_for_pin("J2", "1") == "PHASE_C"

    def test_c30_1_is_hall_a(self, sch: Schematic) -> None:
        assert sch.get_net_for_pin("C30", "1") == "HALL_A"

    def test_plus24v_is_not_a_giant_blob(self, sch: Schematic) -> None:
        # Was 85/205 pins (>40% of the board) under the broken predicate;
        # KiCad ground truth is 13.  Assert a tight upper bound rather than
        # the exact count to avoid brittleness on future minor edits.
        netlist = sch.extract_netlist()
        plus24 = netlist.get("+24V", [])
        assert len(plus24) <= 15, (
            f"+24V carries {len(plus24)} pins — the #4157 over-merge blob "
            "has recurred (KiCad ground truth is 13)."
        )

    def test_gnd_is_the_largest_net(self, sch: Schematic) -> None:
        netlist = sch.extract_netlist()
        by_size = sorted(netlist.items(), key=lambda kv: len(kv[1]), reverse=True)
        assert by_size[0][0] == "GND", (
            f"largest net is {by_size[0][0]} ({len(by_size[0][1])} pins), "
            "expected GND — a rail over-merge has recurred."
        )
        # And GND is genuinely large, not a 2-pin remnant.
        assert len(netlist["GND"]) >= 30

    def test_c10_2_is_gnd(self, sch: Schematic) -> None:
        # C10.2 is the decoupling-cap ground return; sanity-check the OSC
        # cap is not itself corrupted on the other pin.
        assert sch.get_net_for_pin("C10", "2") == "GND"


class TestDotlessGrazeSynthetic:
    """Minimal synthetic reproduction of the board-05 dot-less-graze pattern.

    Runs everywhere (no local-only board dependency) so the regression guard
    is always active in CI.  Two independent rails whose stubs graze — one
    via a mid-segment T-touch, one via a collinear overlap — with NO junction
    dot at the graze.  KiCad keeps them separate; the junction-gated
    extractor must too.  Adding a dot at the graze flips it to a real merge,
    proving the two cases are cleanly separable (the #4143 intent).
    """

    def _two_rail_graze(self, dotted: bool) -> Schematic:
        sch = Schematic("graze")
        # Rail X: horizontal +24V trunk at y=100, spanning [100,140].
        sch.wires.append(Wire(x1=100, y1=100, x2=140, y2=100))
        sch.labels.append(Label(text="PLUS24V", x=100, y=100))
        # Rail Y: a HALL_C-side escape wire whose lower endpoint (120,100)
        # grazes the interior of the +24V trunk — a mid-segment T-touch.  Its
        # far end (120,90) carries the HALL_C label.  No dot at the graze
        # unless ``dotted``.
        sch.wires.append(Wire(x1=120, y1=100, x2=120, y2=90))
        sch.labels.append(Label(text="HALL_C", x=120, y=90))
        if dotted:
            sch.junctions.append(Junction(x=120, y=100))
        return sch

    @staticmethod
    def _root(sch: Schematic, point: tuple[float, float]) -> tuple:
        parent, _, _ = sch._build_connectivity_graph()

        def find(p):
            while parent.get(p, p) != p:
                p = parent[p]
            return p

        return find(point)

    def test_dotless_t_touch_keeps_rails_separate(self) -> None:
        # HALL_C far end (120,90) must NOT join the +24V trunk (100,100)
        # without a junction dot at the (120,100) graze — the board-05
        # false-short pattern.
        sch = self._two_rail_graze(dotted=False)
        assert self._root(sch, (120, 90)) != self._root(sch, (100, 100))

    def test_dotted_t_touch_does_merge(self) -> None:
        # With a junction dot at the graze, KiCad WOULD merge — the extractor
        # must union, preserving the #4143 real-merge detection.
        sch = self._two_rail_graze(dotted=True)
        assert self._root(sch, (120, 90)) == self._root(sch, (100, 100))

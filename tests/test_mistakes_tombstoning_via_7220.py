"""detect-mistakes tombstoning / via-in-pad regressions (issue #7220).

``TombstoningRiskCheck``'s thermal-imbalance branch and ``ViaInPadCheck`` now
delegate to the ``kct check`` rules (``thermal_asymmetry`` and ``via_in_pad``)
instead of keeping their own geometry.  Before, the tombstoning check counted
any same-net zone on any layer (false positives on 05/07, misses on 00/01/04)
and the via check ignored footprint rotation (false "Via in SMD pad" on
board 07 C12.1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.explain.checks.tombstoning import TombstoningRiskCheck
from kicad_tools.explain.checks.via import ViaInPadCheck
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.thermal_asymmetry import asymmetric_passives

REPO = Path(__file__).resolve().parents[1]
BOARD_05 = REPO / "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"
BOARD_04 = REPO / "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb"
BOARD_07 = REPO / "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb"

_THERMAL = "Thermal imbalance may cause tombstoning"


def _load(path: Path) -> PCB:
    if not path.exists():
        pytest.skip(f"{path} not committed")
    return PCB.load(str(path))


def _thermal_refs(pcb: PCB) -> list[str]:
    return sorted(m.components[0] for m in TombstoningRiskCheck().check(pcb) if m.title == _THERMAL)


@pytest.mark.integration
@pytest.mark.parametrize("board", [BOARD_05, BOARD_07], ids=["05", "07"])
def test_no_thermal_imbalance_false_positives(board):
    """05 R9 (+5V plane on In2.Cu only) and the 07 passives no longer flag."""
    assert _thermal_refs(_load(board)) == []


_NET_COUNT_BOARD = """(kicad_pcb (version 20240108) (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "GND") (net 2 "VCC")
{footprints}
  (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "z-gnd")
    (connect_pads (clearance 0.2)) (min_thickness 0.2) (filled_areas_thickness no)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 0 0) (xy 9.8 0) (xy 9.8 40) (xy 0 40)))
    (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 9.8 0) (xy 9.8 40) (xy 0 40))))
  (zone (net 2) (net_name "VCC") (layer "F.Cu") (uuid "z-vcc")
    (connect_pads (clearance 0.2)) (min_thickness 0.2) (filled_areas_thickness no)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 10.2 0) (xy 20 0) (xy 20 40) (xy 10.2 40)))
    (filled_polygon (layer "F.Cu") (pts (xy 10.2 0) (xy 20 0) (xy 20 40) (xy 10.2 40))))
)
"""


def _cap(ref: str, y: float, pad1: str = "") -> str:
    """0805 at (10, y): pad 1 (GND) reaches the left fill, pad 2 (VCC) the right."""
    return f"""  (footprint "Capacitor_SMD:C_0805_2012Metric" (layer "F.Cu") (at 10 {y})
    (property "Reference" "{ref}" (at 0 -2) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.95 0) (size 1.0 1.25) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND") {pad1})
    (pad "2" smd roundrect (at 0.95 0) (size 1.0 1.25) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "VCC"))
  )
"""


def test_tombstoning_reports_solid_vs_thermal_passive_with_both_nets_poured(tmp_path):
    """The case the net-count heuristic missed: both terminals' nets have a
    fill, so counting zones looked balanced, yet C2's GND pad is tied solid
    (``zone_connect 2``) while its VCC pad has a thermal relief.  C1 (both
    thermal) is genuinely balanced and must stay quiet (issue #7374 keeps
    this positive control after board 04's real cases were refilled away)."""
    board = tmp_path / "net_count.kicad_pcb"
    board.write_text(
        _NET_COUNT_BOARD.format(footprints=_cap("C1", 10) + _cap("C2", 30, "(zone_connect 2)"))
    )
    pcb = PCB.load(str(board))
    assert sorted(a.footprint.reference for a in asymmetric_passives(pcb)) == ["C2"]
    assert _thermal_refs(pcb) == ["C2"]


@pytest.mark.integration
def test_tombstoning_reports_the_thermal_asymmetry_passives():
    """detect-mistakes reports exactly the ``thermal_asymmetry`` passives.

    Board 04 had nine real cases until the #7295 thermal-spoke ladder refill
    (PR #7317) deliberately removed them; the expectation is derived from the
    board, and the positive control lives in the synthetic test above.
    """
    pcb = _load(BOARD_04)
    expected = sorted(a.footprint.reference for a in asymmetric_passives(pcb))
    assert _thermal_refs(pcb) == expected
    assert expected == []


@pytest.mark.integration
def test_rotated_c12_is_not_a_via_in_pad():
    """07 C12.1 (B.Cu, -90 deg): the old unrotated box reported a via in it."""
    pcb = _load(BOARD_07)
    assert [m.components for m in ViaInPadCheck().check(pcb)] == []


def test_via_in_pad_check_maps_rule_findings(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(
        """(kicad_pcb (version 20240108) (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "SIG")
  (footprint "R" (layer "F.Cu") (at 10 10 90)
    (property "Reference" "R7" (at 0 0) (layer "F.SilkS"))
    (pad "2" smd rect (at 0.95 0 90) (size 1.0 1.25) (layers "F.Cu") (net 1 "SIG")))
  (via (at 10 9.05) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
)
"""
    )
    (mistake,) = ViaInPadCheck().check(PCB.load(str(board)))
    assert mistake.title == "Via in SMD pad"
    assert mistake.components == ["R7", "pad 2"]
    assert mistake.location == (10.0, 9.05)

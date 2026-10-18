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


@pytest.mark.integration
def test_tombstoning_reports_the_thermal_asymmetry_passives():
    """Board 04's nine real cases, which the net-count heuristic missed."""
    pcb = _load(BOARD_04)
    expected = sorted(a.footprint.reference for a in asymmetric_passives(pcb))
    assert expected == ["C1", "C12", "C13", "C14", "C15", "C16", "C2", "C3", "R1"]
    assert _thermal_refs(pcb) == expected


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

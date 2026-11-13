"""THT / via to SMD-pad spacing (Issue #8843): error / warning / clean bands."""

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.tht_smd_spacing import check_tht_smd_spacing


def board(*items):
    return PCB(
        parse_string(
            """(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 0 "") (net 1 "A") (net 2 "B")
    """
            + "\n".join(items)
            + ")"
        )
    )


# THT pad: 1.6 mm round at x=0 (copper edge at x=0.8).  SMD pad: 1 mm square
# centred at x = 0.8 + gap + 0.5, so the copper-edge gap is exactly ``gap``.
def tht(layer_side="F.Cu"):
    return """(footprint "T" (layer "F.Cu") (at 0 0) (property "Reference" "J1")
      (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.9)
        (layers "*.Cu" "*.Mask") (net 1 "A") (uuid "tht")))"""


def smd(gap, layer="F.Cu"):
    x = 0.8 + gap + 0.5
    return f"""(footprint "S" (layer "{layer}") (at {x} 0) (property "Reference" "R1")
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "{layer}") (net 2 "B") (uuid "smd")))"""


@pytest.mark.parametrize(("gap", "severity"), [(1.5, "error"), (2.3, "warning"), (3.0, None)])
def test_bands(gap, severity):
    found = check_tht_smd_spacing(board(tht(), smd(gap))).violations
    if severity is None:
        assert not found
    else:
        assert [v.severity for v in found] == [severity]
        assert found[0].actual_value == pytest.approx(gap, abs=0.002)
        assert set(found[0].items) == {"tht", "smd"}


def test_back_side_smd_pad_is_measured():
    found = check_tht_smd_spacing(board(tht(), smd(1.5, "B.Cu"))).violations
    assert [v.severity for v in found] == ["error"]
    assert found[0].layer == "B.Cu"


def test_via_arm_is_opt_in():
    via = '(via (at 0 0) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "v"))'
    # Default: routine via fan-out near SMD pads is not a finding.
    assert not check_tht_smd_spacing(board(via, smd(1.5))).violations
    found = check_tht_smd_spacing(board(via, smd(1.5)), include_vias=True).violations
    assert [v.severity for v in found] == ["error"]


def test_same_footprint_pads_are_not_compared():
    """A connector's own shield tab vs its SMD pin is footprint design, not placement."""
    one = """(footprint "C" (layer "F.Cu") (at 0 0) (property "Reference" "J1")
      (pad "S1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.9)
        (layers "*.Cu" "*.Mask") (net 1 "A") (uuid "tab"))
      (pad "1" smd rect (at 2.3 0) (size 1 1) (layers "F.Cu") (net 2 "B") (uuid "pin")))"""
    assert not check_tht_smd_spacing(board(one)).violations
    # The same geometry across two footprints is still reported.
    assert check_tht_smd_spacing(board(tht(), smd(1.5))).violations


def test_non_plated_hole_ignored():
    npth = """(footprint "N" (layer "F.Cu") (at 0 0) (property "Reference" "H1")
      (pad "" np_thru_hole circle (at 0 0) (size 1.6 1.6) (drill 1.6) (layers "*.Cu")))"""
    assert not check_tht_smd_spacing(board(npth, smd(1.5))).violations


def test_checker_only_under_fab_view():
    pcb = board(tht(), smd(1.5))
    assert not DRCChecker(pcb).check_tht_smd_spacing().violations
    assert DRCChecker(pcb, fab_view=True).check_tht_smd_spacing().violations

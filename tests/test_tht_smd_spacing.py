"""THT / via to SMD-pad spacing (Issue #8843): error / warning / clean bands."""

import math

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.copper_refs import via_ref
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


def board_4layer(*items):
    """Same fixture with two inner layers, for via layer-span coverage."""
    return PCB(
        parse_string(
            """(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))
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


def smd_at(edge, gap, layer="F.Cu"):
    """1 mm square SMD pad whose copper edge sits ``gap`` from copper edge ``edge``."""
    x = edge + gap + 0.5
    return f"""(footprint "S" (layer "{layer}") (at {x} 0) (property "Reference" "R1")
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "{layer}") (net 2 "B") (uuid "smd")))"""


def smd(gap, layer="F.Cu"):
    return smd_at(0.8, gap, layer)


# Issue #8914: a via is judged by its drill, not by being a via.  ``size`` is the
# annulus diameter, so the copper edge sits at ``size / 2``.
def via(*, drill, size=1.9, layers='"F.Cu" "B.Cu"', kind=""):
    # ``kind`` ("buried"/"blind") is load-bearing for a partial span: PCB widens a
    # keyword-less partial ``(layers ...)`` pair to F.Cu-B.Cu, as KiCad does (#6493).
    token = f"{kind} " if kind else ""
    return (
        f'(via {token}(at 0 0) (size {size}) (drill {drill}) (layers {layers}) (net 1) (uuid "v"))'
    )


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


def test_anchor_sized_via_is_judged_like_a_tht_pad():
    """Issue #8914: JLCDFM's three missed top-side dangers were 1.3-1.42 mm anchor vias."""
    found = check_tht_smd_spacing(board(via(drill=1.4), smd_at(0.95, 1.5))).violations
    assert [v.severity for v in found] == ["error"]
    assert found[0].actual_value == pytest.approx(1.5, abs=0.002)
    # The via is identified by its copper_refs via reference, not a pad uuid.
    assert "smd" in found[0].items
    assert [item for item in found[0].items if item != "smd"] == [
        via_ref(board(via(drill=1.4)).vias[0], (0.0, 0.0))
    ]
    assert found[0].layer == "F.Cu"


def test_fanout_sized_via_is_not_judged():
    """The per-board finding flood the old vias-excluded default prevented."""
    assert not check_tht_smd_spacing(board(via(drill=0.4, size=0.8), smd_at(0.4, 1.5))).violations


def test_via_drill_threshold_is_configurable():
    fanout = board(via(drill=0.4, size=0.8), smd_at(0.4, 1.5))
    # 0 judges every via (the old ``include_vias=True`` behaviour)...
    assert [v.severity for v in check_tht_smd_spacing(fanout, via_drill_mm=0).violations] == [
        "error"
    ]
    # ...and inf judges none, without touching THT component pads.
    assert not check_tht_smd_spacing(fanout, via_drill_mm=math.inf).violations
    assert check_tht_smd_spacing(board(tht(), smd(1.5)), via_drill_mm=math.inf).violations


@pytest.mark.parametrize("bad", [-1.0, float("nan")])
def test_via_drill_threshold_must_be_a_non_negative_number(bad):
    with pytest.raises(ValueError):
        check_tht_smd_spacing(board(tht(), smd(1.5)), via_drill_mm=bad)


def test_inner_layer_only_via_is_not_compared_with_an_outer_smd_pad():
    """An anchor via that never reaches F.Cu cannot threaten an F.Cu pad."""
    inner = board_4layer(
        via(drill=1.4, layers='"In1.Cu" "In2.Cu"', kind="buried"),
        smd_at(0.95, 1.5),
    )
    assert not check_tht_smd_spacing(inner).violations
    spanning = board_4layer(via(drill=1.4, layers='"F.Cu" "B.Cu"'), smd_at(0.95, 1.5))
    assert [v.severity for v in check_tht_smd_spacing(spanning).violations] == ["error"]


def test_via_copper_touching_an_smd_pad_stays_via_in_pad_territory():
    assert not check_tht_smd_spacing(board(via(drill=1.4), smd_at(0.95, 0.0))).violations


def test_unmeasurable_pad_shape_is_reported_not_cleared():
    """Issue #8914: a shape the rule cannot measure must not look like a cleared pad."""
    custom = """(footprint "T" (layer "F.Cu") (at 0 0) (property "Reference" "J2")
      (pad "1" thru_hole custom (at 0 0) (size 1.6 1.6) (drill 0.9)
        (layers "*.Cu" "*.Mask") (net 1 "A") (uuid "odd")))"""
    found = check_tht_smd_spacing(board(custom, smd(1.5))).violations
    assert [(v.rule_id, v.severity) for v in found] == [("tht_smd_spacing_incomplete", "info")]
    assert "custom" in found[0].message
    assert "odd" in found[0].message
    # A measurable pad on the same board is still judged.
    both = check_tht_smd_spacing(board(custom, tht(), smd(1.5))).violations
    assert sorted(v.rule_id for v in both) == ["tht_smd_spacing", "tht_smd_spacing_incomplete"]


def test_checker_judges_anchor_vias_by_default():
    """The reachability regression: before #8914 no ``kct check`` run ever judged a via."""
    pcb = board(via(drill=1.4), smd_at(0.95, 1.5))
    assert not DRCChecker(pcb).check_tht_smd_spacing().violations
    found = DRCChecker(pcb, fab_view=True).check_tht_smd_spacing().violations
    assert [v.severity for v in found] == ["error"]
    # ...and the threshold is reachable from the checker, not pinned to a default.
    assert not DRCChecker(pcb, fab_view=True).check_tht_smd_spacing(via_drill_mm=2.0).violations


def test_reproduces_the_8914_jlcdfm_danger_items():
    """Issue #8914: the four top-side danger items the fab reported, as measured.

    From the reporter's own DFM table (absolute coordinates omitted): three
    1.916 mm / 1.416 mm-drill wire-anchor **vias** at 1.34, 1.79 and 1.95 mm from
    an SMD pad, which kct did not report at all, plus one 2.4 mm / 1.2 mm-drill
    through-hole **pad** at 1.98 mm, which it did.  All four are below the 2.0 mm
    danger band, so all four must be errors.
    """
    anchors = [
        via(drill=1.416, size=1.916, layers='"F.Cu" "B.Cu"').replace('"v"', f'"v{i}"')
        for i in range(3)
    ]
    # Each anchor via sits on its own x so the three pairs do not interact.
    boards = [
        board(a, smd_at(1.916 / 2, gap)) for a, gap in zip(anchors, (1.34, 1.79, 1.95), strict=True)
    ]
    for pcb, gap in zip(boards, (1.34, 1.79, 1.95), strict=True):
        found = check_tht_smd_spacing(pcb).violations
        assert [v.severity for v in found] == ["error"], f"anchor via at {gap} mm"
        assert found[0].actual_value == pytest.approx(gap, abs=0.002)

    pad = """(footprint "J" (layer "F.Cu") (at 0 0) (property "Reference" "J9")
      (pad "1" thru_hole circle (at 0 0) (size 2.4 2.4) (drill 1.2)
        (layers "*.Cu" "*.Mask") (net 1 "A") (uuid "tht24")))"""
    found = check_tht_smd_spacing(board(pad, smd_at(1.2, 1.98))).violations
    assert [v.severity for v in found] == ["error"]
    assert found[0].actual_value == pytest.approx(1.98, abs=0.002)


def test_8914_item_5_via_to_via_is_a_known_remaining_gap():
    """The fab's fifth danger pairs two holes; this rule's `smd` side is pads only.

    JLCDFM item 5 (bottom, 1.5 mm) is a 1.9 mm / 1.3 mm anchor via against an
    ordinary 0.8 mm / 0.4 mm **via** pad -- so the fab's "smd" side also counts a
    small exposed via annulus.  That pairing is deliberately still out of scope --
    it needs its own calibration and no board in this repo has an anchor-sized via
    to calibrate against (#8925) -- and this test pins the gap so a future change
    to it is a visible decision rather than an accident.
    """
    anchor = via(drill=1.3, size=1.9).replace('"v"', '"anchor"')
    small = '(via (at 2.85 0) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "small"))'
    assert not check_tht_smd_spacing(board(anchor, small)).violations


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

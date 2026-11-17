"""Persisted copper-contact and stable identity regressions for #4986."""

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.dangling_copper import DanglingCopperRule

TRACK_UUID = "11111111-1111-4111-8111-111111111111"


def _check(tmp_path, kind, detached=False, layer="F.Cu", width=0.13):
    # Endpoint anchors sit outside the candidate copper; the 0.13mm track
    # cap reaches it. A further 0.1mm separation is a true detached control.
    shift = 0.1 if detached else 0
    if kind == "via":
        start, end = (10.28 + shift, 10), (13.72 - shift, 10)
        candidates = """(via (at 10 10) (size .45) (drill .2) (layers "F.Cu" "B.Cu") (net 1))
        (via (at 14 10) (size .45) (drill .2) (layers "F.Cu" "B.Cu") (net 1))"""
    elif kind == "pad":
        start, end = (10.55 + shift, 10), (13.45 - shift, 10)
        candidates = """(footprint "Test:Pads" (layer "F.Cu") (at 0 0)
          (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))
          (pad "1" smd roundrect (at 10 10) (size 1 1) (layers "F.Cu") (roundrect_rratio .25) (net 1 "SIG"))
          (pad "2" smd roundrect (at 14 10) (size 1 1) (layers "F.Cu") (roundrect_rratio .25) (net 1 "SIG")))"""
    else:
        start, end = (10.15 + shift, 10), (13.85 - shift, 10)
        candidates = """(segment (start 10 9) (end 10 11) (width .2) (layer "F.Cu") (net 1))
        (segment (start 14 9) (end 14 11) (width .2) (layer "F.Cu") (net 1))"""
    path = tmp_path / "contact.kicad_pcb"
    path.write_text(f'''(kicad_pcb (version 20240108) (generator "test")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (net 0 "") (net 1 "SIG")
      (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))
      {candidates}
      (segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]})
        (width {width}) (layer "{layer}") (net 1) (uuid "{TRACK_UUID}")))''')
    board = PCB.load(path)
    saved = tmp_path / "saved.kicad_pcb"
    board.save(saved)
    board = PCB.load(saved)
    result = DanglingCopperRule().check(board, design_rules=None)
    # Other candidate tracks have genuinely free ends; isolate our subject
    # by its endpoint so this test also works before UUID metadata is added.
    return [
        v for v in result.violations if v.rule_id == "track_dangling" and v.location in (start, end)
    ]


@pytest.mark.parametrize("kind", ["via", "pad", "track"])
def test_track_caps_touch_candidate_copper(tmp_path, kind):
    assert _check(tmp_path, kind) == []


@pytest.mark.parametrize("kind", ["via", "pad", "track"])
def test_detached_track_remains_flagged_with_stable_uuid(tmp_path, kind):
    findings = _check(tmp_path, kind, detached=True)
    assert len(findings) == 1
    assert TRACK_UUID in findings[0].items
    assert "both ends dangling" in findings[0].message


@pytest.mark.parametrize("kind", ["pad", "track"])
def test_copper_on_other_layer_does_not_terminate_track(tmp_path, kind):
    assert len(_check(tmp_path, kind, layer="B.Cu")) == 1


@pytest.mark.parametrize("kind", ["via", "pad", "track"])
def test_thin_cap_does_not_reach_candidate_copper(tmp_path, kind):
    assert len(_check(tmp_path, kind, width=0.02)) == 1

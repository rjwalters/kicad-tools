"""``silk_ref_obscured`` and off-board ``silk_edge_clearance`` text (#7228)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    check_silk_edge_clearance,
    check_silk_ref_obscured,
)

ROOT = Path(__file__).resolve().parent.parent
BOARD02 = ROOT / "boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb"
BOARD03 = ROOT / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"


@pytest.fixture
def rules():
    return get_profile("jlcpcb").get_design_rules(layers=2)


def _ref(pcb: PCB, reference: str):
    footprint = next(fp for fp in pcb.footprints if fp.reference == reference)
    return next(t for t in footprint.texts if t.text_type == "reference")


def test_unmutated_boards_have_no_obscured_ref(rules):
    for path in (BOARD02, BOARD03):
        assert check_silk_ref_obscured(PCB.load(path), rules).violations == [], path.name


def test_hidden_reference_is_flagged(rules):
    pcb = PCB.load(BOARD02)
    _ref(pcb, "D1").hidden = True
    found = check_silk_ref_obscured(pcb, rules).violations
    assert [(v.rule_id, v.severity, v.items) for v in found] == [
        ("silk_ref_obscured", "warning", ("D1 (reference)", "hidden"))
    ]


def test_reference_inside_own_body_is_flagged(rules):
    pcb = PCB.load(BOARD02)
    _ref(pcb, "U1").position = (3.81, 3.81)  # DIP-8 body centre
    found = check_silk_ref_obscured(pcb, rules).violations
    assert [v.items for v in found] == [("U1 (reference)", "courtyard")]


def test_reference_off_board_is_flagged_as_outside(rules):
    pcb = PCB.load(BOARD02)
    _ref(pcb, "D2").position = (0.0, -15.0)  # D2 is at y=8: 7 mm above the top edge
    found = check_silk_edge_clearance(pcb, rules).violations
    assert [(v.items, v.actual_value) for v in found] == [(("D2 (reference)", "Edge.Cuts"), None)]
    assert "outside" in found[0].message


def test_overhanging_footprint_outline_is_not_flagged_outside(rules):
    """Board 03's J1 body strokes overhang the edge by design (#6494)."""
    assert check_silk_edge_clearance(PCB.load(BOARD03), rules).violations == []


_FIDUCIAL_FP = """(kicad_pcb (version 20240108) (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user))
  (footprint "Fiducial:Fiducial_1mm_Mask2mm" (layer "F.Cu") (at 10 10)
    (attr smd)
    (property "Reference" "FID1" (at 0 -1.5 0) (layer "F.SilkS") (hide yes)
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_circle (center 0 0) (end 1 0) (layer "F.CrtYd") (width 0.05))
    (pad "" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") %s)))
"""


def _fiducial_board(prop: str, tmp_path: Path) -> PCB:
    path = tmp_path / "fid.kicad_pcb"
    path.write_text(_FIDUCIAL_FP % prop)
    return PCB.load(path)


@pytest.mark.parametrize("prop", ["pad_prop_fiducial_glob", "pad_prop_fiducial_loc"])
def test_hidden_fiducial_reference_not_flagged(rules, tmp_path, prop):
    pcb = _fiducial_board(f"(property {prop})", tmp_path)
    assert check_silk_ref_obscured(pcb, rules).violations == []


def test_hidden_reference_without_fiducial_property_still_flagged(rules, tmp_path):
    pcb = _fiducial_board("", tmp_path)
    found = check_silk_ref_obscured(pcb, rules).violations
    assert [v.items for v in found] == [("FID1 (reference)", "hidden")]


def _attr_board(attr: str, tmp_path):
    path = tmp_path / "attr.kicad_pcb"
    path.write_text(_FIDUCIAL_FP.replace("(attr smd)", f"(attr {attr})"))
    return PCB.load(path)


def test_board_only_exclude_from_pos_files_attr_exempts_fiducial(rules, tmp_path):
    pcb = _attr_board("smd board_only exclude_from_pos_files", tmp_path)
    assert check_silk_ref_obscured(pcb, rules).violations == []


@pytest.mark.parametrize("attr", ["smd board_only", "smd exclude_from_pos_files"])
def test_single_attr_flag_still_flagged(rules, tmp_path, attr):
    found = check_silk_ref_obscured(_attr_board(attr, tmp_path), rules).violations
    assert [v.items for v in found] == [("FID1 (reference)", "hidden")]


# --- hidden reference replaced by a function label (#9435) -------------------


def _label(pcb: PCB, fp_ref: str, *, dx: float = 0.0, dy: float = 0.0, **kw):
    """A board ``gr_text`` placed over ``fp_ref`` (sheet-absolute, like the file)."""
    from kicad_tools.schema.pcb import GraphicText

    fp = next(f for f in pcb.footprints if f.reference == fp_ref)
    ox, oy = getattr(pcb, "_board_origin", (0.0, 0.0))
    fields = {
        "text": "PWR",
        "position": (fp.position[0] + dx + ox, fp.position[1] + dy + oy),
        "layer": "F.SilkS",
        "font_size": (1.0, 1.0),
        "font_thickness": 0.15,
    }
    fields.update(kw)
    text = GraphicText(**fields)
    pcb.texts.append(text)
    return text


def _items(pcb: PCB, rules):
    return [v.items for v in check_silk_ref_obscured(pcb, rules).violations]


def test_hidden_reference_with_function_label_is_not_flagged(rules):
    pcb = PCB.load(BOARD02)
    _ref(pcb, "D1").hidden = True
    _label(pcb, "D1")
    assert _items(pcb, rules) == []


def test_hidden_reference_and_obscured_visible_one_on_same_board(rules):
    pcb = PCB.load(BOARD02)
    _ref(pcb, "D1").hidden = True
    _label(pcb, "D1")
    _ref(pcb, "D2").hidden = True  # hidden, nothing replaces it
    _ref(pcb, "U1").position = (3.81, 3.81)  # visible but inside its own body
    assert sorted(_items(pcb, rules)) == [
        ("D2 (reference)", "hidden"),
        ("U1 (reference)", "courtyard"),
    ]


@pytest.mark.parametrize(
    "kw",
    [
        {"layer": "B.SilkS"},  # opposite side
        {"hidden": True},  # not visible
        {"dx": 40.0},  # outside the courtyard
        {"layer": "F.Cu"},  # not silkscreen
    ],
)
def test_replacement_text_that_does_not_count(rules, kw):
    pcb = PCB.load(BOARD02)
    _ref(pcb, "D1").hidden = True
    _label(pcb, "D1", **kw)
    assert _items(pcb, rules) == [("D1 (reference)", "hidden")]


def test_function_label_exempts_with_nonzero_board_origin(rules):
    pcb = PCB.load(BOARD02)
    pcb._board_origin = (100.0, 50.0)
    _ref(pcb, "D1").hidden = True
    _label(pcb, "D1")  # absolute position includes the origin shift
    assert _items(pcb, rules) == []
    pcb.texts[-1].position = (pcb.texts[-1].position[0] + 30.0, pcb.texts[-1].position[1])
    assert _items(pcb, rules) == [("D1 (reference)", "hidden")]

"""Pad geometry write-through and fail-closed save (issue #7479).

``PCB.save()`` serialises the S-expression tree, so a ``Pad``/``Footprint``
field without write-through used to change only the Python object and vanish
on save.  Geometry/fab fields now write through; identity/net fields make
``save()`` raise instead of dropping the edit.
"""

from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB, Pad

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "board03_pre6394_split_brain"
    / "usb_joystick_routed.kicad_pcb"
)
NAME_ONLY = Path(__file__).parent / "fixtures" / "test_kicad10_save_board.kicad_pcb"


def _pad(pcb: PCB, ref: str, predicate) -> Pad:
    fp = pcb.get_footprint(ref)
    return next(p for p in fp.pads if predicate(p))


def _round_tht(pcb: PCB) -> Pad:
    return _pad(pcb, "J1", lambda p: p.type == "thru_hole" and p.drill_size is None and p.drill > 0)


def _oval_tht(pcb: PCB) -> Pad:
    return _pad(pcb, "J1", lambda p: p.drill_size is not None)


def _reload_pad(path: Path, original: Pad, ref: str = "J1") -> Pad:
    fp = PCB.load(path).get_footprint(ref)
    return next(p for p in fp.pads if p.uuid == original.uuid)


def _roundtrip(pcb: PCB, tmp_path: Path) -> Path:
    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)
    return out


@pytest.fixture
def pcb() -> PCB:
    return PCB.load(FIXTURE)


def test_size_and_drill_round_trip(pcb, tmp_path):
    pad = _round_tht(pcb)
    pad.size = (2.5, 2.25)
    pad.drill = 1.3
    again = _reload_pad(_roundtrip(pcb, tmp_path), pad)
    assert again.size == (2.5, 2.25)
    assert again.drill == 1.3
    assert again.net_name == pad.net_name and again.uuid == pad.uuid


def test_size_keeps_untouched_axis_byte_for_byte(pcb, tmp_path):
    pad = _round_tht(pcb)
    node = pad._sexp_node
    before_h = node.find_child("size").children[1]
    pad.size = (2.5, pad.size[1])
    assert node.find_child("size").children[1] is before_h


def test_oval_drill_scalar_assignment_is_refused(pcb):
    pad = _oval_tht(pcb)
    before = pad._sexp_node.find_child("drill").to_string()
    with pytest.raises(ValueError, match="drill_size"):
        pad.drill = 0.8
    assert pad._sexp_node.find_child("drill").to_string() == before


def test_oval_drill_size_and_offset_round_trip(pcb, tmp_path):
    pad = _oval_tht(pcb)
    pad.drill_size = (0.9, 1.9)
    pad.drill_offset = (0.1, -0.2)
    node = pad._sexp_node.find_child("drill")
    assert node.get_string(0) == "oval"
    again = _reload_pad(_roundtrip(pcb, tmp_path), pad)
    assert again.drill_size == (0.9, 1.9)
    assert again.drill_offset == (0.1, -0.2)


def test_round_drill_offset_round_trip_keeps_diameter(pcb, tmp_path):
    pad = _round_tht(pcb)
    diameter = pad.drill
    pad.drill_offset = (0.05, 0.15)
    again = _reload_pad(_roundtrip(pcb, tmp_path), pad)
    assert again.drill == diameter
    assert again.drill_offset == (0.05, 0.15)


def test_shape_round_trip(pcb, tmp_path):
    pad = _round_tht(pcb)
    pad.shape = "oval"
    assert _reload_pad(_roundtrip(pcb, tmp_path), pad).shape == "oval"


@pytest.mark.parametrize(
    ("field", "value"),
    [("roundrect_rratio", 0.1), ("solder_mask_margin", 0.07), ("zone_connect", 1)],
)
def test_optional_nodes_are_created_and_round_trip(pcb, tmp_path, field, value):
    pad = _pad(pcb, "J1", lambda p: p._sexp_node.find_child(field) is None)
    setattr(pad, field, value)
    assert pad._sexp_node.find_child(field) is not None
    assert getattr(_reload_pad(_roundtrip(pcb, tmp_path), pad), field) == value


@pytest.mark.parametrize("field", ["solder_mask_margin", "zone_connect"])
def test_none_removes_optional_node(pcb, tmp_path, field):
    pad = _pad(pcb, "J1", lambda p: True)
    setattr(pad, field, 1 if field == "zone_connect" else 0.05)
    setattr(pad, field, None)
    assert pad._sexp_node.find_child(field) is None
    assert getattr(_reload_pad(_roundtrip(pcb, tmp_path), pad), field) is None


def test_size_creates_missing_node(pcb, tmp_path):
    pad = _round_tht(pcb)
    pad._sexp_node.remove_child("drill")
    pad.drill = 0.6
    assert _reload_pad(_roundtrip(pcb, tmp_path), pad).drill == 0.6


# --- part 2: fail-closed save -------------------------------------------------


def _edit_pad(field, value):
    def edit(pcb):
        setattr(_pad(pcb, "J1", lambda p: True), field, value)

    return edit


def _edit_fp(field, value):
    def edit(pcb):
        setattr(pcb.get_footprint("J1"), field, value)

    return edit


def _edit_property(pcb):
    pcb.get_footprint("J1").properties["X"] = "y"


def _append_pad(pcb):
    fp = pcb.get_footprint("J1")
    fp.pads.append(
        Pad(number="99", type="smd", shape="rect", position=(0, 0), size=(1, 1), layers=[])
    )


REFUSED = {
    "pad.number": _edit_pad("number", "ZZ"),
    "pad.type": _edit_pad("type", "np_thru_hole"),
    "pad.net_name": _edit_pad("net_name", "NOT_A_NET"),
    "pad.uuid": _edit_pad("uuid", "deadbeef"),
    "pad.pintype": _edit_pad("pintype", "test_only_pintype"),
    "pad.pinfunction": _edit_pad("pinfunction", "FOO"),
    "fp.reference": _edit_fp("reference", "J99"),
    "fp.value": _edit_fp("value", "nope"),
    "fp.name": _edit_fp("name", "Other:Name"),
    "fp.description": _edit_fp("description", "changed"),
    "fp.tags": _edit_fp("tags", "changed"),
    "fp.uuid": _edit_fp("uuid", "deadbeef"),
    "fp.properties": _edit_property,
    "fp.pads.append": _append_pad,
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_save_refuses_unpersisted_edit(pcb, tmp_path, case):
    REFUSED[case](pcb)
    out = tmp_path / "refused.kicad_pcb"
    with pytest.raises(ValueError, match="J"):
        pcb.save(out)
    assert not out.exists()


def test_refusal_names_footprint_pad_and_field(pcb, tmp_path):
    pad = _pad(pcb, "J1", lambda p: True)
    original = pad.number
    pad.pinfunction = "CHANGED"
    with pytest.raises(ValueError) as exc:
        pcb.save(tmp_path / "x.kicad_pcb")
    assert f"J1/pad {original}.pinfunction" in str(exc.value)


def test_refusal_does_not_overwrite_existing_target(pcb, tmp_path):
    out = tmp_path / "keep.kicad_pcb"
    out.write_text("sentinel")
    _edit_fp("value", "nope")(pcb)
    with pytest.raises(ValueError):
        pcb.save(out)
    assert out.read_text() == "sentinel"


def test_supported_mutators_still_save(pcb, tmp_path):
    assert pcb.update_footprint_reference("J1", "J7")
    assert pcb.update_footprint_value("J7", "CONN")
    pad = _pad(pcb, "J7", lambda p: p.net_name)
    assert pcb.assign_net_to_footprint_pad("J7", pad.number, "BRAND_NEW")
    again = PCB.load(_roundtrip(pcb, tmp_path))
    fp = again.get_footprint("J7")
    assert fp.value == "CONN"
    assert next(p for p in fp.pads if p.number == pad.number).net_name == "BRAND_NEW"


def test_unedited_save_is_byte_identical(tmp_path):
    for fixture in (FIXTURE, NAME_ONLY):
        out = tmp_path / fixture.name
        PCB.load(fixture).save(out)
        again = tmp_path / ("again_" + fixture.name)
        PCB.load(out).save(again)
        assert out.read_bytes() == again.read_bytes()


def test_name_only_net_board_saves(tmp_path):
    pcb = PCB.load(NAME_ONLY)
    assert pcb._unpersisted_edits() == []


def test_number_only_net_pad_unedited_save_succeeds(tmp_path):
    """``(net N)`` pads get ``net_name`` filled in memory; not an edit (#7519)."""
    import re

    src = tmp_path / "number_only.kicad_pcb"
    text = FIXTURE.read_text()
    # Strip the name atom from one pad's net node: (net N "name") -> (net N).
    text, n = re.subn(
        r'(\(pad [^\n]*\n(?:(?!\(pad ).*\n)*?\s*)\(net (\d+) "[^"]+"\)',
        r"\1(net \2)",
        text,
        count=1,
    )
    assert n == 1
    src.write_text(text)
    pcb = PCB.load(src)
    assert pcb._unpersisted_edits() == []
    pcb.save(tmp_path / "out.kicad_pcb")

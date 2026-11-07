"""Back-side placement via ``PCB.add_footprint_from_file`` (Issue #8581)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB, _flip_side_layer

FIXTURES = Path(__file__).parent / "fixtures"
SOT = FIXTURES / "Test_Library.pretty" / "SOT-23-5.kicad_mod"
RES = FIXTURES / "KiCad10_Library.pretty" / "R_0603_1608Metric.kicad_mod"


def _place(path: Path, layer: str, rotation: float = 0.0, ref: str = "U1"):
    pcb = PCB.create(width=100, height=100)
    pcb.add_footprint_from_file(path, ref, 50, 50, rotation=rotation, layer=layer)
    return pcb, pcb._footprints[-1]._sexp_node


def _layer_names(node) -> list[str]:
    names: list[str] = []

    def walk(n):
        for child in n.iter_children():
            if child.tag == "layer":
                names.append(child.get_string(0))
            elif child.tag == "layers":
                names.extend(a.value for a in child.children if a.is_atom)
            else:
                walk(child)

    walk(node)
    return names


def _is_mirrored(text) -> bool:
    justify = text.find_child("effects").find_child("justify")
    return justify is not None and any(a.is_atom and a.value == "mirror" for a in justify.children)


def test_back_pads_on_back_layers():
    _, node = _place(SOT, "B.Cu")
    for pad in node.find_all("pad"):
        layers = [a.value for a in pad.find_child("layers").children if a.is_atom]
        assert sorted(layers) == ["B.Cu", "B.Mask", "B.Paste"]


def test_back_graphics_and_text_on_back_layers():
    _, node = _place(RES, "B.Cu", ref="R1")
    names = _layer_names(node)
    assert not [n for n in names if n.startswith("F.")]
    assert {"B.SilkS", "B.Fab", "B.CrtYd"} <= set(names)


def test_back_text_is_mirrored():
    _, node = _place(RES, "B.Cu", ref="R1")
    texts = [c for c in node.iter_children() if c.tag in ("property", "fp_text")]
    assert texts
    assert all(_is_mirrored(t) for t in texts)


def test_back_local_y_mirrored_and_pad_angle_follows_pcbnew():
    _, front = _place(SOT, "F.Cu")
    _, back = _place(SOT, "B.Cu", rotation=90)
    for fpad, bpad in zip(front.find_all("pad"), back.find_all("pad"), strict=True):
        f_at, b_at = fpad.find_child("at"), bpad.find_child("at")
        assert b_at.get_float(0) == pytest.approx(f_at.get_float(0))
        assert b_at.get_float(1) == pytest.approx(-f_at.get_float(1))
        # absolute pad angle = rotation - local angle (pcbnew Flip + SetOrientation)
        assert (b_at.get_float(2) or 0.0) == pytest.approx(90.0)


def test_back_text_angle_follows_pcbnew():
    _, node = _place(RES, "B.Cu", rotation=90, ref="R1")
    ref = next(
        c for c in node.iter_children() if c.tag == "property" and c.get_string(0) == "Reference"
    )
    # 180 + rotation - local(0) = 270
    assert ref.find_child("at").get_float(2) == pytest.approx(270.0)


def test_side_neutral_layers_unchanged():
    names = ("*.Cu", "In1.Cu", "Edge.Cuts", "F&B.Cu")
    assert [_flip_side_layer(n) for n in names] == list(names)


def test_front_placement_has_no_back_layers_or_mirror():
    _, node = _place(RES, "F.Cu", ref="R1")
    assert not [n for n in _layer_names(node) if n.startswith("B.")]
    for text in (c for c in node.iter_children() if c.tag in ("property", "fp_text")):
        assert not _is_mirrored(text)


def test_back_round_trips_through_save(tmp_path):
    pcb, _ = _place(SOT, "B.Cu")
    out = tmp_path / "b.kicad_pcb"
    pcb.save(out)
    fp = PCB.load(out).footprints[0]
    assert fp.layer == "B.Cu"
    assert all("B.Cu" in pad.layers for pad in fp.pads)

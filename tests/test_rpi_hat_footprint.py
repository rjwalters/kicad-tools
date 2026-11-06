"""``kct footprint generate rpi-hat``: 2x20 header + four mounting holes (#8529)."""

from __future__ import annotations

import json
import re

import pytest

from kicad_tools.cli.footprint_generate import GENERATORS, main
from kicad_tools.export.pnp import _tht_centroid_offset
from kicad_tools.library.generators import create_rpi_hat
from kicad_tools.schema.pcb import PCB

_HOLES = {(1.27, -4.87), (-47.73, -4.87), (1.27, 53.13), (-47.73, 53.13)}


def test_geometry_matches_hat_spec():
    fp = create_rpi_hat()
    numbered = [p for p in fp.pads if p.name]
    holes = [p for p in fp.pads if not p.name]

    assert len(numbered) == 40
    pin1 = next(p for p in numbered if p.name == "1")
    assert (pin1.x, pin1.y) == (0, 0)
    assert {(round(p.x, 3), round(p.y, 3)) for p in holes} == _HOLES
    for h in holes:
        assert h.pad_type == "np_thru_hole"
        assert h.drill == 2.7

    xs = sorted({round(h.x, 2) for h in holes})
    ys = sorted({round(h.y, 2) for h in holes})
    assert round(xs[1] - xs[0], 2) == 49.0
    assert round(ys[1] - ys[0], 2) == 58.0


def test_outline_and_keepouts():
    fp = create_rpi_hat()
    fab = [g for g in fp.graphics if getattr(g, "layer", "") == "F.Fab"]
    outline = max(fab, key=lambda g: abs(g.end_x - g.start_x) * abs(g.end_y - g.start_y))
    assert sorted([abs(outline.end_x - outline.start_x), abs(outline.end_y - outline.start_y)]) == [
        56.5,
        65.0,
    ]
    assert len(fp.zones) == 4
    for zone in fp.zones:
        xs = [x for x, _ in zone.points]
        assert max(xs) - min(xs) == pytest.approx(6.0, abs=0.01)
    assert len(re.findall(r"\(zone\b", fp.to_sexp())) == 4
    assert create_rpi_hat(keepout_diameter=0).zones == []


def test_list_and_cli_output(tmp_path, capsys):
    assert "rpi-hat" in GENERATORS
    out = tmp_path / "hat.kicad_mod"
    assert main(["rpi-hat", "-o", str(out)]) == 0
    text = out.read_text()
    assert "RPi_HAT_2x20_P2.54mm_MountingHoles" in text
    assert text.count("np_thru_hole") == 4

    capsys.readouterr()
    assert main(["--list"]) == 0
    assert "rpi-hat" in capsys.readouterr().out
    assert main(["rpi-hat", "--keepout-diameter", "0", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["pads"]) == 44


def _placed(tmp_path, fp, name):
    body = fp.to_sexp().replace('(layer "F.Cu")', '(layer "F.Cu")\n\t(at 100 50)', 1)
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n' + body + ")\n"
    )
    return PCB.load(path).footprints[0]


def test_cpl_centroid_equals_stock_header(tmp_path):
    hat = create_rpi_hat()
    stock = create_rpi_hat(keepout_diameter=0)
    stock.pads = [p for p in stock.pads if p.name]  # header only, same placement

    hat_off = _tht_centroid_offset(_placed(tmp_path, hat, "hat"))
    stock_off = _tht_centroid_offset(_placed(tmp_path, stock, "stock"))
    assert hat_off == pytest.approx(stock_off)
    assert hat_off == pytest.approx((1.27, 24.13), abs=0.01)

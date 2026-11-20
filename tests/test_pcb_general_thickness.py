"""``PCB.general_thickness`` reads the ``(general (thickness X))`` header (#9746)."""

from __future__ import annotations

import pytest

from kicad_tools.schema.pcb import PCB

_BOARD = """\
(kicad_pcb
  (version 20240108)
  (generator "test_general_thickness")
{general}  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
)
"""


def _load(tmp_path, general: str) -> PCB:
    p = tmp_path / "board.kicad_pcb"
    p.write_text(_BOARD.format(general=general))
    return PCB.load(p)


def test_general_thickness_parsed(tmp_path):
    pcb = _load(tmp_path, "  (general (thickness 0.8) (legacy_teardrops no))\n")
    assert pcb.general_thickness == pytest.approx(0.8)


def test_general_thickness_none_when_absent(tmp_path):
    assert _load(tmp_path, "").general_thickness is None


def test_general_thickness_none_without_thickness_child(tmp_path):
    assert _load(tmp_path, "  (general (legacy_teardrops no))\n").general_thickness is None


@pytest.mark.parametrize("value", ["0", "-1.6"])
def test_general_thickness_none_when_non_positive(tmp_path, value):
    pcb = _load(tmp_path, f"  (general (thickness {value}))\n")
    assert pcb.general_thickness is None


def test_general_thickness_survives_save_roundtrip(tmp_path):
    pcb = _load(tmp_path, "  (general (thickness 0.8))\n")
    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)
    assert PCB.load(out).general_thickness == pytest.approx(0.8)

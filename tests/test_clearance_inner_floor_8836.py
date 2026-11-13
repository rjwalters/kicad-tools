"""Issue #8836: ``kct check`` applies the inner-layer clearance floor on inner layers.

JLCPCB ``4layer_2oz`` is 0.15 mm outer / 0.1016 mm inner.  The emitted
``.kicad_dru`` scopes them with ``(layer outer)`` / ``(layer inner)``; the
Python checker must agree.
"""

from __future__ import annotations

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker

_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (4 "In1.Cu" signal)
    (6 "In2.Cu" signal)
    (2 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "A")
  (net 2 "B")
"""


def _board(layer: str, gap: float = 0.13) -> str:
    """Two parallel 0.2 mm different-net tracks with edge-to-edge ``gap``."""
    y2 = 100 + 0.2 + gap
    return (
        _HEADER
        + f'  (segment (start 100 100) (end 105 100) (width 0.2) (layer "{layer}")'
        + ' (net 1 "A") (uuid "s1"))\n'
        + f'  (segment (start 100 {y2}) (end 105 {y2}) (width 0.2) (layer "{layer}")'
        + ' (net 2 "B") (uuid "s2"))\n'
        + ")\n"
    )


def _clearance_layers(tmp_path, layer: str, copper_oz: float) -> list[str]:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board(layer))
    checker = DRCChecker(PCB.load(path), manufacturer="jlcpcb", layers=4, copper_oz=copper_oz)
    results = checker.check_clearances()
    return [v.layer for v in results.violations if v.rule_id == "clearance_segment_segment"]


def test_two_oz_outer_gap_fails(tmp_path) -> None:
    assert _clearance_layers(tmp_path, "F.Cu", 2.0) == ["F.Cu"]


@pytest.mark.parametrize("layer", ["In1.Cu", "In2.Cu"])
def test_two_oz_inner_gap_passes(tmp_path, layer) -> None:
    assert _clearance_layers(tmp_path, layer, 2.0) == []


def test_one_oz_unchanged(tmp_path) -> None:
    assert _clearance_layers(tmp_path, "F.Cu", 1.0) == []
    assert _clearance_layers(tmp_path, "In1.Cu", 1.0) == []

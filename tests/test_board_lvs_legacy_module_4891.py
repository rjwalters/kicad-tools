"""Regression test: ``lvs/board_lvs.py`` on pre-KiCad-6 ``(module ...)`` boards.

``_pcb_pin_to_net`` used ``doc.find_all("footprint")`` to walk the PCB side of
an LVS comparison, so every pad living inside a legacy ``(module ...)``
container was invisible to the comparator, even though ``schema/pcb.py``
(#4879) already parses ``(module ...)`` as a footprint (issue #4891).
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.lvs.board_lvs import _pcb_pin_to_net

_LEGACY_MODULE_BOARD = """(kicad_pcb (version 4)
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (module R_0603 (layer F.Cu) (at 5 5)
    (fp_text reference R1 (at 0 1.5) (layer F.SilkS))
    (pad 1 smd rect (at -0.8 0) (size 0.9 0.8) (layers F.Cu) (net 1 "GND"))
    (pad 2 smd rect (at 0.8 0) (size 0.9 0.8) (layers F.Cu) (net 2 "SIG"))
  )
)
"""

_MODERN_FOOTPRINT_BOARD = """(kicad_pcb (version 20241229)
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (footprint "R_0603" (layer "F.Cu") (at 5 5)
    (fp_text reference "R1" (at 0 1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at -0.8 0) (size 0.9 0.8) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.8 0) (size 0.9 0.8) (layers "F.Cu") (net 2 "SIG"))
  )
)
"""


def _write(tmp_path: Path, text: str, name: str) -> Path:
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(text)
    return path


class TestPcbPinToNetLegacyModuleBoards:
    def test_reads_pads_inside_module_footprint(self, tmp_path: Path):
        pin_map = _pcb_pin_to_net(_write(tmp_path, _LEGACY_MODULE_BOARD, "legacy"))

        assert pin_map == {
            ("R1", "1"): "GND",
            ("R1", "2"): "SIG",
        }, "zero pins found inside (module ...) footprint (issue #4891)"

    def test_matches_modern_footprint_spelling(self, tmp_path: Path):
        legacy = _pcb_pin_to_net(_write(tmp_path, _LEGACY_MODULE_BOARD, "legacy"))
        modern = _pcb_pin_to_net(_write(tmp_path, _MODERN_FOOTPRINT_BOARD, "modern"))

        assert legacy == modern

    def test_no_footprints_of_either_spelling_yields_empty_map(self, tmp_path: Path):
        empty_board = '(kicad_pcb (version 20241229) (net 0 ""))'

        assert _pcb_pin_to_net(_write(tmp_path, empty_board, "empty")) == {}

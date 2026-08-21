"""Regression tests: ``panel/panel.py`` on pre-KiCad-6 ``(module ...)`` boards.

``Panel._place_board_copy`` filtered board content through a ``content_tags``
set containing only the modern ``"footprint"`` tag, so a legacy
``(module ...)`` board's entire component graph was silently dropped when
panelized (every ``child.name not in content_tags`` check discarded it before
the ``child.name == "footprint"`` reference-remap check downstream was ever
reached).  Both are fixed together as issue #4891 (the `content_tags`
membership check is what makes the specified ``child.name`` fix meaningful).
"""

from __future__ import annotations

from pathlib import Path

import pytest

shapely = pytest.importorskip("shapely", reason="Shapely required for panel tests")

from kicad_tools.panel.panel import Panel  # noqa: E402

# A minimal legacy (module ...) board: one footprint, two pads, Edge.Cuts rect.
_LEGACY_MODULE_BOARD = """(kicad_pcb (version 4)
  (general (thickness 1.6))
  (page A4)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts") (width 0.05))
  (module R_0603 (layer F.Cu) (at 5 5)
    (property "Reference" "R1" (at 0 -1.5) (layer "F.SilkS"))
    (pad 1 smd rect (at -0.8 0) (size 0.9 0.8) (layers F.Cu) (net 1 "GND"))
    (pad 2 smd rect (at 0.8 0) (size 0.9 0.8) (layers F.Cu) (net 2 "SIG"))
  )
)
"""

_MODERN_FOOTPRINT_BOARD = """(kicad_pcb (version 20241229)
  (general (thickness 1.6))
  (page A4)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts") (width 0.05))
  (footprint "R_0603" (layer "F.Cu") (at 5 5)
    (property "Reference" "R1" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at -0.8 0) (size 0.9 0.8) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.8 0) (size 0.9 0.8) (layers "F.Cu") (net 2 "SIG"))
  )
)
"""


def _footprint_like_children(sexp) -> list:
    return [c for c in sexp.children if c.name in ("footprint", "module")]


class TestPanelizeLegacyModuleBoards:
    def test_module_footprint_is_cloned_into_panel(self, tmp_path: Path):
        board_file = tmp_path / "legacy.kicad_pcb"
        board_file.write_text(_LEGACY_MODULE_BOARD)

        panel = Panel()
        panel.append_board(board_file, rows=1, cols=2, spacing=2.0)
        sexp = panel.build()

        clones = _footprint_like_children(sexp)
        assert len(clones) == 2, "(module ...) footprints dropped during panelization (issue #4891)"

    def test_matches_modern_footprint_spelling_clone_count(self, tmp_path: Path):
        legacy_file = tmp_path / "legacy.kicad_pcb"
        legacy_file.write_text(_LEGACY_MODULE_BOARD)
        modern_file = tmp_path / "modern.kicad_pcb"
        modern_file.write_text(_MODERN_FOOTPRINT_BOARD)

        legacy_sexp = Panel().append_board(legacy_file, rows=1, cols=2, spacing=2.0).build()
        modern_sexp = Panel().append_board(modern_file, rows=1, cols=2, spacing=2.0).build()

        assert len(_footprint_like_children(legacy_sexp)) == len(
            _footprint_like_children(modern_sexp)
        )

    def test_module_footprint_pads_survive_offset_and_net_remap(self, tmp_path: Path):
        board_file = tmp_path / "legacy.kicad_pcb"
        board_file.write_text(_LEGACY_MODULE_BOARD)

        panel = Panel()
        panel.append_board(board_file, rows=1, cols=1, spacing=2.0)
        sexp = panel.build()

        clones = _footprint_like_children(sexp)
        assert len(clones) == 1
        pads = clones[0].find_all("pad")
        assert len(pads) == 2

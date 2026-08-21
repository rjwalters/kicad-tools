"""Regression tests: ``drc/repair_silkscreen.py`` on pre-KiCad-6 ``(module ...)`` boards.

``SilkscreenRepairer.repair_line_widths`` and ``repair_text_heights`` both
walked ``self.doc.find_all("footprint")`` to reach footprint-level silkscreen
graphics/text, so a legacy ``(module ...)`` board's silkscreen was silently
skipped even though ``schema/pcb.py`` (#4879) already parses ``(module ...)``
as a footprint (issue #4891).
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer

# An undersized fp_line stroke and an undersized fp_text height, both inside a
# legacy (module ...) footprint.
_LEGACY_MODULE_BOARD = """(kicad_pcb (version 4)
  (module R_0603 (layer F.Cu) (at 5 5)
    (fp_text reference R1 (at 0 1.5) (layer F.SilkS)
      (effects (font (size 0.5 0.5) (thickness 0.075)))
    )
    (fp_line (start 0 0) (end 5 0)
      (stroke (width 0.10) (type solid)) (layer F.SilkS))
  )
)
"""

_MODERN_FOOTPRINT_BOARD = """(kicad_pcb (version 20241229)
  (footprint "R_0603" (layer "F.Cu") (at 5 5)
    (fp_text reference "R1" (at 0 1.5) (layer "F.SilkS")
      (effects (font (size 0.5 0.5) (thickness 0.075)))
    )
    (fp_line (start 0 0) (end 5 0)
      (stroke (width 0.10) (type solid)) (layer "F.SilkS"))
  )
)
"""


def _write(tmp_path: Path, text: str, name: str) -> Path:
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(text)
    return path


class TestRepairLineWidthsLegacyModuleBoards:
    def test_fixes_undersized_fp_line_inside_module(self, tmp_path: Path):
        repairer = SilkscreenRepairer(_write(tmp_path, _LEGACY_MODULE_BOARD, "legacy"))

        result = repairer.repair_line_widths(min_width_mm=0.15)

        assert result.total_fixed == 1, "fp_line inside (module ...) not repaired (issue #4891)"
        assert result.fixes[0].new_width == 0.15

    def test_matches_modern_footprint_spelling(self, tmp_path: Path):
        legacy = SilkscreenRepairer(_write(tmp_path, _LEGACY_MODULE_BOARD, "legacy"))
        modern = SilkscreenRepairer(_write(tmp_path, _MODERN_FOOTPRINT_BOARD, "modern"))

        legacy_result = legacy.repair_line_widths(min_width_mm=0.15)
        modern_result = modern.repair_line_widths(min_width_mm=0.15)

        assert legacy_result.total_fixed == modern_result.total_fixed == 1


class TestRepairTextHeightsLegacyModuleBoards:
    def test_fixes_undersized_fp_text_inside_module(self, tmp_path: Path):
        repairer = SilkscreenRepairer(_write(tmp_path, _LEGACY_MODULE_BOARD, "legacy"))

        result = repairer.repair_text_heights(min_height_mm=1.0)

        assert result.total_fixed == 1, "fp_text inside (module ...) not repaired (issue #4891)"
        assert result.fixes[0].new_height == 1.0

    def test_matches_modern_footprint_spelling(self, tmp_path: Path):
        legacy = SilkscreenRepairer(_write(tmp_path, _LEGACY_MODULE_BOARD, "legacy"))
        modern = SilkscreenRepairer(_write(tmp_path, _MODERN_FOOTPRINT_BOARD, "modern"))

        legacy_result = legacy.repair_text_heights(min_height_mm=1.0)
        modern_result = modern.repair_text_heights(min_height_mm=1.0)

        assert legacy_result.total_fixed == modern_result.total_fixed == 1

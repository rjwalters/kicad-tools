"""Regression tests: ``drc/repair_clearance.py`` on pre-KiCad-6 ``(module ...)`` boards.

``ClearanceRepairer._find_pads_near`` and ``ClearanceRepairer._find_footprint_by_ref``
both used ``self.doc.find_all("footprint")``, so every pad and footprint living
inside a legacy ``(module ...)`` container was invisible to pad-clearance nudging
and footprint-nudge repair, even though ``schema/pcb.py`` (#4879) already parses
``(module ...)`` as a footprint (issue #4891).
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.drc.repair_clearance import ClearanceRepairer

# Two 0603 footprints on a legacy (module ...) board, pads close enough
# together to be found by a nearby-point search.
_LEGACY_MODULE_BOARD = """(kicad_pcb (version 4)
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (module R_0402 (layer F.Cu) (at 100 100)
    (fp_text reference R1 (at 0 1.5) (layer F.SilkS))
    (pad 1 smd roundrect (at -0.5 0) (size 0.5 0.6) (layers F.Cu F.Paste F.Mask) (net 1 "GND"))
    (pad 2 smd roundrect (at 0.5 0) (size 0.5 0.6) (layers F.Cu F.Paste F.Mask) (net 2 "+3.3V"))
  )
  (module R_0402 (layer F.Cu) (at 101.1 100)
    (fp_text reference R2 (at 0 1.5) (layer F.SilkS))
    (pad 1 smd roundrect (at -0.5 0) (size 0.5 0.6) (layers F.Cu F.Paste F.Mask) (net 2 "+3.3V"))
    (pad 2 smd roundrect (at 0.5 0) (size 0.5 0.6) (layers F.Cu F.Paste F.Mask) (net 1 "GND"))
  )
)
"""

# The same board, modern (footprint ...) spelling + quoted references.
_MODERN_FOOTPRINT_BOARD = """(kicad_pcb (version 20241229)
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "R_0402" (layer "F.Cu") (at 100 100)
    (fp_text reference "R1" (at 0 1.5) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
  (footprint "R_0402" (layer "F.Cu") (at 101.1 100)
    (fp_text reference "R2" (at 0 1.5) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
)
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "legacy.kicad_pcb"
    path.write_text(text)
    return path


class TestFindPadsNearLegacyModuleBoards:
    """``_find_pads_near`` must see pads inside ``(module ...)`` footprints."""

    def test_finds_pads_inside_module_footprints(self, tmp_path: Path):
        repairer = ClearanceRepairer(_write(tmp_path, _LEGACY_MODULE_BOARD))

        results = repairer._find_pads_near(100.5, 100.0, radius=1.0, layer=None, nets=None)

        assert len(results) >= 2, "zero pads found inside (module ...) footprints (issue #4891)"

    def test_matches_modern_footprint_spelling_pad_count(self, tmp_path: Path):
        legacy = ClearanceRepairer(_write(tmp_path, _LEGACY_MODULE_BOARD))
        modern_path = tmp_path / "modern.kicad_pcb"
        modern_path.write_text(_MODERN_FOOTPRINT_BOARD)
        modern = ClearanceRepairer(modern_path)

        legacy_results = legacy._find_pads_near(100.5, 100.0, radius=1.0, layer=None, nets=None)
        modern_results = modern._find_pads_near(100.5, 100.0, radius=1.0, layer=None, nets=None)

        assert len(legacy_results) == len(modern_results)


class TestFindFootprintByRefLegacyModuleBoards:
    """``_find_footprint_by_ref`` must find a ``(module ...)`` footprint by reference."""

    def test_finds_module_footprint_by_reference(self, tmp_path: Path):
        repairer = ClearanceRepairer(_write(tmp_path, _LEGACY_MODULE_BOARD))

        result = repairer._find_footprint_by_ref("R1")

        assert result is not None, "footprint R1 not found inside (module ...) block (issue #4891)"
        fp_node, ref, x, y, locked, pad_count, is_connector = result
        assert ref == "R1"
        assert (x, y) == (100.0, 100.0)
        assert pad_count == 2

    def test_returns_none_for_missing_reference(self, tmp_path: Path):
        repairer = ClearanceRepairer(_write(tmp_path, _LEGACY_MODULE_BOARD))

        assert repairer._find_footprint_by_ref("U99") is None

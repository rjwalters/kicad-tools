"""Regression tests for pre-KiCad-6 ``(module ...)`` boards (issue #4893).

PR #4879 taught ``schema/pcb.py`` to recognize the legacy pre-KiCad-6
``(module ...)`` container as a footprint (exporting ``FOOTPRINT_TAGS`` /
``_is_footprint_tag()``). This issue is Part 1c of #4882: the last group of
call sites in the PCB-core/export/reasoning/silkscreen tree-walks that were
still blind to the legacy spelling -- each one silently treated a
``(module ...)`` board as if it had zero footprints, with no error.

Covers:
- ``cli/pcb_modify.py``: ``find_footprint_sexp``
- ``export/dsn.py``: ``KiCadToDSNExporter._extract_footprints``
- ``pcb/center_sheet.py``: ``translate_pcb_text`` (``_iter_child_blocks``
  tag-string comparison)
- ``pcb/editor.py``: ``PCBEditor._parse_footprints``
- ``reasoning/state.py``: ``PCBState._parse_pcb`` (component parsing) and
  ``PCBState._estimate_bounds_from_components`` (bounding-box fallback)
- ``silkscreen/generator.py``: ``SilkscreenGenerator.ensure_ref_des_visible``
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.pcb_modify import find_footprint_sexp
from kicad_tools.core.sexp_file import load_pcb
from kicad_tools.export.dsn import KiCadToDSNExporter
from kicad_tools.pcb.center_sheet import translate_pcb_text
from kicad_tools.pcb.editor import PCBEditor
from kicad_tools.reasoning.state import PCBState
from kicad_tools.silkscreen.generator import SilkscreenGenerator

# A pre-KiCad-6 board: (module ...) container, unquoted fp_text values --
# mirrors the fixture PR #4879 used for schema/pcb.py's own regression tests
# (tests/test_pcb.py::TestLegacyModuleBoards) and the one PR #4909 used for
# #4891's sibling call sites.
LEGACY_MODULE_BOARD = """(kicad_pcb (version 4) (host pcbnew "(2017-11-30)")
  (general (thickness 1.6))
  (page A4)
  (layers (0 F.Cu signal) (31 B.Cu signal) (44 Edge.Cuts user))
  (net 0 "")
  (net 1 GND)
  (net 2 SIG)
  (gr_line (start 0 0) (end 20 0) (layer Edge.Cuts) (width 0.05))
  (gr_line (start 20 0) (end 20 10) (layer Edge.Cuts) (width 0.05))
  (gr_line (start 20 10) (end 0 10) (layer Edge.Cuts) (width 0.05))
  (gr_line (start 0 10) (end 0 0) (layer Edge.Cuts) (width 0.05))
  (module R_0603 (layer F.Cu) (tedit 5A1F2B3C) (at 5 5)
    (attr smd)
    (fp_text reference R1 (at 0 1.5) (layer F.SilkS))
    (fp_text value 10K (at 0 -1.5) (layer F.Fab))
    (pad 1 smd rect (at -0.8 0) (size 0.9 0.8) (layers F.Cu F.Paste F.Mask) (net 1 GND))
    (pad 2 smd rect (at 0.8 0) (size 0.9 0.8) (layers F.Cu F.Paste F.Mask) (net 2 SIG))
  )
  (module C_0402 (layer B.Cu) (tedit 5A1F2B3D) (at 12 5 90)
    (fp_text reference C1 (at 0 1.5) (layer B.SilkS))
    (pad 1 smd rect (at -0.5 0) (size 0.6 0.6) (layers B.Cu B.Paste B.Mask) (net 2 SIG))
  )
  (segment (start 4.2 5) (end 12.8 5) (width 0.25) (layer F.Cu) (net 1))
)
"""

# Same board, no footprints of either spelling -- must not crash any touched
# call site, and must not fabricate footprints out of thin air.
EMPTY_BOARD = """(kicad_pcb (version 4)
  (general (thickness 1.6))
  (page A4)
  (layers (0 F.Cu signal) (31 B.Cu signal) (44 Edge.Cuts user))
  (net 0 "")
)
"""


def _write(tmp_path: Path, text: str, name: str = "legacy") -> Path:
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    return path


class TestPcbModifyFindFootprintSexp:
    """``pcb_modify.find_footprint_sexp`` must locate ``(module ...)`` footprints."""

    def test_finds_footprint_by_reference(self, tmp_path: Path):
        doc = load_pcb(str(_write(tmp_path, LEGACY_MODULE_BOARD)))

        fp = find_footprint_sexp(doc, "R1")

        assert fp is not None
        assert fp.tag == "module"

    def test_missing_reference_returns_none(self, tmp_path: Path):
        doc = load_pcb(str(_write(tmp_path, LEGACY_MODULE_BOARD)))

        assert find_footprint_sexp(doc, "U99") is None

    def test_zero_footprints_does_not_crash(self, tmp_path: Path):
        doc = load_pcb(str(_write(tmp_path, EMPTY_BOARD, "empty")))

        assert find_footprint_sexp(doc, "R1") is None


class TestDsnExportLegacyModuleBoards:
    """``KiCadToDSNExporter`` must not see zero footprints on a legacy board."""

    def test_legacy_module_board_yields_footprints(self, tmp_path: Path):
        exporter = KiCadToDSNExporter(_write(tmp_path, LEGACY_MODULE_BOARD))

        fps = exporter.footprints

        assert len(fps) == 2, "zero footprints extracted from a (module ...) board (issue #4893)"
        assert sorted(fp.reference for fp in fps) == ["C1", "R1"]

    def test_export_succeeds_on_legacy_board(self, tmp_path: Path):
        exporter = KiCadToDSNExporter(_write(tmp_path, LEGACY_MODULE_BOARD))

        dsn_text = exporter.export()

        assert "R1" in dsn_text
        assert "C1" in dsn_text

    def test_zero_footprints_does_not_crash(self, tmp_path: Path):
        exporter = KiCadToDSNExporter(_write(tmp_path, EMPTY_BOARD, "empty"))

        assert exporter.footprints == []


class TestCenterSheetLegacyModuleBoards:
    """``translate_pcb_text`` must move a ``(module ...)``'s own ``(at ...)``."""

    def test_module_at_node_translates(self):
        new_text = translate_pcb_text(LEGACY_MODULE_BOARD, dx_mm=10.0, dy_mm=0.0)

        # R1's footprint-level (at 5 5) must shift; its child fp_text/pad
        # coordinates (footprint-relative) must NOT.
        assert "(at 15 5)" in new_text
        assert "(at -0.8 0)" in new_text, "pad-relative coordinates must stay untouched"

    def test_second_module_at_with_rotation_translates(self):
        new_text = translate_pcb_text(LEGACY_MODULE_BOARD, dx_mm=10.0, dy_mm=0.0)

        # C1's footprint-level (at 12 5 90); rotation angle must be preserved.
        assert "(at 22 5 90)" in new_text

    def test_zero_footprints_does_not_crash(self):
        new_text = translate_pcb_text(EMPTY_BOARD, dx_mm=5.0, dy_mm=5.0)

        assert "(kicad_pcb" in new_text


class TestPcbEditorLegacyModuleBoards:
    """``PCBEditor`` must parse footprints out of a ``(module ...)`` board."""

    def test_parses_module_footprints(self, tmp_path: Path):
        editor = PCBEditor(str(_write(tmp_path, LEGACY_MODULE_BOARD)))

        assert set(editor.footprints) == {"R1", "C1"}
        assert editor.footprints["R1"]["layer"] == "F.Cu"

    def test_zero_footprints_does_not_crash(self, tmp_path: Path):
        editor = PCBEditor(str(_write(tmp_path, EMPTY_BOARD, "empty")))

        assert editor.footprints == {}


class TestReasoningStateLegacyModuleBoards:
    """``PCBState`` must parse components (and estimate bounds) from ``(module ...)``."""

    def test_components_parsed_from_module_boards(self, tmp_path: Path):
        state = PCBState.from_pcb(_write(tmp_path, LEGACY_MODULE_BOARD))

        assert set(state.components) == {"R1", "C1"}
        assert state.components["R1"].footprint == "R_0603"
        assert state.components["R1"].position == (5.0, 5.0)

    def test_bounding_box_estimated_from_module_footprints_when_no_outline(self, tmp_path: Path):
        # No Edge.Cuts geometry at all -- forces the component-position fallback.
        board = """(kicad_pcb (version 4)
  (general (thickness 1.6))
  (page A4)
  (layers (0 F.Cu signal) (31 B.Cu signal))
  (net 0 "")
  (net 1 GND)
  (module R_0603 (layer F.Cu) (at 5 5)
    (fp_text reference R1 (at 0 1.5) (layer F.SilkS))
    (pad 1 smd rect (at -0.8 0) (size 0.9 0.8) (layers F.Cu) (net 1 GND))
  )
)
"""
        state = PCBState.from_pcb(_write(tmp_path, board, "no_outline"))

        assert state.outline is not None
        # The 5mm margin the estimator adds means the outline must at least
        # enclose the (5, 5) footprint position -- it must not be empty/zero.
        assert state.outline.width > 0
        assert state.outline.height > 0

    def test_zero_footprints_does_not_crash(self, tmp_path: Path):
        state = PCBState.from_pcb(_write(tmp_path, EMPTY_BOARD, "empty"))

        assert state.components == {}


class TestSilkscreenGeneratorLegacyModuleBoards:
    """``ensure_ref_des_visible`` must walk ``(module ...)`` footprints too."""

    def test_unhides_reference_on_module_footprint(self, tmp_path: Path):
        board = """(kicad_pcb (version 4)
  (general (thickness 1.6))
  (page A4)
  (layers (0 F.Cu signal) (31 B.Cu signal))
  (net 0 "")
  (module R_0603 (layer F.Cu) (at 5 5)
    (fp_text reference R1 (at 0 1.5) (layer F.SilkS) (effects (font (size 1 1))) (hide))
  )
)
"""
        gen = SilkscreenGenerator(_write(tmp_path, board, "hidden_ref"))

        result = gen.ensure_ref_des_visible()

        assert result.refs_unhidden == 1, (
            "reference on a (module ...) footprint was not unhidden (issue #4893)"
        )
        assert "R1" in result.messages[0]

    def test_zero_footprints_does_not_crash(self, tmp_path: Path):
        gen = SilkscreenGenerator(_write(tmp_path, EMPTY_BOARD, "empty"))

        result = gen.ensure_ref_des_visible()

        assert result.refs_unhidden == 0

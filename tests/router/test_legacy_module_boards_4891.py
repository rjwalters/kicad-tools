"""Regression tests for pre-KiCad-6 ``(module ...)`` boards in router/io.py (#4891).

``load_pcb_for_routing``, ``load_pads_for_analysis``, and ``extract_pad_positions``
all split the raw PCB text on ``re.split(r"(?=\\(footprint\\s)", pcb_text)``, so a
legacy board written with the pre-KiCad-6 ``(module ...)`` container spelling
never produced a single section that ``.startswith("(footprint")`` -- every
footprint (and therefore every pad) silently vanished, with no error and no
warning.  This is the same "the parser reads it but nothing downstream sees
it" defect PR #4879 fixed for ``schema/pcb.py``, recurring in the router I/O
tree-walks (issue #4873 -> #4882 -> #4891).

``load_pcb_for_routing``'s footprint-reference regex also required a quoted
``fp_text reference "R1"`` value; real pre-KiCad-6 boards write this value
*unquoted* (``fp_text reference R1``), which -- pre-fix -- silently dropped
the whole footprint even once the ``(module ...)`` container was recognized.
Fixed to accept an optional quote, matching the fallback already used by
``load_pads_for_analysis`` in this same file.
"""

from pathlib import Path

from kicad_tools.router.io import (
    extract_pad_positions,
    load_pads_for_analysis,
    load_pcb_for_routing,
)

# A pre-KiCad-6 board: (module ...) container, unquoted fp_text values --
# mirrors the fixture PR #4879 used for schema/pcb.py's own regression tests
# (tests/test_pcb.py::TestLegacyModuleBoards) and matches the shape of real
# corpus boards fetched during manual verification (e.g. os-0064015-pcb0,
# os-0070757-pcb1 -- see issue #4882).
LEGACY_MODULE_BOARD = """(kicad_pcb (version 4) (host pcbnew "(2017-11-30)")
  (general (thickness 1.6))
  (page A4)
  (layers (0 F.Cu signal) (31 B.Cu signal) (44 Edge.Cuts user))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (gr_rect (start 0 0) (end 20 10) (layer Edge.Cuts) (width 0.05))
  (module R_0603 (layer F.Cu) (tedit 5A1F2B3C) (at 5 5)
    (attr smd)
    (fp_text reference R1 (at 0 1.5) (layer F.SilkS))
    (fp_text value 10K (at 0 -1.5) (layer F.Fab))
    (pad 1 smd rect (at -0.8 0) (size 0.9 0.8) (layers F.Cu F.Paste F.Mask) (net 1 "GND"))
    (pad 2 smd rect (at 0.8 0) (size 0.9 0.8) (layers F.Cu F.Paste F.Mask) (net 2 "SIG"))
  )
  (module C_0402 (layer B.Cu) (tedit 5A1F2B3D) (at 12 5 90)
    (fp_text reference C1 (at 0 1.5) (layer B.SilkS))
    (pad 1 smd rect (at -0.5 0) (size 0.6 0.6) (layers B.Cu B.Paste B.Mask) (net 2 "SIG"))
  )
  (segment (start 4.2 5) (end 12.8 5) (width 0.25) (layer F.Cu) (net 1))
)
"""

# The same board, modern (footprint ...) spelling + quoted references, as the
# "no regression" control -- must classify identically to the legacy board.
MODERN_FOOTPRINT_BOARD = """(kicad_pcb (version 20241229)
  (general (thickness 1.6))
  (page A4)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts") (width 0.05))
  (footprint "R_0603" (layer "F.Cu") (at 5 5)
    (attr smd)
    (fp_text reference "R1" (at 0 1.5) (layer "F.SilkS"))
    (fp_text value "10K" (at 0 -1.5) (layer "F.Fab"))
    (pad "1" smd rect (at -0.8 0) (size 0.9 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd rect (at 0.8 0) (size 0.9 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SIG"))
  )
  (footprint "C_0402" (layer "B.Cu") (at 12 5 90)
    (fp_text reference "C1" (at 0 1.5) (layer "B.SilkS"))
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "B.Cu" "B.Paste" "B.Mask") (net 2 "SIG"))
  )
  (segment (start 4.2 5) (end 12.8 5) (width 0.25) (layer "F.Cu") (net 1))
)
"""


class TestLoadPcbForRoutingLegacyModuleBoards:
    """``load_pcb_for_routing`` must not see zero pads on a ``(module ...)`` board."""

    def test_legacy_module_board_yields_nonzero_pads(self, tmp_path: Path):
        pcb_file = tmp_path / "legacy.kicad_pcb"
        pcb_file.write_text(LEGACY_MODULE_BOARD)

        router, net_map = load_pcb_for_routing(pcb_file)

        assert len(router.pads) == 3, "zero pads on a (module ...) board (issue #4891)"
        assert "GND" in net_map
        assert "SIG" in net_map

    def test_legacy_and_modern_spelling_agree_on_pad_count(self, tmp_path: Path):
        """Same board, two spellings -- must classify identically (no regression)."""
        legacy_file = tmp_path / "legacy.kicad_pcb"
        legacy_file.write_text(LEGACY_MODULE_BOARD)
        modern_file = tmp_path / "modern.kicad_pcb"
        modern_file.write_text(MODERN_FOOTPRINT_BOARD)

        legacy_router, legacy_nets = load_pcb_for_routing(legacy_file)
        modern_router, modern_nets = load_pcb_for_routing(modern_file)

        assert len(legacy_router.pads) == len(modern_router.pads) == 3
        assert set(legacy_nets) == set(modern_nets)

    def test_zero_footprints_of_either_spelling_yields_empty_result(self, tmp_path: Path):
        """A board with no footprints at all must not crash."""
        pcb_content = """(kicad_pcb (version 20241229)
  (general (thickness 1.6))
  (page A4)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "")
  (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts") (width 0.05))
)
"""
        pcb_file = tmp_path / "empty.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(pcb_file)

        assert len(router.pads) == 0


class TestLoadPadsForAnalysisLegacyModuleBoards:
    """``load_pads_for_analysis`` must not see zero pads on a ``(module ...)`` board."""

    def test_legacy_module_board_yields_nonzero_pads(self):
        pads = load_pads_for_analysis(LEGACY_MODULE_BOARD)

        assert len(pads) == 3, "zero pads on a (module ...) board (issue #4891)"
        assert sorted({p.ref for p in pads}) == ["C1", "R1"]

    def test_legacy_and_modern_spelling_agree(self):
        legacy_pads = load_pads_for_analysis(LEGACY_MODULE_BOARD)
        modern_pads = load_pads_for_analysis(MODERN_FOOTPRINT_BOARD)

        assert len(legacy_pads) == len(modern_pads) == 3
        assert sorted({p.ref for p in legacy_pads}) == sorted({p.ref for p in modern_pads})

    def test_zero_footprints_of_either_spelling_yields_empty_result(self):
        pads = load_pads_for_analysis('(kicad_pcb (version 20241229) (net 0 ""))')

        assert pads == []


class TestExtractPadPositionsLegacyModuleBoards:
    """``extract_pad_positions`` must not see zero pads on a ``(module ...)`` board."""

    def test_legacy_module_board_yields_nonzero_positions(self):
        positions = extract_pad_positions(LEGACY_MODULE_BOARD)

        assert len(positions) == 3, "zero pad positions on a (module ...) board (issue #4891)"

    def test_legacy_and_modern_spelling_agree_on_position_count(self):
        legacy_positions = extract_pad_positions(LEGACY_MODULE_BOARD)
        modern_positions = extract_pad_positions(MODERN_FOOTPRINT_BOARD)

        assert len(legacy_positions) == len(modern_positions) == 3

    def test_zero_footprints_of_either_spelling_yields_empty_result(self):
        positions = extract_pad_positions('(kicad_pcb (version 20241229) (net 0 ""))')

        assert positions == []


class TestUnquotedLegacyReference:
    """``load_pcb_for_routing``'s fp_text fallback must accept an unquoted value.

    Real pre-KiCad-6 boards write ``(fp_text reference R1 ...)`` with no
    quotes around the reference.  A quote-only regex silently drops the whole
    footprint's pads even after the ``(module ...)`` container itself is
    recognized -- verified live against the corpus board ``os-0064015-pcb0``
    (#4882), which reports zero pads through ``load_pcb_for_routing`` with
    only the container-spelling fix applied.
    """

    def test_unquoted_fp_text_reference_is_not_dropped(self, tmp_path: Path):
        pcb_content = """(kicad_pcb (version 4)
  (net 0 "")
  (net 1 "GND")
  (module R_0603 (layer F.Cu) (at 5 5)
    (fp_text reference R1 (at 0 1.5) (layer F.SilkS))
    (pad 1 smd rect (at -0.8 0) (size 0.9 0.8) (layers F.Cu) (net 1 "GND"))
  )
)
"""
        pcb_file = tmp_path / "unquoted_ref.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, _net_map = load_pcb_for_routing(pcb_file)

        assert len(router.pads) == 1

"""Regression tests for pre-KiCad-6 ``(module ...)`` boards in stitch_cmd.py (#4892).

Several tree-walks in ``kicad_tools.cli.stitch_cmd`` did an exact-match
``child.tag == "footprint"`` (or the negated ``fp.tag != "footprint"`` guard
clause) comparison, so a pre-KiCad-6 board using the legacy ``(module ...)``
container spelling silently contributed zero pads/footprints to these
tree-walks -- the same "the parser reads it but nothing downstream sees it"
defect PR #4879 fixed for ``schema/pcb.py``, and PR #4909 (#4891) fixed for
the router/zones/DRC/LVS/panel tree-walks.

Each test below builds a small ``(module ...)`` board, exercises one touched
function, and asserts the legacy spelling is not silently dropped.  A
matching modern ``(footprint ...)`` board is used as a "no regression"
control where useful.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.stitch_cmd import (
    find_all_drills,
    find_all_pad_bboxes,
    find_all_pads,
    find_pads_on_nets,
    find_smd_pad_bboxes_on_nets,
    find_thermal_pad_candidates,
    get_name_to_net_map,
    get_net_map,
)
from kicad_tools.core.sexp_file import load_pcb

# A pre-KiCad-6 board: (module ...) container, unquoted fp_text values --
# mirrors the fixture used by tests/test_pcb.py::TestLegacyModuleBoards and
# tests/router/test_legacy_module_boards_4891.py.
LEGACY_MODULE_BOARD = """(kicad_pcb (version 4) (host pcbnew "(2017-11-30)")
  (general (thickness 1.6))
  (page A4)
  (layers (0 F.Cu signal) (31 B.Cu signal) (44 Edge.Cuts user))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
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

# The same board, modern spelling, as the no-regression control.
MODERN_FOOTPRINT_BOARD = """(kicad_pcb
  (version 20241229)
  (general (thickness 1.6))
  (page A4)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
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

# A board with no footprints of either spelling -- the zero-footprint edge case.
NO_FOOTPRINT_BOARD = """(kicad_pcb
  (version 20241229)
  (general (thickness 1.6))
  (page A4)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "")
  (net 1 "GND")
  (segment (start 0 0) (end 10 0) (width 0.25) (layer "F.Cu") (net 1))
)
"""


def _write(tmp_path: Path, text: str, name: str = "board") -> Path:
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    return path


class TestGetNetMapLegacyModule:
    """``get_net_map``'s name-only synthesis fallback (line ~445) must
    collect pad net references from (module ...) footprints, not just
    (footprint ...) ones.
    """

    def test_synthesizes_net_names_from_legacy_module_pads(self, tmp_path: Path):
        # No (net N "name") header table -- forces the synthesis fallback --
        # and pads use KiCad-10-style name-only inline net references.
        board = """(kicad_pcb (version 4)
          (general (thickness 1.6))
          (layers (0 F.Cu signal) (31 B.Cu signal))
          (module R_0603 (layer F.Cu) (at 5 5)
            (fp_text reference R1 (at 0 1.5) (layer F.SilkS))
            (pad 1 smd rect (at 0 0) (size 0.9 0.8) (layers F.Cu) (net "GND"))
            (pad 2 smd rect (at 1 0) (size 0.9 0.8) (layers F.Cu) (net "SIG"))
          )
        )
        """
        sexp = load_pcb(_write(tmp_path, board))
        net_map = get_net_map(sexp)
        assert set(net_map.values()) == {"GND", "SIG"}, (
            "zero net names synthesized from (module ...) pad references (issue #4892)"
        )

    def test_get_name_to_net_map_reverse_lookup(self, tmp_path: Path):
        board = """(kicad_pcb (version 4)
          (general (thickness 1.6))
          (layers (0 F.Cu signal) (31 B.Cu signal))
          (module R_0603 (layer F.Cu) (at 5 5)
            (fp_text reference R1 (at 0 1.5) (layer F.SilkS))
            (pad 1 smd rect (at 0 0) (size 0.9 0.8) (layers F.Cu) (net "GND"))
          )
        )
        """
        sexp = load_pcb(_write(tmp_path, board))
        name_to_num = get_name_to_net_map(sexp)
        assert "GND" in name_to_num


class TestFindPadsOnNetsLegacyModule:
    """``find_pads_on_nets`` (stitch_cmd.py line ~767) must see (module ...) pads."""

    def test_legacy_module_pads_found(self, tmp_path: Path):
        sexp = load_pcb(_write(tmp_path, LEGACY_MODULE_BOARD))
        pads = find_pads_on_nets(sexp, {"GND", "SIG"})
        assert len(pads) == 3, "zero pads found inside (module ...) footprints (issue #4892)"
        refs = {f"{p.reference}.{p.pad_number}" for p in pads}
        assert refs == {"R1.1", "R1.2", "C1.1"}

    def test_legacy_and_modern_spelling_agree(self, tmp_path: Path):
        legacy_sexp = load_pcb(_write(tmp_path, LEGACY_MODULE_BOARD, "legacy"))
        modern_sexp = load_pcb(_write(tmp_path, MODERN_FOOTPRINT_BOARD, "modern"))

        legacy_pads = find_pads_on_nets(legacy_sexp, {"GND", "SIG"})
        modern_pads = find_pads_on_nets(modern_sexp, {"GND", "SIG"})
        assert len(legacy_pads) == len(modern_pads)

    def test_no_footprints_returns_empty(self, tmp_path: Path):
        sexp = load_pcb(_write(tmp_path, NO_FOOTPRINT_BOARD, "empty"))
        assert find_pads_on_nets(sexp, {"GND"}) == []


class TestFindSmdPadBboxesOnNetsLegacyModule:
    """``find_smd_pad_bboxes_on_nets`` (line ~890) must see (module ...) pads."""

    def test_legacy_module_bboxes_found(self, tmp_path: Path):
        sexp = load_pcb(_write(tmp_path, LEGACY_MODULE_BOARD))
        net_map = get_net_map(sexp)
        net_nums = set(net_map.keys()) - {0}
        bboxes = find_smd_pad_bboxes_on_nets(sexp, net_nums)
        assert len(bboxes) == 3, "zero SMD pad bboxes inside (module ...) footprints"

    def test_no_footprints_returns_empty(self, tmp_path: Path):
        sexp = load_pcb(_write(tmp_path, NO_FOOTPRINT_BOARD, "empty"))
        assert find_smd_pad_bboxes_on_nets(sexp, {1}) == []


class TestFindAllPadBboxesLegacyModule:
    """``find_all_pad_bboxes`` (line ~999) must see (module ...) pads."""

    def test_legacy_module_bboxes_found(self, tmp_path: Path):
        sexp = load_pcb(_write(tmp_path, LEGACY_MODULE_BOARD))
        bboxes = find_all_pad_bboxes(sexp)
        assert len(bboxes) == 3, "zero pad bboxes inside (module ...) footprints"

    def test_no_footprints_returns_empty(self, tmp_path: Path):
        sexp = load_pcb(_write(tmp_path, NO_FOOTPRINT_BOARD, "empty"))
        assert find_all_pad_bboxes(sexp) == []


class TestFindAllPadsLegacyModule:
    """``find_all_pads`` (line ~1288) must see (module ...) pads."""

    def test_legacy_module_pads_found(self, tmp_path: Path):
        sexp = load_pcb(_write(tmp_path, LEGACY_MODULE_BOARD))
        pads = find_all_pads(sexp)
        assert len(pads) == 3, "zero pads found inside (module ...) footprints"

    def test_legacy_and_modern_spelling_agree(self, tmp_path: Path):
        legacy_sexp = load_pcb(_write(tmp_path, LEGACY_MODULE_BOARD, "legacy"))
        modern_sexp = load_pcb(_write(tmp_path, MODERN_FOOTPRINT_BOARD, "modern"))
        assert len(find_all_pads(legacy_sexp)) == len(find_all_pads(modern_sexp))

    def test_no_footprints_returns_empty(self, tmp_path: Path):
        sexp = load_pcb(_write(tmp_path, NO_FOOTPRINT_BOARD, "empty"))
        assert find_all_pads(sexp) == []


class TestFindAllDrillsLegacyModule:
    """``find_all_drills`` (line ~1413) must see thru-hole pads in (module ...)."""

    def test_legacy_module_thru_hole_pad_drill_found(self, tmp_path: Path):
        board = """(kicad_pcb (version 4)
          (general (thickness 1.6))
          (layers (0 F.Cu signal) (31 B.Cu signal))
          (net 0 "")
          (net 1 "GND")
          (module Conn (layer F.Cu) (at 10 10)
            (fp_text reference J1 (at 0 1.5) (layer F.SilkS))
            (pad 1 thru_hole circle (at 0 0) (size 1.5 1.5) (drill 0.8)
              (layers F.Cu B.Cu) (net 1 "GND"))
          )
        )
        """
        sexp = load_pcb(_write(tmp_path, board))
        drills = find_all_drills(sexp)
        assert len(drills) == 1, "zero thru-hole pad drills inside (module ...) footprint"
        x, y, drill, net_num = drills[0]
        assert abs(x - 10.0) < 1e-6
        assert abs(y - 10.0) < 1e-6
        assert abs(drill - 0.8) < 1e-6
        assert net_num == 1

    def test_no_footprints_returns_only_via_drills(self, tmp_path: Path):
        board = """(kicad_pcb
          (version 20241229)
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (net 0 "")
          (net 1 "GND")
          (via (at 5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
        )
        """
        sexp = load_pcb(_write(tmp_path, board, "vias_only"))
        drills = find_all_drills(sexp)
        assert len(drills) == 1
        assert drills[0][2] == 0.3


class TestFindThermalPadCandidatesLegacyModule:
    """``find_thermal_pad_candidates`` (line ~3848) must see (module ...) pads."""

    def test_large_pad_in_legacy_module_is_a_candidate(self, tmp_path: Path):
        board = """(kicad_pcb (version 4)
          (general (thickness 1.6))
          (layers (0 F.Cu signal) (31 B.Cu signal))
          (net 0 "")
          (net 1 "GND")
          (module TO-220 (layer F.Cu) (at 20 20)
            (fp_text reference Q1 (at 0 1.5) (layer F.SilkS))
            (pad 1 smd rect (at 0 0) (size 3.0 3.0) (layers F.Cu F.Paste F.Mask)
              (net 1 "GND"))
          )
        )
        """
        sexp = load_pcb(_write(tmp_path, board))
        candidates = find_thermal_pad_candidates(sexp, {"GND"})
        assert len(candidates) == 1, "zero thermal pad candidates inside (module ...) footprint"
        assert candidates[0].pad.reference == "Q1"

    def test_no_footprints_returns_empty(self, tmp_path: Path):
        sexp = load_pcb(_write(tmp_path, NO_FOOTPRINT_BOARD, "empty"))
        assert find_thermal_pad_candidates(sexp, {"GND"}) == []

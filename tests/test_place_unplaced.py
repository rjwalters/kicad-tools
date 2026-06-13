"""Tests for place-unplaced command and module (issue #1984).

Verifies:
- Detection of components at sheet origin and outside board bounds
- Grid computation with margin and spacing parameters
- Placement algorithm assigns unplaced components to free grid cells
- Overflow reporting when grid space is exhausted
- Dry-run mode reports without modifying the PCB
- Clustering by shared nets groups related components
- Graceful error when no Edge.Cuts outline exists
- All-placed scenario returns zero count
- CLI dispatch and subcommand parsing
- MCP tool registration and delegation
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.placement.place_unplaced import (
    PlaceUnplacedResult,
    _build_grid,
    _cell_occupied,
    _cluster_footprints,
    _detect_unplaced,
    _footprint_size,
    _get_board_bounds,
    _GridCell,
    _is_at_origin_absolute,
    _is_outside_bounds,
    place_unplaced,
)
from kicad_tools.schema.pcb import PCB

# ---------------------------------------------------------------------------
# Inline PCB fixtures
# ---------------------------------------------------------------------------

# A 50x50mm board at sheet position (100, 100) with two footprints:
#   R1 at origin (0, 0) -- unplaced
#   R2 properly placed inside the board at (125, 125) -- board-relative (25, 25)
BOARD_WITH_UNPLACED = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (gr_rect (start 100 100) (end 150 150) (layer "Edge.Cuts") (stroke (width 0.05) (type default)))
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 0 0)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 125 125)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
)
"""

# Same board, but R1 is far outside the board outline instead of at origin.
BOARD_WITH_OUT_OF_BOUNDS = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (gr_rect (start 100 100) (end 150 150) (layer "Edge.Cuts") (stroke (width 0.05) (type default)))
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 300 300)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 125 125)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
)
"""

# Board where all components are properly placed inside the outline.
BOARD_ALL_PLACED = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (gr_rect (start 100 100) (end 150 150) (layer "Edge.Cuts") (stroke (width 0.05) (type default)))
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 120 120)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
  )
)
"""

# Board with no Edge.Cuts outline.
BOARD_NO_OUTLINE = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 0 0)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
  )
)
"""

# Board with many components at origin to test overflow.
# Tiny 10x10mm board with 20 components at origin -- not all will fit.
_ORIGIN_FP_TEMPLATE = """\
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{idx:02d}")
    (at 0 0)
    (property "Reference" "R{idx}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net {net_a} "N{net_a}"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net {net_b} "N{net_b}"))
  )"""


def _make_overflow_board() -> str:
    fps = []
    for i in range(1, 21):
        fps.append(_ORIGIN_FP_TEMPLATE.format(idx=i, net_a=(i * 2 - 1), net_b=(i * 2)))
    nets = "\n".join(f'  (net {n} "N{n}")' for n in range(1, 41))
    return f"""\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
{nets}
  (gr_rect (start 100 100) (end 110 110) (layer "Edge.Cuts") (stroke (width 0.05) (type default)))
{"".join(fps)}
)
"""


# Board with cluster-able components: R1 and R2 share net 1, R3 is isolated.
BOARD_WITH_CLUSTERS = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG")
  (gr_rect (start 100 100) (end 200 200) (layer "Edge.Cuts") (stroke (width 0.05) (type default)))
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 0 0)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 0 0)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "SIG"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000003")
    (at 0 0)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "SIG"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pcb(tmp_path: Path, content: str, name: str = "test.kicad_pcb") -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Unit: detection logic
# ---------------------------------------------------------------------------


class TestDetection:
    """Tests for _is_at_origin_absolute and _is_outside_bounds."""

    def test_at_origin_detects_origin_component(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        pcb = PCB.load(str(pcb_path))
        # R1 is at sheet-absolute (0, 0) which is at origin
        r1 = [fp for fp in pcb.footprints if fp.reference == "R1"][0]
        assert _is_at_origin_absolute(r1, pcb.board_origin)

    def test_at_origin_does_not_flag_placed_component(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        pcb = PCB.load(str(pcb_path))
        # R2 at (125, 125) is not at origin
        r2 = [fp for fp in pcb.footprints if fp.reference == "R2"][0]
        assert not _is_at_origin_absolute(r2, pcb.board_origin)

    def test_outside_bounds_detects_far_component(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_OUT_OF_BOUNDS)
        pcb = PCB.load(str(pcb_path))
        bounds = _get_board_bounds(pcb)
        assert bounds is not None
        r1 = [fp for fp in pcb.footprints if fp.reference == "R1"][0]
        assert _is_outside_bounds(r1, bounds)

    def test_inside_bounds_not_flagged(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_OUT_OF_BOUNDS)
        pcb = PCB.load(str(pcb_path))
        bounds = _get_board_bounds(pcb)
        assert bounds is not None
        r2 = [fp for fp in pcb.footprints if fp.reference == "R2"][0]
        assert not _is_outside_bounds(r2, bounds)

    def test_detect_unplaced_returns_only_unplaced(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        pcb = PCB.load(str(pcb_path))
        bounds = _get_board_bounds(pcb)
        assert bounds is not None
        unplaced = _detect_unplaced(pcb, bounds)
        refs = {fp.reference for fp in unplaced}
        assert "R1" in refs
        assert "R2" not in refs


# ---------------------------------------------------------------------------
# Unit: board bounds
# ---------------------------------------------------------------------------


class TestBoardBounds:
    """Tests for _get_board_bounds."""

    def test_returns_correct_bounds(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        pcb = PCB.load(str(pcb_path))
        bounds = _get_board_bounds(pcb)
        assert bounds is not None
        min_x, min_y, max_x, max_y = bounds
        # The board is 50x50, so width and height should be ~50
        assert pytest.approx(max_x - min_x, abs=0.5) == 50.0
        assert pytest.approx(max_y - min_y, abs=0.5) == 50.0

    def test_nonzero_origin_bounds_are_board_relative(self, tmp_path: Path):
        """Board bounds must start near (0,0) when origin is non-zero.

        The board rect is (100,100)-(150,150), so board origin is (100,100).
        After the origin transform, bounds should be (0,0)-(50,50).
        """
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        pcb = PCB.load(str(pcb_path))
        bounds = _get_board_bounds(pcb)
        assert bounds is not None
        min_x, min_y, max_x, max_y = bounds
        assert min_x == pytest.approx(0.0, abs=0.5)
        assert min_y == pytest.approx(0.0, abs=0.5)
        assert max_x == pytest.approx(50.0, abs=0.5)
        assert max_y == pytest.approx(50.0, abs=0.5)

    def test_returns_none_without_outline(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_NO_OUTLINE)
        pcb = PCB.load(str(pcb_path))
        assert _get_board_bounds(pcb) is None


# ---------------------------------------------------------------------------
# Unit: grid computation
# ---------------------------------------------------------------------------


class TestGrid:
    """Tests for _build_grid and _cell_occupied."""

    def test_grid_cells_fit_inside_area(self):
        bounds = (0.0, 0.0, 20.0, 20.0)
        cells = _build_grid(bounds, margin=2.0, spacing=1.0, cell_w=3.0, cell_h=3.0)
        assert len(cells) > 0
        for cell in cells:
            assert cell.x >= 2.0
            assert cell.y >= 2.0
            assert cell.x + cell.w <= 18.0
            assert cell.y + cell.h <= 18.0

    def test_grid_returns_empty_for_tiny_area(self):
        bounds = (0.0, 0.0, 2.0, 2.0)
        cells = _build_grid(bounds, margin=2.0, spacing=1.0, cell_w=3.0, cell_h=3.0)
        assert len(cells) == 0

    def test_grid_count_matches_expected(self):
        # 100x100 area, 5mm margin each side -> 90x90 usable
        # Cell 10x10 with 2mm spacing -> step 12mm
        # Columns: floor(90 / 12) ~= 7 (since 7*12=84, 8*12=96 > 90 but check fit)
        # Actually: 5 + 10 = 15 first cell end, and area goes to 95
        # 7 cells fit: 5, 17, 29, 41, 53, 65, 77 (77+10=87 <= 95)
        bounds = (0.0, 0.0, 100.0, 100.0)
        cells = _build_grid(bounds, margin=5.0, spacing=2.0, cell_w=10.0, cell_h=10.0)
        # 90mm usable / 12mm step = 7 per row/col
        assert len(cells) == 7 * 7

    def test_cell_occupied_detects_overlap(self):
        cell = _GridCell(x=10.0, y=10.0, w=5.0, h=5.0)
        # A component at the same center
        placed = [(12.5, 12.5, 5.0, 5.0)]
        assert _cell_occupied(cell, placed)

    def test_cell_occupied_no_overlap(self):
        cell = _GridCell(x=10.0, y=10.0, w=5.0, h=5.0)
        # A component far away
        placed = [(50.0, 50.0, 5.0, 5.0)]
        assert not _cell_occupied(cell, placed)


# ---------------------------------------------------------------------------
# Unit: footprint sizing
# ---------------------------------------------------------------------------


class TestFootprintSizing:
    """Tests for _footprint_size."""

    def test_pad_fallback_size(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        pcb = PCB.load(str(pcb_path))
        r1 = [fp for fp in pcb.footprints if fp.reference == "R1"][0]
        w, h = _footprint_size(r1)
        # 0402 has two pads ~1mm apart, each ~0.54 wide, with 0.5mm margin
        assert w > 0
        assert h > 0


# ---------------------------------------------------------------------------
# Integration: place_unplaced public API
# ---------------------------------------------------------------------------


class TestPlaceUnplaced:
    """Integration tests for the place_unplaced() function."""

    def test_dry_run_reports_without_modifying(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        original = pcb_path.read_text()

        result = place_unplaced(pcb_path, dry_run=True)

        assert result.dry_run is True
        assert result.total_unplaced >= 1
        assert result.placed_count >= 1
        assert "R1" in result.placed_refs
        # File should be unmodified
        assert pcb_path.read_text() == original

    def test_placement_moves_component(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        result = place_unplaced(pcb_path)

        assert result.dry_run is False
        assert result.placed_count >= 1
        assert "R1" in result.placed_refs

        # Reload and verify R1 is now inside bounds
        pcb = PCB.load(str(pcb_path))
        bounds = _get_board_bounds(pcb)
        assert bounds is not None
        r1 = [fp for fp in pcb.footprints if fp.reference == "R1"][0]
        min_x, min_y, max_x, max_y = bounds
        assert min_x <= r1.position[0] <= max_x
        assert min_y <= r1.position[1] <= max_y

    def test_out_of_bounds_component_detected(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_OUT_OF_BOUNDS)
        result = place_unplaced(pcb_path, dry_run=True)

        assert result.total_unplaced >= 1
        assert "R1" in result.placed_refs

    def test_all_placed_returns_zero_count(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_ALL_PLACED)
        result = place_unplaced(pcb_path, dry_run=True)

        assert result.total_unplaced == 0
        assert result.placed_count == 0
        assert result.overflow_count == 0

    def test_no_outline_raises_value_error(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_NO_OUTLINE)
        with pytest.raises(ValueError, match="No Edge.Cuts"):
            place_unplaced(pcb_path)

    def test_overflow_reported_for_small_board(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, _make_overflow_board())
        result = place_unplaced(pcb_path, dry_run=True)

        assert result.total_unplaced == 20
        # Not all 20 can fit in a 10x10mm board
        assert result.overflow_count > 0
        assert len(result.overflow_refs) == result.overflow_count
        assert result.placed_count + result.overflow_count == result.total_unplaced

    def test_output_path_writes_to_separate_file(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        out_path = tmp_path / "output.kicad_pcb"

        result = place_unplaced(pcb_path, output_path=out_path)

        assert result.placed_count >= 1
        assert out_path.exists()
        # Original should be untouched
        pcb_original = PCB.load(str(pcb_path))
        r1_orig = [fp for fp in pcb_original.footprints if fp.reference == "R1"][0]
        # R1 in original is still at origin (approximately)
        abs_x = r1_orig.position[0] + pcb_original.board_origin[0]
        abs_y = r1_orig.position[1] + pcb_original.board_origin[1]
        assert abs(abs_x) < 1.0 and abs(abs_y) < 1.0

    def test_cluster_mode_groups_by_net(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_CLUSTERS)
        result = place_unplaced(pcb_path, cluster=True, dry_run=True)

        assert result.total_unplaced == 3
        assert result.placed_count == 3
        # All three share nets (VCC, GND, SIG) so they end up in one cluster
        # and are placed adjacently. We cannot verify adjacency in dry_run
        # but we can verify all were placed.
        assert set(result.placed_refs) == {"R1", "R2", "R3"}


# ---------------------------------------------------------------------------
# Unit: clustering
# ---------------------------------------------------------------------------


class TestClustering:
    """Tests for _cluster_footprints."""

    def test_shared_net_creates_single_cluster(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path, BOARD_WITH_CLUSTERS)
        pcb = PCB.load(str(pcb_path))
        bounds = _get_board_bounds(pcb)
        assert bounds is not None
        unplaced = _detect_unplaced(pcb, bounds)
        clusters = _cluster_footprints(unplaced, pcb)
        # R1 shares VCC with R2; R2 shares SIG with R3; R3 shares GND with R1
        # So all three should be in one cluster
        assert len(clusters) == 1
        refs = {fp.reference for fp in clusters[0]}
        assert refs == {"R1", "R2", "R3"}


# ---------------------------------------------------------------------------
# Unit: PlaceUnplacedResult serialization
# ---------------------------------------------------------------------------


class TestResultSerialization:
    """Tests for PlaceUnplacedResult.to_dict."""

    def test_to_dict_roundtrip(self):
        result = PlaceUnplacedResult(
            total_unplaced=5,
            placed_count=3,
            overflow_count=2,
            placed_refs=["R1", "R2", "R3"],
            overflow_refs=["R4", "R5"],
            board_bounds=(0.0, 0.0, 50.0, 50.0),
            dry_run=True,
        )
        d = result.to_dict()
        assert d["total_unplaced"] == 5
        assert d["placed_count"] == 3
        assert d["overflow_count"] == 2
        assert d["placed_refs"] == ["R1", "R2", "R3"]
        assert d["overflow_refs"] == ["R4", "R5"]
        assert d["board_bounds"] == [0.0, 0.0, 50.0, 50.0]
        assert d["dry_run"] is True


# ---------------------------------------------------------------------------
# CLI dispatch test
# ---------------------------------------------------------------------------


class TestCLIDispatch:
    """Test that the CLI main() dispatches place-unplaced correctly."""

    def test_cli_dry_run(self, tmp_path: Path):
        from kicad_tools.cli.placement_cmd import main

        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        rc = main(["place-unplaced", str(pcb_path), "--dry-run", "--quiet"])
        assert rc == 0

    def test_cli_json_output(self, tmp_path: Path, capsys):
        from kicad_tools.cli.placement_cmd import main

        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        rc = main(["place-unplaced", str(pcb_path), "--dry-run", "--format", "json"])
        assert rc == 0
        captured = capsys.readouterr()
        import json

        data = json.loads(captured.out)
        assert "total_unplaced" in data
        assert "placed_count" in data

    def test_cli_no_outline_returns_error(self, tmp_path: Path):
        from kicad_tools.cli.placement_cmd import main

        pcb_path = _write_pcb(tmp_path, BOARD_NO_OUTLINE)
        rc = main(["place-unplaced", str(pcb_path), "--quiet"])
        assert rc == 1

    def test_cli_file_not_found_returns_error(self):
        from kicad_tools.cli.placement_cmd import main

        rc = main(["place-unplaced", "/nonexistent/board.kicad_pcb", "--quiet"])
        assert rc == 1


# ---------------------------------------------------------------------------
# MCP tool registration test
# ---------------------------------------------------------------------------


class TestMCPRegistration:
    """Test that the MCP tool is registered and callable."""

    def test_tool_registered(self):
        from kicad_tools.mcp.tools.registry import get_tool

        tool = get_tool("placement_place_unplaced")
        assert tool is not None
        assert tool.category == "placement"
        assert "pcb_path" in tool.parameters["properties"]

    def test_mcp_handler_dry_run(self, tmp_path: Path):
        from kicad_tools.mcp.tools.registry import get_tool

        pcb_path = _write_pcb(tmp_path, BOARD_WITH_UNPLACED)
        tool = get_tool("placement_place_unplaced")
        assert tool is not None
        result = tool.handler({"pcb_path": str(pcb_path), "dry_run": True})
        assert result["total_unplaced"] >= 1
        assert result["placed_count"] >= 1
        assert result["dry_run"] is True

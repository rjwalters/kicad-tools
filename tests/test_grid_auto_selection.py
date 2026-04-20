"""Tests for automatic grid resolution selection."""

import pytest

from kicad_tools.router.io import (
    GridAutoSelection,
    PadPosition,
    _compute_gcd_grid_candidates,
    _is_on_grid,
    auto_select_grid_resolution,
    extract_pad_positions,
    recommend_grid_for_board_size,
)
from kicad_tools.router.primitives import Pad


class TestIsOnGrid:
    """Tests for _is_on_grid helper function."""

    def test_on_grid_exact(self):
        """Test value exactly on grid."""
        assert _is_on_grid(0.5, 0.25)
        assert _is_on_grid(1.0, 0.25)
        assert _is_on_grid(2.54, 0.127)

    def test_on_grid_within_threshold(self):
        """Test value within default threshold (resolution/10)."""
        # 0.25mm grid, threshold is 0.025mm
        assert _is_on_grid(0.51, 0.25)  # 0.01 from 0.5
        assert _is_on_grid(0.49, 0.25)  # 0.01 from 0.5

    def test_off_grid(self):
        """Test value clearly off grid."""
        assert not _is_on_grid(0.33, 0.25)  # 0.08 from 0.25
        assert not _is_on_grid(0.15, 0.25)  # 0.10 from 0.0 or 0.25

    def test_custom_threshold(self):
        """Test with custom threshold."""
        # Strict threshold
        assert not _is_on_grid(0.51, 0.25, threshold=0.005)
        # Relaxed threshold
        assert _is_on_grid(0.35, 0.25, threshold=0.15)

    def test_zero_value(self):
        """Test zero is always on grid."""
        assert _is_on_grid(0.0, 0.25)
        assert _is_on_grid(0.0, 0.1)


class TestPadPosition:
    """Tests for PadPosition dataclass."""

    def test_creation(self):
        """Test creating a pad position."""
        pos = PadPosition(x=2.54, y=5.08)
        assert pos.x == 2.54
        assert pos.y == 5.08


class TestAutoSelectGridResolution:
    """Tests for auto_select_grid_resolution function."""

    def test_empty_pads(self):
        """Test with no pads."""
        result = auto_select_grid_resolution([], clearance=0.15)
        assert result.total_pads == 0
        assert result.off_grid_pads == 0

    def test_pads_on_standard_grid(self):
        """Test pads on 2.54mm (100mil) grid prefer coarser resolution."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=2.54),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        # 2.54mm is divisible by 0.127mm (5mil), so should prefer that or coarser
        assert result.off_grid_pads == 0 or result.resolution >= 0.127

    def test_pads_on_metric_grid(self):
        """Test pads on 1mm metric grid."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=1.0, y=0.0),
            PadPosition(x=2.0, y=1.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        # 1mm is divisible by 0.25, 0.1, 0.05
        assert result.off_grid_pads == 0

    def test_pads_on_fine_grid(self):
        """Test pads requiring fine grid (0.5mm pitch)."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.5, y=0.0),
            PadPosition(x=1.0, y=0.5),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.2)
        assert result.off_grid_pads == 0
        assert result.resolution <= 0.25

    def test_mixed_grid_pads(self):
        """Test pads on mixed grid (some off-grid)."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),  # 100mil
            PadPosition(x=0.33, y=0.0),  # Off-grid for most resolutions
        ]
        result = auto_select_grid_resolution(pads, clearance=0.2)
        # Should select resolution that minimizes off-grid count
        assert result.total_pads == 3
        assert result.candidates_tried  # Should have tried multiple candidates

    def test_drc_compliance(self):
        """Test that selected resolution respects DRC clearance."""
        pads = [PadPosition(x=0.0, y=0.0)]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        # Resolution must be <= clearance/2 for DRC compliance
        assert result.resolution <= 0.15 / 2

    def test_prefers_coarser_when_equal(self):
        """Test that coarser resolution is preferred when off-grid counts are equal."""
        # All pads on a grid that works for multiple resolutions
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=1.0, y=0.0),
            PadPosition(x=2.0, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.5)
        # Should prefer coarser resolution (0.5mm or 0.25mm over 0.1mm)
        assert result.resolution >= 0.25

    def test_with_pad_objects(self):
        """Test with full Pad objects instead of PadPosition."""
        pads = [
            Pad(x=0.0, y=0.0, width=1.0, height=1.0, net=1, net_name="NET1"),
            Pad(x=2.54, y=0.0, width=1.0, height=1.0, net=1, net_name="NET1"),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.2)
        assert result.total_pads == 2

    def test_with_pad_dict(self):
        """Test with dict of pads (as returned by router)."""
        pads = {
            ("U1", "1"): Pad(x=0.0, y=0.0, width=1.0, height=1.0, net=1, net_name="NET1"),
            ("U1", "2"): Pad(x=2.54, y=0.0, width=1.0, height=1.0, net=1, net_name="NET1"),
        }
        result = auto_select_grid_resolution(pads, clearance=0.2)
        assert result.total_pads == 2

    def test_custom_candidates(self):
        """Test with custom candidate resolutions."""
        pads = [PadPosition(x=0.0, y=0.0)]
        result = auto_select_grid_resolution(
            pads,
            clearance=0.2,
            candidates=[0.2, 0.15, 0.1],
        )
        # Should only try specified candidates
        resolutions_tried = [c[0] for c in result.candidates_tried]
        assert 0.5 not in resolutions_tried  # Default candidate not tried

    def test_tssop_pitch_alignment_with_default_candidates(self):
        """Test that default candidates include TSSOP-friendly 0.065mm."""
        # TSSOP pitch is 0.65mm, which divides evenly by 0.065mm
        # Use clearance=0.15 so that 0.065 <= 0.15/2 = 0.075 passes the filter
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),  # TSSOP pitch
            PadPosition(x=1.30, y=0.0),  # 2x TSSOP pitch
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        # Should include 0.065mm in candidates tried
        resolutions_tried = [c[0] for c in result.candidates_tried]
        assert 0.065 in resolutions_tried

    def test_selects_0065_for_tssop_pads(self):
        """Test that 0.065mm is selected for pure TSSOP placement."""
        # All pads on 0.65mm grid
        # Use clearance=0.15 so that 0.065 <= 0.075 passes the filter
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
            PadPosition(x=1.95, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        # 0.065mm should have zero off-grid pads (0.65 / 0.065 = 10 exact)
        # So should 0.05mm (0.65 / 0.05 = 13 exact)
        # Function prefers coarser when equal, so 0.065mm should be selected
        assert result.off_grid_pads == 0
        assert result.resolution in [0.065, 0.05]  # Either is valid


    def test_no_candidate_exceeds_half_clearance(self):
        """With clearance=0.15, no selected candidate should exceed 0.075."""
        pads = [PadPosition(x=0.0, y=0.0), PadPosition(x=1.0, y=0.0)]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        assert result.resolution <= 0.15 / 2
        # Also verify all *tried* candidates respect the threshold
        for res, _off in result.candidates_tried:
            assert res <= 0.15 / 2

    def test_tight_clearance_floor(self):
        """With very tight clearance (0.1mm), grid must not go below 0.05mm floor."""
        pads = [PadPosition(x=0.0, y=0.0)]
        result = auto_select_grid_resolution(pads, clearance=0.1)
        # clearance/2 = 0.05, only candidate that fits is 0.05
        assert result.resolution == 0.05

    def test_board05_clearance_selects_fine_grid(self):
        """Board 05 scenario: clearance=0.2mm should select grid <= 0.1mm."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=1.27, y=0.0),
            PadPosition(x=2.54, y=1.27),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.2)
        assert result.resolution <= 0.2 / 2  # Must be <= 0.1mm

    def test_imperial_tht_pads_zero_off_grid_with_loose_clearance(self):
        """Imperial THT pads (2.54mm, 5.08mm) should have zero off-grid with loose clearance.

        When clearance allows 0.127mm grid (clearance >= 0.254mm), the auto-selector
        should pick 0.127mm which divides evenly into 2.54mm and 5.08mm.
        """
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
            PadPosition(x=0.0, y=2.54),
            PadPosition(x=2.54, y=2.54),
            PadPosition(x=5.08, y=2.54),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        assert result.off_grid_pads == 0, (
            f"Imperial THT pads should have zero off-grid pads, got {result.off_grid_pads} "
            f"with grid {result.resolution}mm"
        )

    def test_imperial_tht_pads_with_tight_clearance(self):
        """Imperial THT pads with tight clearance (0.127mm) should use 0.0508mm grid.

        When clearance is 0.127mm (JLCPCB), max_grid is 0.0635mm.
        The 0.0508mm (2 mil) candidate divides evenly into 2.54mm (50x).
        """
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.127)
        # 0.0508mm divides evenly into 2.54mm and 5.08mm
        assert result.off_grid_pads == 0, (
            f"Imperial THT pads should have zero off-grid pads even with tight clearance, "
            f"got {result.off_grid_pads} with grid {result.resolution}mm"
        )

    def test_mixed_imperial_metric_pads(self):
        """Mixed imperial THT + metric SMD pads should minimise off-grid count."""
        pads = [
            # Imperial THT pads at 2.54mm pitch
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
            # Metric SMD pads at 0.65mm pitch (TSSOP)
            PadPosition(x=10.0, y=0.0),
            PadPosition(x=10.65, y=0.0),
            PadPosition(x=11.30, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        # No single grid aligns with both; auto-selector picks the one
        # that minimises off-grid count
        assert result.total_pads == 6
        assert result.off_grid_pads < result.total_pads, (
            "Should have fewer off-grid pads than total"
        )

    def test_0508mm_candidate_included(self):
        """Default candidates should include 0.0508mm (2 mil) for imperial compatibility."""
        pads = [PadPosition(x=0.0, y=0.0)]
        result = auto_select_grid_resolution(pads, clearance=0.127)
        resolutions_tried = [c[0] for c in result.candidates_tried]
        assert 0.0508 in resolutions_tried, (
            f"0.0508mm should be in candidates, got {resolutions_tried}"
        )


class TestGridAutoSelectionSummary:
    """Tests for GridAutoSelection.summary() method."""

    def test_summary_format(self):
        """Test summary output format."""
        result = GridAutoSelection(
            resolution=0.127,
            off_grid_pads=2,
            total_pads=10,
            off_grid_percentage=20.0,
            candidates_tried=[(0.25, 5), (0.127, 2)],
        )
        summary = result.summary()
        assert "0.127mm" in summary
        assert "10" in summary  # Total pads
        assert "2" in summary  # Off-grid pads
        assert "20.0%" in summary
        assert "selected" in summary.lower()


class TestExtractPadPositions:
    """Tests for extract_pad_positions function."""

    @pytest.fixture
    def minimal_pcb(self, tmp_path):
        """Create a minimal PCB file for testing."""
        pcb_content = """(kicad_pcb (version 20230121) (generator "test")
  (general
    (thickness 1.6)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "VCC")

  (footprint "Package_SO:SOIC-8" (layer "F.Cu")
    (at 100 100)
    (property "Reference" "U1")
    (pad "1" smd rect (at -2.54 -0.635) (size 0.6 0.9) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at -2.54 0.635) (size 0.6 0.9) (layers "F.Cu") (net 1 "VCC"))
    (pad "3" smd rect (at 2.54 0.635) (size 0.6 0.9) (layers "F.Cu") (net 1 "VCC"))
  )
)"""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(pcb_content)
        return pcb_file

    def test_extract_from_file(self, minimal_pcb):
        """Test extracting pad positions from a PCB file."""
        positions = extract_pad_positions(minimal_pcb)
        assert len(positions) == 3
        # Verify positions are transformed correctly
        for pos in positions:
            assert pos.x > 90  # Near 100mm
            assert pos.y > 90  # Near 100mm

    def test_extract_from_text(self, minimal_pcb):
        """Test extracting pad positions from PCB text content."""
        pcb_text = minimal_pcb.read_text()
        positions = extract_pad_positions(pcb_text)
        assert len(positions) == 3

    def test_footprint_rotation(self, tmp_path):
        """Test that footprint rotation is applied correctly."""
        # Footprint rotated 90 degrees
        pcb_content = """(kicad_pcb (version 20230121) (generator "test")
  (layers (0 "F.Cu" signal))
  (net 0 "")

  (footprint "Test" (layer "F.Cu")
    (at 100 100 90)
    (property "Reference" "U1")
    (pad "1" smd rect (at 1 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "rotated.kicad_pcb"
        pcb_file.write_text(pcb_content)

        positions = extract_pad_positions(pcb_file)
        assert len(positions) == 1
        # With 90 degree rotation, pad at (1, 0) relative becomes (0, 1) relative
        # Absolute: (100 + 0, 100 + 1) = (100, 101)
        pos = positions[0]
        assert abs(pos.x - 100.0) < 0.01
        assert abs(pos.y - 101.0) < 0.01


class TestRecommendGridForBoardSize:
    """Tests for recommend_grid_for_board_size function."""

    def test_small_board_gets_fine_grid(self):
        """Test that small boards get 0.05mm grid for best pitch alignment."""
        # 65x56mm board is small
        grid = recommend_grid_for_board_size(65, 56, clearance=0.15)
        assert grid == 0.05

    def test_medium_board_gets_balanced_grid(self):
        """Test that medium boards get 0.1mm grid."""
        # 120x80mm board is medium
        grid = recommend_grid_for_board_size(120, 80, clearance=0.15)
        assert grid == 0.1

    def test_large_board_gets_coarse_grid(self):
        """Test that large boards get 0.25mm grid for memory efficiency."""
        # 200x120mm board is large
        grid = recommend_grid_for_board_size(200, 120, clearance=0.3)
        assert grid == 0.25

    def test_grid_clamped_to_clearance(self):
        """Test that grid resolution never exceeds clearance."""
        # Large board with small clearance
        grid = recommend_grid_for_board_size(200, 120, clearance=0.127)
        assert grid == 0.127  # Clamped to clearance, not 0.25

    def test_tssop_pitch_alignment(self):
        """Test that small board grid aligns with TSSOP 0.65mm pitch."""
        grid = recommend_grid_for_board_size(50, 40, clearance=0.15)
        # 0.05mm grid divides evenly into 0.65mm: 0.65 / 0.05 = 13
        # Use round to avoid floating point precision issues
        assert round(0.65 / grid) == 0.65 / grid or abs(0.65 / grid - round(0.65 / grid)) < 0.01

    def test_qfp_pitch_alignment(self):
        """Test that recommended grids align with QFP 0.5mm pitch."""
        # Small board
        grid_small = recommend_grid_for_board_size(50, 40, clearance=0.15)
        # 0.5 / 0.05 = 10 exact
        divisions = 0.5 / grid_small
        assert abs(divisions - round(divisions)) < 0.01

        # Medium board
        grid_medium = recommend_grid_for_board_size(120, 80, clearance=0.15)
        # 0.5 / 0.1 = 5 exact
        divisions = 0.5 / grid_medium
        assert abs(divisions - round(divisions)) < 0.01

    def test_custom_thresholds(self):
        """Test with custom board size thresholds."""
        # Use smaller thresholds
        grid = recommend_grid_for_board_size(
            80,
            60,
            clearance=0.15,
            small_board_threshold=(50, 40),
            medium_board_threshold=(75, 55),
        )
        # 80x60 is now "large" with custom thresholds
        assert grid == 0.15  # 0.25 clamped to clearance

    def test_boundary_conditions(self):
        """Test boards at exact threshold boundaries."""
        # Exactly at small threshold
        grid = recommend_grid_for_board_size(100, 75, clearance=0.15)
        assert grid == 0.05  # Still small

        # Just over small threshold
        grid = recommend_grid_for_board_size(101, 75, clearance=0.15)
        assert grid == 0.1  # Now medium

        # Exactly at medium threshold
        grid = recommend_grid_for_board_size(150, 100, clearance=0.3)
        assert grid == 0.1  # Still medium

        # Just over medium threshold
        grid = recommend_grid_for_board_size(151, 100, clearance=0.3)
        assert grid == 0.25  # Now large


class TestComputeGcdGridCandidates:
    """Tests for _compute_gcd_grid_candidates helper."""

    def test_empty_pads(self):
        """No candidates from fewer than 2 pads."""
        assert _compute_gcd_grid_candidates([]) == []
        assert _compute_gcd_grid_candidates([PadPosition(x=0.0, y=0.0)]) == []

    def test_single_spacing(self):
        """Two pads 0.65mm apart produce GCD = 0.65mm."""
        pads = [PadPosition(x=0.0, y=0.0), PadPosition(x=0.65, y=0.0)]
        result = _compute_gcd_grid_candidates(pads)
        # GCD should be 0.65mm; multiples 1.3mm and 3.25mm also returned
        assert 0.65 in result

    def test_tssop_pitch(self):
        """Multiple 0.65mm-spaced pads produce GCD = 0.65mm."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
            PadPosition(x=1.95, y=0.0),
        ]
        result = _compute_gcd_grid_candidates(pads)
        assert 0.65 in result

    def test_mixed_065_254_gcd(self):
        """Mixed 0.65mm and 2.54mm spacings produce GCD = 0.01mm."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
            PadPosition(x=10.0, y=0.0),
            PadPosition(x=12.54, y=0.0),
        ]
        result = _compute_gcd_grid_candidates(pads)
        # GCD of 650 and 2540 (in microns) is 10 microns = 0.01mm
        assert 0.01 in result

    def test_pure_imperial(self):
        """Pure 2.54mm pads produce GCD = 2.54mm."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
        ]
        result = _compute_gcd_grid_candidates(pads)
        assert 2.54 in result

    def test_min_grid_filter(self):
        """Candidates below min_grid are filtered out."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.003, y=0.0),  # 3um spacing
        ]
        result = _compute_gcd_grid_candidates(pads, min_grid=0.005)
        # 0.003mm rounds to 0.005 after the 5um rounding, but delta may be 0
        # Either way, nothing below 0.005mm should appear
        for c in result:
            assert c >= 0.005


class TestGcdBasedGridSelection:
    """Tests for GCD-based candidate integration in auto_select_grid_resolution."""

    def test_ssop_065_pitch_zero_off_grid(self):
        """Board with 0.65mm-pitch SSOP pads achieves 0 off-grid.

        This is the core scenario from issue #1753: SSOP/TSSOP packages
        with 0.65mm pitch should be fully on-grid after GCD candidate
        injection.
        """
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
            PadPosition(x=1.95, y=0.0),
            PadPosition(x=2.60, y=0.0),
            PadPosition(x=3.25, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        assert result.off_grid_pads == 0, (
            f"SSOP 0.65mm pads should have zero off-grid, got {result.off_grid_pads} "
            f"with grid {result.resolution}mm"
        )

    def test_mixed_065_and_254_minimises_off_grid(self):
        """Mixed 0.65mm SSOP + 2.54mm THT pads with GCD candidates.

        The GCD of spacings should produce a candidate that aligns with
        both pitches, or at least minimise off-grid count better than
        the fixed candidates alone.
        """
        pads = [
            # SSOP at 0.65mm pitch
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
            PadPosition(x=1.95, y=0.0),
            # THT at 2.54mm pitch
            PadPosition(x=10.0, y=5.0),
            PadPosition(x=12.54, y=5.0),
            PadPosition(x=15.08, y=5.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        # With GCD candidates, off-grid count should be lower than without
        assert result.total_pads == 7
        # The GCD-derived candidate (e.g. 0.01mm) should achieve all on-grid
        # if it passes the memory filter
        assert result.off_grid_pads <= 3, (
            f"Mixed board should have at most 3 off-grid pads, got {result.off_grid_pads}"
        )

    def test_standard_pitch_regression(self):
        """Pure standard-pitch boards behave unchanged.

        For boards with only 0.5mm/1.27mm/2.54mm components, the GCD
        candidates should not change the selected grid (the fixed
        candidates already handle these pitches).
        """
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=1.27, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        # 0.127mm is the classic imperial grid; should still be selected
        assert result.off_grid_pads == 0
        assert result.resolution == 0.127

    def test_gcd_candidate_respects_memory_budget(self):
        """Fine GCD candidates are filtered out when they exceed memory budget."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
        ]
        # Board where 0.065mm grid would exceed budget but 0.65mm fits
        # 100x100mm board, max_cells=500k: 100*100/0.065^2 = 2.37M (too much)
        # but 100*100/0.65^2 = 23.7k (fits)
        result = auto_select_grid_resolution(
            pads,
            clearance=1.5,  # Very loose clearance so all candidates pass DRC
            board_width=100.0,
            board_height=100.0,
            max_cells=500_000,
        )
        # The fine GCD candidates (0.065mm etc.) should be filtered out
        # by the memory budget; only coarser ones should survive.
        cells = (100.0 * 100.0) / (result.resolution ** 2)
        assert cells <= 500_000, (
            f"Selected grid {result.resolution}mm produces {cells:.0f} cells, "
            f"exceeds budget of 500000"
        )

    def test_gcd_candidates_in_summary(self):
        """GCD-derived candidates appear in the summary output."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=1.5)
        summary = result.summary()
        # The GCD of 0.65mm spacings is 0.65mm; it or its multiples should
        # appear as candidates in the summary
        assert "0.65mm" in summary or "1.3mm" in summary or "3.25mm" in summary, (
            f"GCD-derived candidates should appear in summary:\n{summary}"
        )

    def test_single_component_single_pad(self):
        """Board with only 1 pad produces no GCD candidates (no crash)."""
        pads = [PadPosition(x=5.0, y=5.0)]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        # Should work fine, just use fixed candidates
        assert result.total_pads == 1

    def test_custom_candidates_skips_gcd(self):
        """When custom candidates are provided, GCD injection is skipped."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
        ]
        result = auto_select_grid_resolution(
            pads, clearance=1.5, candidates=[0.5, 0.25]
        )
        resolutions_tried = [c[0] for c in result.candidates_tried]
        # Only the user-specified candidates should be tried
        assert 0.65 not in resolutions_tried

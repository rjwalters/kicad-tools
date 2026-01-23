"""Tests for automatic grid resolution selection."""

import pytest

from kicad_tools.router.io import (
    GridAutoSelection,
    PadPosition,
    _is_on_grid,
    auto_select_grid_resolution,
    extract_pad_positions,
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
        # Resolution must be <= clearance for DRC compliance
        assert result.resolution <= 0.15

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

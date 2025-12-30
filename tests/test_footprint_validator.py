"""Tests for footprint validation and repair functionality."""

import pytest

from kicad_tools.footprints.fixer import (
    FootprintFix,
    FootprintFixer,
    PadAdjustment,
)
from kicad_tools.footprints.validator import (
    FootprintIssue,
    FootprintValidator,
    IssueSeverity,
    IssueType,
    _calculate_pad_gap,
)
from kicad_tools.schema.pcb import Footprint, Pad


class TestPadGapCalculation:
    """Tests for pad gap calculation."""

    def test_pads_not_overlapping_horizontal(self):
        """Test gap between horizontally separated pads."""
        # Two pads 2mm apart center-to-center, each 0.5mm wide
        pad1 = Pad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.0, 0.0),
            size=(0.5, 0.5),
            layers=["F.Cu"],
        )
        pad2 = Pad(
            number="2",
            type="smd",
            shape="rect",
            position=(2.0, 0.0),
            size=(0.5, 0.5),
            layers=["F.Cu"],
        )

        gap = _calculate_pad_gap(pad1, pad2)
        # Gap should be 2.0 - 0.25 - 0.25 = 1.5mm
        assert gap == pytest.approx(1.5, abs=0.01)

    def test_pads_not_overlapping_vertical(self):
        """Test gap between vertically separated pads."""
        pad1 = Pad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.0, 0.0),
            size=(0.5, 0.5),
            layers=["F.Cu"],
        )
        pad2 = Pad(
            number="2",
            type="smd",
            shape="rect",
            position=(0.0, 2.0),
            size=(0.5, 0.5),
            layers=["F.Cu"],
        )

        gap = _calculate_pad_gap(pad1, pad2)
        assert gap == pytest.approx(1.5, abs=0.01)

    def test_pads_overlapping(self):
        """Test detection of overlapping pads."""
        # Two pads 0.5mm apart center-to-center, each 0.6mm wide
        # They overlap by 0.1mm
        pad1 = Pad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.0, 0.0),
            size=(0.6, 0.6),
            layers=["F.Cu"],
        )
        pad2 = Pad(
            number="2",
            type="smd",
            shape="rect",
            position=(0.5, 0.0),
            size=(0.6, 0.6),
            layers=["F.Cu"],
        )

        gap = _calculate_pad_gap(pad1, pad2)
        # Gap should be negative: 0.5 - 0.3 - 0.3 = -0.1mm
        assert gap == pytest.approx(-0.1, abs=0.01)

    def test_pads_touching(self):
        """Test detection of touching pads."""
        # Two pads exactly touching
        pad1 = Pad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.0, 0.0),
            size=(0.5, 0.5),
            layers=["F.Cu"],
        )
        pad2 = Pad(
            number="2",
            type="smd",
            shape="rect",
            position=(0.5, 0.0),
            size=(0.5, 0.5),
            layers=["F.Cu"],
        )

        gap = _calculate_pad_gap(pad1, pad2)
        # Gap should be 0: 0.5 - 0.25 - 0.25 = 0
        assert gap == pytest.approx(0.0, abs=0.01)

    def test_pads_diagonal(self):
        """Test gap between diagonally separated pads."""
        # Two pads separated diagonally
        pad1 = Pad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.0, 0.0),
            size=(0.5, 0.5),
            layers=["F.Cu"],
        )
        pad2 = Pad(
            number="2",
            type="smd",
            shape="rect",
            position=(2.0, 2.0),
            size=(0.5, 0.5),
            layers=["F.Cu"],
        )

        gap = _calculate_pad_gap(pad1, pad2)
        # Both axes have 1.5mm gap, diagonal gap is sqrt(1.5^2 + 1.5^2) ≈ 2.12mm
        import math
        expected = math.sqrt(1.5**2 + 1.5**2)
        assert gap == pytest.approx(expected, abs=0.01)


class TestFootprintValidator:
    """Tests for FootprintValidator class."""

    def _create_simple_footprint(
        self,
        reference: str = "R1",
        name: str = "R_0402",
        pad_spacing: float = 1.0,
        pad_width: float = 0.5,
    ) -> Footprint:
        """Create a simple 2-pad footprint for testing."""
        half_spacing = pad_spacing / 2
        return Footprint(
            name=name,
            layer="F.Cu",
            position=(0.0, 0.0),
            rotation=0.0,
            reference=reference,
            value="10k",
            pads=[
                Pad(
                    number="1",
                    type="smd",
                    shape="rect",
                    position=(-half_spacing, 0.0),
                    size=(pad_width, pad_width),
                    layers=["F.Cu"],
                ),
                Pad(
                    number="2",
                    type="smd",
                    shape="rect",
                    position=(half_spacing, 0.0),
                    size=(pad_width, pad_width),
                    layers=["F.Cu"],
                ),
            ],
        )

    def test_no_issues_with_well_spaced_pads(self):
        """Test that well-spaced pads produce no issues."""
        # Pads 1.0mm apart center-to-center, 0.4mm wide
        # Gap = 1.0 - 0.2 - 0.2 = 0.6mm > 0.15mm
        fp = self._create_simple_footprint(pad_spacing=1.0, pad_width=0.4)

        validator = FootprintValidator(min_pad_gap=0.15)
        issues = validator.validate_footprint(fp)

        assert len(issues) == 0

    def test_detect_overlapping_pads(self):
        """Test detection of overlapping pads."""
        # Pads 0.5mm apart center-to-center, 0.6mm wide
        # Gap = 0.5 - 0.3 - 0.3 = -0.1mm (overlap)
        fp = self._create_simple_footprint(pad_spacing=0.5, pad_width=0.6)

        validator = FootprintValidator(min_pad_gap=0.15)
        issues = validator.validate_footprint(fp)

        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.PAD_OVERLAP
        assert issues[0].severity == IssueSeverity.ERROR

    def test_detect_touching_pads(self):
        """Test detection of touching pads."""
        # Pads exactly touching
        fp = self._create_simple_footprint(pad_spacing=0.5, pad_width=0.5)

        validator = FootprintValidator(min_pad_gap=0.15)
        issues = validator.validate_footprint(fp)

        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.PAD_TOUCHING
        assert issues[0].severity == IssueSeverity.WARNING

    def test_detect_insufficient_spacing(self):
        """Test detection of insufficient pad spacing."""
        # Pads with 0.1mm gap (less than 0.15mm minimum)
        # Gap = 0.6 - 0.25 - 0.25 = 0.1mm
        fp = self._create_simple_footprint(pad_spacing=0.6, pad_width=0.5)

        validator = FootprintValidator(min_pad_gap=0.15)
        issues = validator.validate_footprint(fp)

        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.PAD_SPACING
        assert issues[0].severity == IssueSeverity.WARNING

    def test_issue_contains_details(self):
        """Test that issues contain proper details."""
        fp = self._create_simple_footprint(pad_spacing=0.5, pad_width=0.6)

        validator = FootprintValidator(min_pad_gap=0.15)
        issues = validator.validate_footprint(fp)

        assert len(issues) == 1
        issue = issues[0]

        assert issue.footprint_ref == "R1"
        assert issue.footprint_name == "R_0402"
        assert "pad1" in issue.details
        assert "pad2" in issue.details
        assert "gap_mm" in issue.details

    def test_summarize_issues(self):
        """Test issue summarization."""
        fp1 = self._create_simple_footprint(
            reference="R1", name="R_0402", pad_spacing=0.5, pad_width=0.6
        )
        fp2 = self._create_simple_footprint(
            reference="R2", name="R_0402", pad_spacing=0.5, pad_width=0.6
        )

        validator = FootprintValidator(min_pad_gap=0.15)
        issues1 = validator.validate_footprint(fp1)
        issues2 = validator.validate_footprint(fp2)
        all_issues = issues1 + issues2

        summary = validator.summarize(all_issues)

        assert summary["total"] == 2
        assert summary["footprints_with_issues"] == 2
        assert "R_0402" in summary["by_footprint_name"]

    def test_group_by_footprint_name(self):
        """Test grouping issues by footprint name."""
        fp1 = self._create_simple_footprint(
            reference="R1", name="R_0402", pad_spacing=0.5, pad_width=0.6
        )
        fp2 = self._create_simple_footprint(
            reference="C1", name="C_0402", pad_spacing=0.5, pad_width=0.6
        )

        validator = FootprintValidator(min_pad_gap=0.15)
        issues1 = validator.validate_footprint(fp1)
        issues2 = validator.validate_footprint(fp2)
        all_issues = issues1 + issues2

        grouped = validator.group_by_footprint_name(all_issues)

        assert "R_0402" in grouped
        assert "C_0402" in grouped
        assert len(grouped["R_0402"]) == 1
        assert len(grouped["C_0402"]) == 1


class TestFootprintFixer:
    """Tests for FootprintFixer class."""

    def _create_simple_footprint(
        self,
        reference: str = "R1",
        name: str = "R_0402",
        pad_spacing: float = 1.0,
        pad_width: float = 0.5,
    ) -> Footprint:
        """Create a simple 2-pad footprint for testing."""
        half_spacing = pad_spacing / 2
        return Footprint(
            name=name,
            layer="F.Cu",
            position=(0.0, 0.0),
            rotation=0.0,
            reference=reference,
            value="10k",
            pads=[
                Pad(
                    number="1",
                    type="smd",
                    shape="rect",
                    position=(-half_spacing, 0.0),
                    size=(pad_width, pad_width),
                    layers=["F.Cu"],
                ),
                Pad(
                    number="2",
                    type="smd",
                    shape="rect",
                    position=(half_spacing, 0.0),
                    size=(pad_width, pad_width),
                    layers=["F.Cu"],
                ),
            ],
        )

    def test_no_fix_needed(self):
        """Test that well-spaced pads don't get fixed."""
        # Pads with 0.6mm gap (> 0.2mm target)
        fp = self._create_simple_footprint(pad_spacing=1.0, pad_width=0.4)

        fixer = FootprintFixer(min_pad_gap=0.2)
        fix = fixer.fix_footprint_pads(fp)

        assert fix is None

    def test_fix_insufficient_spacing_dry_run(self):
        """Test dry-run of pad spacing fix."""
        # Pads with 0.1mm gap (< 0.2mm target)
        fp = self._create_simple_footprint(pad_spacing=0.6, pad_width=0.5)

        # Record original positions
        orig_pos1 = fp.pads[0].position
        orig_pos2 = fp.pads[1].position

        fixer = FootprintFixer(min_pad_gap=0.2)
        fix = fixer.fix_footprint_pads(fp, dry_run=True)

        assert fix is not None
        assert len(fix.adjustments) == 2
        # Positions should NOT have changed in dry run
        assert fp.pads[0].position == orig_pos1
        assert fp.pads[1].position == orig_pos2

    def test_fix_insufficient_spacing_applies_changes(self):
        """Test that fix applies changes correctly."""
        # Pads with 0.1mm gap (< 0.2mm target)
        fp = self._create_simple_footprint(pad_spacing=0.6, pad_width=0.5)

        fixer = FootprintFixer(min_pad_gap=0.2)
        fix = fixer.fix_footprint_pads(fp, dry_run=False)

        assert fix is not None

        # Check that pads were moved outward
        # New spacing should give 0.2mm gap: new_spacing = 0.2 + 0.25 + 0.25 = 0.7mm
        # So pads should be at -0.35 and +0.35
        assert fp.pads[0].position[0] == pytest.approx(-0.35, abs=0.01)
        assert fp.pads[1].position[0] == pytest.approx(0.35, abs=0.01)
        # Y positions should be unchanged
        assert fp.pads[0].position[1] == 0.0
        assert fp.pads[1].position[1] == 0.0

    def test_fix_preserves_center(self):
        """Test that fix preserves footprint center."""
        fp = self._create_simple_footprint(pad_spacing=0.6, pad_width=0.5)

        # Calculate original center
        orig_center_x = (fp.pads[0].position[0] + fp.pads[1].position[0]) / 2

        fixer = FootprintFixer(min_pad_gap=0.2)
        fixer.fix_footprint_pads(fp, dry_run=False)

        # Calculate new center
        new_center_x = (fp.pads[0].position[0] + fp.pads[1].position[0]) / 2

        # Center should be preserved
        assert new_center_x == pytest.approx(orig_center_x, abs=0.001)

    def test_fix_vertical_pads(self):
        """Test fixing vertically arranged pads."""
        # Create vertically arranged pads
        fp = Footprint(
            name="C_0805",
            layer="F.Cu",
            position=(0.0, 0.0),
            rotation=0.0,
            reference="C1",
            value="100nF",
            pads=[
                Pad(
                    number="1",
                    type="smd",
                    shape="rect",
                    position=(0.0, -0.3),
                    size=(0.5, 0.5),
                    layers=["F.Cu"],
                ),
                Pad(
                    number="2",
                    type="smd",
                    shape="rect",
                    position=(0.0, 0.3),
                    size=(0.5, 0.5),
                    layers=["F.Cu"],
                ),
            ],
        )

        fixer = FootprintFixer(min_pad_gap=0.2)
        fix = fixer.fix_footprint_pads(fp, dry_run=False)

        assert fix is not None
        # X positions should be unchanged
        assert fp.pads[0].position[0] == 0.0
        assert fp.pads[1].position[0] == 0.0
        # Y positions should have been adjusted
        assert fp.pads[0].position[1] != -0.3 or fp.pads[1].position[1] != 0.3

    def test_summarize_fixes(self):
        """Test fix summarization."""
        fp1 = self._create_simple_footprint(
            reference="R1", name="R_0402", pad_spacing=0.6, pad_width=0.5
        )
        fp2 = self._create_simple_footprint(
            reference="R2", name="R_0402", pad_spacing=0.6, pad_width=0.5
        )

        fixer = FootprintFixer(min_pad_gap=0.2)
        fix1 = fixer.fix_footprint_pads(fp1)
        fix2 = fixer.fix_footprint_pads(fp2)
        fixes = [f for f in [fix1, fix2] if f is not None]

        summary = fixer.summarize(fixes)

        assert summary["total_footprints_fixed"] == 2
        assert summary["total_pads_adjusted"] == 4  # 2 pads per footprint
        assert "R_0402" in summary["by_footprint_name"]


class TestFootprintIssue:
    """Tests for FootprintIssue dataclass."""

    def test_str_representation(self):
        """Test string representation of FootprintIssue."""
        issue = FootprintIssue(
            footprint_ref="R1",
            footprint_name="R_0402",
            issue_type=IssueType.PAD_OVERLAP,
            severity=IssueSeverity.ERROR,
            message="Pad 1 and Pad 2 are overlapping",
            details={"gap_mm": -0.1},
        )

        s = str(issue)
        assert "R1" in s
        assert "R_0402" in s
        assert "ERROR" in s
        assert "overlapping" in s


class TestFootprintFix:
    """Tests for FootprintFix dataclass."""

    def test_str_representation(self):
        """Test string representation of FootprintFix."""
        fix = FootprintFix(
            footprint_ref="R1",
            footprint_name="R_0402",
            adjustments=[
                PadAdjustment(
                    footprint_ref="R1",
                    pad_number="1",
                    old_position=(-0.3, 0.0),
                    new_position=(-0.35, 0.0),
                    reason="Increase pad spacing",
                ),
            ],
            old_pad_spacing=0.6,
            new_pad_spacing=0.7,
        )

        s = str(fix)
        assert "R1" in s
        assert "R_0402" in s
        assert "0.600" in s or "0.6" in s
        assert "0.700" in s or "0.7" in s

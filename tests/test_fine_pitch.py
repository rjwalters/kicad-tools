"""Tests for fine-pitch component detection and routing compatibility analysis."""


from kicad_tools.router.fine_pitch import (
    ComponentGridAnalysis,
    FinePitchReport,
    FinePitchSeverity,
    OffGridPad,
    analyze_fine_pitch_components,
)
from kicad_tools.router.primitives import Pad


def make_pad(x, y, net, ref, pin, width=0.5, height=0.5, net_name=""):
    """Helper to create Pad objects with default values."""
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=net_name or f"NET{net}",
        ref=ref,
        pin=pin,
    )


class TestFinePitchAnalysis:
    """Tests for the analyze_fine_pitch_components function."""

    def test_all_pads_on_grid(self):
        """Test analysis when all pads align with the grid."""
        # Create pads that are perfectly on a 0.5mm grid
        pads = {
            ("U1", "1"): make_pad(x=0.0, y=0.0, net=1, ref="U1", pin="1"),
            ("U1", "2"): make_pad(x=0.5, y=0.0, net=1, ref="U1", pin="2"),
            ("U1", "3"): make_pad(x=1.0, y=0.0, net=2, ref="U1", pin="3"),
            ("U1", "4"): make_pad(x=1.5, y=0.0, net=2, ref="U1", pin="4"),
        }

        report = analyze_fine_pitch_components(
            pads=pads,
            grid_resolution=0.5,
            trace_width=0.2,
            clearance=0.2,
        )

        assert not report.has_warnings
        assert report.total_off_grid == 0
        assert report.max_severity == FinePitchSeverity.OK

    def test_off_grid_pads_detected(self):
        """Test that off-grid pads are correctly detected."""
        # Create pads with 0.65mm pitch (TSSOP-like) on a 0.5mm grid
        pads = {
            ("U1", "1"): make_pad(x=0.0, y=0.0, width=0.3, height=0.8, net=1, ref="U1", pin="1"),
            ("U1", "2"): make_pad(x=0.65, y=0.0, width=0.3, height=0.8, net=2, ref="U1", pin="2"),
            ("U1", "3"): make_pad(x=1.30, y=0.0, width=0.3, height=0.8, net=3, ref="U1", pin="3"),
            ("U1", "4"): make_pad(x=1.95, y=0.0, width=0.3, height=0.8, net=4, ref="U1", pin="4"),
        }

        report = analyze_fine_pitch_components(
            pads=pads,
            grid_resolution=0.5,
            trace_width=0.2,
            clearance=0.2,
        )

        # With 0.5mm grid, 0.65mm pitch pads will be off-grid
        assert report.has_warnings
        assert report.total_off_grid > 0

    def test_fine_pitch_severity_calculation(self):
        """Test that severity is correctly calculated based on off-grid percentage."""
        # Create a TSSOP-like component with many off-grid pads
        pads = {}
        for i in range(20):
            x = i * 0.65  # 0.65mm pitch (TSSOP)
            pads[("U1", str(i + 1))] = make_pad(
                x=x, y=0.0, width=0.3, height=0.8, net=i + 1, ref="U1", pin=str(i + 1)
            )

        report = analyze_fine_pitch_components(
            pads=pads,
            grid_resolution=0.25,  # Grid that doesn't align with 0.65mm pitch
            trace_width=0.2,
            clearance=0.2,
        )

        # Should have warnings with medium or high severity
        assert report.has_warnings
        assert report.max_severity != FinePitchSeverity.OK

    def test_recommendations_generated(self):
        """Test that recommendations are generated for problematic components."""
        pads = {}
        for i in range(10):
            x = i * 0.65
            pads[("U1", str(i + 1))] = make_pad(
                x=x, y=0.0, width=0.3, height=0.8, net=i + 1, ref="U1", pin=str(i + 1)
            )

        report = analyze_fine_pitch_components(
            pads=pads,
            grid_resolution=0.25,
            trace_width=0.2,
            clearance=0.2,
        )

        # Check that recommendations exist for components with issues
        for comp in report.components_with_issues:
            assert len(comp.recommendations) > 0

    def test_multiple_components(self):
        """Test analysis with multiple components."""
        pads = {}

        # Component 1: On-grid pads
        for i in range(4):
            pads[("R1", str(i + 1))] = make_pad(
                x=i * 1.0, y=0.0, width=0.5, height=0.5, net=i + 1, ref="R1", pin=str(i + 1)
            )

        # Component 2: Off-grid pads (fine-pitch)
        for i in range(10):
            pads[("U1", str(i + 1))] = make_pad(
                x=10 + i * 0.65,
                y=0.0,
                width=0.3,
                height=0.8,
                net=10 + i,
                ref="U1",
                pin=str(i + 1),
            )

        report = analyze_fine_pitch_components(
            pads=pads,
            grid_resolution=0.25,
            trace_width=0.2,
            clearance=0.2,
        )

        # Should have 2 components analyzed
        assert len(report.components) == 2

        # U1 should have issues, R1 should be fine (or have fewer issues)
        u1_analysis = next((c for c in report.components if c.ref == "U1"), None)
        r1_analysis = next((c for c in report.components if c.ref == "R1"), None)

        assert u1_analysis is not None
        assert r1_analysis is not None
        assert u1_analysis.off_grid_count > r1_analysis.off_grid_count

    def test_affected_nets_tracked(self):
        """Test that affected nets are tracked for off-grid pads."""
        pads = {
            ("U1", "1"): make_pad(
                x=0.0, y=0.0, width=0.3, height=0.8, net=1, net_name="NET1", ref="U1", pin="1"
            ),
            ("U1", "2"): make_pad(
                x=0.65, y=0.0, width=0.3, height=0.8, net=2, net_name="NET2", ref="U1", pin="2"
            ),
            ("U1", "3"): make_pad(
                x=1.30, y=0.0, width=0.3, height=0.8, net=3, net_name="NET3", ref="U1", pin="3"
            ),
        }

        report = analyze_fine_pitch_components(
            pads=pads,
            grid_resolution=0.5,
            trace_width=0.2,
            clearance=0.2,
        )

        # Check that affected nets are tracked
        if report.has_warnings:
            for comp in report.components_with_issues:
                if comp.off_grid_count > 0:
                    assert len(comp.affected_nets) > 0

    def test_format_warnings_output(self):
        """Test that format_warnings produces readable output."""
        pads = {}
        for i in range(10):
            pads[("U1", str(i + 1))] = make_pad(
                x=i * 0.65, y=0.0, width=0.3, height=0.8, net=i + 1, ref="U1", pin=str(i + 1)
            )

        report = analyze_fine_pitch_components(
            pads=pads,
            grid_resolution=0.25,
            trace_width=0.2,
            clearance=0.2,
        )

        output = report.format_warnings()

        # Should produce non-empty output if there are warnings
        if report.has_warnings:
            assert len(output) > 0
            assert "U1" in output
            assert "off-grid" in output.lower()

    def test_to_dict_serialization(self):
        """Test that report can be serialized to dict."""
        pads = {
            ("U1", "1"): make_pad(x=0.0, y=0.0, width=0.3, height=0.8, net=1, ref="U1", pin="1"),
            ("U1", "2"): make_pad(x=0.65, y=0.0, width=0.3, height=0.8, net=2, ref="U1", pin="2"),
        }

        report = analyze_fine_pitch_components(
            pads=pads,
            grid_resolution=0.5,
            trace_width=0.2,
            clearance=0.2,
        )

        result = report.to_dict()

        assert "grid_resolution" in result
        assert "has_warnings" in result
        assert "max_severity" in result
        assert "components" in result
        assert result["grid_resolution"] == 0.5

    def test_empty_pads(self):
        """Test analysis with empty pads dict."""
        report = analyze_fine_pitch_components(
            pads={},
            grid_resolution=0.25,
            trace_width=0.2,
            clearance=0.2,
        )

        assert not report.has_warnings
        assert report.total_pads == 0
        assert len(report.components) == 0

    def test_single_pad_component(self):
        """Test analysis with single-pad components (should be skipped)."""
        pads = {
            ("R1", "1"): make_pad(x=0.0, y=0.0, width=0.5, height=0.5, net=1, ref="R1", pin="1"),
            ("R2", "1"): make_pad(x=2.0, y=0.0, width=0.5, height=0.5, net=2, ref="R2", pin="1"),
        }

        report = analyze_fine_pitch_components(
            pads=pads,
            grid_resolution=0.25,
            trace_width=0.2,
            clearance=0.2,
        )

        # Single-pad components are skipped (need 2+ pads to calculate pitch)
        assert report.total_pads == 2
        assert len(report.components) == 0


class TestComponentGridAnalysis:
    """Tests for the ComponentGridAnalysis dataclass."""

    def test_has_issues_property(self):
        """Test has_issues property."""
        analysis_ok = ComponentGridAnalysis(
            ref="U1",
            package_type="SOIC-8",
            pin_count=8,
            pin_pitch=1.27,
            severity=FinePitchSeverity.OK,
        )
        assert not analysis_ok.has_issues

        analysis_warn = ComponentGridAnalysis(
            ref="U1",
            package_type="TSSOP-20",
            pin_count=20,
            pin_pitch=0.65,
            severity=FinePitchSeverity.MEDIUM,
        )
        assert analysis_warn.has_issues

    def test_format_summary(self):
        """Test format_summary method."""
        analysis = ComponentGridAnalysis(
            ref="U1",
            package_type="TSSOP-20",
            pin_count=20,
            pin_pitch=0.65,
            off_grid_count=12,
            off_grid_percentage=60.0,
            severity=FinePitchSeverity.HIGH,
            recommendations=["Use 0.025mm grid"],
        )

        summary = analysis.format_summary()
        assert "U1" in summary
        assert "TSSOP-20" in summary
        assert "0.65" in summary


class TestFinePitchReport:
    """Tests for the FinePitchReport dataclass."""

    def test_max_severity_empty(self):
        """Test max_severity with empty components."""
        report = FinePitchReport()
        assert report.max_severity == FinePitchSeverity.OK

    def test_max_severity_mixed(self):
        """Test max_severity with mixed severities."""
        report = FinePitchReport(
            components=[
                ComponentGridAnalysis(
                    ref="R1",
                    package_type="0603",
                    pin_count=2,
                    pin_pitch=1.0,
                    severity=FinePitchSeverity.OK,
                ),
                ComponentGridAnalysis(
                    ref="U1",
                    package_type="TSSOP-20",
                    pin_count=20,
                    pin_pitch=0.65,
                    severity=FinePitchSeverity.HIGH,
                ),
                ComponentGridAnalysis(
                    ref="U2",
                    package_type="SOIC-8",
                    pin_count=8,
                    pin_pitch=1.27,
                    severity=FinePitchSeverity.LOW,
                ),
            ]
        )
        assert report.max_severity == FinePitchSeverity.HIGH

    def test_components_with_issues_filter(self):
        """Test components_with_issues property filters correctly."""
        report = FinePitchReport(
            components=[
                ComponentGridAnalysis(
                    ref="R1",
                    package_type="0603",
                    pin_count=2,
                    pin_pitch=1.0,
                    severity=FinePitchSeverity.OK,
                ),
                ComponentGridAnalysis(
                    ref="U1",
                    package_type="TSSOP-20",
                    pin_count=20,
                    pin_pitch=0.65,
                    severity=FinePitchSeverity.MEDIUM,
                ),
            ]
        )

        issues = report.components_with_issues
        assert len(issues) == 1
        assert issues[0].ref == "U1"


class TestOffGridPad:
    """Tests for the OffGridPad dataclass."""

    def test_position_property(self):
        """Test position property."""
        pad = OffGridPad(
            ref="U1",
            pin="1",
            x=1.23,
            y=4.56,
            offset_x=0.05,
            offset_y=0.02,
            max_offset=0.05,
        )
        assert pad.position == (1.23, 4.56)

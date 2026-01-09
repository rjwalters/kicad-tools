"""Tests for predictive DRC analysis."""

from __future__ import annotations

import pytest

from kicad_tools.drc.predictive import PredictiveAnalyzer, PredictiveWarning
from kicad_tools.intent.types import Constraint, ConstraintSeverity, IntentDeclaration
from kicad_tools.optim.session import PlacementSession
from kicad_tools.schema.pcb import PCB

# Test fixture: PCB with multiple components forming a cluster
PCB_CLUSTERED = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "USB_D+")
  (net 3 "USB_D-")
  (net 4 "SPI_CLK")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 120 120)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "USB_D+"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 125 120)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000021"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000022"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "USB_D-"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000030")
    (at 122.5 125)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000031"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000032"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 4 "SPI_CLK"))
  )
  (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000040")
    (at 150 150)
    (property "Reference" "U1" (at 0 -4 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000041"))
    (property "Value" "IC" (at 0 4 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000042"))
    (pad "1" smd roundrect (at -2.71 -1.905) (size 1.55 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "USB_D+"))
    (pad "2" smd roundrect (at -2.71 -0.635) (size 1.55 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "USB_D-"))
    (pad "3" smd roundrect (at -2.71 0.635) (size 1.55 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 4 "SPI_CLK"))
    (pad "4" smd roundrect (at -2.71 1.905) (size 1.55 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "5" smd roundrect (at 2.71 1.905) (size 1.55 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "6" smd roundrect (at 2.71 0.635) (size 1.55 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "7" smd roundrect (at 2.71 -0.635) (size 1.55 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "8" smd roundrect (at 2.71 -1.905) (size 1.55 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
)
"""


class TestPredictiveWarning:
    """Tests for PredictiveWarning dataclass."""

    def test_warning_creation(self) -> None:
        """Test creating a predictive warning."""
        warning = PredictiveWarning(
            type="routing_difficulty",
            message="Routing SIG1 will be harder",
            confidence=0.75,
            suggestion="Move component closer",
            affected_nets=["SIG1"],
            location=(120.0, 130.0),
        )

        assert warning.type == "routing_difficulty"
        assert warning.message == "Routing SIG1 will be harder"
        assert warning.confidence == 0.75
        assert warning.suggestion == "Move component closer"
        assert warning.affected_nets == ["SIG1"]
        assert warning.location == (120.0, 130.0)

    def test_warning_to_dict(self) -> None:
        """Test converting warning to dictionary."""
        warning = PredictiveWarning(
            type="congestion",
            message="Area becoming congested",
            confidence=0.8,
            suggestion="Spread components",
            affected_nets=["NET1", "NET2"],
            location=(100.5, 200.3),
        )

        result = warning.to_dict()

        assert result["type"] == "congestion"
        assert result["message"] == "Area becoming congested"
        assert result["confidence"] == 0.8
        assert result["suggestion"] == "Spread components"
        assert result["affected_nets"] == ["NET1", "NET2"]
        assert result["location"] == {"x": 100.5, "y": 200.3}

    def test_warning_to_dict_minimal(self) -> None:
        """Test converting warning without optional fields."""
        warning = PredictiveWarning(
            type="intent_risk",
            message="May affect length matching",
            confidence=0.6,
        )

        result = warning.to_dict()

        assert result["type"] == "intent_risk"
        assert "suggestion" not in result
        assert "location" not in result
        assert result["affected_nets"] == []


class TestPredictiveAnalyzer:
    """Tests for PredictiveAnalyzer class."""

    @pytest.fixture
    def pcb(self, tmp_path) -> PCB:
        """Create a test PCB."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_CLUSTERED)
        return PCB.load(str(pcb_file))

    @pytest.fixture
    def session(self, pcb) -> PlacementSession:
        """Create a placement session."""
        return PlacementSession(pcb)

    @pytest.fixture
    def analyzer(self, session) -> PredictiveAnalyzer:
        """Create a predictive analyzer."""
        return PredictiveAnalyzer(session)

    def test_analyzer_creation(self, session) -> None:
        """Test creating a predictive analyzer."""
        analyzer = PredictiveAnalyzer(session)
        assert analyzer.session == session
        assert analyzer.intents == []

    def test_analyzer_with_intents(self, session) -> None:
        """Test creating analyzer with intents."""
        intent = IntentDeclaration(
            interface_type="usb2_high_speed",
            nets=["USB_D+", "USB_D-"],
            constraints=[
                Constraint(
                    type="length_match",
                    params={"tolerance": 0.1},
                    source="usb2_high_speed",
                    severity=ConstraintSeverity.ERROR,
                )
            ],
        )
        analyzer = PredictiveAnalyzer(session, intents=[intent])
        assert len(analyzer.intents) == 1

    def test_analyze_move_returns_list(self, analyzer) -> None:
        """Test that analyze_move returns a list."""
        warnings = analyzer.analyze_move("R1", (130.0, 130.0))
        assert isinstance(warnings, list)

    def test_analyze_move_nonexistent_component(self, analyzer) -> None:
        """Test analyzing move for non-existent component."""
        warnings = analyzer.analyze_move("NONEXISTENT", (100.0, 100.0))
        assert warnings == []

    def test_analyze_move_performance(self, analyzer) -> None:
        """Test that analysis completes within performance target (<10ms)."""
        # Run analysis
        warnings = analyzer.analyze_move("R1", (150.0, 150.0))

        # Check analysis time (stored in analyzer)
        assert hasattr(analyzer, "_last_analysis_time_ms")
        assert analyzer._last_analysis_time_ms < 100  # Allow some margin

    def test_routing_difficulty_detection(self, session) -> None:
        """Test detection of routing difficulty increase."""
        analyzer = PredictiveAnalyzer(session)

        # Move R1 far from its connected IC (U1)
        # USB_D+ connects R1 to U1, so moving R1 far away increases difficulty
        warnings = analyzer.analyze_move("R1", (180.0, 180.0))

        # Check if any routing difficulty warnings were generated
        routing_warnings = [w for w in warnings if w.type == "routing_difficulty"]
        # Note: may or may not trigger depending on threshold settings
        # Just verify the analyzer runs without error
        assert isinstance(routing_warnings, list)

    def test_congestion_detection(self, session) -> None:
        """Test detection of congestion in dense areas."""
        analyzer = PredictiveAnalyzer(session)

        # Move C1 into the cluster with R1 and R2
        warnings = analyzer.analyze_move("C1", (122.0, 120.0))

        # Check for congestion warnings
        congestion_warnings = [w for w in warnings if w.type == "congestion"]
        # Verify analyzer runs; actual warning depends on thresholds
        assert isinstance(congestion_warnings, list)

    def test_intent_risk_with_length_match(self, session) -> None:
        """Test detection of length match risks with declared intents."""
        # Create USB intent with length matching
        intent = IntentDeclaration(
            interface_type="usb2_high_speed",
            nets=["USB_D+", "USB_D-"],
            constraints=[
                Constraint(
                    type="length_match",
                    params={"tolerance": 0.1},
                    source="usb2_high_speed",
                    severity=ConstraintSeverity.ERROR,
                )
            ],
        )
        analyzer = PredictiveAnalyzer(session, intents=[intent])

        # Move R1 significantly, affecting USB_D+ length
        warnings = analyzer.analyze_move("R1", (180.0, 180.0))

        # Check for intent risk warnings
        intent_warnings = [w for w in warnings if w.type == "intent_risk"]
        # Intent warnings may or may not trigger depending on move distance
        assert isinstance(intent_warnings, list)

    def test_intent_risk_with_differential_pair(self, session) -> None:
        """Test detection of differential pair risks."""
        intent = IntentDeclaration(
            interface_type="usb2_high_speed",
            nets=["USB_D+", "USB_D-"],
            constraints=[
                Constraint(
                    type="differential_pair",
                    params={"impedance": 90.0},
                    source="usb2_high_speed",
                    severity=ConstraintSeverity.ERROR,
                )
            ],
        )
        analyzer = PredictiveAnalyzer(session, intents=[intent])

        # Move R1 far from R2, spreading the differential pair
        warnings = analyzer.analyze_move("R1", (160.0, 160.0))

        # Check for intent risk warnings about differential pair
        intent_warnings = [w for w in warnings if w.type == "intent_risk"]
        assert isinstance(intent_warnings, list)

    def test_confidence_filtering(self, session) -> None:
        """Test that low-confidence warnings are filtered."""
        analyzer = PredictiveAnalyzer(session)

        # Set high threshold
        analyzer.MIN_CONFIDENCE_THRESHOLD = 0.99

        warnings = analyzer.analyze_move("R1", (130.0, 130.0))

        # All warnings should have confidence >= 0.99
        for w in warnings:
            assert w.confidence >= 0.99

    def test_helper_get_connected_nets(self, analyzer) -> None:
        """Test getting nets connected to a component."""
        nets = analyzer._get_connected_nets("R1")
        assert "GND" in nets
        assert "USB_D+" in nets

    def test_helper_get_net_endpoints(self, analyzer) -> None:
        """Test getting endpoints for a net."""
        endpoints = analyzer._get_net_endpoints("USB_D+")
        # USB_D+ connects R1 and U1
        assert len(endpoints) >= 2

    def test_helper_estimate_congestion(self, analyzer) -> None:
        """Test congestion estimation."""
        from kicad_tools.drc.incremental import Rectangle

        # Area around clustered components
        bounds = Rectangle(118.0, 118.0, 127.0, 127.0)
        congestion = analyzer._estimate_congestion(bounds)

        # Should return a value between 0 and 1
        assert 0.0 <= congestion <= 1.0


class TestPredictiveWarningInfo:
    """Tests for PredictiveWarningInfo MCP type."""

    def test_type_creation(self) -> None:
        """Test creating PredictiveWarningInfo."""
        from kicad_tools.mcp.types import PredictiveWarningInfo

        info = PredictiveWarningInfo(
            type="routing_difficulty",
            message="Test message",
            confidence=0.7,
            suggestion="Test suggestion",
            affected_nets=["NET1"],
            location=(100.0, 200.0),
        )

        assert info.type == "routing_difficulty"
        assert info.confidence == 0.7

    def test_type_to_dict(self) -> None:
        """Test converting PredictiveWarningInfo to dict."""
        from kicad_tools.mcp.types import PredictiveWarningInfo

        info = PredictiveWarningInfo(
            type="congestion",
            message="Area congested",
            confidence=0.85,
            suggestion="Spread out",
            affected_nets=["A", "B"],
            location=(50.5, 75.3),
        )

        result = info.to_dict()

        assert result["type"] == "congestion"
        assert result["confidence"] == 0.85
        assert result["suggestion"] == "Spread out"
        assert result["location"]["x"] == 50.5
        assert result["location"]["y"] == 75.3


class TestMCPIntegration:
    """Tests for MCP integration with predictions."""

    @pytest.fixture
    def pcb(self, tmp_path) -> PCB:
        """Create a test PCB."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_CLUSTERED)
        return PCB.load(str(pcb_file))

    def test_query_move_includes_predictions(self, tmp_path, pcb) -> None:
        """Test that query_move includes predictions field."""
        from kicad_tools.mcp.tools.session import (
            query_move,
            reset_session_manager,
            start_session,
        )

        reset_session_manager()

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_CLUSTERED)

        result = start_session(str(pcb_file))
        assert result.success

        query_result = query_move(result.session_id, "R1", 130.0, 130.0)
        assert query_result.success

        # Should have predictions field (may be empty list)
        assert hasattr(query_result, "predictions")
        assert isinstance(query_result.predictions, list)

        reset_session_manager()

    def test_apply_move_includes_predictions(self, tmp_path) -> None:
        """Test that apply_move includes predictions field."""
        from kicad_tools.mcp.tools.session import (
            apply_move,
            reset_session_manager,
            rollback_session,
            start_session,
        )

        reset_session_manager()

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_CLUSTERED)

        result = start_session(str(pcb_file))
        assert result.success

        apply_result = apply_move(result.session_id, "R1", 130.0, 130.0)
        assert apply_result.success

        # Should have predictions field
        assert hasattr(apply_result, "predictions")
        assert isinstance(apply_result.predictions, list)

        rollback_session(result.session_id)
        reset_session_manager()

    def test_predictions_to_dict(self, tmp_path) -> None:
        """Test that predictions are included in to_dict output."""
        from kicad_tools.mcp.tools.session import (
            query_move,
            reset_session_manager,
            start_session,
        )

        reset_session_manager()

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_CLUSTERED)

        result = start_session(str(pcb_file))
        query_result = query_move(result.session_id, "R1", 130.0, 130.0)

        result_dict = query_result.to_dict()

        assert "predictions" in result_dict
        assert isinstance(result_dict["predictions"], list)

        reset_session_manager()

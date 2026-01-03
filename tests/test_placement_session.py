"""Tests for the placement session and query API."""

import json
from pathlib import Path

import pytest

from kicad_tools.optim import (
    MoveResult,
    PlacementSession,
    RoutingImpact,
    SessionPlacementSuggestion,
    Violation,
    find_best_position,
    process_json_request,
    query_alignment,
    query_position,
    query_swap,
)
from kicad_tools.schema.pcb import PCB

# Test PCB with multiple components for placement testing
PLACEMENT_TEST_PCB = """(kicad_pcb
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
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 120 120)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 140 120)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
  (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000003")
    (at 150 150)
    (property "Reference" "U1" (at 0 -3.5 0) (layer "F.SilkS"))
    (property "Value" "IC" (at 0 3.5 0) (layer "F.Fab"))
    (pad "1" smd rect (at -2.7 -1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at -2.7 -0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
    (pad "3" smd rect (at -2.7 0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "SIG"))
    (pad "4" smd rect (at -2.7 1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "5" smd rect (at 2.7 1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "6" smd rect (at 2.7 0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "SIG"))
    (pad "7" smd rect (at 2.7 -0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
    (pad "8" smd rect (at 2.7 -1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
  )
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000004")
    (at 180 180)
    (property "Reference" "J1" (at 0 -2.5 0) (layer "F.SilkS"))
    (property "Value" "Conn" (at 0 2.5 0) (layer "F.Fab"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 1 "VCC"))
    (pad "2" thru_hole circle (at 0 2.54) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 2 "GND"))
  )
)
"""


@pytest.fixture
def placement_test_pcb(tmp_path: Path) -> Path:
    """Create a PCB file for placement testing."""
    pcb_file = tmp_path / "placement_test.kicad_pcb"
    pcb_file.write_text(PLACEMENT_TEST_PCB)
    return pcb_file


@pytest.fixture
def pcb(placement_test_pcb: Path) -> PCB:
    """Load the test PCB."""
    return PCB.load(str(placement_test_pcb))


@pytest.fixture
def session(pcb: PCB) -> PlacementSession:
    """Create a placement session."""
    return PlacementSession(pcb)


class TestPlacementSession:
    """Tests for PlacementSession class."""

    def test_session_creation(self, session: PlacementSession):
        """Test session is created with correct initial state."""
        status = session.get_status()
        assert status["components"] > 0
        assert status["pending_moves"] == 0
        assert status["history_depth"] == 0
        assert status["initial_score"] > 0

    def test_list_components(self, session: PlacementSession):
        """Test listing all components."""
        components = session.list_components()
        assert len(components) >= 4
        refs = {c["ref"] for c in components}
        assert "C1" in refs
        assert "C2" in refs
        assert "U1" in refs
        assert "J1" in refs

    def test_get_component_position(self, session: PlacementSession):
        """Test getting component position."""
        pos = session.get_component_position("C1")
        assert pos is not None
        assert pos["ref"] == "C1"
        assert "x" in pos
        assert "y" in pos
        assert "rotation" in pos
        assert "fixed" in pos

    def test_get_nonexistent_component(self, session: PlacementSession):
        """Test getting position of nonexistent component."""
        pos = session.get_component_position("NOTEXIST")
        assert pos is None

    def test_query_move_success(self, session: PlacementSession):
        """Test querying a valid move."""
        result = session.query_move("C1", 130.0, 130.0)
        assert isinstance(result, MoveResult)
        assert result.success is True
        assert isinstance(result.score_delta, float)
        assert isinstance(result.routing_impact, RoutingImpact)

    def test_query_move_nonexistent_component(self, session: PlacementSession):
        """Test querying move for nonexistent component."""
        result = session.query_move("NOTEXIST", 130.0, 130.0)
        assert result.success is False
        assert "not found" in result.error_message.lower()

    def test_query_move_with_rotation(self, session: PlacementSession):
        """Test querying move with rotation."""
        result = session.query_move("C1", 130.0, 130.0, 90.0)
        assert result.success is True

    def test_apply_move(self, session: PlacementSession):
        """Test applying a move."""
        result = session.apply_move("C1", 125.0, 125.0)
        assert result.success is True
        assert len(session.pending_moves) == 1

        # Verify position changed
        pos = session.get_component_position("C1")
        assert pos is not None
        assert abs(pos["x"] - 125.0) < 0.01
        assert abs(pos["y"] - 125.0) < 0.01

    def test_undo_move(self, session: PlacementSession):
        """Test undoing a move."""
        original_pos = session.get_component_position("C1")
        assert original_pos is not None

        session.apply_move("C1", 130.0, 130.0)
        assert len(session.pending_moves) == 1

        success = session.undo()
        assert success is True
        assert len(session.pending_moves) == 0

        # Verify position restored
        restored_pos = session.get_component_position("C1")
        assert restored_pos is not None
        assert abs(restored_pos["x"] - original_pos["x"]) < 0.01
        assert abs(restored_pos["y"] - original_pos["y"]) < 0.01

    def test_undo_empty_history(self, session: PlacementSession):
        """Test undo with no history."""
        success = session.undo()
        assert success is False

    def test_rollback(self, session: PlacementSession):
        """Test rolling back all changes."""
        original_pos = session.get_component_position("C1")
        assert original_pos is not None

        # Make multiple moves
        session.apply_move("C1", 130.0, 130.0)
        session.apply_move("C1", 135.0, 135.0)
        assert len(session.pending_moves) == 2

        # Rollback
        session.rollback()
        assert len(session.pending_moves) == 0
        assert len(session.history) == 0

        # Verify original position restored
        restored_pos = session.get_component_position("C1")
        assert restored_pos is not None
        assert abs(restored_pos["x"] - original_pos["x"]) < 0.01

    def test_commit(self, session: PlacementSession, pcb: PCB):
        """Test committing changes."""
        session.apply_move("C1", 125.0, 125.0)
        committed_pcb = session.commit()

        assert committed_pcb is pcb
        assert len(session.pending_moves) == 0
        assert len(session.history) == 0

    def test_get_suggestions(self, session: PlacementSession):
        """Test getting placement suggestions."""
        suggestions = session.get_suggestions("C1")
        assert isinstance(suggestions, list)
        # May or may not find improvements depending on initial state
        for s in suggestions:
            assert isinstance(s, SessionPlacementSuggestion)
            assert s.score >= 0  # Only improvements are returned

    def test_status_updates(self, session: PlacementSession):
        """Test that status updates after moves."""
        # Verify initial state
        status = session.get_status()
        assert status["pending_moves"] == 0

        session.apply_move("C1", 130.0, 130.0)
        new_status = session.get_status()

        assert new_status["pending_moves"] == 1
        assert new_status["history_depth"] == 1
        # Score should have changed
        assert new_status["score_change"] != 0.0


class TestQueryFunctions:
    """Tests for query functions."""

    def test_query_position(self, session: PlacementSession):
        """Test query_position function."""
        result = query_position(session, "C1", 130.0, 130.0)
        assert result.success is True

    def test_query_swap(self, session: PlacementSession):
        """Test query_swap function."""
        result = query_swap(session, "C1", "C2")
        assert result.success is True
        # Both components should be in affected list
        assert "C1" in result.affected_components or "C2" in result.affected_components

    def test_query_swap_nonexistent(self, session: PlacementSession):
        """Test query_swap with nonexistent component."""
        result = query_swap(session, "C1", "NOTEXIST")
        assert result.success is False

    def test_query_alignment_x(self, session: PlacementSession):
        """Test query_alignment on X axis."""
        result = query_alignment(session, ["C1", "C2"], axis="x", align_to="center")
        assert result.success is True

    def test_query_alignment_y(self, session: PlacementSession):
        """Test query_alignment on Y axis."""
        result = query_alignment(session, ["C1", "C2"], axis="y", align_to="center")
        assert result.success is True

    def test_query_alignment_too_few(self, session: PlacementSession):
        """Test query_alignment with too few components."""
        result = query_alignment(session, ["C1"], axis="x")
        assert result.success is False
        assert "at least 2" in result.error_message.lower()

    def test_find_best_position(self, session: PlacementSession):
        """Test find_best_position function."""
        from kicad_tools.optim.query import Rectangle

        region = Rectangle(x_min=110, y_min=110, x_max=140, y_max=140)
        suggestions = find_best_position(session, "C1", region, num_suggestions=3)
        assert isinstance(suggestions, list)


class TestMoveResult:
    """Tests for MoveResult dataclass."""

    def test_to_dict(self):
        """Test MoveResult.to_dict()."""
        result = MoveResult(
            success=True,
            score_delta=0.5,
            new_violations=[Violation(type="overlap", description="Test violation")],
            affected_components=["C1", "C2"],
            routing_impact=RoutingImpact(
                affected_nets=["VCC", "GND"],
                estimated_length_change_mm=1.5,
            ),
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["score_delta"] == 0.5
        assert len(d["new_violations"]) == 1
        assert d["affected_components"] == ["C1", "C2"]
        assert d["routing_impact"]["affected_nets"] == ["VCC", "GND"]

    def test_to_json(self):
        """Test MoveResult.to_json()."""
        result = MoveResult(success=True, score_delta=0.25)
        json_str = result.to_json()
        parsed = json.loads(json_str)
        assert parsed["success"] is True
        assert parsed["score_delta"] == 0.25


class TestJSONAPI:
    """Tests for JSON API."""

    def test_query_move_json(self, session: PlacementSession):
        """Test query_move via JSON API."""
        request = {
            "action": "query_move",
            "reference": "C1",
            "x": 130.0,
            "y": 130.0,
        }
        response_str = process_json_request(session, request)
        response = json.loads(response_str)
        assert response["success"] is True
        assert "result" in response

    def test_apply_move_json(self, session: PlacementSession):
        """Test apply_move via JSON API."""
        request = json.dumps(
            {
                "action": "apply_move",
                "reference": "C1",
                "x": 125.0,
                "y": 125.0,
            }
        )
        response_str = process_json_request(session, request)
        response = json.loads(response_str)
        assert response["success"] is True

    def test_get_status_json(self, session: PlacementSession):
        """Test get_status via JSON API."""
        request = {"action": "get_status"}
        response_str = process_json_request(session, request)
        response = json.loads(response_str)
        assert response["success"] is True
        assert "result" in response
        assert "components" in response["result"]

    def test_list_components_json(self, session: PlacementSession):
        """Test list_components via JSON API."""
        request = {"action": "list_components"}
        response_str = process_json_request(session, request)
        response = json.loads(response_str)
        assert response["success"] is True
        assert "components" in response["result"]

    def test_undo_json(self, session: PlacementSession):
        """Test undo via JSON API."""
        # First apply a move
        session.apply_move("C1", 130.0, 130.0)

        request = {"action": "undo"}
        response_str = process_json_request(session, request)
        response = json.loads(response_str)
        assert response["success"] is True

    def test_commit_json(self, session: PlacementSession):
        """Test commit via JSON API."""
        request = {"action": "commit"}
        response_str = process_json_request(session, request)
        response = json.loads(response_str)
        assert response["success"] is True

    def test_rollback_json(self, session: PlacementSession):
        """Test rollback via JSON API."""
        session.apply_move("C1", 130.0, 130.0)

        request = {"action": "rollback"}
        response_str = process_json_request(session, request)
        response = json.loads(response_str)
        assert response["success"] is True

    def test_invalid_action_json(self, session: PlacementSession):
        """Test invalid action via JSON API."""
        request = {"action": "invalid_action"}
        response_str = process_json_request(session, request)
        response = json.loads(response_str)
        assert response["success"] is False
        assert "unknown" in response["error"].lower()

    def test_invalid_json(self, session: PlacementSession):
        """Test invalid JSON input."""
        response_str = process_json_request(session, "not valid json {")
        response = json.loads(response_str)
        assert response["success"] is False
        assert "invalid" in response["error"].lower()

    def test_get_suggestions_json(self, session: PlacementSession):
        """Test get_suggestions via JSON API."""
        request = {
            "action": "get_suggestions",
            "reference": "C1",
            "num_suggestions": 3,
        }
        response_str = process_json_request(session, request)
        response = json.loads(response_str)
        assert response["success"] is True
        assert "suggestions" in response["result"]

    def test_query_swap_json(self, session: PlacementSession):
        """Test query_swap via JSON API."""
        request = {
            "action": "query_swap",
            "references": ["C1", "C2"],
        }
        response_str = process_json_request(session, request)
        response = json.loads(response_str)
        assert response["success"] is True


class TestViolation:
    """Tests for Violation dataclass."""

    def test_violation_to_dict(self):
        """Test Violation.to_dict()."""
        v = Violation(
            type="overlap",
            description="C1 overlaps with C2",
            severity="error",
            component="C1",
            location=(100.0, 100.0),
        )
        d = v.to_dict()
        assert d["type"] == "overlap"
        assert d["description"] == "C1 overlaps with C2"
        assert d["severity"] == "error"
        assert d["component"] == "C1"
        assert d["location"] == [100.0, 100.0]


class TestRoutingImpact:
    """Tests for RoutingImpact dataclass."""

    def test_routing_impact_to_dict(self):
        """Test RoutingImpact.to_dict()."""
        ri = RoutingImpact(
            affected_nets=["VCC", "GND"],
            estimated_length_change_mm=2.5,
            crossing_changes=1,
        )
        d = ri.to_dict()
        assert d["affected_nets"] == ["VCC", "GND"]
        assert d["estimated_length_change_mm"] == 2.5
        assert d["crossing_changes"] == 1


class TestSessionPlacementSuggestion:
    """Tests for SessionPlacementSuggestion dataclass."""

    def test_suggestion_to_dict(self):
        """Test SessionPlacementSuggestion.to_dict()."""
        s = SessionPlacementSuggestion(
            x=125.0,
            y=130.0,
            rotation=90.0,
            score=0.15,
            rationale="Reduces wire length",
        )
        d = s.to_dict()
        assert d["x"] == 125.0
        assert d["y"] == 130.0
        assert d["rotation"] == 90.0
        assert d["score"] == 0.15
        assert d["rationale"] == "Reduces wire length"


class TestFixedComponents:
    """Tests for fixed component handling."""

    def test_session_with_fixed_refs(self, pcb: PCB):
        """Test session with fixed components."""
        session = PlacementSession(pcb, fixed_refs=["J1"])
        pos = session.get_component_position("J1")
        assert pos is not None
        assert pos["fixed"] is True

    def test_cannot_move_fixed_component(self, pcb: PCB):
        """Test that fixed components cannot be moved."""
        session = PlacementSession(pcb, fixed_refs=["J1"])
        result = session.query_move("J1", 150.0, 150.0)
        assert result.success is False
        assert "fixed" in result.error_message.lower()

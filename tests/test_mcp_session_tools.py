"""Tests for MCP session management tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.mcp.server import create_server
from kicad_tools.mcp.session_manager import (
    SessionManager,
    SessionNotFoundError,
    get_session_manager,
)
from kicad_tools.mcp.tools.session import query_move, start_session
from kicad_tools.mcp.types import (
    ComponentPosition,
    MoveQueryResult,
    SessionRoutingImpact,
    SessionStartResult,
    SessionViolation,
)


class TestComponentPosition:
    """Tests for ComponentPosition dataclass."""

    def test_creation(self):
        pos = ComponentPosition(
            ref="C1",
            x=45.0,
            y=32.5,
            rotation=90.0,
            fixed=False,
            width=1.0,
            height=0.5,
        )
        assert pos.ref == "C1"
        assert pos.x == 45.0
        assert pos.y == 32.5
        assert pos.rotation == 90.0
        assert pos.fixed is False
        assert pos.width == 1.0
        assert pos.height == 0.5

    def test_to_dict(self):
        pos = ComponentPosition(
            ref="U3",
            x=100.123,
            y=50.456,
            rotation=180.0,
            fixed=True,
            width=5.0,
            height=10.0,
        )
        d = pos.to_dict()

        assert d["ref"] == "U3"
        assert d["x"] == 100.123
        assert d["y"] == 50.456
        assert d["rotation"] == 180.0
        assert d["fixed"] is True
        assert d["width"] == 5.0
        assert d["height"] == 10.0


class TestSessionStartResult:
    """Tests for SessionStartResult dataclass."""

    def test_creation(self):
        components = [
            ComponentPosition("R1", 10.0, 20.0, 0.0, False, 1.0, 0.5),
            ComponentPosition("C1", 30.0, 40.0, 90.0, False, 1.0, 0.5),
        ]
        result = SessionStartResult(
            session_id="abc12345",
            pcb_path="/path/to/board.kicad_pcb",
            components=components,
            initial_score=245.3,
            fixed_refs=["J1", "J2"],
            expires_at="2025-01-01T12:30:00",
        )
        assert result.session_id == "abc12345"
        assert result.pcb_path == "/path/to/board.kicad_pcb"
        assert len(result.components) == 2
        assert result.initial_score == 245.3
        assert result.fixed_refs == ["J1", "J2"]
        assert result.expires_at == "2025-01-01T12:30:00"

    def test_to_dict(self):
        components = [
            ComponentPosition("R1", 10.0, 20.0, 0.0, False, 1.0, 0.5),
        ]
        result = SessionStartResult(
            session_id="abc12345",
            pcb_path="/path/to/board.kicad_pcb",
            components=components,
            initial_score=245.3456,
            fixed_refs=["J1"],
            expires_at="2025-01-01T12:30:00",
        )
        d = result.to_dict()

        assert d["session_id"] == "abc12345"
        assert d["pcb_path"] == "/path/to/board.kicad_pcb"
        assert len(d["components"]) == 1
        assert d["components"][0]["ref"] == "R1"
        assert d["initial_score"] == 245.3456
        assert d["fixed_refs"] == ["J1"]
        assert d["expires_at"] == "2025-01-01T12:30:00"


class TestSessionViolation:
    """Tests for SessionViolation dataclass."""

    def test_creation(self):
        violation = SessionViolation(
            type="clearance",
            description="C1 too close to R1",
            severity="error",
            component="C1",
            location=(45.0, 32.0),
        )
        assert violation.type == "clearance"
        assert violation.description == "C1 too close to R1"
        assert violation.severity == "error"
        assert violation.component == "C1"
        assert violation.location == (45.0, 32.0)

    def test_default_values(self):
        violation = SessionViolation(
            type="boundary",
            description="Component outside board",
        )
        assert violation.severity == "error"
        assert violation.component == ""
        assert violation.location is None

    def test_to_dict(self):
        violation = SessionViolation(
            type="overlap",
            description="C1 overlaps with R1",
            severity="warning",
            component="C1",
            location=(10.0, 20.0),
        )
        d = violation.to_dict()

        assert d["type"] == "overlap"
        assert d["description"] == "C1 overlaps with R1"
        assert d["severity"] == "warning"
        assert d["component"] == "C1"
        assert d["location"] == [10.0, 20.0]

    def test_to_dict_no_location(self):
        violation = SessionViolation(
            type="boundary",
            description="Out of bounds",
        )
        d = violation.to_dict()
        assert d["location"] is None


class TestSessionRoutingImpact:
    """Tests for SessionRoutingImpact dataclass."""

    def test_creation(self):
        impact = SessionRoutingImpact(
            affected_nets=["VCC", "GND"],
            estimated_length_change_mm=4.2,
            new_congestion_areas=[(10.0, 20.0), (30.0, 40.0)],
            crossing_changes=2,
        )
        assert impact.affected_nets == ["VCC", "GND"]
        assert impact.estimated_length_change_mm == 4.2
        assert len(impact.new_congestion_areas) == 2
        assert impact.crossing_changes == 2

    def test_default_values(self):
        impact = SessionRoutingImpact()
        assert impact.affected_nets == []
        assert impact.estimated_length_change_mm == 0.0
        assert impact.new_congestion_areas == []
        assert impact.crossing_changes == 0

    def test_to_dict(self):
        impact = SessionRoutingImpact(
            affected_nets=["NET1"],
            estimated_length_change_mm=-2.5,
        )
        d = impact.to_dict()

        assert d["affected_nets"] == ["NET1"]
        assert d["estimated_length_change_mm"] == -2.5
        assert d["new_congestion_areas"] == []
        assert d["crossing_changes"] == 0


class TestMoveQueryResult:
    """Tests for MoveQueryResult dataclass."""

    def test_valid_move(self):
        result = MoveQueryResult(
            valid=True,
            score_delta=-8.3,
            affected_components=["R1", "R2"],
            warnings=["Large routing change"],
        )
        assert result.valid is True
        assert result.score_delta == -8.3
        assert result.affected_components == ["R1", "R2"]
        assert len(result.warnings) == 1
        assert result.error_message is None

    def test_invalid_move(self):
        result = MoveQueryResult(
            valid=False,
            error_message="Component 'X1' not found",
        )
        assert result.valid is False
        assert result.error_message == "Component 'X1' not found"

    def test_to_dict(self):
        violations = [
            SessionViolation("clearance", "Too close", "warning", "C1"),
        ]
        impact = SessionRoutingImpact(affected_nets=["GND"])
        result = MoveQueryResult(
            valid=True,
            score_delta=-5.0,
            new_violations=violations,
            routing_impact=impact,
        )
        d = result.to_dict()

        assert d["valid"] is True
        assert d["score_delta"] == -5.0
        assert len(d["new_violations"]) == 1
        assert d["routing_impact"]["affected_nets"] == ["GND"]
        assert d["error_message"] is None


class TestSessionManager:
    """Tests for SessionManager class."""

    def test_create_session(self, routing_test_pcb: Path):
        """Test creating a new session."""
        manager = SessionManager(timeout_minutes=30)
        info = manager.create(str(routing_test_pcb))

        assert info.id is not None
        assert len(info.id) == 8
        assert info.pcb_path == str(routing_test_pcb)
        assert info.pending_moves == 0
        assert info.components > 0
        assert info.current_score > 0

    def test_create_session_with_fixed_refs(self, routing_test_pcb: Path):
        """Test creating a session with fixed components."""
        manager = SessionManager()
        info = manager.create(str(routing_test_pcb), fixed_refs=["J1"])

        assert info.fixed_refs == ["J1"]

    def test_get_session(self, routing_test_pcb: Path):
        """Test retrieving a session."""
        manager = SessionManager()
        info = manager.create(str(routing_test_pcb))
        session = manager.get(info.id)

        assert session is not None
        # Session should be a PlacementSession
        assert hasattr(session, "query_move")
        assert hasattr(session, "list_components")

    def test_get_nonexistent_session(self):
        """Test that getting nonexistent session raises error."""
        manager = SessionManager()
        with pytest.raises(SessionNotFoundError):
            manager.get("nonexistent")

    def test_destroy_session(self, routing_test_pcb: Path):
        """Test destroying a session."""
        manager = SessionManager()
        info = manager.create(str(routing_test_pcb))
        assert manager.session_count == 1

        result = manager.destroy(info.id)
        assert result is True
        assert manager.session_count == 0

        with pytest.raises(SessionNotFoundError):
            manager.get(info.id)

    def test_destroy_nonexistent_session(self):
        """Test destroying nonexistent session returns False."""
        manager = SessionManager()
        result = manager.destroy("nonexistent")
        assert result is False

    def test_list_sessions(self, routing_test_pcb: Path):
        """Test listing all sessions."""
        manager = SessionManager()
        manager.create(str(routing_test_pcb))
        manager.create(str(routing_test_pcb))

        sessions = manager.list_sessions()
        assert len(sessions) == 2

    def test_session_count(self, routing_test_pcb: Path):
        """Test session count property."""
        manager = SessionManager()
        assert manager.session_count == 0

        manager.create(str(routing_test_pcb))
        assert manager.session_count == 1

        manager.create(str(routing_test_pcb))
        assert manager.session_count == 2

    def test_global_session_manager(self):
        """Test global session manager instance."""
        manager1 = get_session_manager()
        manager2 = get_session_manager()
        assert manager1 is manager2


class TestStartSession:
    """Tests for start_session tool function."""

    def test_start_session_success(self, routing_test_pcb: Path):
        """Test starting a session successfully."""
        result = start_session(str(routing_test_pcb))

        assert result.session_id is not None
        assert len(result.session_id) == 8
        assert result.pcb_path == str(routing_test_pcb)
        assert len(result.components) > 0
        assert result.initial_score > 0
        assert result.fixed_refs == []
        assert result.expires_at is not None

    def test_start_session_with_fixed_refs(self, routing_test_pcb: Path):
        """Test starting a session with fixed components."""
        result = start_session(str(routing_test_pcb), fixed_refs=["J1"])

        assert result.fixed_refs == ["J1"]

    def test_start_session_file_not_found(self):
        """Test starting session with nonexistent file."""
        with pytest.raises(Exception) as exc_info:
            start_session("/nonexistent/board.kicad_pcb")
        assert "not found" in str(exc_info.value).lower()

    def test_start_session_invalid_extension(self, tmp_path: Path):
        """Test starting session with wrong file extension."""
        bad_file = tmp_path / "board.pcb"
        bad_file.write_text("(kicad_pcb)")

        with pytest.raises(Exception) as exc_info:
            start_session(str(bad_file))
        assert "extension" in str(exc_info.value).lower()

    def test_components_have_positions(self, routing_test_pcb: Path):
        """Test that components have position data."""
        result = start_session(str(routing_test_pcb))

        for comp in result.components:
            assert comp.ref is not None
            assert isinstance(comp.x, float)
            assert isinstance(comp.y, float)
            assert isinstance(comp.rotation, float)
            assert isinstance(comp.fixed, bool)
            assert isinstance(comp.width, float)
            assert isinstance(comp.height, float)


class TestQueryMove:
    """Tests for query_move tool function."""

    def test_query_valid_move(self, routing_test_pcb: Path):
        """Test querying a valid component move."""
        session = start_session(str(routing_test_pcb))
        result = query_move(session.session_id, "R1", 130.0, 120.0)

        assert result.valid is True
        assert result.error_message is None
        assert isinstance(result.score_delta, float)

    def test_query_move_with_rotation(self, routing_test_pcb: Path):
        """Test querying a move with rotation change."""
        session = start_session(str(routing_test_pcb))
        result = query_move(session.session_id, "R1", 130.0, 120.0, rotation=90.0)

        assert result.valid is True

    def test_query_move_nonexistent_component(self, routing_test_pcb: Path):
        """Test querying move for nonexistent component."""
        session = start_session(str(routing_test_pcb))
        result = query_move(session.session_id, "X99", 100.0, 100.0)

        assert result.valid is False
        assert result.error_message is not None
        assert "not found" in result.error_message.lower()

    def test_query_move_invalid_session(self):
        """Test querying move with invalid session ID."""
        result = query_move("invalid_session", "R1", 100.0, 100.0)

        assert result.valid is False
        assert result.error_message is not None
        assert "not found" in result.error_message.lower()

    def test_query_move_fixed_component(self, routing_test_pcb: Path):
        """Test querying move for fixed component."""
        session = start_session(str(routing_test_pcb), fixed_refs=["R1"])
        result = query_move(session.session_id, "R1", 130.0, 120.0)

        assert result.valid is False
        assert result.error_message is not None
        assert "fixed" in result.error_message.lower()

    def test_query_move_returns_routing_impact(self, routing_test_pcb: Path):
        """Test that query_move returns routing impact."""
        session = start_session(str(routing_test_pcb))
        result = query_move(session.session_id, "R1", 130.0, 120.0)

        assert result.routing_impact is not None
        assert hasattr(result.routing_impact, "affected_nets")
        assert hasattr(result.routing_impact, "estimated_length_change_mm")

    def test_query_move_returns_affected_components(self, routing_test_pcb: Path):
        """Test that query_move returns affected components."""
        session = start_session(str(routing_test_pcb))
        result = query_move(session.session_id, "R1", 130.0, 120.0)

        assert isinstance(result.affected_components, list)


class TestMCPServerSession:
    """Tests for MCP server with session tools."""

    def test_create_server_has_session_tools(self):
        """Test that server has session tools registered."""
        server = create_server()
        assert "start_session" in server.tools
        assert "query_move" in server.tools

    def test_get_tools_list_includes_session(self):
        """Test that tools list includes session tools."""
        server = create_server()
        tools = server.get_tools_list()

        start_tool = next((t for t in tools if t["name"] == "start_session"), None)
        query_tool = next((t for t in tools if t["name"] == "query_move"), None)

        assert start_tool is not None
        assert query_tool is not None

    def test_start_session_tool_schema(self):
        """Test start_session tool has correct schema."""
        server = create_server()
        tools = server.get_tools_list()

        tool = next(t for t in tools if t["name"] == "start_session")
        schema = tool["inputSchema"]

        assert "pcb_path" in schema["properties"]
        assert "fixed_refs" in schema["properties"]
        assert "pcb_path" in schema["required"]

    def test_query_move_tool_schema(self):
        """Test query_move tool has correct schema."""
        server = create_server()
        tools = server.get_tools_list()

        tool = next(t for t in tools if t["name"] == "query_move")
        schema = tool["inputSchema"]

        assert "session_id" in schema["properties"]
        assert "ref" in schema["properties"]
        assert "x" in schema["properties"]
        assert "y" in schema["properties"]
        assert "rotation" in schema["properties"]
        assert set(schema["required"]) == {"session_id", "ref", "x", "y"}

    def test_handle_tools_call_start_session_missing_file(self):
        """Test start_session with missing file via MCP."""
        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "start_session",
                    "arguments": {
                        "pcb_path": "/nonexistent/board.kicad_pcb",
                    },
                },
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        # Should be an error response
        assert "error" in response

    def test_handle_tools_call_query_move_invalid_session(self):
        """Test query_move with invalid session via MCP."""
        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "query_move",
                    "arguments": {
                        "session_id": "invalid",
                        "ref": "R1",
                        "x": 100.0,
                        "y": 100.0,
                    },
                },
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "result" in response

        result_text = response["result"]["content"][0]["text"]
        result_data = json.loads(result_text)
        assert result_data["valid"] is False
        assert result_data["error_message"] is not None

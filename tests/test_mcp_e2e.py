"""End-to-end integration tests for MCP server.

These tests validate the complete MCP server workflow including:
- MCP protocol compliance (JSON-RPC)
- Tool discovery and invocation
- Board analysis workflows
- Concurrent session handling
- Error handling
- Performance benchmarks

Test Scenarios from Issue #478:
1. Basic Connectivity - Claude Desktop can connect and list tools
2. Board Analysis Workflow - Complete analysis via MCP
3. Placement Refinement Session - Stateful session lifecycle
4. Manufacturing Export - Complete package generation
5. Error Handling - Graceful error messages
6. Concurrent Sessions - Multiple sessions don't interfere
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from kicad_tools.mcp.server import MCPServer, create_server
from kicad_tools.mcp.tools.analysis import analyze_board, get_drc_violations
from kicad_tools.mcp.tools.session import (
    apply_move,
    commit_session,
    get_session_manager,
    query_move,
    reset_session_manager,
    rollback_session,
    start_session,
    undo_move,
)

# =============================================================================
# Test Fixtures
# =============================================================================

# Simple 2-layer board for basic tests
SIMPLE_BOARD_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "SIG1")
  (gr_line (start 100 100) (end 160 100) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 160 100) (end 160 150) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 160 150) (end 100 150) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 100 150) (end 100 100) (layer "Edge.Cuts") (stroke (width 0.1)))
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 120 120)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "ref-r1"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val-r1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 140 120)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "ref-r2"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val-r2"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG1"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000030")
    (at 130 140)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "ref-c1"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "val-c1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (segment (start 120.51 120) (end 139.49 120) (width 0.25) (layer "F.Cu") (net 2)
    (uuid "seg-1"))
)
"""


@pytest.fixture(autouse=True)
def reset_sessions():
    """Reset session manager before and after each test."""
    reset_session_manager()
    yield
    reset_session_manager()


@pytest.fixture
def simple_pcb_path(tmp_path: Path) -> str:
    """Create a simple PCB file for testing."""
    pcb_file = tmp_path / "simple_board.kicad_pcb"
    pcb_file.write_text(SIMPLE_BOARD_PCB)
    return str(pcb_file)


@pytest.fixture
def mcp_server() -> MCPServer:
    """Create a fresh MCP server instance."""
    return create_server()


# =============================================================================
# 1. Basic Connectivity Tests
# =============================================================================


class TestBasicConnectivity:
    """Test basic MCP protocol connectivity (Test Scenario 1)."""

    def test_server_creation(self, mcp_server: MCPServer) -> None:
        """Test that MCP server can be created."""
        assert mcp_server is not None
        assert mcp_server.name == "kicad-tools"
        assert mcp_server.version is not None

    def test_initialize_protocol(self, mcp_server: MCPServer) -> None:
        """Test MCP initialize request."""
        response = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "result" in response
        assert "protocolVersion" in response["result"]
        assert response["result"]["protocolVersion"] == "2024-11-05"
        assert "serverInfo" in response["result"]
        assert response["result"]["serverInfo"]["name"] == "kicad-tools"

    def test_list_tools(self, mcp_server: MCPServer) -> None:
        """Test that all expected tools are listed."""
        response = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert "result" in response
        assert "tools" in response["result"]

        tools = response["result"]["tools"]
        tool_names = [t["name"] for t in tools]

        # Verify core tools are available
        expected_tools = [
            "export_gerbers",
            "export_bom",
            "export_assembly",
            "placement_analyze",
            "start_session",
            "query_move",
            "apply_move",
            "undo_move",
            "commit_session",
            "rollback_session",
            "measure_clearance",
        ]

        for expected in expected_tools:
            assert expected in tool_names, f"Tool {expected} not found in tool list"

    def test_tools_have_proper_schema(self, mcp_server: MCPServer) -> None:
        """Test that each tool has proper input schema."""
        tools = mcp_server.get_tools_list()

        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert tool["description"], f"Tool {tool['name']} has no description"
            assert "type" in tool["inputSchema"]
            assert tool["inputSchema"]["type"] == "object"

    def test_notification_initialized(self, mcp_server: MCPServer) -> None:
        """Test that initialized notification is handled."""
        response = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )

        # Notifications should return empty response
        assert response == {}


# =============================================================================
# 2. Board Analysis Workflow Tests
# =============================================================================


class TestBoardAnalysisWorkflow:
    """Test board analysis workflow (Test Scenario 2)."""

    def test_analyze_board_returns_comprehensive_data(self, simple_pcb_path: str) -> None:
        """Test that analyze_board returns complete board metadata."""
        result = analyze_board(simple_pcb_path)

        # Verify all expected fields are present
        assert result.file_path == simple_pcb_path
        assert result.board_dimensions is not None
        assert result.layers is not None
        assert result.components is not None
        assert result.nets is not None
        assert result.routing_status is not None

    def test_analyze_board_dimensions(self, simple_pcb_path: str) -> None:
        """Test board dimension extraction."""
        result = analyze_board(simple_pcb_path)

        dims = result.board_dimensions
        assert dims.width_mm > 0
        assert dims.height_mm > 0
        assert dims.area_mm2 > 0
        assert dims.outline_type in ("rectangle", "polygon", "complex", "unknown")

    def test_analyze_board_layers(self, simple_pcb_path: str) -> None:
        """Test layer information extraction."""
        result = analyze_board(simple_pcb_path)

        layers = result.layers
        assert layers.copper_layers == 2  # F.Cu and B.Cu
        assert "F.Cu" in layers.layer_names
        assert "B.Cu" in layers.layer_names

    def test_analyze_board_components(self, simple_pcb_path: str) -> None:
        """Test component summary extraction."""
        result = analyze_board(simple_pcb_path)

        components = result.components
        assert components.total_count == 3  # R1, R2, C1
        assert components.smd_count == 3
        assert components.through_hole_count == 0
        assert "resistor" in components.by_type
        assert "capacitor" in components.by_type

    def test_analyze_board_nets(self, simple_pcb_path: str) -> None:
        """Test net summary extraction."""
        result = analyze_board(simple_pcb_path)

        nets = result.nets
        assert nets.total_nets >= 3  # GND, +3.3V, SIG1
        assert "GND" in nets.power_nets or "+3.3V" in nets.power_nets

    def test_analyze_board_routing_status(self, simple_pcb_path: str) -> None:
        """Test routing status calculation."""
        result = analyze_board(simple_pcb_path)

        routing = result.routing_status
        assert 0 <= routing.completion_percent <= 100
        assert routing.total_trace_length_mm >= 0
        assert routing.via_count >= 0

    def test_analyze_board_to_dict(self, simple_pcb_path: str) -> None:
        """Test that result can be serialized to dict/JSON."""
        result = analyze_board(simple_pcb_path)
        data = result.to_dict()

        # Verify JSON-serializable
        json_str = json.dumps(data)
        assert json_str is not None

        # Verify structure
        assert "file_path" in data
        assert "board_dimensions" in data
        assert "layers" in data
        assert "components" in data
        assert "nets" in data
        assert "routing_status" in data


class TestDRCWorkflow:
    """Test DRC workflow."""

    def test_get_drc_violations_basic(self, simple_pcb_path: str) -> None:
        """Test basic DRC check."""
        result = get_drc_violations(simple_pcb_path)

        assert result is not None
        assert isinstance(result.passed, bool)
        assert result.violation_count >= 0
        assert result.manufacturer == "jlcpcb"

    def test_get_drc_violations_with_manufacturer(self, simple_pcb_path: str) -> None:
        """Test DRC with different manufacturer presets."""
        manufacturers = ["jlcpcb", "oshpark", "pcbway", "seeed"]

        for mfr in manufacturers:
            result = get_drc_violations(simple_pcb_path, rules=mfr)
            assert result.manufacturer == mfr

    def test_get_drc_violations_severity_filter(self, simple_pcb_path: str) -> None:
        """Test DRC with severity filtering."""
        # Get all violations
        all_result = get_drc_violations(simple_pcb_path, severity_filter="all")
        assert all_result is not None

        # Get only errors
        errors_result = get_drc_violations(simple_pcb_path, severity_filter="error")
        assert errors_result.warning_count == 0

        # Get only warnings
        warnings_result = get_drc_violations(simple_pcb_path, severity_filter="warning")
        assert warnings_result.error_count == 0

    def test_get_drc_violations_to_dict(self, simple_pcb_path: str) -> None:
        """Test DRC result serialization."""
        result = get_drc_violations(simple_pcb_path)
        data = result.to_dict()

        json_str = json.dumps(data)
        assert json_str is not None

        assert "passed" in data
        assert "violation_count" in data
        assert "violations" in data


# =============================================================================
# 3. Placement Refinement Session Tests
# =============================================================================


class TestPlacementSession:
    """Test placement refinement session workflow (Test Scenario 3)."""

    def test_session_lifecycle(self, simple_pcb_path: str, tmp_path: Path) -> None:
        """Test complete session lifecycle: start -> query -> apply -> commit."""
        # 1. Start session
        start_result = start_session(simple_pcb_path)
        assert start_result.success
        session_id = start_result.session_id
        assert session_id

        # 2. Query a move
        query_result = query_move(session_id, "R1", 125.0, 125.0)
        assert query_result.success
        assert query_result.would_succeed

        # 3. Apply the move
        apply_result = apply_move(session_id, "R1", 125.0, 125.0)
        assert apply_result.success
        assert apply_result.pending_moves == 1

        # 4. Undo the move
        undo_result = undo_move(session_id)
        assert undo_result.success
        assert undo_result.pending_moves == 0

        # 5. Apply different move
        apply_result2 = apply_move(session_id, "R2", 145.0, 125.0)
        assert apply_result2.success

        # 6. Commit changes
        output_path = str(tmp_path / "output.kicad_pcb")
        commit_result = commit_session(session_id, output_path)
        assert commit_result.success
        assert commit_result.moves_applied == 1
        assert "R2" in commit_result.components_moved

        # Verify file was created
        assert Path(output_path).exists()

    def test_session_rollback(self, simple_pcb_path: str) -> None:
        """Test session rollback discards all changes."""
        # Start and make changes
        start_result = start_session(simple_pcb_path)
        session_id = start_result.session_id

        apply_move(session_id, "R1", 125.0, 125.0)
        apply_move(session_id, "R2", 145.0, 125.0)

        # Rollback
        rollback_result = rollback_session(session_id)
        assert rollback_result.success
        assert rollback_result.moves_discarded == 2
        assert rollback_result.session_closed

        # Session should be gone
        manager = get_session_manager()
        assert manager.get(session_id) is None

    def test_session_fixed_components(self, simple_pcb_path: str) -> None:
        """Test that fixed components cannot be moved."""
        result = start_session(simple_pcb_path, fixed_refs=["R1"])
        session_id = result.session_id
        assert result.fixed_count >= 1

        # Try to move fixed component
        move_result = apply_move(session_id, "R1", 125.0, 125.0)
        assert not move_result.success
        assert "fixed" in move_result.error_message.lower()

        rollback_session(session_id)


# =============================================================================
# 4. Manufacturing Export Tests
# =============================================================================


class TestManufacturingExport:
    """Test manufacturing export workflow (Test Scenario 4)."""

    def test_placement_analyze_via_mcp(self, mcp_server: MCPServer, simple_pcb_path: str) -> None:
        """Test placement analysis via MCP protocol."""
        response = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "placement_analyze",
                    "arguments": {
                        "pcb_path": simple_pcb_path,
                    },
                },
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert "result" in response
        assert "content" in response["result"]

        # Parse the result
        result_text = response["result"]["content"][0]["text"]
        result_data = json.loads(result_text)

        assert "overall_score" in result_data
        assert 0 <= result_data["overall_score"] <= 100


# =============================================================================
# 5. Error Handling Tests
# =============================================================================


class TestErrorHandling:
    """Test error handling (Test Scenario 5)."""

    def test_nonexistent_file_error(self) -> None:
        """Test error for non-existent PCB file."""
        from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError

        with pytest.raises(KiCadFileNotFoundError):
            analyze_board("/nonexistent/path/board.kicad_pcb")

    def test_invalid_session_id_error(self) -> None:
        """Test error for invalid session ID."""
        result = query_move("invalid-session-id", "R1", 100.0, 100.0)
        assert not result.success
        assert "not found" in result.error_message.lower()

    def test_invalid_component_error(self, simple_pcb_path: str) -> None:
        """Test error for invalid component reference."""
        start_result = start_session(simple_pcb_path)
        session_id = start_result.session_id

        result = apply_move(session_id, "INVALID_REF", 100.0, 100.0)
        assert not result.success
        assert "not found" in result.error_message.lower()

        rollback_session(session_id)

    def test_unknown_tool_error(self, mcp_server: MCPServer) -> None:
        """Test error for unknown tool."""
        response = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "nonexistent_tool",
                    "arguments": {},
                },
            }
        )

        assert "error" in response

    def test_unknown_method_error(self, mcp_server: MCPServer) -> None:
        """Test error for unknown method."""
        response = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "unknown/method",
                "params": {},
            }
        )

        assert "error" in response
        assert response["error"]["code"] == -32601

    def test_invalid_manufacturer_error(self, simple_pcb_path: str) -> None:
        """Test error for invalid manufacturer preset."""
        with pytest.raises(ValueError, match="Unknown manufacturer"):
            get_drc_violations(simple_pcb_path, rules="invalid_manufacturer")


# =============================================================================
# 6. Concurrent Sessions Tests
# =============================================================================


class TestConcurrentSessions:
    """Test concurrent session handling (Test Scenario 6)."""

    def test_multiple_sessions_isolated(self, tmp_path: Path) -> None:
        """Test that multiple sessions don't interfere with each other."""
        # Create two separate PCB files
        pcb1 = tmp_path / "board1.kicad_pcb"
        pcb2 = tmp_path / "board2.kicad_pcb"
        pcb1.write_text(SIMPLE_BOARD_PCB)
        pcb2.write_text(SIMPLE_BOARD_PCB)

        # Start two sessions
        session1 = start_session(str(pcb1))
        session2 = start_session(str(pcb2))

        assert session1.session_id != session2.session_id

        # Make changes in session 1
        apply_move(session1.session_id, "R1", 125.0, 125.0)

        # Make different changes in session 2
        apply_move(session2.session_id, "R2", 145.0, 145.0)

        # Verify sessions are isolated
        manager = get_session_manager()
        meta1 = manager.get(session1.session_id)
        meta2 = manager.get(session2.session_id)

        assert len(meta1.session.pending_moves) == 1
        assert len(meta2.session.pending_moves) == 1
        assert meta1.session.pending_moves[0].ref == "R1"
        assert meta2.session.pending_moves[0].ref == "R2"

        # Commit session 1
        output1 = str(tmp_path / "output1.kicad_pcb")
        commit_session(session1.session_id, output1)

        # Session 2 should still be active
        assert manager.get(session2.session_id) is not None

        # Rollback session 2
        rollback_session(session2.session_id)

    def test_session_ids_unique(self, simple_pcb_path: str) -> None:
        """Test that session IDs are unique across multiple creations."""
        sessions = []
        for _ in range(5):
            result = start_session(simple_pcb_path)
            sessions.append(result.session_id)

        # All session IDs should be unique
        assert len(set(sessions)) == 5

        # Clean up
        for sid in sessions:
            rollback_session(sid)

    def test_closed_session_not_accessible(self, simple_pcb_path: str) -> None:
        """Test that closed sessions cannot be accessed."""
        start_result = start_session(simple_pcb_path)
        session_id = start_result.session_id

        # Close the session
        rollback_session(session_id)

        # Try to use closed session
        result = query_move(session_id, "R1", 100.0, 100.0)
        assert not result.success
        assert "not found" in result.error_message.lower()


# =============================================================================
# 7. Performance Benchmarks
# =============================================================================


class TestPerformanceBenchmarks:
    """Performance benchmark tests."""

    def test_analyze_board_performance(self, simple_pcb_path: str) -> None:
        """Verify analyze_board completes quickly for simple boards."""
        start = time.time()
        result = analyze_board(simple_pcb_path)
        elapsed = time.time() - start

        assert result is not None
        # Should complete within 2 seconds for simple boards
        assert elapsed < 2.0, f"analyze_board took {elapsed:.2f}s (expected < 2s)"

    def test_session_start_performance(self, simple_pcb_path: str) -> None:
        """Verify session creation is fast."""
        start = time.time()
        result = start_session(simple_pcb_path)
        elapsed = time.time() - start

        assert result.success
        # Should complete within 1 second
        assert elapsed < 1.0, f"start_session took {elapsed:.2f}s (expected < 1s)"

        rollback_session(result.session_id)

    def test_query_move_performance(self, simple_pcb_path: str) -> None:
        """Verify query_move is fast enough for interactive use."""
        session_result = start_session(simple_pcb_path)
        session_id = session_result.session_id

        start = time.time()
        result = query_move(session_id, "R1", 125.0, 125.0)
        elapsed = time.time() - start

        assert result.success
        # Should complete within 500ms for interactive use
        assert elapsed < 0.5, f"query_move took {elapsed:.2f}s (expected < 0.5s)"

        rollback_session(session_id)

    def test_drc_check_performance(self, simple_pcb_path: str) -> None:
        """Verify DRC check completes in reasonable time."""
        start = time.time()
        result = get_drc_violations(simple_pcb_path)
        elapsed = time.time() - start

        assert result is not None
        # Should complete within 5 seconds
        assert elapsed < 5.0, f"get_drc_violations took {elapsed:.2f}s (expected < 5s)"


# =============================================================================
# 8. MCP Protocol Compliance Tests
# =============================================================================


class TestMCPProtocolCompliance:
    """Tests for MCP protocol compliance."""

    def test_jsonrpc_version(self, mcp_server: MCPServer) -> None:
        """Test that responses include correct JSON-RPC version."""
        response = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            }
        )

        assert response["jsonrpc"] == "2.0"

    def test_id_preserved_in_response(self, mcp_server: MCPServer) -> None:
        """Test that request ID is preserved in response."""
        test_ids = [1, "test-id-123", 42]

        for test_id in test_ids:
            response = mcp_server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": test_id,
                    "method": "tools/list",
                    "params": {},
                }
            )

            assert response["id"] == test_id

    def test_tool_call_returns_content_array(
        self, mcp_server: MCPServer, simple_pcb_path: str
    ) -> None:
        """Test that tool calls return content as array."""
        response = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "placement_analyze",
                    "arguments": {"pcb_path": simple_pcb_path},
                },
            }
        )

        assert "result" in response
        assert "content" in response["result"]
        assert isinstance(response["result"]["content"], list)
        assert len(response["result"]["content"]) > 0
        assert response["result"]["content"][0]["type"] == "text"

    def test_capabilities_in_initialize(self, mcp_server: MCPServer) -> None:
        """Test that capabilities are reported in initialize."""
        response = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            }
        )

        assert "capabilities" in response["result"]
        assert "tools" in response["result"]["capabilities"]

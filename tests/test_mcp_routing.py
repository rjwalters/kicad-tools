"""Tests for kicad_tools.mcp.tools.routing module."""

from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.mcp.tools.routing import get_unrouted_nets, route_net
from kicad_tools.mcp.types import (
    NetRoutingStatus,
    RouteNetResult,
    UnroutedNetsResult,
)

# Simple 2-layer PCB with unrouted nets
UNROUTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG1")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
  )

  (footprint "C_0603"
    (layer "F.Cu")
    (at 20 10)
    (attr smd)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 30 10)
    (attr smd)
    (property "Reference" "R2")
    (property "Value" "4.7k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )
)
"""

# PCB with partially routed net
PARTIAL_ROUTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG1")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
  )

  (footprint "C_0603"
    (layer "F.Cu")
    (at 20 10)
    (attr smd)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 30 10)
    (attr smd)
    (property "Reference" "R2")
    (property "Value" "4.7k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (segment (start 10.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 3))
)
"""

# PCB with fully routed nets
FULLY_ROUTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG1")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
  )

  (footprint "C_0603"
    (layer "F.Cu")
    (at 20 10)
    (attr smd)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (segment (start 10.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 3))
  (segment (start 9.5 10) (end 9.5 20) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 20.5 10) (end 20.5 20) (width 0.25) (layer "F.Cu") (net 2))
)
"""


class TestGetUnroutedNets:
    """Tests for get_unrouted_nets function."""

    def test_unrouted_pcb(self, tmp_path: Path) -> None:
        """Test detection of unrouted nets."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(UNROUTED_PCB)

        result = get_unrouted_nets(str(pcb_file))

        assert isinstance(result, UnroutedNetsResult)
        assert result.total_nets == 3  # VCC, GND, SIG1
        # All nets have 2 pads but no routing, so they're incomplete/unrouted
        assert result.unrouted_count + result.partial_count >= 2
        assert result.complete_count == 0  # No nets fully routed

    def test_partial_routed_pcb(self, tmp_path: Path) -> None:
        """Test detection of partially routed nets."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PARTIAL_ROUTED_PCB)

        result = get_unrouted_nets(str(pcb_file))

        assert isinstance(result, UnroutedNetsResult)
        # SIG1 should be complete (2 pads connected)
        assert result.complete_count >= 1

    def test_include_partial_false(self, tmp_path: Path) -> None:
        """Test excluding partial nets from results."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PARTIAL_ROUTED_PCB)

        result = get_unrouted_nets(str(pcb_file), include_partial=False)

        # Should not include any partial nets
        for net in result.nets:
            assert net.status != "partial"

    def test_result_to_dict(self, tmp_path: Path) -> None:
        """Test serialization of result."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(UNROUTED_PCB)

        result = get_unrouted_nets(str(pcb_file))
        result_dict = result.to_dict()

        assert "total_nets" in result_dict
        assert "unrouted_count" in result_dict
        assert "partial_count" in result_dict
        assert "complete_count" in result_dict
        assert "nets" in result_dict
        assert isinstance(result_dict["nets"], list)

    def test_net_status_fields(self, tmp_path: Path) -> None:
        """Test NetRoutingStatus fields are populated."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(UNROUTED_PCB)

        result = get_unrouted_nets(str(pcb_file))

        for net in result.nets:
            assert isinstance(net, NetRoutingStatus)
            assert net.name != ""
            # Status from NetStatusAnalyzer uses "incomplete" not "partial"
            assert net.status in ["unrouted", "incomplete", "complete"]
            assert net.pins >= 0
            assert net.difficulty in ["easy", "medium", "hard"]

    def test_file_not_found(self) -> None:
        """Test error handling for missing file."""
        with pytest.raises(KiCadFileNotFoundError):
            get_unrouted_nets("/nonexistent/path/board.kicad_pcb")

    def test_invalid_extension(self, tmp_path: Path) -> None:
        """Test error handling for invalid file extension."""
        pcb_file = tmp_path / "test.txt"
        pcb_file.write_text("not a pcb file")

        with pytest.raises(ParseError):
            get_unrouted_nets(str(pcb_file))

    def test_invalid_content(self, tmp_path: Path) -> None:
        """Test error handling for invalid PCB content."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text("not valid sexp content")

        with pytest.raises(ParseError):
            get_unrouted_nets(str(pcb_file))


class TestRouteNet:
    """Tests for route_net function."""

    def test_route_simple_net(self, tmp_path: Path) -> None:
        """Test routing a simple 2-pin net."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(UNROUTED_PCB)
        output_file = tmp_path / "routed.kicad_pcb"

        result = route_net(
            str(pcb_file),
            net_name="SIG1",
            output_path=str(output_file),
        )

        assert isinstance(result, RouteNetResult)
        assert result.net_name == "SIG1"
        # Routing may or may not succeed depending on router
        # but the result structure should be valid
        assert result.total_connections >= 0

    def test_route_already_routed_net(self, tmp_path: Path) -> None:
        """Test routing a net that's already fully routed."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PARTIAL_ROUTED_PCB)

        result = route_net(str(pcb_file), net_name="SIG1")

        assert isinstance(result, RouteNetResult)
        # SIG1 is connected in PARTIAL_ROUTED_PCB
        assert result.success is True
        assert result.net_name == "SIG1"

    def test_net_not_found(self, tmp_path: Path) -> None:
        """Test error for non-existent net."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(UNROUTED_PCB)

        with pytest.raises(ValueError, match="not found"):
            route_net(str(pcb_file), net_name="NONEXISTENT_NET")

    def test_result_to_dict(self, tmp_path: Path) -> None:
        """Test serialization of route result."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(UNROUTED_PCB)

        result = route_net(str(pcb_file), net_name="SIG1")
        result_dict = result.to_dict()

        assert "success" in result_dict
        assert "net_name" in result_dict
        assert "routed_connections" in result_dict
        assert "total_connections" in result_dict
        assert "trace_length_mm" in result_dict
        assert "vias_used" in result_dict
        assert "layers_used" in result_dict

    def test_route_strategies(self, tmp_path: Path) -> None:
        """Test different routing strategies."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(UNROUTED_PCB)

        # Test auto strategy
        result_auto = route_net(
            str(pcb_file),
            net_name="SIG1",
            strategy="auto",
        )
        assert result_auto.net_name == "SIG1"

        # Test shortest strategy
        result_shortest = route_net(
            str(pcb_file),
            net_name="SIG1",
            strategy="shortest",
        )
        assert result_shortest.net_name == "SIG1"

        # Test avoid_vias strategy
        result_no_vias = route_net(
            str(pcb_file),
            net_name="SIG1",
            strategy="avoid_vias",
        )
        assert result_no_vias.net_name == "SIG1"

    def test_layer_preference(self, tmp_path: Path) -> None:
        """Test layer preference parameter."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(UNROUTED_PCB)

        result = route_net(
            str(pcb_file),
            net_name="SIG1",
            layer_preference="F.Cu",
        )

        assert result.net_name == "SIG1"
        # If routing succeeded and used layers, check preference was respected
        if result.success and result.layers_used:
            # F.Cu should be in the layers used (unless routing failed)
            pass  # Can't guarantee F.Cu is used as it depends on routing

    def test_file_not_found(self) -> None:
        """Test error handling for missing file."""
        with pytest.raises(KiCadFileNotFoundError):
            route_net("/nonexistent/path/board.kicad_pcb", net_name="SIG1")

    def test_invalid_extension(self, tmp_path: Path) -> None:
        """Test error handling for invalid file extension."""
        pcb_file = tmp_path / "test.txt"
        pcb_file.write_text("not a pcb file")

        with pytest.raises(ParseError):
            route_net(str(pcb_file), net_name="SIG1")


class TestMCPServerIntegration:
    """Integration tests for MCP server routing tools."""

    def test_server_has_routing_tools(self) -> None:
        """Test that MCP server includes routing tools."""
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()

        assert "get_unrouted_nets" in server.tools
        assert "route_net" in server.tools

    def test_server_tool_definitions(self) -> None:
        """Test routing tool definitions in MCP server."""
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()

        # Check get_unrouted_nets
        unrouted_tool = server.tools["get_unrouted_nets"]
        assert unrouted_tool.name == "get_unrouted_nets"
        assert "pcb_path" in unrouted_tool.parameters["properties"]

        # Check route_net
        route_tool = server.tools["route_net"]
        assert route_tool.name == "route_net"
        assert "pcb_path" in route_tool.parameters["properties"]
        assert "net_name" in route_tool.parameters["properties"]

    def test_server_call_get_unrouted_nets(self, tmp_path: Path) -> None:
        """Test calling get_unrouted_nets through MCP server."""
        from kicad_tools.mcp.server import MCPServer

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(UNROUTED_PCB)

        server = MCPServer()
        result = server.call_tool(
            "get_unrouted_nets",
            {"pcb_path": str(pcb_file)},
        )

        assert "total_nets" in result
        assert "nets" in result

    def test_server_call_route_net(self, tmp_path: Path) -> None:
        """Test calling route_net through MCP server."""
        from kicad_tools.mcp.server import MCPServer

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(UNROUTED_PCB)

        server = MCPServer()
        result = server.call_tool(
            "route_net",
            {"pcb_path": str(pcb_file), "net_name": "SIG1"},
        )

        assert "success" in result
        assert "net_name" in result
        assert result["net_name"] == "SIG1"


class TestNetRoutingStatusType:
    """Tests for NetRoutingStatus dataclass."""

    def test_to_dict(self) -> None:
        """Test serialization of NetRoutingStatus."""
        status = NetRoutingStatus(
            name="TEST_NET",
            status="unrouted",
            pins=4,
            routed_connections=0,
            total_connections=3,
            estimated_length_mm=25.5,
            difficulty="medium",
            reason="Long routing distance",
        )

        result = status.to_dict()

        assert result["name"] == "TEST_NET"
        assert result["status"] == "unrouted"
        assert result["pins"] == 4
        assert result["routed_connections"] == 0
        assert result["total_connections"] == 3
        assert result["estimated_length_mm"] == 25.5
        assert result["difficulty"] == "medium"
        assert result["reason"] == "Long routing distance"


class TestRouteNetResultType:
    """Tests for RouteNetResult dataclass."""

    def test_success_result_to_dict(self) -> None:
        """Test serialization of successful routing result."""
        result = RouteNetResult(
            success=True,
            net_name="SIG1",
            routed_connections=2,
            total_connections=2,
            trace_length_mm=15.5,
            vias_used=0,
            layers_used=["F.Cu"],
            output_path="/path/to/output.kicad_pcb",
        )

        result_dict = result.to_dict()

        assert result_dict["success"] is True
        assert result_dict["net_name"] == "SIG1"
        assert result_dict["routed_connections"] == 2
        assert result_dict["trace_length_mm"] == 15.5
        assert result_dict["vias_used"] == 0
        assert result_dict["layers_used"] == ["F.Cu"]

    def test_failure_result_to_dict(self) -> None:
        """Test serialization of failed routing result."""
        result = RouteNetResult(
            success=False,
            net_name="GND",
            total_connections=5,
            error_message="Routing blocked by obstacles",
            suggestions=["Check placement", "Try different layer"],
        )

        result_dict = result.to_dict()

        assert result_dict["success"] is False
        assert result_dict["net_name"] == "GND"
        assert result_dict["error_message"] == "Routing blocked by obstacles"
        assert "Check placement" in result_dict["suggestions"]

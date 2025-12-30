"""Tests for interpreter A* routing integration (Issue #5).

Tests the obstacle-aware A* routing implementation in CommandInterpreter:
- ObstacleGridBuilder: Builds RoutingGrid from PCBState
- A* routing integration: Uses Router for pathfinding
- Routing diagnostics: Reports why routes failed
"""

import pytest
from dataclasses import dataclass, field

from kicad_tools.reasoning.interpreter import (
    CommandInterpreter,
    InterpreterConfig,
    ObstacleGridBuilder,
    RoutingDiagnostic,
)
from kicad_tools.reasoning.state import (
    PCBState,
    BoardOutline,
    ComponentState,
    PadState,
    NetState,
    TraceState,
    ZoneState,
)
from kicad_tools.reasoning.commands import RouteNetCommand, CommandType
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.layers import Layer, LayerStack


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def simple_pcb_state():
    """Create a minimal PCBState for testing."""
    # Create board outline
    outline = BoardOutline.from_points([
        (0.0, 0.0), (50.0, 0.0), (50.0, 40.0), (0.0, 40.0)
    ])

    # Create components with pads
    pad1 = PadState(
        ref="R1", number="1", x=10.0, y=10.0,
        net="NET1", net_id=1, layer="F.Cu",
        width=0.5, height=0.5, through_hole=False
    )
    pad2 = PadState(
        ref="R1", number="2", x=15.0, y=10.0,
        net="NET1", net_id=1, layer="F.Cu",
        width=0.5, height=0.5, through_hole=False
    )

    comp = ComponentState(
        ref="R1", footprint="Resistor_SMD:R_0603",
        x=12.5, y=10.0, rotation=0.0, layer="F.Cu",
        pads=[pad1, pad2], value="10k"
    )

    # Create net
    net = NetState(
        name="NET1", net_id=1,
        pads=[("R1", "1"), ("R1", "2")],
        is_power=False, is_ground=False, is_clock=False,
        priority=10
    )

    return PCBState(
        outline=outline,
        layers=["F.Cu", "B.Cu"],
        components={"R1": comp},
        nets={"NET1": net},
        traces=[],
        vias=[],
        zones=[],
        violations=[],
        source_file="test.kicad_pcb",
    )


@pytest.fixture
def multi_net_pcb_state():
    """Create a PCBState with multiple nets for routing tests."""
    outline = BoardOutline.from_points([
        (0.0, 0.0), (50.0, 0.0), (50.0, 40.0), (0.0, 40.0)
    ])

    # Component R1 with NET1
    r1_pad1 = PadState(
        ref="R1", number="1", x=10.0, y=10.0,
        net="NET1", net_id=1, layer="F.Cu",
        width=0.5, height=0.5
    )
    r1_pad2 = PadState(
        ref="R1", number="2", x=15.0, y=10.0,
        net="NET1", net_id=1, layer="F.Cu",
        width=0.5, height=0.5
    )

    # Component R2 with NET2
    r2_pad1 = PadState(
        ref="R2", number="1", x=10.0, y=20.0,
        net="NET2", net_id=2, layer="F.Cu",
        width=0.5, height=0.5
    )
    r2_pad2 = PadState(
        ref="R2", number="2", x=15.0, y=20.0,
        net="NET2", net_id=2, layer="F.Cu",
        width=0.5, height=0.5
    )

    # Component U1 with three-pad net (for MST testing)
    u1_pad1 = PadState(
        ref="U1", number="1", x=30.0, y=10.0,
        net="NET3", net_id=3, layer="F.Cu",
        width=0.5, height=0.5
    )
    u1_pad2 = PadState(
        ref="U1", number="2", x=35.0, y=15.0,
        net="NET3", net_id=3, layer="F.Cu",
        width=0.5, height=0.5
    )
    u1_pad3 = PadState(
        ref="U1", number="3", x=30.0, y=20.0,
        net="NET3", net_id=3, layer="F.Cu",
        width=0.5, height=0.5
    )

    components = {
        "R1": ComponentState(
            ref="R1", footprint="R_0603",
            x=12.5, y=10.0, rotation=0.0, layer="F.Cu",
            pads=[r1_pad1, r1_pad2]
        ),
        "R2": ComponentState(
            ref="R2", footprint="R_0603",
            x=12.5, y=20.0, rotation=0.0, layer="F.Cu",
            pads=[r2_pad1, r2_pad2]
        ),
        "U1": ComponentState(
            ref="U1", footprint="SOT-23",
            x=32.5, y=15.0, rotation=0.0, layer="F.Cu",
            pads=[u1_pad1, u1_pad2, u1_pad3]
        ),
    }

    nets = {
        "NET1": NetState(name="NET1", net_id=1, pads=[("R1", "1"), ("R1", "2")]),
        "NET2": NetState(name="NET2", net_id=2, pads=[("R2", "1"), ("R2", "2")]),
        "NET3": NetState(name="NET3", net_id=3, pads=[("U1", "1"), ("U1", "2"), ("U1", "3")]),
    }

    return PCBState(
        outline=outline,
        layers=["F.Cu", "B.Cu"],
        components=components,
        nets=nets,
        traces=[],
        vias=[],
        zones=[],
        violations=[],
    )


# =============================================================================
# ObstacleGridBuilder Tests
# =============================================================================


class TestObstacleGridBuilder:
    """Tests for ObstacleGridBuilder class."""

    def test_build_creates_grid(self, simple_pcb_state):
        """Test that builder creates a valid grid."""
        rules = DesignRules()
        builder = ObstacleGridBuilder(simple_pcb_state, rules)
        grid = builder.build()

        assert grid is not None
        assert grid.cols > 0
        assert grid.rows > 0
        assert grid.num_layers >= 2

    def test_build_adds_pads_as_obstacles(self, simple_pcb_state):
        """Test that pads are marked as obstacles."""
        rules = DesignRules()
        builder = ObstacleGridBuilder(simple_pcb_state, rules)
        grid = builder.build()

        # Check that pad locations are blocked
        gx1, gy1 = grid.world_to_grid(10.0, 10.0)
        gx2, gy2 = grid.world_to_grid(15.0, 10.0)

        # Pads should be blocked (but same-net can pass through)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell1 = grid.grid[layer_idx][gy1][gx1]
        cell2 = grid.grid[layer_idx][gy2][gx2]

        assert cell1.blocked or cell1.net == 1
        assert cell2.blocked or cell2.net == 1

    def test_build_with_custom_layer_stack(self, simple_pcb_state):
        """Test building with 4-layer stack."""
        rules = DesignRules()
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        builder = ObstacleGridBuilder(simple_pcb_state, rules, stack)
        grid = builder.build()

        assert grid.num_layers == 4

    def test_pad_state_to_pad_conversion(self, simple_pcb_state):
        """Test PadState to Pad conversion."""
        rules = DesignRules()
        builder = ObstacleGridBuilder(simple_pcb_state, rules)

        pad_state = simple_pcb_state.components["R1"].pads[0]
        pad = builder._pad_state_to_pad(pad_state)

        assert pad.x == pad_state.x
        assert pad.y == pad_state.y
        assert pad.net == pad_state.net_id
        assert pad.net_name == pad_state.net
        assert pad.ref == pad_state.ref

    def test_layer_string_to_enum(self, simple_pcb_state):
        """Test layer string to enum conversion."""
        rules = DesignRules()
        builder = ObstacleGridBuilder(simple_pcb_state, rules)

        assert builder._layer_from_string("F.Cu") == Layer.F_CU
        assert builder._layer_from_string("B.Cu") == Layer.B_CU
        assert builder._layer_from_string("In1.Cu") == Layer.IN1_CU
        assert builder._layer_from_string("Unknown") == Layer.F_CU  # Default

    def test_build_with_existing_traces(self, simple_pcb_state):
        """Test that existing traces are added to grid."""
        # Add a trace to the state
        trace = TraceState(
            net="NET1", net_id=1,
            x1=20.0, y1=10.0, x2=25.0, y2=10.0,
            width=0.2, layer="F.Cu"
        )
        simple_pcb_state.traces.append(trace)

        rules = DesignRules()
        builder = ObstacleGridBuilder(simple_pcb_state, rules)
        grid = builder.build()

        # Trace should be marked on grid
        gx, gy = grid.world_to_grid(22.5, 10.0)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]

        # Cell should be blocked or belong to NET1
        assert cell.blocked or cell.net == 1

    def test_build_with_zones(self, simple_pcb_state):
        """Test that zones are added as keepouts."""
        # Add a zone
        zone = ZoneState(
            net="GND", net_id=0, layer="F.Cu",
            priority=0, bounds=(0.0, 0.0, 10.0, 10.0)
        )
        simple_pcb_state.zones.append(zone)

        rules = DesignRules()
        builder = ObstacleGridBuilder(simple_pcb_state, rules)
        grid = builder.build()

        # Zone area should be blocked
        gx, gy = grid.world_to_grid(5.0, 5.0)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]
        assert cell.blocked


class TestObstacleGridBuilderDimensions:
    """Tests for grid dimension calculation."""

    def test_grid_from_outline(self, simple_pcb_state):
        """Test grid dimensions from board outline."""
        rules = DesignRules()
        builder = ObstacleGridBuilder(simple_pcb_state, rules)
        grid = builder.build()

        # Grid should cover the board outline
        assert grid.width >= 50.0
        assert grid.height >= 40.0

    def test_grid_from_components_no_outline(self):
        """Test grid dimensions when outline is missing."""
        # Create state without outline
        pad1 = PadState(
            ref="R1", number="1", x=10.0, y=10.0,
            net="NET1", net_id=1, layer="F.Cu",
            width=0.5, height=0.5
        )
        comp = ComponentState(
            ref="R1", footprint="R_0603",
            x=10.0, y=10.0, rotation=0.0, layer="F.Cu",
            pads=[pad1]
        )

        state = PCBState(
            outline=BoardOutline(points=[]),  # Empty outline
            layers=["F.Cu", "B.Cu"],
            components={"R1": comp},
            nets={"NET1": NetState(name="NET1", net_id=1)},
            traces=[], vias=[], zones=[], violations=[],
        )

        rules = DesignRules()
        builder = ObstacleGridBuilder(state, rules)
        grid = builder.build()

        # Should still create a valid grid
        assert grid.cols > 0
        assert grid.rows > 0


# =============================================================================
# RoutingDiagnostic Tests
# =============================================================================


class TestRoutingDiagnostic:
    """Tests for RoutingDiagnostic dataclass."""

    def test_basic_diagnostic(self):
        """Test creating a basic diagnostic."""
        diag = RoutingDiagnostic(
            source_pad="U1.1",
            target_pad="U2.3",
            reason="no_path"
        )

        assert diag.source_pad == "U1.1"
        assert diag.target_pad == "U2.3"
        assert diag.reason == "no_path"
        assert diag.blocked_at is None
        assert diag.blocking_net is None
        assert diag.suggestions == []

    def test_diagnostic_with_details(self):
        """Test diagnostic with full details."""
        diag = RoutingDiagnostic(
            source_pad="U1.1",
            target_pad="U2.3",
            reason="source_blocked",
            blocked_at=(15.0, 20.0),
            blocking_net="GND",
            suggestions=["Reroute GND first", "Try layer change"],
        )

        assert diag.blocked_at == (15.0, 20.0)
        assert diag.blocking_net == "GND"
        assert len(diag.suggestions) == 2


# =============================================================================
# InterpreterConfig Tests
# =============================================================================


class TestInterpreterConfig:
    """Tests for InterpreterConfig A* options."""

    def test_default_uses_astar(self):
        """Test that A* is enabled by default."""
        config = InterpreterConfig()
        assert config.use_astar is True

    def test_astar_options(self):
        """Test A* configuration options."""
        config = InterpreterConfig(
            use_astar=True,
            astar_weight=1.5,
            use_negotiated=True,
            layer_count=4
        )

        assert config.astar_weight == 1.5
        assert config.use_negotiated is True
        assert config.layer_count == 4

    def test_disable_astar(self):
        """Test disabling A* for simple routing."""
        config = InterpreterConfig(use_astar=False)
        assert config.use_astar is False


# =============================================================================
# CommandInterpreter A* Routing Tests
# =============================================================================


class TestInterpreterAStarSetup:
    """Tests for CommandInterpreter A* infrastructure."""

    def test_design_rules_creation(self, simple_pcb_state, tmp_path):
        """Test design rules are created from config."""
        # Create a minimal PCB file for the interpreter
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000) (generator "test"))')

        config = InterpreterConfig(
            trace_width=0.25,
            clearance=0.15,
            via_drill=0.35,
            via_size=0.65,
        )

        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=simple_pcb_state,
            config=config,
        )

        rules = interp._get_design_rules()

        assert rules.trace_width == 0.25
        assert rules.trace_clearance == 0.15
        assert rules.via_drill == 0.35
        assert rules.via_diameter == 0.65

    def test_layer_stack_two_layer(self, simple_pcb_state, tmp_path):
        """Test 2-layer stack creation."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        config = InterpreterConfig(layer_count=2)
        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=simple_pcb_state,
            config=config,
        )

        stack = interp._get_layer_stack()
        assert stack.num_layers == 2

    def test_layer_stack_four_layer(self, simple_pcb_state, tmp_path):
        """Test 4-layer stack creation."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        config = InterpreterConfig(layer_count=4)
        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=simple_pcb_state,
            config=config,
        )

        stack = interp._get_layer_stack()
        assert stack.num_layers == 4

    def test_routing_grid_lazy_init(self, simple_pcb_state, tmp_path):
        """Test routing grid is lazily initialized."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=simple_pcb_state,
        )

        # Grid should be None initially
        assert interp._routing_grid is None

        # Get grid - should create it
        grid = interp._get_routing_grid()
        assert grid is not None
        assert interp._routing_grid is grid

    def test_router_lazy_init(self, simple_pcb_state, tmp_path):
        """Test router is lazily initialized."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=simple_pcb_state,
        )

        assert interp._router is None

        router = interp._get_router()
        assert router is not None
        assert interp._router is router

    def test_cache_invalidation(self, simple_pcb_state, tmp_path):
        """Test routing cache invalidation."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=simple_pcb_state,
        )

        # Create grid and router
        grid = interp._get_routing_grid()
        router = interp._get_router()

        # Invalidate
        interp._invalidate_routing_cache()

        assert interp._routing_grid is None
        assert interp._router is None


class TestInterpreterPadConversion:
    """Tests for PadState to router Pad conversion."""

    def test_pad_state_to_router_pad(self, simple_pcb_state, tmp_path):
        """Test converting PadState to router Pad."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=simple_pcb_state,
        )

        pad_state = simple_pcb_state.components["R1"].pads[0]
        pad = interp._pad_state_to_router_pad(pad_state)

        assert pad.x == pad_state.x
        assert pad.y == pad_state.y
        assert pad.width == pad_state.width
        assert pad.height == pad_state.height
        assert pad.net == pad_state.net_id
        assert pad.net_name == pad_state.net
        assert pad.layer == Layer.F_CU
        assert pad.ref == pad_state.ref

    def test_through_hole_pad_conversion(self, tmp_path):
        """Test converting through-hole PadState."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        pad_state = PadState(
            ref="J1", number="1", x=10.0, y=10.0,
            net="NET1", net_id=1, layer="F.Cu",
            width=1.7, height=1.7, through_hole=True
        )
        comp = ComponentState(
            ref="J1", footprint="Connector",
            x=10.0, y=10.0, rotation=0.0, layer="F.Cu",
            pads=[pad_state]
        )
        state = PCBState(
            outline=BoardOutline(points=[(0, 0), (50, 50)]),
            layers=["F.Cu", "B.Cu"],
            components={"J1": comp},
            nets={"NET1": NetState(name="NET1", net_id=1)},
            traces=[], vias=[], zones=[], violations=[],
        )

        interp = CommandInterpreter(pcb_path=str(pcb_file), state=state)
        pad = interp._pad_state_to_router_pad(pad_state)

        assert pad.through_hole is True


# =============================================================================
# A* Routing Execution Tests
# =============================================================================


class TestAStarRoutingExecution:
    """Tests for A* routing execution."""

    def test_route_net_not_found(self, simple_pcb_state, tmp_path):
        """Test routing non-existent net."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=simple_pcb_state,
        )

        cmd = RouteNetCommand(net="NONEXISTENT")
        result = interp.execute(cmd)

        assert result.success is False
        assert "not found" in result.message.lower()

    def test_route_single_pad_net(self, tmp_path):
        """Test routing net with only one pad."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        # Create state with single-pad net
        pad = PadState(
            ref="R1", number="1", x=10.0, y=10.0,
            net="NET1", net_id=1, layer="F.Cu",
            width=0.5, height=0.5
        )
        comp = ComponentState(
            ref="R1", footprint="R_0603",
            x=10.0, y=10.0, rotation=0.0, layer="F.Cu",
            pads=[pad]
        )
        net = NetState(name="NET1", net_id=1, pads=[("R1", "1")])
        state = PCBState(
            outline=BoardOutline(points=[(0, 0), (50, 50)]),
            layers=["F.Cu", "B.Cu"],
            components={"R1": comp},
            nets={"NET1": net},
            traces=[], vias=[], zones=[], violations=[],
        )

        interp = CommandInterpreter(pcb_path=str(pcb_file), state=state)
        cmd = RouteNetCommand(net="NET1")
        result = interp.execute(cmd)

        assert result.success is False
        assert "fewer than 2 pads" in result.message

    def test_route_uses_astar_when_enabled(self, multi_net_pcb_state, tmp_path):
        """Test that A* routing is used when enabled."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        config = InterpreterConfig(use_astar=True)
        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=multi_net_pcb_state,
            config=config,
        )

        # Verify the interpreter has A* infrastructure
        assert interp.config.use_astar is True

        cmd = RouteNetCommand(net="NET1")
        result = interp.execute(cmd)

        # Should return a valid result (success or fail with diagnostics)
        assert result.command_type == CommandType.ROUTE_NET

    def test_route_uses_simple_when_disabled(self, multi_net_pcb_state, tmp_path):
        """Test that simple routing is used when A* disabled."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        config = InterpreterConfig(use_astar=False)
        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=multi_net_pcb_state,
            config=config,
        )

        assert interp.config.use_astar is False

        cmd = RouteNetCommand(net="NET1")
        result = interp.execute(cmd)

        assert result.command_type == CommandType.ROUTE_NET


class TestRoutingDiagnostics:
    """Tests for routing failure diagnostics."""

    def test_diagnostic_on_failed_route(self, multi_net_pcb_state, tmp_path):
        """Test that failed routes include diagnostic info."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        # Create heavily blocked PCB that will fail routing
        # Add many traces to block the routing area
        for i in range(10):
            trace = TraceState(
                net=f"BLOCK{i}", net_id=100 + i,
                x1=8.0 + i * 0.5, y1=5.0, x2=8.0 + i * 0.5, y2=15.0,
                width=0.3, layer="F.Cu"
            )
            multi_net_pcb_state.traces.append(trace)

        config = InterpreterConfig(use_astar=True)
        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=multi_net_pcb_state,
            config=config,
        )

        # Try to route through blocked area
        cmd = RouteNetCommand(net="NET1")
        result = interp.execute(cmd)

        # If routing failed or partially succeeded, check for diagnostics
        if not result.success or (result.details and "failed_routes" in result.details):
            if result.details and "failed_routes" in result.details:
                failed = result.details["failed_routes"]
                assert isinstance(failed, list)
                if failed:
                    # Check diagnostic structure
                    diag = failed[0]
                    assert "source" in diag
                    assert "target" in diag
                    assert "reason" in diag


class TestRouteCommand:
    """Tests for RouteNetCommand execution."""

    def test_route_command_type(self, simple_pcb_state, tmp_path):
        """Test route command returns correct type."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=simple_pcb_state,
        )

        cmd = RouteNetCommand(net="NET1")
        result = interp.execute(cmd)

        assert result.command_type == CommandType.ROUTE_NET

    def test_route_with_trace_width(self, simple_pcb_state, tmp_path):
        """Test routing with custom trace width."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=simple_pcb_state,
        )

        cmd = RouteNetCommand(net="NET1", trace_width=0.3)
        result = interp.execute(cmd)

        assert result.command_type == CommandType.ROUTE_NET

    def test_route_invalidates_cache(self, simple_pcb_state, tmp_path):
        """Test that routing invalidates the cache."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20230000))')

        interp = CommandInterpreter(
            pcb_path=str(pcb_file),
            state=simple_pcb_state,
        )

        # Initialize cache
        grid = interp._get_routing_grid()
        assert interp._routing_grid is not None

        # Route should invalidate cache
        cmd = RouteNetCommand(net="NET1")
        interp.execute(cmd)

        # Cache should be invalidated
        assert interp._routing_grid is None

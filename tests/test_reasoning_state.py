"""Tests for reasoning/state.py module to increase coverage.

Tests for:
- PadState, ComponentState, TraceState, ViaState, ZoneState
- ViolationState, NetState, BoardOutline
- PCBState parsing and query methods
"""

import pytest

from kicad_tools.reasoning.state import (
    BoardOutline,
    ComponentState,
    NetState,
    PadState,
    PCBState,
    TraceState,
    ViaState,
    ViolationState,
    ZoneState,
)


class TestPadState:
    """Tests for PadState dataclass."""

    def test_pad_creation(self):
        """Create pad with all attributes."""
        pad = PadState(
            ref="U1",
            number="1",
            x=10.0,
            y=20.0,
            net="VCC",
            net_id=1,
            layer="F.Cu",
            width=0.5,
            height=0.5,
            through_hole=False,
        )
        assert pad.ref == "U1"
        assert pad.number == "1"
        assert pad.x == 10.0
        assert pad.y == 20.0
        assert pad.net == "VCC"
        assert pad.net_id == 1
        assert pad.layer == "F.Cu"
        assert pad.width == 0.5
        assert pad.height == 0.5
        assert pad.through_hole is False

    def test_pad_position(self):
        """Pad position property."""
        pad = PadState(
            ref="U1",
            number="1",
            x=10.0,
            y=20.0,
            net="VCC",
            net_id=1,
            layer="F.Cu",
            width=0.5,
            height=0.5,
        )
        assert pad.position == (10.0, 20.0)

    def test_pad_distance_to(self):
        """Pad distance to another pad (Manhattan distance)."""
        pad1 = PadState(
            ref="U1",
            number="1",
            x=10.0,
            y=20.0,
            net="VCC",
            net_id=1,
            layer="F.Cu",
            width=0.5,
            height=0.5,
        )
        pad2 = PadState(
            ref="U2",
            number="1",
            x=15.0,
            y=30.0,
            net="VCC",
            net_id=1,
            layer="F.Cu",
            width=0.5,
            height=0.5,
        )
        assert pad1.distance_to(pad2) == 15.0  # |15-10| + |30-20| = 5 + 10 = 15

    def test_pad_through_hole_default(self):
        """Through hole defaults to False."""
        pad = PadState(
            ref="U1",
            number="1",
            x=10.0,
            y=20.0,
            net="VCC",
            net_id=1,
            layer="F.Cu",
            width=0.5,
            height=0.5,
        )
        assert pad.through_hole is False


class TestComponentState:
    """Tests for ComponentState dataclass."""

    def test_component_creation(self):
        """Create component with all attributes."""
        comp = ComponentState(
            ref="U1",
            footprint="Package_SO:SOIC-8",
            x=50.0,
            y=60.0,
            rotation=90.0,
            layer="F.Cu",
            value="LM7805",
        )
        assert comp.ref == "U1"
        assert comp.footprint == "Package_SO:SOIC-8"
        assert comp.x == 50.0
        assert comp.y == 60.0
        assert comp.rotation == 90.0
        assert comp.layer == "F.Cu"
        assert comp.value == "LM7805"

    def test_component_position(self):
        """Component position property."""
        comp = ComponentState(
            ref="U1", footprint="SOIC-8", x=50.0, y=60.0, rotation=0, layer="F.Cu"
        )
        assert comp.position == (50.0, 60.0)

    def test_component_bounds_no_pads(self):
        """Component bounds without pads."""
        comp = ComponentState(
            ref="U1", footprint="SOIC-8", x=50.0, y=60.0, rotation=0, layer="F.Cu"
        )
        # Should return default 1mm around center
        bounds = comp.bounds
        assert bounds == (49.0, 59.0, 51.0, 61.0)

    def test_component_bounds_with_pads(self):
        """Component bounds calculated from pads."""
        pads = [
            PadState(
                ref="U1",
                number="1",
                x=45.0,
                y=55.0,
                net="",
                net_id=0,
                layer="F.Cu",
                width=0.5,
                height=0.5,
            ),
            PadState(
                ref="U1",
                number="2",
                x=55.0,
                y=65.0,
                net="",
                net_id=0,
                layer="F.Cu",
                width=0.5,
                height=0.5,
            ),
        ]
        comp = ComponentState(
            ref="U1", footprint="SOIC-8", x=50.0, y=60.0, rotation=0, layer="F.Cu", pads=pads
        )
        bounds = comp.bounds
        assert bounds == (44.5, 54.5, 55.5, 65.5)  # With 0.5 margin

    def test_component_fixed_default(self):
        """Fixed property defaults to False."""
        comp = ComponentState(
            ref="U1", footprint="SOIC-8", x=50.0, y=60.0, rotation=0, layer="F.Cu"
        )
        assert comp.fixed is False


class TestTraceState:
    """Tests for TraceState dataclass."""

    def test_trace_creation(self):
        """Create trace with all attributes."""
        trace = TraceState(
            net="VCC",
            net_id=1,
            x1=10.0,
            y1=20.0,
            x2=30.0,
            y2=20.0,
            width=0.25,
            layer="F.Cu",
            uuid="trace-uuid",
        )
        assert trace.net == "VCC"
        assert trace.net_id == 1
        assert trace.x1 == 10.0
        assert trace.y1 == 20.0
        assert trace.x2 == 30.0
        assert trace.y2 == 20.0
        assert trace.width == 0.25
        assert trace.layer == "F.Cu"

    def test_trace_length(self):
        """Trace length (Euclidean distance)."""
        # Horizontal trace
        trace = TraceState(
            net="VCC", net_id=1, x1=10.0, y1=20.0, x2=30.0, y2=20.0, width=0.25, layer="F.Cu"
        )
        assert trace.length == 20.0

        # 3-4-5 triangle
        trace2 = TraceState(
            net="VCC", net_id=1, x1=0.0, y1=0.0, x2=3.0, y2=4.0, width=0.25, layer="F.Cu"
        )
        assert trace2.length == 5.0

    def test_trace_start_end(self):
        """Trace start and end properties."""
        trace = TraceState(
            net="VCC", net_id=1, x1=10.0, y1=20.0, x2=30.0, y2=40.0, width=0.25, layer="F.Cu"
        )
        assert trace.start == (10.0, 20.0)
        assert trace.end == (30.0, 40.0)


class TestViaState:
    """Tests for ViaState dataclass."""

    def test_via_creation(self):
        """Create via with all attributes."""
        via = ViaState(net="VCC", net_id=1, x=50.0, y=60.0, size=0.6, drill=0.3, uuid="via-uuid")
        assert via.net == "VCC"
        assert via.net_id == 1
        assert via.x == 50.0
        assert via.y == 60.0
        assert via.size == 0.6
        assert via.drill == 0.3

    def test_via_position(self):
        """Via position property."""
        via = ViaState(net="VCC", net_id=1, x=50.0, y=60.0, size=0.6, drill=0.3)
        assert via.position == (50.0, 60.0)

    def test_via_layers_default(self):
        """Via layers default to F.Cu/B.Cu."""
        via = ViaState(net="VCC", net_id=1, x=50.0, y=60.0, size=0.6, drill=0.3)
        assert via.layers == ("F.Cu", "B.Cu")


class TestZoneState:
    """Tests for ZoneState dataclass."""

    def test_zone_creation(self):
        """Create zone with all attributes."""
        zone = ZoneState(
            net="GND",
            net_id=1,
            layer="F.Cu",
            priority=1,
            bounds=(0.0, 0.0, 100.0, 100.0),
            filled=True,
        )
        assert zone.net == "GND"
        assert zone.net_id == 1
        assert zone.layer == "F.Cu"
        assert zone.priority == 1
        assert zone.bounds == (0.0, 0.0, 100.0, 100.0)
        assert zone.filled is True


class TestViolationState:
    """Tests for ViolationState dataclass."""

    def test_violation_creation(self):
        """Create violation with all attributes."""
        violation = ViolationState(
            type="clearance",
            severity="error",
            message="Clearance violation between traces",
            x=50.0,
            y=60.0,
            layer="F.Cu",
            nets=["VCC", "GND"],
            items=["trace1", "trace2"],
        )
        assert violation.type == "clearance"
        assert violation.severity == "error"
        assert violation.message == "Clearance violation between traces"
        assert violation.x == 50.0
        assert violation.y == 60.0

    def test_violation_is_error(self):
        """Violation is_error property."""
        error = ViolationState(type="clearance", severity="error", message="Error", x=0, y=0)
        assert error.is_error is True

        warning = ViolationState(type="clearance", severity="warning", message="Warning", x=0, y=0)
        assert warning.is_error is False

    def test_violation_position(self):
        """Violation position property."""
        violation = ViolationState(
            type="clearance", severity="error", message="Error", x=50.0, y=60.0
        )
        assert violation.position == (50.0, 60.0)


class TestNetState:
    """Tests for NetState dataclass."""

    def test_net_creation(self):
        """Create net with all attributes."""
        net = NetState(
            name="VCC",
            net_id=1,
            pads=[("U1", "1"), ("U2", "4")],
            is_power=True,
            priority=2,
        )
        assert net.name == "VCC"
        assert net.net_id == 1
        assert net.pad_count == 2
        assert net.is_power is True
        assert net.priority == 2

    def test_net_is_routed_false(self):
        """Net is not routed when no traces."""
        net = NetState(name="VCC", net_id=1)
        assert net.is_routed is False

    def test_net_is_routed_true(self):
        """Net is routed when has traces."""
        trace = TraceState(net="VCC", net_id=1, x1=0, y1=0, x2=10, y2=0, width=0.25, layer="F.Cu")
        net = NetState(name="VCC", net_id=1, traces=[trace])
        assert net.is_routed is True

    def test_net_total_trace_length(self):
        """Net total trace length."""
        traces = [
            TraceState(
                net="VCC", net_id=1, x1=0, y1=0, x2=10, y2=0, width=0.25, layer="F.Cu"
            ),  # 10mm
            TraceState(
                net="VCC", net_id=1, x1=10, y1=0, x2=10, y2=5, width=0.25, layer="F.Cu"
            ),  # 5mm
        ]
        net = NetState(name="VCC", net_id=1, traces=traces)
        assert net.total_trace_length == 15.0

    def test_net_classifications(self):
        """Net type classifications."""
        ground = NetState(name="GND", net_id=1, is_ground=True)
        power = NetState(name="VCC", net_id=2, is_power=True)
        clock = NetState(name="MCLK", net_id=3, is_clock=True)

        assert ground.is_ground is True
        assert power.is_power is True
        assert clock.is_clock is True


class TestBoardOutline:
    """Tests for BoardOutline dataclass."""

    def test_outline_from_points(self):
        """Create outline from points."""
        points = [(0, 0), (100, 0), (100, 50), (0, 50)]
        outline = BoardOutline.from_points(points)

        assert outline.width == 100.0
        assert outline.height == 50.0
        assert outline.center_x == 50.0
        assert outline.center_y == 25.0

    def test_outline_from_empty_points(self):
        """Create outline from empty points."""
        outline = BoardOutline.from_points([])
        assert outline.width == 0.0
        assert outline.height == 0.0


class TestPCBStateQueries:
    """Tests for PCBState query methods."""

    @pytest.fixture
    def sample_pcb_state(self):
        """Create a sample PCBState for testing."""
        pads = [
            PadState(
                ref="U1",
                number="1",
                x=45.0,
                y=55.0,
                net="VCC",
                net_id=1,
                layer="F.Cu",
                width=0.5,
                height=0.5,
            ),
            PadState(
                ref="U1",
                number="2",
                x=55.0,
                y=55.0,
                net="GND",
                net_id=2,
                layer="F.Cu",
                width=0.5,
                height=0.5,
            ),
        ]
        components = {
            "U1": ComponentState(
                ref="U1", footprint="SOIC-8", x=50.0, y=55.0, rotation=0, layer="F.Cu", pads=pads
            ),
            "R1": ComponentState(
                ref="R1", footprint="0402", x=60.0, y=60.0, rotation=0, layer="F.Cu"
            ),
        }

        nets = {
            "VCC": NetState(name="VCC", net_id=1, is_power=True, pads=[("U1", "1")]),
            "GND": NetState(name="GND", net_id=2, is_ground=True, pads=[("U1", "2")]),
            "SIG1": NetState(name="SIG1", net_id=3, pads=[("U1", "3"), ("R1", "1")]),
        }

        traces = [
            TraceState(
                net="VCC", net_id=1, x1=45.0, y1=55.0, x2=40.0, y2=55.0, width=0.25, layer="F.Cu"
            ),
        ]
        # Add trace to net
        nets["VCC"].traces = traces.copy()

        violations = [
            ViolationState(type="clearance", severity="error", message="Clearance", x=50.0, y=55.0),
            ViolationState(
                type="shorting_items", severity="error", message="Short", x=60.0, y=60.0
            ),
            ViolationState(
                type="unconnected_items", severity="warning", message="Unconnected", x=70.0, y=70.0
            ),
        ]

        outline = BoardOutline.from_points([(0, 0), (100, 0), (100, 80), (0, 80)])

        return PCBState(
            outline=outline,
            layers=["F.Cu", "B.Cu"],
            components=components,
            nets=nets,
            traces=traces,
            vias=[],
            zones=[],
            violations=violations,
        )

    def test_get_component(self, sample_pcb_state):
        """Get component by reference."""
        comp = sample_pcb_state.get_component("U1")
        assert comp is not None
        assert comp.ref == "U1"

    def test_get_component_not_found(self, sample_pcb_state):
        """Get component returns None when not found."""
        comp = sample_pcb_state.get_component("NONEXISTENT")
        assert comp is None

    def test_get_net(self, sample_pcb_state):
        """Get net by name."""
        net = sample_pcb_state.get_net("VCC")
        assert net is not None
        assert net.name == "VCC"
        assert net.is_power is True

    def test_get_net_not_found(self, sample_pcb_state):
        """Get net returns None when not found."""
        net = sample_pcb_state.get_net("NONEXISTENT")
        assert net is None

    def test_get_pad(self, sample_pcb_state):
        """Get specific pad."""
        pad = sample_pcb_state.get_pad("U1", "1")
        assert pad is not None
        assert pad.number == "1"
        assert pad.net == "VCC"

    def test_get_pad_not_found(self, sample_pcb_state):
        """Get pad returns None when not found."""
        pad = sample_pcb_state.get_pad("U1", "999")
        assert pad is None

        pad = sample_pcb_state.get_pad("NONEXISTENT", "1")
        assert pad is None

    def test_components_near(self, sample_pcb_state):
        """Find components near a point."""
        comps = sample_pcb_state.components_near(50.0, 55.0, radius=5)
        assert len(comps) == 1
        assert comps[0].ref == "U1"

    def test_components_near_larger_radius(self, sample_pcb_state):
        """Find multiple components with larger radius."""
        comps = sample_pcb_state.components_near(55.0, 57.5, radius=15)
        assert len(comps) == 2

    def test_violations_near(self, sample_pcb_state):
        """Find violations near a point."""
        violations = sample_pcb_state.violations_near(50.0, 55.0, radius=5)
        assert len(violations) == 1
        assert violations[0].type == "clearance"


class TestPCBStateStatistics:
    """Tests for PCBState statistics properties."""

    @pytest.fixture
    def sample_pcb_state(self):
        """Create sample PCBState."""
        nets = {
            "VCC": NetState(name="VCC", net_id=1, is_power=True, pads=[("U1", "1"), ("U2", "1")]),
            "GND": NetState(name="GND", net_id=2, is_ground=True, pads=[("U1", "2"), ("U2", "2")]),
            "SIG1": NetState(name="SIG1", net_id=3, pads=[("U1", "3"), ("R1", "1")]),
        }
        # Add traces to some nets
        nets["VCC"].traces = [
            TraceState(net="VCC", net_id=1, x1=0, y1=0, x2=10, y2=0, width=0.25, layer="F.Cu")
        ]

        violations = [
            ViolationState(type="clearance", severity="error", message="Clear", x=0, y=0),
            ViolationState(type="shorting_items", severity="error", message="Short", x=10, y=10),
            ViolationState(
                type="unconnected_items", severity="warning", message="Uncon", x=20, y=20
            ),
        ]

        outline = BoardOutline.from_points([(0, 0), (100, 0), (100, 80), (0, 80)])

        return PCBState(
            outline=outline,
            layers=["F.Cu", "B.Cu"],
            components={},
            nets=nets,
            traces=nets["VCC"].traces,
            vias=[],
            zones=[],
            violations=violations,
        )

    def test_unrouted_nets(self, sample_pcb_state):
        """Get unrouted nets."""
        unrouted = sample_pcb_state.unrouted_nets
        assert len(unrouted) == 2  # GND and SIG1
        names = [n.name for n in unrouted]
        assert "GND" in names
        assert "SIG1" in names

    def test_routed_nets(self, sample_pcb_state):
        """Get routed nets."""
        routed = sample_pcb_state.routed_nets
        assert len(routed) == 1
        assert routed[0].name == "VCC"

    def test_shorts(self, sample_pcb_state):
        """Get short-circuit violations."""
        shorts = sample_pcb_state.shorts
        assert len(shorts) == 1
        assert shorts[0].type == "shorting_items"

    def test_clearance_violations(self, sample_pcb_state):
        """Get clearance violations."""
        clearance = sample_pcb_state.clearance_violations
        assert len(clearance) == 1
        assert clearance[0].type == "clearance"

    def test_unconnected_violations(self, sample_pcb_state):
        """Get unconnected violations."""
        unconnected = sample_pcb_state.unconnected_violations
        assert len(unconnected) == 1
        assert unconnected[0].type == "unconnected_items"

    def test_summary(self, sample_pcb_state):
        """Generate summary statistics."""
        summary = sample_pcb_state.summary()
        assert "board_size" in summary
        assert summary["layers"] == 2
        assert summary["nets_total"] == 3
        assert summary["nets_routed"] == 1
        assert summary["nets_unrouted"] == 2
        assert summary["violations_total"] == 3
        assert summary["shorts"] == 1


class TestPCBStatePrompt:
    """Tests for PCBState LLM interface."""

    @pytest.fixture
    def sample_pcb_state(self):
        """Create sample PCBState."""
        nets = {
            "VCC": NetState(name="VCC", net_id=1, is_power=True, priority=2, pads=[("U1", "1")]),
            "GND": NetState(
                name="GND", net_id=2, is_ground=True, priority=1, pads=[("U1", "2"), ("U2", "2")]
            ),
            "MCLK": NetState(
                name="MCLK", net_id=3, is_clock=True, priority=3, pads=[("U1", "3"), ("U2", "3")]
            ),
        }

        violations = [
            ViolationState(
                type="clearance", severity="error", message="Clear", x=50, y=50, nets=["VCC"]
            ),
        ]

        outline = BoardOutline.from_points([(0, 0), (100, 0), (100, 80), (0, 80)])

        return PCBState(
            outline=outline,
            layers=["F.Cu", "B.Cu"],
            components={},
            nets=nets,
            traces=[],
            vias=[],
            zones=[],
            violations=violations,
        )

    def test_to_prompt_includes_board_info(self, sample_pcb_state):
        """Prompt includes board information."""
        prompt = sample_pcb_state.to_prompt()
        assert "PCB State" in prompt
        assert "100.0 x 80.0 mm" in prompt
        assert "F.Cu" in prompt

    def test_to_prompt_includes_routing_progress(self, sample_pcb_state):
        """Prompt includes routing progress."""
        prompt = sample_pcb_state.to_prompt()
        assert "Routing Progress" in prompt
        assert "Nets routed:" in prompt

    def test_to_prompt_includes_unrouted_nets(self, sample_pcb_state):
        """Prompt includes unrouted nets."""
        prompt = sample_pcb_state.to_prompt()
        assert "Unrouted Nets" in prompt
        assert "GND" in prompt
        assert "[GND]" in prompt
        # VCC is routed (no traces but only 1 pad), MCLK is unrouted (2 pads, no traces)
        assert "[CLOCK]" in prompt

    def test_to_prompt_includes_violations(self, sample_pcb_state):
        """Prompt includes violations."""
        prompt = sample_pcb_state.to_prompt()
        assert "DRC Violations" in prompt
        assert "clearance" in prompt

    def test_to_prompt_without_violations(self, sample_pcb_state):
        """Prompt can exclude violations."""
        prompt = sample_pcb_state.to_prompt(include_violations=False)
        assert "DRC Violations" not in prompt


class TestPCBStateParsing:
    """Tests for PCBState parsing from PCB files."""

    def test_from_pcb_minimal(self, minimal_pcb):
        """Parse minimal PCB file."""
        state = PCBState.from_pcb(minimal_pcb)

        assert state is not None
        assert len(state.layers) >= 2
        assert "R1" in state.components
        assert state.source_file == str(minimal_pcb)

    def test_from_pcb_with_zones(self, zone_test_pcb):
        """Parse PCB file with zones."""
        state = PCBState.from_pcb(zone_test_pcb)

        assert len(state.zones) > 0

    def test_from_pcb_routing_test(self, routing_test_pcb):
        """Parse routing test PCB."""
        state = PCBState.from_pcb(routing_test_pcb)

        assert len(state.components) >= 2
        assert len(state.nets) >= 2

    def test_from_pcb_multilayer_board(self):
        """Parse multilayer PCB file with gr_rect outline.

        Regression test for issue #425: Reason agent reports wrong board size
        and layer count. The board uses gr_rect for the outline (not gr_line)
        and has 4 copper layers.
        """
        from pathlib import Path

        pcb_path = Path(__file__).parent / "fixtures/projects/multilayer_zones.kicad_pcb"
        state = PCBState.from_pcb(pcb_path)

        # Board dimensions from gr_rect: (start 100 100) (end 180 160) = 80x60mm
        assert state.outline.width == 80.0
        assert state.outline.height == 60.0

        # Should find 4 copper layers: F.Cu, In1.Cu, In2.Cu, B.Cu
        assert len(state.layers) == 4
        assert "F.Cu" in state.layers
        assert "In1.Cu" in state.layers
        assert "In2.Cu" in state.layers
        assert "B.Cu" in state.layers

        # Verify summary reports correct values
        summary = state.summary()
        assert summary["board_size"] == "80.0 x 60.0 mm"
        assert summary["layers"] == 4

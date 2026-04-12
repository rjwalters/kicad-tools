"""Tests for router I/O, DRC compliance, zone rules, edge clearance, and S-expression output."""

import warnings

import pytest

from kicad_tools.cli.route_cmd import _insert_sexp_before_closing, _validate_sexp_parentheses
from kicad_tools.router.core import Autorouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.heuristics import (
    DIAGONAL_COST,
    CongestionAwareHeuristic,
    GreedyHeuristic,
    HeuristicContext,
    ManhattanHeuristic,
    octile_distance,
)
from kicad_tools.router.io import (
    GridAdjustment,
    GridResolutionError,
    PCBDesignRules,
    _extract_edge_segments,
    adjust_grid_for_compliance,
    generate_netclass_setup,
    load_pcb_for_routing,
    merge_routes_into_pcb,
    parse_pcb_design_rules,
    route_pcb,
    validate_grid_resolution,
    validate_routes,
)
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import (
    NET_CLASS_POWER,
    DesignRules,
    NetClassRouting,
    ZoneRules,
)


class TestRoutePcb:
    """Tests for route_pcb function."""

    def test_route_pcb_basic(self):
        """Test basic route_pcb function."""
        components = [
            {
                "ref": "U1",
                "x": 10.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 5.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ],
            },
            {
                "ref": "R1",
                "x": 30.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ],
            },
        ]
        net_map = {"VCC": 1, "GND": 2}

        sexp, stats = route_pcb(
            board_width=50.0,
            board_height=50.0,
            components=components,
            net_map=net_map,
        )

        # Should return some routing data
        assert isinstance(sexp, str)
        assert isinstance(stats, dict)
        assert "routes" in stats
        assert "segments" in stats
        assert "vias" in stats

    def test_route_pcb_with_rotation(self):
        """Test route_pcb with rotated components."""
        components = [
            {
                "ref": "U1",
                "x": 15.0,
                "y": 15.0,
                "rotation": 90,
                "pads": [
                    {"number": "1", "x": 0.0, "y": -2.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                    {"number": "2", "x": 0.0, "y": 2.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                ],
            },
            {
                "ref": "R1",
                "x": 35.0,
                "y": 15.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ],
            },
        ]
        net_map = {"SIG": 1, "GND": 2}

        sexp, stats = route_pcb(
            board_width=50.0,
            board_height=50.0,
            components=components,
            net_map=net_map,
        )

        assert isinstance(sexp, str)
        assert stats["routes"] >= 0

    def test_route_pcb_skip_nets(self):
        """Test route_pcb with skip_nets parameter."""
        components = [
            {
                "ref": "U1",
                "x": 10.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                    {"number": "3", "x": 4.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                ],
            },
            {
                "ref": "R1",
                "x": 30.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                ],
            },
        ]
        net_map = {"VCC": 1, "GND": 2, "SIG": 3}

        # Skip VCC and GND (power/ground planes)
        sexp, stats = route_pcb(
            board_width=50.0,
            board_height=50.0,
            components=components,
            net_map=net_map,
            skip_nets=["VCC", "GND"],
        )

        assert isinstance(sexp, str)

    def test_route_pcb_with_origin(self):
        """Test route_pcb with custom origin."""
        components = [
            {
                "ref": "U1",
                "x": 110.0,
                "y": 60.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                    {"number": "2", "x": 5.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                ],
            },
        ]
        net_map = {"NET1": 1}

        sexp, stats = route_pcb(
            board_width=50.0,
            board_height=50.0,
            components=components,
            net_map=net_map,
            origin_x=100.0,
            origin_y=50.0,
        )

        assert isinstance(sexp, str)

    def test_route_pcb_assigns_new_net_numbers(self):
        """Test that route_pcb assigns net numbers for unknown nets."""
        components = [
            {
                "ref": "U1",
                "x": 10.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {
                        "number": "1",
                        "x": 0.0,
                        "y": 0.0,
                        "width": 0.5,
                        "height": 0.5,
                        "net": "NEW_NET",
                    },
                    {
                        "number": "2",
                        "x": 5.0,
                        "y": 0.0,
                        "width": 0.5,
                        "height": 0.5,
                        "net": "NEW_NET",
                    },
                ],
            },
        ]
        net_map = {}  # Empty net map

        sexp, stats = route_pcb(
            board_width=50.0,
            board_height=50.0,
            components=components,
            net_map=net_map,
        )

        # Net map should have been updated
        assert "NEW_NET" in net_map


class TestLoadPcbForRouting:
    """Tests for load_pcb_for_routing function."""

    def test_load_pcb_basic(self, routing_test_pcb):
        """Test loading a PCB file for routing."""
        router, net_map = load_pcb_for_routing(str(routing_test_pcb))

        assert router is not None
        assert isinstance(net_map, dict)
        assert len(net_map) > 0
        # Check expected nets
        assert "NET1" in net_map
        assert "GND" in net_map
        assert "+3.3V" in net_map

    def test_load_pcb_dimensions(self, routing_test_pcb):
        """Test that board dimensions are parsed correctly."""
        router, net_map = load_pcb_for_routing(str(routing_test_pcb))

        # gr_rect defines edge cuts from (100,100) to (150,140)
        assert router.grid.width == 50.0  # 150 - 100
        assert router.grid.height == 40.0  # 140 - 100
        assert router.grid.origin_x == 100.0
        assert router.grid.origin_y == 100.0

    def test_load_pcb_with_skip_nets(self, routing_test_pcb):
        """Test loading PCB with skip_nets."""
        router, net_map = load_pcb_for_routing(str(routing_test_pcb), skip_nets=["GND", "+3.3V"])

        assert router is not None
        # Skipped nets should still be in net_map
        assert "GND" in net_map

    def test_load_pcb_with_netlist_override(self, routing_test_pcb):
        """Test loading PCB with netlist overrides."""
        netlist = {
            "R1.1": "OVERRIDE_NET",
        }

        router, net_map = load_pcb_for_routing(
            str(routing_test_pcb),
            netlist=netlist,
        )

        # Override net should be in net_map
        assert "OVERRIDE_NET" in net_map

    def test_load_pcb_with_custom_rules(self, routing_test_pcb):
        """Test loading PCB with custom design rules."""
        rules = DesignRules(
            trace_width=0.3,
            trace_clearance=0.25,
            grid_resolution=0.5,
        )

        # Use validate_drc=False since this test is about custom rules, not DRC compliance
        # (Note: grid_resolution > clearance would always fail even with strict_drc=False)
        router, net_map = load_pcb_for_routing(
            str(routing_test_pcb),
            rules=rules,
            validate_drc=False,
        )

        assert router.rules.trace_width == 0.3
        assert router.rules.trace_clearance == 0.25

    def test_load_pcb_components_added(self, routing_test_pcb):
        """Test that components are added to router."""
        router, net_map = load_pcb_for_routing(str(routing_test_pcb))

        # Check that pads were added
        assert len(router.pads) > 0

        # Check that nets were registered
        assert len(router.nets) > 0

    def test_load_pcb_through_hole_detection(self, routing_test_pcb):
        """Test that through-hole pads are detected correctly."""
        router, net_map = load_pcb_for_routing(str(routing_test_pcb))

        # J1 has through-hole pads
        # Look for through-hole pads
        has_through_hole = any(pad.through_hole for pad in router.pads.values())
        assert has_through_hole

    def test_load_pcb_default_dimensions(self, tmp_path):
        """Test default dimensions when no edge cuts present."""
        # Create a PCB without gr_rect
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "NET1")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 80)
    (fp_text reference "U1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 5 0) (size 1 1) (layers "F.Cu") (net 1 "NET1"))
  )
)
"""
        pcb_file = tmp_path / "no_edge.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(str(pcb_file))

        # Should use default HAT dimensions
        assert router.grid.width == 65.0
        assert router.grid.height == 56.0

    def test_load_pcb_unquoted_pad_numbers(self, tmp_path):
        """Test parsing pads with unquoted numeric pad numbers (Issue #173).

        KiCad uses unquoted pad numbers for numeric pads:
            (pad 1 smd rect ...)
        But quoted for alphanumeric (BGA):
            (pad "A1" smd rect ...)
        """
        # Create a PCB with UNQUOTED pad numbers (real KiCad format)
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG")
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "U1" (at 0 -3) (layer "F.SilkS"))
    (pad 1 smd rect (at -1.905 -2.475) (size 0.6 1.2) (layers "F.Cu") (net 1 "VCC"))
    (pad 2 smd rect (at -0.635 -2.475) (size 0.6 1.2) (layers "F.Cu") (net 2 "GND"))
    (pad 3 smd rect (at 0.635 -2.475) (size 0.6 1.2) (layers "F.Cu") (net 3 "SIG"))
    (pad 4 smd rect (at 1.905 -2.475) (size 0.6 1.2) (layers "F.Cu") (net 2 "GND"))
    (pad 5 smd rect (at 1.905 2.475) (size 0.6 1.2) (layers "F.Cu") (net 2 "GND"))
    (pad 6 smd rect (at 0.635 2.475) (size 0.6 1.2) (layers "F.Cu") (net 3 "SIG"))
    (pad 7 smd rect (at -0.635 2.475) (size 0.6 1.2) (layers "F.Cu") (net 2 "GND"))
    (pad 8 smd rect (at -1.905 2.475) (size 0.6 1.2) (layers "F.Cu") (net 1 "VCC"))
  )
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x04"
    (layer "F.Cu")
    (at 140 120)
    (fp_text reference "J1" (at 0 -3) (layer "F.SilkS"))
    (pad 1 thru_hole oval (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 1 "VCC"))
    (pad 2 thru_hole oval (at 0 2.54) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 2 "GND"))
    (pad 3 thru_hole oval (at 0 5.08) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 3 "SIG"))
    (pad 4 thru_hole oval (at 0 7.62) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 2 "GND"))
  )
)
"""
        pcb_file = tmp_path / "unquoted_pads.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(str(pcb_file))

        # Should have found all nets
        assert "VCC" in net_map
        assert "GND" in net_map
        assert "SIG" in net_map

        # Should have found all 12 pads (8 from U1 + 4 from J1)
        assert len(router.pads) == 12

        # Check specific pads were parsed correctly
        pad_refs = {ref for ref, _ in router.pads.keys()}
        assert "U1" in pad_refs
        assert "J1" in pad_refs

        # Check pad numbers were parsed (should be strings "1", "2", etc.)
        pad_nums = {num for _, num in router.pads.keys()}
        assert "1" in pad_nums
        assert "8" in pad_nums

    def test_load_pcb_mixed_quoted_unquoted_pads(self, tmp_path):
        """Test parsing PCB with both quoted and unquoted pad numbers.

        This tests the case where a board has both:
        - Numeric pads: (pad 1 smd ...) - unquoted
        - BGA pads: (pad "A1" smd ...) - quoted
        """
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (gr_rect (start 100 100) (end 160 150) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 110 120)
    (fp_text reference "U1" (at 0 0) (layer "F.SilkS"))
    (pad 1 smd rect (at 0 0) (size 0.6 1.2) (layers "F.Cu") (net 1 "VCC"))
    (pad 2 smd rect (at 1.27 0) (size 0.6 1.2) (layers "F.Cu") (net 2 "GND"))
  )
  (footprint "Package_BGA:BGA-4"
    (layer "F.Cu")
    (at 140 130)
    (fp_text reference "U2" (at 0 0) (layer "F.SilkS"))
    (pad "A1" smd circle (at -0.5 -0.5) (size 0.4 0.4) (layers "F.Cu") (net 1 "VCC"))
    (pad "A2" smd circle (at 0.5 -0.5) (size 0.4 0.4) (layers "F.Cu") (net 2 "GND"))
    (pad "B1" smd circle (at -0.5 0.5) (size 0.4 0.4) (layers "F.Cu") (net 2 "GND"))
    (pad "B2" smd circle (at 0.5 0.5) (size 0.4 0.4) (layers "F.Cu") (net 1 "VCC"))
  )
)
"""
        pcb_file = tmp_path / "mixed_pads.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(str(pcb_file))

        # Should have found all 6 pads (2 from U1 + 4 from U2)
        assert len(router.pads) == 6

        # Check both numeric and alphanumeric pad numbers
        pad_keys = set(router.pads.keys())
        assert ("U1", "1") in pad_keys  # Unquoted numeric
        assert ("U1", "2") in pad_keys  # Unquoted numeric
        assert ("U2", "A1") in pad_keys  # Quoted alphanumeric
        assert ("U2", "B2") in pad_keys  # Quoted alphanumeric

    def test_load_pcb_multiline_pad_format(self, tmp_path):
        """Test parsing pads in KiCad 7+ multi-line format.

        KiCad 7+ formats pads across multiple lines:
            (pad "1" smd roundrect
              (at -0.9500 0.9000)
              (size 0.6000 1.1000)
              (roundrect_rratio 0.25)
              (layers "F.Cu" "F.Paste" "F.Mask")
              (net 2 "+5V")
              (uuid "...")
            )

        This is different from single-line format that older versions used.
        """
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "+5V")
  (net 2 "GND")
  (net 3 "SIG")
  (footprint "SOT-23-5"
    (layer "F.Cu")
    (uuid "test-uuid-1")
    (at 120 120 180)
    (fp_text reference "U1"
      (at 0 -2.05 0)
      (layer "F.SilkS")
    )
    (pad "1" smd roundrect
      (at -0.9500 0.9000)
      (size 0.6000 1.1000)
      (roundrect_rratio 0.25)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "+5V")
      (uuid "pad-uuid-1")
    )
    (pad "2" smd roundrect
      (at 0.0000 0.9000)
      (size 0.6000 1.1000)
      (roundrect_rratio 0.25)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 2 "GND")
      (uuid "pad-uuid-2")
    )
    (pad "3" smd roundrect
      (at 0.9500 0.9000)
      (size 0.6000 1.1000)
      (roundrect_rratio 0.25)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 3 "SIG")
      (uuid "pad-uuid-3")
    )
    (pad "4" smd roundrect
      (at 0.9500 -0.9000)
      (size 0.6000 1.1000)
      (roundrect_rratio 0.25)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 2 "GND")
      (uuid "pad-uuid-4")
    )
    (pad "5" smd roundrect
      (at -0.9500 -0.9000)
      (size 0.6000 1.1000)
      (roundrect_rratio 0.25)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "+5V")
      (uuid "pad-uuid-5")
    )
  )
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x03"
    (layer "F.Cu")
    (uuid "test-uuid-2")
    (at 140 120)
    (fp_text reference "J1"
      (at 0 -3)
      (layer "F.SilkS")
    )
    (pad "1" thru_hole oval
      (at 0 0)
      (size 1.7 1.7)
      (drill 1.0)
      (layers "*.Cu")
      (net 1 "+5V")
      (uuid "pad-uuid-j1-1")
    )
    (pad "2" thru_hole oval
      (at 0 2.54)
      (size 1.7 1.7)
      (drill 1.0)
      (layers "*.Cu")
      (net 2 "GND")
      (uuid "pad-uuid-j1-2")
    )
    (pad "3" thru_hole oval
      (at 0 5.08)
      (size 1.7 1.7)
      (drill 1.0)
      (layers "*.Cu")
      (net 3 "SIG")
      (uuid "pad-uuid-j1-3")
    )
  )
)
"""
        pcb_file = tmp_path / "multiline_pads.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(str(pcb_file))

        # Should have found all nets
        assert "+5V" in net_map
        assert "GND" in net_map
        assert "SIG" in net_map

        # Should have found all 8 pads (5 from U1 + 3 from J1)
        assert len(router.pads) == 8, f"Expected 8 pads, got {len(router.pads)}"

        # Check specific pads were parsed correctly
        pad_refs = {ref for ref, _ in router.pads.keys()}
        assert "U1" in pad_refs
        assert "J1" in pad_refs

        # Check all pad numbers from U1 were parsed
        u1_pads = {num for ref, num in router.pads.keys() if ref == "U1"}
        assert u1_pads == {"1", "2", "3", "4", "5"}

        # Check J1 pads
        j1_pads = {num for ref, num in router.pads.keys() if ref == "J1"}
        assert j1_pads == {"1", "2", "3"}

        # Verify nets were assigned correctly to pads
        # U1 pin 1 should be on +5V (net 1)
        pad_u1_1 = router.pads.get(("U1", "1"))
        assert pad_u1_1 is not None
        assert pad_u1_1.net == 1  # +5V

    def test_load_pcb_negative_coordinates(self, tmp_path):
        """Test parsing footprints with negative X/Y coordinates (Issue #942).

        When PCBs have non-standard origins or footprints placed outside the
        board area, footprint positions can be negative. The parser must handle
        this correctly.
        """
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (gr_rect (start 0 0) (end 50 40) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG")
  (footprint "Resistor:R_0805"
    (layer "F.Cu")
    (at -10 20)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS"))
    (pad 1 smd rect (at -1 0) (size 1.2 1.0) (layers "F.Cu") (net 1 "VCC"))
    (pad 2 smd rect (at 1 0) (size 1.2 1.0) (layers "F.Cu") (net 2 "GND"))
  )
  (footprint "Resistor:R_0805"
    (layer "F.Cu")
    (at 25 -15)
    (fp_text reference "R2" (at 0 -1.5) (layer "F.SilkS"))
    (pad 1 smd rect (at -1 0) (size 1.2 1.0) (layers "F.Cu") (net 2 "GND"))
    (pad 2 smd rect (at 1 0) (size 1.2 1.0) (layers "F.Cu") (net 3 "SIG"))
  )
  (footprint "Resistor:R_0805"
    (layer "F.Cu")
    (at -5 -8 45)
    (fp_text reference "R3" (at 0 -1.5) (layer "F.SilkS"))
    (pad 1 smd rect (at -1 0) (size 1.2 1.0) (layers "F.Cu") (net 1 "VCC"))
    (pad 2 smd rect (at 1 0) (size 1.2 1.0) (layers "F.Cu") (net 3 "SIG"))
  )
)
"""
        pcb_file = tmp_path / "negative_coords.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(str(pcb_file))

        # Should have found all 3 nets
        assert "VCC" in net_map
        assert "GND" in net_map
        assert "SIG" in net_map

        # Should have found all 6 pads (2 from each of R1, R2, R3)
        assert len(router.pads) == 6, f"Expected 6 pads, got {len(router.pads)}"

        # Check all components were parsed
        pad_refs = {ref for ref, _ in router.pads.keys()}
        assert "R1" in pad_refs, "R1 at negative X position not parsed"
        assert "R2" in pad_refs, "R2 at negative Y position not parsed"
        assert "R3" in pad_refs, "R3 at negative X,Y with rotation not parsed"

        # Verify pad positions for R1 (at -10, 20)
        r1_pad1 = router.pads.get(("R1", "1"))
        r1_pad2 = router.pads.get(("R1", "2"))
        assert r1_pad1 is not None
        assert r1_pad2 is not None
        # R1 pad 1 should be at (-10-1, 20) = (-11, 20)
        assert abs(r1_pad1.x - (-11)) < 0.01
        assert abs(r1_pad1.y - 20) < 0.01
        # R1 pad 2 should be at (-10+1, 20) = (-9, 20)
        assert abs(r1_pad2.x - (-9)) < 0.01
        assert abs(r1_pad2.y - 20) < 0.01

        # Verify nets were assigned correctly
        assert r1_pad1.net == net_map["VCC"]
        assert r1_pad2.net == net_map["GND"]


class TestParsePcbDesignRules:
    """Tests for parse_pcb_design_rules function."""

    def test_returns_defaults_for_empty_setup(self):
        """Test that defaults are returned when setup section is empty."""
        pcb_text = """(kicad_pcb
  (version 20240108)
  (setup
  )
)"""
        rules = parse_pcb_design_rules(pcb_text)

        assert rules.min_track_width == 0.2
        assert rules.min_via_diameter == 0.6
        assert rules.min_via_drill == 0.3
        assert rules.min_clearance == 0.2

    def test_returns_defaults_for_no_setup(self):
        """Test that defaults are returned when no setup section exists."""
        pcb_text = """(kicad_pcb
  (version 20240108)
)"""
        rules = parse_pcb_design_rules(pcb_text)

        assert rules.min_track_width == 0.2
        assert rules.min_clearance == 0.2

    def test_parses_net_class_clearance(self):
        """Test parsing clearance from net class definition."""
        pcb_text = """(kicad_pcb
  (version 20240108)
  (setup)
  (net_class "Default" "Default net class"
    (clearance 0.15)
    (trace_width 0.25)
    (via_dia 0.8)
    (via_drill 0.4)
  )
)"""
        rules = parse_pcb_design_rules(pcb_text)

        assert rules.min_clearance == 0.15
        assert rules.min_track_width == 0.25
        assert rules.min_via_diameter == 0.8
        assert rules.min_via_drill == 0.4

    def test_uses_minimum_from_multiple_net_classes(self):
        """Test that minimum values are used across multiple net classes."""
        pcb_text = """(kicad_pcb
  (version 20240108)
  (net_class "Default"
    (clearance 0.2)
    (trace_width 0.25)
  )
  (net_class "HighSpeed"
    (clearance 0.1)
    (trace_width 0.15)
  )
)"""
        rules = parse_pcb_design_rules(pcb_text)

        # Should use the minimum values
        assert rules.min_clearance == 0.1
        assert rules.min_track_width == 0.15

    def test_to_design_rules_conversion(self):
        """Test converting PCBDesignRules to DesignRules."""
        pcb_rules = PCBDesignRules(
            min_track_width=0.15,
            min_via_diameter=0.5,
            min_via_drill=0.25,
            min_clearance=0.1,
        )

        design_rules = pcb_rules.to_design_rules()

        assert design_rules.trace_width == 0.15
        assert design_rules.via_diameter == 0.5
        assert design_rules.via_drill == 0.25
        assert design_rules.trace_clearance == 0.1
        # Grid resolution should be clearance / 2 for DRC compliance
        assert design_rules.grid_resolution == 0.05

    def test_to_design_rules_custom_grid(self):
        """Test converting with custom grid resolution."""
        pcb_rules = PCBDesignRules(min_clearance=0.2)
        design_rules = pcb_rules.to_design_rules(grid_resolution=0.1)

        assert design_rules.grid_resolution == 0.1


class TestValidateGridResolution:
    """Tests for validate_grid_resolution function."""

    def test_no_warning_when_compliant(self):
        """Test no warnings when grid resolution is <= clearance/2."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            issues = validate_grid_resolution(0.1, 0.2, warn=True, strict=True)

            assert len(issues) == 0
            assert len(w) == 0

    def test_strict_raises_when_resolution_exceeds_half_clearance(self):
        """Test strict mode raises GridResolutionError when grid > clearance/2."""
        with pytest.raises(GridResolutionError) as exc_info:
            validate_grid_resolution(0.15, 0.2, strict=True)

        assert exc_info.value.grid_resolution == 0.15
        assert exc_info.value.clearance == 0.2
        assert "may cause clearance violations" in str(exc_info.value)

    def test_lenient_warns_when_resolution_exceeds_half_clearance(self):
        """Test lenient mode warns but doesn't raise when grid > clearance/2."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            issues = validate_grid_resolution(0.15, 0.2, warn=True, strict=False)

            assert len(issues) == 1
            assert "may cause clearance violations" in issues[0]
            assert len(w) == 1

    def test_always_raises_when_resolution_exceeds_clearance(self):
        """Test exception always raised when grid resolution > clearance."""
        # Even with strict=False, this should raise because it WILL cause violations
        with pytest.raises(GridResolutionError) as exc_info:
            validate_grid_resolution(0.3, 0.2, strict=False)

        assert exc_info.value.grid_resolution == 0.3
        assert exc_info.value.clearance == 0.2
        assert "WILL cause DRC violations" in str(exc_info.value)

    def test_warn_false_suppresses_warnings_in_lenient_mode(self):
        """Test that warn=False suppresses warnings.warn() calls in lenient mode."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            issues = validate_grid_resolution(0.15, 0.2, warn=False, strict=False)

            # Issues should still be returned
            assert len(issues) == 1
            # But no warning should be emitted
            assert len(w) == 0

    def test_exact_half_is_compliant(self):
        """Test that exactly clearance/2 is compliant (edge case)."""
        issues = validate_grid_resolution(0.1, 0.2, warn=False, strict=True)
        assert len(issues) == 0

    def test_exception_has_correct_attributes(self):
        """Test GridResolutionError has correct attributes."""
        with pytest.raises(GridResolutionError) as exc_info:
            validate_grid_resolution(0.3, 0.2)

        error = exc_info.value
        assert error.grid_resolution == 0.3
        assert error.clearance == 0.2
        assert error.recommended == 0.1  # clearance / 2

    def test_default_is_strict_mode(self):
        """Test that strict=True is the default behavior."""
        # Default behavior should raise for grid > clearance/2
        with pytest.raises(GridResolutionError):
            validate_grid_resolution(0.15, 0.2)  # 0.15 > 0.1 (half of 0.2)


class TestAdjustGridForCompliance:
    """Tests for adjust_grid_for_compliance function (Issue #705)."""

    def test_no_adjustment_when_compliant(self):
        """Test no adjustment when grid resolution is already compliant."""
        adjustment = adjust_grid_for_compliance(0.1, 0.2)

        assert adjustment.was_adjusted is False
        assert adjustment.original == 0.1
        assert adjustment.adjusted == 0.1
        assert adjustment.clearance == 0.2

    def test_adjustment_when_too_coarse(self):
        """Test adjustment when grid resolution exceeds clearance/2."""
        adjustment = adjust_grid_for_compliance(0.25, 0.2)

        assert adjustment.was_adjusted is True
        assert adjustment.original == 0.25
        assert adjustment.adjusted == 0.1  # clearance / 2
        assert adjustment.clearance == 0.2

    def test_adjustment_at_boundary(self):
        """Test no adjustment at exact boundary (grid == clearance/2)."""
        adjustment = adjust_grid_for_compliance(0.1, 0.2)

        assert adjustment.was_adjusted is False
        assert adjustment.adjusted == 0.1

    def test_adjustment_slightly_over_boundary(self):
        """Test adjustment when grid is slightly over clearance/2."""
        adjustment = adjust_grid_for_compliance(0.11, 0.2)

        assert adjustment.was_adjusted is True
        assert adjustment.adjusted == 0.1

    def test_message_when_adjusted(self):
        """Test message property when adjustment was made."""
        adjustment = adjust_grid_for_compliance(0.25, 0.2)

        assert "adjusted" in adjustment.message.lower()
        assert "0.25" in adjustment.message
        assert "0.1" in adjustment.message

    def test_message_when_compliant(self):
        """Test message property when no adjustment needed."""
        adjustment = adjust_grid_for_compliance(0.1, 0.2)

        assert "compliant" in adjustment.message.lower()

    def test_adjustment_dataclass_immutable_fields(self):
        """Test GridAdjustment has expected fields."""
        adjustment = GridAdjustment(
            original=0.25,
            adjusted=0.1,
            clearance=0.2,
            was_adjusted=True,
        )

        assert adjustment.original == 0.25
        assert adjustment.adjusted == 0.1
        assert adjustment.clearance == 0.2
        assert adjustment.was_adjusted is True


class TestValidateRoutes:
    """Tests for validate_routes function."""

    def test_no_violations_for_empty_routes(self):
        """Test no violations when router has no routes."""
        router = Autorouter(width=50, height=50)
        violations = validate_routes(router)
        assert len(violations) == 0

    def test_detects_clearance_violation(self):
        """Test detection of clearance violations between routes and pads."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            grid_resolution=0.1,
        )
        router = Autorouter(width=50, height=50, rules=rules)

        # Add two pads on different nets
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10, "y": 10, "width": 1.0, "height": 1.0, "net": 1},
                {"number": "2", "x": 20, "y": 10, "width": 1.0, "height": 1.0, "net": 2},
            ],
        )

        # Manually add a route that passes very close to the second pad
        segment = Segment(x1=10, y1=10, x2=19.5, y2=10, layer=Layer.F_CU, width=0.2)
        route = Route(net=1, net_name="NET1", segments=[segment], vias=[])
        router.routes.append(route)

        violations = validate_routes(router)

        # Should detect the violation (route too close to pad on net 2)
        assert len(violations) >= 1
        assert violations[0].obstacle_type == "pad"
        assert violations[0].net == 1
        assert violations[0].obstacle_net == 2

    def test_no_violation_for_same_net_proximity(self):
        """Test no violation when route is near pad on same net."""
        rules = DesignRules(trace_clearance=0.2, grid_resolution=0.1)
        router = Autorouter(width=50, height=50, rules=rules)

        # Add two pads on the same net
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10, "y": 10, "width": 1.0, "height": 1.0, "net": 1},
                {"number": "2", "x": 20, "y": 10, "width": 1.0, "height": 1.0, "net": 1},
            ],
        )

        # Add route connecting them (passes very close)
        segment = Segment(x1=10.5, y1=10, x2=19.5, y2=10, layer=Layer.F_CU, width=0.2)
        route = Route(net=1, net_name="NET1", segments=[segment], vias=[])
        router.routes.append(route)

        violations = validate_routes(router)

        # No violation - route is on same net as nearby pads
        assert len(violations) == 0


class TestLoadPcbForRoutingDrcCompliance:
    """Tests for DRC compliance features in load_pcb_for_routing."""

    def test_uses_pcb_rules_by_default(self, tmp_path):
        """Test that PCB design rules are used when available."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "NET1")
  (net_class "Default"
    (clearance 0.15)
    (trace_width 0.18)
    (via_dia 0.5)
    (via_drill 0.25)
  )
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "NET1"))
  )
)"""
        pcb_file = tmp_path / "test_drc.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(
            str(pcb_file), use_pcb_rules=True, validate_drc=False
        )

        # Should use rules from PCB
        assert router.rules.trace_clearance == 0.15
        assert router.rules.trace_width == 0.18
        assert router.rules.via_diameter == 0.5
        assert router.rules.via_drill == 0.25

    def test_use_pcb_rules_false_uses_defaults(self, tmp_path):
        """Test that use_pcb_rules=False ignores PCB design rules."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "NET1")
  (net_class "Default"
    (clearance 0.15)
    (trace_width 0.18)
  )
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "NET1"))
  )
)"""
        pcb_file = tmp_path / "test_drc.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(
            str(pcb_file), use_pcb_rules=False, validate_drc=False
        )

        # Should use default rules, not PCB rules
        assert router.rules.trace_clearance == 0.2  # Default
        assert router.rules.grid_resolution == 0.1  # Default

    def test_validate_drc_emits_warning(self, tmp_path):
        """Test that validate_drc=True emits warnings for bad grid resolution."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "test_drc.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Use rules with grid > recommended (clearance/2) but <= clearance
        # This triggers a warning in non-strict mode, not an error
        # grid=0.15, clearance=0.2: recommended=0.1, so grid > recommended but grid <= clearance
        rules = DesignRules(grid_resolution=0.15, trace_clearance=0.2)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            router, net_map = load_pcb_for_routing(
                str(pcb_file), rules=rules, validate_drc=True, strict_drc=False
            )

            # Should emit a warning
            assert len(w) >= 1
            assert "clearance" in str(w[0].message).lower()

    def test_validate_drc_false_no_warning(self, tmp_path):
        """Test that validate_drc=False suppresses warnings."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "test_drc.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Use rules with bad grid resolution
        rules = DesignRules(grid_resolution=0.25, trace_clearance=0.2)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            router, net_map = load_pcb_for_routing(str(pcb_file), rules=rules, validate_drc=False)

            # Should NOT emit a warning
            assert len(w) == 0

    def test_custom_rules_override_pcb_rules(self, tmp_path):
        """Test that explicit rules parameter overrides PCB rules."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (net_class "Default"
    (clearance 0.15)
    (trace_width 0.18)
  )
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "test_drc.kicad_pcb"
        pcb_file.write_text(pcb_content)

        custom_rules = DesignRules(
            trace_width=0.3,
            trace_clearance=0.25,
            grid_resolution=0.1,
        )

        router, net_map = load_pcb_for_routing(
            str(pcb_file), rules=custom_rules, use_pcb_rules=True, validate_drc=False
        )

        # Custom rules should be used, not PCB rules
        assert router.rules.trace_width == 0.3
        assert router.rules.trace_clearance == 0.25

    def test_auto_adjust_grid_adjusts_coarse_resolution(self, tmp_path):
        """Test that auto_adjust_grid=True adjusts coarse grid resolution."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "test_auto_adjust.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Use rules with coarse grid resolution (0.25 > 0.2/2 = 0.1)
        rules = DesignRules(grid_resolution=0.25, trace_clearance=0.2)

        router, net_map = load_pcb_for_routing(
            str(pcb_file), rules=rules, auto_adjust_grid=True, validate_drc=True
        )

        # Grid should be adjusted to clearance/2
        assert router.rules.grid_resolution == 0.1
        # Other rules should be preserved
        assert router.rules.trace_clearance == 0.2

    def test_auto_adjust_grid_no_change_when_compliant(self, tmp_path):
        """Test that auto_adjust_grid=True doesn't change compliant grid."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "test_auto_adjust.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Use rules with compliant grid resolution
        rules = DesignRules(grid_resolution=0.1, trace_clearance=0.2)

        router, net_map = load_pcb_for_routing(
            str(pcb_file), rules=rules, auto_adjust_grid=True, validate_drc=True
        )

        # Grid should not be changed
        assert router.rules.grid_resolution == 0.1

    def test_auto_adjust_grid_false_raises_error(self, tmp_path):
        """Test that auto_adjust_grid=False (default) raises error for bad grid."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "test_auto_adjust.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Use rules with coarse grid resolution
        rules = DesignRules(grid_resolution=0.25, trace_clearance=0.2)

        # Should raise GridResolutionError when auto_adjust_grid=False
        with pytest.raises(GridResolutionError):
            load_pcb_for_routing(
                str(pcb_file), rules=rules, auto_adjust_grid=False, validate_drc=True
            )

    def test_auto_adjust_grid_preserves_other_rules(self, tmp_path):
        """Test that auto_adjust_grid preserves all other design rules."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "test_auto_adjust.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Use rules with specific values
        rules = DesignRules(
            grid_resolution=0.25,  # Will be adjusted
            trace_width=0.15,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            via_clearance=0.25,
        )

        router, net_map = load_pcb_for_routing(
            str(pcb_file), rules=rules, auto_adjust_grid=True, validate_drc=True
        )

        # Grid should be adjusted
        assert router.rules.grid_resolution == 0.1
        # All other rules should be preserved
        assert router.rules.trace_width == 0.15
        assert router.rules.trace_clearance == 0.2
        assert router.rules.via_drill == 0.3
        assert router.rules.via_diameter == 0.6
        assert router.rules.via_clearance == 0.25

    def test_auto_adjust_grid_preserves_cost_settings(self, tmp_path):
        """Test that auto_adjust_grid preserves cost and layer settings."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "test_cost_preserve.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Use rules with custom cost settings and layer preferences
        rules = DesignRules(
            grid_resolution=0.25,  # Will be adjusted
            trace_clearance=0.2,
            # Custom cost settings
            cost_turn=15.0,  # Non-default
            cost_via=25.0,  # Non-default
            cost_congestion=5.0,  # Non-default
            # Custom layer preference
            preferred_layer=Layer.B_CU,  # Non-default
        )

        router, net_map = load_pcb_for_routing(
            str(pcb_file), rules=rules, auto_adjust_grid=True, validate_drc=True
        )

        # Grid should be adjusted
        assert router.rules.grid_resolution == 0.1
        # Cost settings should be preserved (these were the bug - previously reset to defaults)
        assert router.rules.cost_turn == 15.0
        assert router.rules.cost_via == 25.0
        assert router.rules.cost_congestion == 5.0
        # Layer preference should be preserved
        assert router.rules.preferred_layer == Layer.B_CU


class TestGenerateNetclassSetup:
    """Tests for generate_netclass_setup function - KiCad 7+ compatibility."""

    def test_empty_returns_empty_string(self):
        """Test that no net classes returns empty string."""
        rules = DesignRules()
        result = generate_netclass_setup(rules)
        assert result == ""

    def test_none_net_classes_returns_empty(self):
        """Test that None net_classes returns empty string."""
        rules = DesignRules()
        result = generate_netclass_setup(rules, net_classes=None)
        assert result == ""

    def test_empty_dict_returns_empty(self):
        """Test that empty dict returns empty string."""
        rules = DesignRules()
        result = generate_netclass_setup(rules, net_classes={})
        assert result == ""

    def test_generates_net_class_assignments(self):
        """Test that net classes generate proper S-expressions."""
        rules = DesignRules()
        net_classes = {
            "Power": ["+5V", "GND"],
            "Signal": ["SDA", "SCL"],
        }
        result = generate_netclass_setup(rules, net_classes)

        # Should contain net_class assignments
        assert '(net_class "Power" "+5V")' in result
        assert '(net_class "Power" "GND")' in result
        assert '(net_class "Signal" "SDA")' in result
        assert '(net_class "Signal" "SCL")' in result

    def test_does_not_use_old_format(self):
        """Test that old KiCad 6 format is not used."""
        rules = DesignRules()
        net_classes = {"Power": ["+5V"]}
        result = generate_netclass_setup(rules, net_classes)

        # Should NOT contain old format
        assert "(net_settings" not in result
        assert "Default net class" not in result
        # Should not have nested net_class with clearance/trace_width
        assert "clearance" not in result.lower()
        assert "via_dia" not in result.lower()


class TestMergeRoutesIntoPcb:
    """Tests for merge_routes_into_pcb function."""

    def test_empty_routes_returns_original(self):
        """Test that empty routes returns original content."""
        pcb_content = "(kicad_pcb\n  (version 20240108)\n)"
        result = merge_routes_into_pcb(pcb_content, "")
        assert result == pcb_content

    def test_inserts_routes_before_closing_paren(self):
        """Test that routes are inserted before final closing paren."""
        pcb_content = "(kicad_pcb\n  (version 20240108)\n)"
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = merge_routes_into_pcb(pcb_content, route_sexp)

        assert "(segment" in result
        assert result.endswith(")\n")
        # Route should be before final paren
        assert result.index("segment") < result.rfind(")")

    def test_handles_trailing_whitespace(self):
        """Test handling of trailing whitespace in PCB content."""
        pcb_content = "(kicad_pcb\n)   \n\n"
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = merge_routes_into_pcb(pcb_content, route_sexp)

        assert "(segment" in result
        assert result.strip().endswith(")")

    def test_preserves_original_content(self):
        """Test that original PCB content is preserved."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 1 "VCC")
  (footprint "Package_SO:SOIC-8")
)"""
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = merge_routes_into_pcb(pcb_content, route_sexp)

        assert "version 20240108" in result
        assert 'generator "test"' in result
        assert 'net 1 "VCC"' in result
        assert "Package_SO:SOIC-8" in result

    def test_does_not_add_net_settings(self):
        """Test that no net_settings block is added (KiCad 7+ compatibility)."""
        pcb_content = "(kicad_pcb\n)"
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = merge_routes_into_pcb(pcb_content, route_sexp)

        # Should NOT contain old net_settings format
        assert "(net_settings" not in result
        assert "(net_class" not in result

    def test_raises_typeerror_for_autorouter_object(self):
        """Test that passing an Autorouter object raises TypeError with helpful message.

        Issue #1046: Users may incorrectly pass the Autorouter object directly
        instead of calling router.to_sexp() first.
        """
        pcb_content = "(kicad_pcb\n)"

        # Create a mock object with to_sexp method to simulate Autorouter
        class MockAutorouter:
            def to_sexp(self) -> str:
                return "(segment (start 0 0) (end 10 10) (width 0.2))"

        mock_router = MockAutorouter()

        with pytest.raises(TypeError) as exc_info:
            merge_routes_into_pcb(pcb_content, mock_router)  # type: ignore[arg-type]

        # Check that error message is helpful
        error_msg = str(exc_info.value)
        assert "to_sexp()" in error_msg
        assert "router.to_sexp()" in error_msg or "S-expression string" in error_msg


class TestKicad7Compatibility:
    """Integration tests for KiCad 7+ compatibility (Issue #45)."""

    def test_routed_segments_are_self_contained(self):
        """Test that segments embed trace width, making net class metadata optional."""
        seg = Segment(x1=0, y1=0, x2=10, y2=0, width=0.25, layer=Layer.F_CU, net=1)
        sexp = seg.to_sexp()

        # Width should be embedded in segment
        assert "(width 0.25)" in sexp
        # No external net class reference needed
        assert "(net_class" not in sexp

    def test_routed_vias_are_self_contained(self):
        """Test that vias embed size and drill, making net class metadata optional."""
        via = Via(x=10.0, y=20.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
        sexp = via.to_sexp()

        # Size and drill should be embedded
        assert "(size 0.6)" in sexp
        assert "(drill 0.3)" in sexp
        # No external net class reference needed
        assert "(net_class" not in sexp

    def test_route_sexp_has_no_net_settings(self):
        """Test that Route.to_sexp() doesn't generate net_settings."""
        route = Route(net=1, net_name="VCC")
        route.segments.append(Segment(0, 0, 10, 0, 0.2, Layer.F_CU, net=1))
        route.vias.append(Via(10, 0, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1))

        sexp = route.to_sexp()

        # Should not contain net_settings (old KiCad 6 format)
        assert "(net_settings" not in sexp
        # Should contain segments and vias with embedded parameters
        assert "(segment" in sexp
        assert "(via" in sexp


class TestDiagonalRouting:
    """Tests for diagonal (45 degree) routing support (Issue #59)."""

    def test_octile_distance_straight(self):
        """Test octile distance for orthogonal movement."""
        # Pure horizontal: 10 units
        assert octile_distance(10, 0) == 10.0
        # Pure vertical: 10 units
        assert octile_distance(0, 10) == 10.0

    def test_octile_distance_diagonal(self):
        """Test octile distance for pure diagonal movement."""
        # Pure diagonal: sqrt(2) * 10
        distance = octile_distance(10, 10)
        expected = 10 * DIAGONAL_COST  # 10 diagonal moves
        assert abs(distance - expected) < 0.001

    def test_octile_distance_mixed(self):
        """Test octile distance for mixed movement."""
        # 10 horizontal, 5 vertical = 5 diagonal + 5 straight
        distance = octile_distance(10, 5)
        expected = max(10, 5) + (DIAGONAL_COST - 1) * min(10, 5)
        assert abs(distance - expected) < 0.001

    def test_octile_distance_negative(self):
        """Test octile distance handles negative values."""
        assert octile_distance(-10, 0) == 10.0
        assert octile_distance(0, -10) == 10.0
        assert octile_distance(-10, -10) == octile_distance(10, 10)

    def test_diagonal_cost_value(self):
        """Test DIAGONAL_COST is sqrt(2)."""
        import math

        assert abs(DIAGONAL_COST - math.sqrt(2)) < 0.001

    def test_router_diagonal_routing_default_enabled(self):
        """Test that diagonal routing is enabled by default."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Should have 8 neighbors (4 orthogonal + 4 diagonal)
        assert len(router.neighbors_2d) == 8

    def test_router_diagonal_routing_disabled(self):
        """Test that diagonal routing can be disabled."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules, diagonal_routing=False)

        # Should only have 4 orthogonal neighbors
        assert len(router.neighbors_2d) == 4

    def test_router_diagonal_neighbors(self):
        """Test diagonal neighbor directions and costs."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules, diagonal_routing=True)

        # Extract diagonal moves (where both dx and dy are non-zero)
        diagonal_moves = [
            (dx, dy, dl, cost) for dx, dy, dl, cost in router.neighbors_2d if dx != 0 and dy != 0
        ]

        assert len(diagonal_moves) == 4
        # All diagonal moves should have cost approx 1.414
        for _dx, _dy, _dl, cost in diagonal_moves:
            assert abs(cost - DIAGONAL_COST) < 0.001

    def test_manhattan_heuristic_octile_with_diagonal(self):
        """Test Manhattan heuristic uses octile distance when diagonal enabled."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules, diagonal_routing=True
        )
        heuristic = ManhattanHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # With diagonal routing: octile distance = 10 * sqrt(2)
        expected = 10 * DIAGONAL_COST * rules.cost_straight
        assert abs(estimate - expected) < 0.01

    def test_manhattan_heuristic_manhattan_without_diagonal(self):
        """Test Manhattan heuristic uses Manhattan distance when diagonal disabled."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules, diagonal_routing=False
        )
        heuristic = ManhattanHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # Without diagonal: Manhattan distance = 20
        assert estimate == 20.0

    def test_heuristic_context_diagonal_default(self):
        """Test HeuristicContext defaults to diagonal_routing=True."""
        rules = DesignRules()
        context = HeuristicContext(goal_x=10, goal_y=10, goal_layer=0, rules=rules)
        assert context.diagonal_routing is True

    def test_congestion_aware_heuristic_with_diagonal(self):
        """Test CongestionAware heuristic uses octile distance."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules, diagonal_routing=True
        )
        heuristic = CongestionAwareHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # Base cost should use octile distance
        expected_base = 10 * DIAGONAL_COST * rules.cost_straight
        # Estimate should be at least the base octile distance
        assert estimate >= expected_base - 0.01

    def test_greedy_heuristic_with_diagonal(self):
        """Test Greedy heuristic scales octile distance."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules, diagonal_routing=True
        )
        heuristic = GreedyHeuristic(greed_factor=2.0)

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # Should be 2x octile distance
        expected = 2.0 * 10 * DIAGONAL_COST * rules.cost_straight
        assert abs(estimate - expected) < 0.01

    def test_router_diagonal_corner_blocking_basic(self):
        """Test diagonal corner clearance checking."""
        rules = DesignRules(grid_resolution=1.0)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules, diagonal_routing=True)

        # Block a cell that would be in the corner of a diagonal move
        layer = 0
        grid.grid[layer][1][0].blocked = True  # Block cell at (0, 1)

        # Check diagonal move from (0, 0) to (1, 1)
        # Adjacent cells are (0, 1) and (1, 0) - (0, 1) is blocked
        is_blocked = router._is_diagonal_corner_blocked(0, 0, 1, 1, layer, net=1)
        assert is_blocked is True

    def test_router_diagonal_corner_clear(self):
        """Test diagonal move allowed when corners are clear."""
        rules = DesignRules(grid_resolution=1.0)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules, diagonal_routing=True)

        layer = 0
        # Don't block any cells

        # Check diagonal move from (2, 2) to (3, 3)
        # Adjacent cells are (2, 3) and (3, 2) - both should be clear
        is_blocked = router._is_diagonal_corner_blocked(2, 2, 1, 1, layer, net=1)
        assert is_blocked is False

    def test_router_orthogonal_not_checked(self):
        """Test orthogonal moves skip corner checking."""
        rules = DesignRules(grid_resolution=1.0)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules, diagonal_routing=True)

        layer = 0
        # Orthogonal moves (dx=0 or dy=0) should always return False
        assert router._is_diagonal_corner_blocked(0, 0, 1, 0, layer, net=1) is False
        assert router._is_diagonal_corner_blocked(0, 0, 0, 1, layer, net=1) is False

    def test_router_route_uses_diagonal(self):
        """Test that routes can use diagonal moves for shorter paths."""
        rules = DesignRules(grid_resolution=1.0)
        grid = RoutingGrid(20.0, 20.0, rules)
        router_diag = Router(grid, rules, diagonal_routing=True)

        # Create pads at diagonal positions
        start_pad = Pad(
            x=2.0, y=2.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )
        end_pad = Pad(
            x=10.0, y=10.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )

        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        route = router_diag.route(start_pad, end_pad)
        assert route is not None
        assert len(route.segments) > 0

    def test_router_diagonal_vs_orthogonal_path_length(self):
        """Test diagonal routing produces shorter paths than orthogonal."""
        rules = DesignRules(grid_resolution=1.0)

        # Create two separate grids for fair comparison
        grid_diag = RoutingGrid(20.0, 20.0, rules)
        grid_orth = RoutingGrid(20.0, 20.0, rules)

        router_diag = Router(grid_diag, rules, diagonal_routing=True)
        router_orth = Router(grid_orth, rules, diagonal_routing=False)

        # Create pads at diagonal positions
        start_diag = Pad(
            x=2.0, y=2.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )
        end_diag = Pad(
            x=10.0, y=10.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )

        start_orth = Pad(
            x=2.0, y=2.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )
        end_orth = Pad(
            x=10.0, y=10.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )

        grid_diag.add_pad(start_diag)
        grid_diag.add_pad(end_diag)
        grid_orth.add_pad(start_orth)
        grid_orth.add_pad(end_orth)

        route_diag = router_diag.route(start_diag, end_diag)
        route_orth = router_orth.route(start_orth, end_orth)

        assert route_diag is not None
        assert route_orth is not None

        # Calculate total path length
        def total_length(route):
            length = 0.0
            for seg in route.segments:
                dx = seg.x2 - seg.x1
                dy = seg.y2 - seg.y1
                length += (dx**2 + dy**2) ** 0.5
            return length

        diag_length = total_length(route_diag)
        orth_length = total_length(route_orth)

        # Diagonal path should be shorter or equal (never longer)
        # Note: May be equal if path is already orthogonal
        assert diag_length <= orth_length + 0.01


class TestZoneRules:
    """Tests for ZoneRules dataclass."""

    def test_zone_rules_defaults(self):
        """Test default values for ZoneRules."""
        rules = ZoneRules()
        assert rules.clearance == 0.2
        assert rules.thermal_gap == 0.3
        assert rules.thermal_bridge_width == 0.3
        assert rules.thermal_spoke_count == 4
        assert rules.thermal_spoke_angle == 45.0
        assert rules.pth_connection == "thermal"
        assert rules.smd_connection == "thermal"
        assert rules.via_connection == "solid"
        assert rules.remove_islands is True
        assert rules.island_min_area == 0.5

    def test_zone_rules_custom(self):
        """Test custom ZoneRules values."""
        rules = ZoneRules(
            clearance=0.3,
            thermal_gap=0.5,
            thermal_spoke_count=2,
            pth_connection="solid",
        )
        assert rules.clearance == 0.3
        assert rules.thermal_gap == 0.5
        assert rules.thermal_spoke_count == 2
        assert rules.pth_connection == "solid"


class TestDesignRulesZoneExtensions:
    """Tests for zone extensions to DesignRules."""

    def test_design_rules_has_zone_rules(self):
        """Test that DesignRules includes zone_rules."""
        rules = DesignRules()
        assert hasattr(rules, "zone_rules")
        assert isinstance(rules.zone_rules, ZoneRules)

    def test_design_rules_zone_costs(self):
        """Test zone cost parameters in DesignRules."""
        rules = DesignRules()
        assert hasattr(rules, "cost_zone_same_net")
        assert hasattr(rules, "cost_zone_clearance")
        assert rules.cost_zone_same_net == 0.1  # Low cost for same-net zones
        assert rules.cost_zone_clearance == 2.0


class TestNetClassZoneExtensions:
    """Tests for zone extensions to NetClassRouting."""

    def test_net_class_has_zone_fields(self):
        """Test that NetClassRouting has zone-related fields."""
        nc = NetClassRouting(name="Test")
        assert hasattr(nc, "zone_priority")
        assert hasattr(nc, "zone_connection")
        assert hasattr(nc, "is_pour_net")

    def test_net_class_defaults(self):
        """Test default zone values for NetClassRouting."""
        nc = NetClassRouting(name="Test")
        assert nc.zone_priority == 0
        assert nc.zone_connection == "thermal"
        assert nc.is_pour_net is False

    def test_power_net_class_is_pour_net(self):
        """Test that NET_CLASS_POWER is marked as pour net."""
        assert NET_CLASS_POWER.is_pour_net is True
        assert NET_CLASS_POWER.zone_priority == 10
        assert NET_CLASS_POWER.zone_connection == "solid"


class TestZoneManager:
    """Tests for ZoneManager class."""

    def test_zone_manager_creation(self):
        """Test ZoneManager creation."""
        from kicad_tools.router import ZoneManager

        rules = DesignRules()
        grid = RoutingGrid(50, 50, rules)
        manager = ZoneManager(grid, rules)

        assert manager.grid is grid
        assert manager.rules is rules
        assert manager.filled_zones == []

    def test_zone_manager_statistics_empty(self):
        """Test zone statistics with no zones."""
        from kicad_tools.router import ZoneManager

        rules = DesignRules()
        grid = RoutingGrid(50, 50, rules)
        manager = ZoneManager(grid, rules)

        stats = manager.get_zone_statistics()
        assert stats["zone_count"] == 0
        assert stats["total_cells"] == 0
        assert stats["zones"] == []


class TestPathfinderZoneAwareness:
    """Tests for zone-aware routing in the pathfinder."""

    def test_zone_cell_detection(self):
        """Test detection of zone cells in pathfinder."""
        rules = DesignRules()
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules)

        # No zones initially
        assert not router._is_zone_cell(5, 5, 0)

        # Mark a cell as zone
        cell = grid.grid[0][5][5]
        cell.is_zone = True
        cell.net = 1

        assert router._is_zone_cell(5, 5, 0)
        assert router._get_zone_net(5, 5, 0) == 1

    def test_zone_blocking_other_net(self):
        """Test that other-net zones block routing."""
        rules = DesignRules()
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules)

        # Mark cell as zone for net 1
        cell = grid.grid[0][5][5]
        cell.is_zone = True
        cell.net = 1

        # Net 2 should be blocked by net 1 zone
        assert router._is_zone_blocked(5, 5, 0, net=2)
        # Net 1 should NOT be blocked by its own zone
        assert not router._is_zone_blocked(5, 5, 0, net=1)

    def test_zone_cost_same_net(self):
        """Test reduced cost for same-net zones."""
        rules = DesignRules()
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules)

        # No zone - cost should be 0
        cost = router._get_zone_cost(5, 5, 0, net=1)
        assert cost == 0.0

        # Mark cell as zone for net 1
        cell = grid.grid[0][5][5]
        cell.is_zone = True
        cell.net = 1

        # Same net - should have reduced cost (negative adjustment)
        cost = router._get_zone_cost(5, 5, 0, net=1)
        assert cost < 0

    def test_via_zone_blocking(self):
        """Test via placement blocked by other-net zones."""
        rules = DesignRules()
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules)

        # No zones - via allowed
        assert router._can_place_via_in_zones(5, 5, net=1)

        # Add zone on layer 0 for net 2
        cell = grid.grid[0][5][5]
        cell.is_zone = True
        cell.net = 2

        # Net 1 via should be blocked (would pierce net 2 zone)
        assert not router._can_place_via_in_zones(5, 5, net=1)
        # Net 2 via should be allowed (through own zone)
        assert router._can_place_via_in_zones(5, 5, net=2)


class TestEdgeClearance:
    """Tests for board edge clearance functionality (Issue #296)."""

    def test_add_edge_keepout_blocks_cells(self):
        """Test that add_edge_keepout blocks cells near board edges."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(20, 20, rules, origin_x=0, origin_y=0)

        # Define a simple rectangular board outline
        edge_segments = [
            ((0, 0), (20, 0)),  # Bottom edge
            ((20, 0), (20, 20)),  # Right edge
            ((20, 20), (0, 20)),  # Top edge
            ((0, 20), (0, 0)),  # Left edge
        ]

        # Apply 1mm edge clearance
        blocked_count = grid.add_edge_keepout(edge_segments, clearance=1.0)

        # Should have blocked some cells
        assert blocked_count > 0

        # Cells at the edge should be blocked (within 1mm = 2 grid cells)
        gx0, gy0 = grid.world_to_grid(0.5, 0.5)  # Near corner
        layer_idx = grid.get_routable_indices()[0]
        assert grid.grid[layer_idx][gy0][gx0].blocked is True

        # Cells in the center should NOT be blocked
        gx_center, gy_center = grid.world_to_grid(10, 10)
        assert grid.grid[layer_idx][gy_center][gx_center].blocked is False

    def test_add_edge_keepout_respects_clearance_distance(self):
        """Test that edge keepout uses correct clearance distance."""
        rules = DesignRules(grid_resolution=0.25)
        grid = RoutingGrid(20, 20, rules, origin_x=0, origin_y=0)

        # Single horizontal edge segment at bottom
        edge_segments = [((0, 0), (20, 0))]

        # Apply 0.5mm edge clearance
        grid.add_edge_keepout(edge_segments, clearance=0.5)

        layer_idx = grid.get_routable_indices()[0]

        # Cell at 0.4mm from edge should be blocked (within 0.5mm clearance)
        gx, gy = grid.world_to_grid(10, 0.4)
        assert grid.grid[layer_idx][gy][gx].blocked is True

        # Cell at 1.0mm from edge should NOT be blocked
        gx, gy = grid.world_to_grid(10, 1.0)
        assert grid.grid[layer_idx][gy][gx].blocked is False

    def test_add_edge_keepout_no_clearance(self):
        """Test that zero clearance blocks no cells."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(20, 20, rules)

        edge_segments = [((0, 0), (20, 0))]
        blocked_count = grid.add_edge_keepout(edge_segments, clearance=0.0)

        assert blocked_count == 0

    def test_add_edge_keepout_empty_segments(self):
        """Test that empty segment list blocks no cells."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(20, 20, rules)

        blocked_count = grid.add_edge_keepout([], clearance=1.0)

        assert blocked_count == 0

    def test_add_edge_keepout_all_layers(self):
        """Test that edge keepout applies to all routable layers."""
        # Use 4-layer board (signal-gnd-pwr-signal configuration)
        layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(20, 20, rules, layer_stack=layer_stack)

        edge_segments = [((0, 0), (20, 0))]
        grid.add_edge_keepout(edge_segments, clearance=1.0)

        # All routable layers should have cells blocked near edge
        routable_indices = grid.get_routable_indices()
        gx, gy = grid.world_to_grid(10, 0.5)

        for layer_idx in routable_indices:
            assert grid.grid[layer_idx][gy][gx].blocked is True


class TestExtractEdgeSegments:
    """Tests for extracting board edge segments from PCB files."""

    def test_extract_gr_rect_edge(self):
        """Test extracting edge segments from gr_rect element."""
        pcb_text = """(kicad_pcb
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
)"""

        segments = _extract_edge_segments(pcb_text)

        # gr_rect should produce 4 edge segments
        assert len(segments) == 4

        # Check that segments form a rectangle
        all_points = set()
        for (x1, y1), (x2, y2) in segments:
            all_points.add((x1, y1))
            all_points.add((x2, y2))

        # Should have 4 corner points
        assert (100, 100) in all_points
        assert (150, 100) in all_points
        assert (150, 140) in all_points
        assert (100, 140) in all_points

    def test_extract_gr_line_edges(self):
        """Test extracting edge segments from gr_line elements."""
        pcb_text = """(kicad_pcb
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
)"""

        segments = _extract_edge_segments(pcb_text)

        # Should extract 2 line segments
        assert len(segments) == 2
        assert ((0, 0), (50, 0)) in segments
        assert ((50, 0), (50, 50)) in segments

    def test_ignores_non_edge_cuts_layer(self):
        """Test that non-Edge.Cuts layers are ignored."""
        pcb_text = """(kicad_pcb
  (gr_line (start 0 0) (end 50 0) (layer "F.SilkS") (width 0.1))
  (gr_rect (start 0 0) (end 50 50) (layer "F.Cu"))
)"""

        segments = _extract_edge_segments(pcb_text)

        # Should not extract any segments (not on Edge.Cuts)
        assert len(segments) == 0

    def test_extract_gr_rect_with_stroke_fill_attributes(self):
        """Test extracting edge from gr_rect with KiCad 7/8 stroke/fill attributes.

        KiCad 7+ includes stroke and fill attributes with nested parentheses.
        The regex must handle these nested structures correctly.
        See issue #318.
        """
        # KiCad 7/8 format with stroke and fill (nested parentheses)
        pcb_text = """(kicad_pcb
  (gr_rect
    (start 0 0)
    (end 15 15)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
)"""

        segments = _extract_edge_segments(pcb_text)

        # gr_rect should produce 4 edge segments
        assert len(segments) == 4

        # Check that segments form the expected rectangle
        all_points = set()
        for (x1, y1), (x2, y2) in segments:
            all_points.add((x1, y1))
            all_points.add((x2, y2))

        # Should have 4 corner points
        assert (0, 0) in all_points
        assert (15, 0) in all_points
        assert (15, 15) in all_points
        assert (0, 15) in all_points

    def test_extract_gr_line_with_stroke_attributes(self):
        """Test extracting edge from gr_line with KiCad 7/8 stroke attributes.

        See issue #318.
        """
        # KiCad 7/8 format with stroke attribute
        pcb_text = """(kicad_pcb
  (gr_line (start 0 0) (end 50 0)
    (stroke (width 0.1) (type default))
    (layer "Edge.Cuts"))
  (gr_line (start 50 0) (end 50 50)
    (stroke (width 0.1) (type default))
    (layer "Edge.Cuts"))
)"""

        segments = _extract_edge_segments(pcb_text)

        # Should extract 2 line segments
        assert len(segments) == 2
        assert ((0, 0), (50, 0)) in segments
        assert ((50, 0), (50, 50)) in segments


class TestLoadPcbEdgeClearance:
    """Tests for edge_clearance parameter in load_pcb_for_routing."""

    def test_edge_clearance_applied(self, tmp_path):
        """Test that edge_clearance is applied when loading PCB."""
        # Create a minimal PCB file with Edge.Cuts rectangle
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text("""(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))
)""")

        # Load with edge clearance
        router, _ = load_pcb_for_routing(
            str(pcb_file),
            edge_clearance=1.0,
            validate_drc=False,
        )

        # Cells near edge should be blocked
        layer_idx = router.grid.get_routable_indices()[0]
        gx, gy = router.grid.world_to_grid(0.5, 0.5)
        assert router.grid.grid[layer_idx][gy][gx].blocked is True

    def test_no_edge_clearance_by_default(self, tmp_path):
        """Test that no edge clearance is applied by default."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text("""(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))
)""")

        # Load without edge clearance (default)
        router, _ = load_pcb_for_routing(
            str(pcb_file),
            edge_clearance=None,
            validate_drc=False,
        )

        # Edge cells should NOT be blocked (no components yet)
        layer_idx = router.grid.get_routable_indices()[0]
        gx, gy = router.grid.world_to_grid(0.5, 0.5)
        assert router.grid.grid[layer_idx][gy][gx].blocked is False


class TestLoadPcbLayerStackAutoDetection:
    """Tests for layer stack auto-detection in load_pcb_for_routing (Issue #949)."""

    def test_auto_detects_2_layer_board(self, tmp_path):
        """Test that 2-layer board is auto-detected from PCB layers section."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text("""(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
)""")

        router, _ = load_pcb_for_routing(str(pcb_file), validate_drc=False)

        # Should auto-detect 2-layer stack
        assert router.grid.layer_stack.num_layers == 2
        assert "2-Layer" in router.grid.layer_stack.name

        # Should have correct layer mapping for F.Cu and B.Cu
        # F.Cu (Layer enum value 0) -> grid index 0
        # B.Cu (Layer enum value 5) -> grid index 1
        assert router.grid.layer_to_index(Layer.F_CU.value) == 0
        assert router.grid.layer_to_index(Layer.B_CU.value) == 1

    def test_auto_detects_4_layer_board(self, tmp_path):
        """Test that 4-layer board with zones is auto-detected."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text("""(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (zone (net 1) (net_name "GND") (layer "In1.Cu"))
  (zone (net 2) (net_name "+3.3V") (layer "In2.Cu"))
)""")

        router, _ = load_pcb_for_routing(str(pcb_file), validate_drc=False)

        # Should auto-detect 4-layer stack
        assert router.grid.layer_stack.num_layers == 4
        assert "4-Layer" in router.grid.layer_stack.name

        # Inner layers should be detected as planes
        assert len(router.grid.layer_stack.plane_layers) == 2

    def test_explicit_layer_stack_overrides_auto_detection(self, tmp_path):
        """Test that explicit layer_stack parameter overrides auto-detection."""
        pcb_file = tmp_path / "test.kicad_pcb"
        # PCB has 4 layers defined
        pcb_file.write_text("""(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
)""")

        # Explicitly request 2-layer stack
        router, _ = load_pcb_for_routing(
            str(pcb_file),
            layer_stack=LayerStack.two_layer(),
            validate_drc=False,
        )

        # Should use the explicit 2-layer stack, not auto-detect
        assert router.grid.layer_stack.num_layers == 2

    def test_layer_mapping_prevents_invalid_layer_error(self, tmp_path):
        """Test that auto-detection prevents 'Layer value not in stack' errors.

        This is the core fix for Issue #949: When loading a 4-layer PCB without
        specifying a layer stack, pads on inner layers would have Layer.IN1_CU
        (value 1) which doesn't exist in the default 2-layer mapping [0, 5].

        With auto-detection, the correct 4-layer mapping is used.
        """
        pcb_file = tmp_path / "test.kicad_pcb"
        # Create a 4-layer PCB with pads on inner layers
        pcb_file.write_text("""(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "NET1")
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (footprint "Test:R0402"
    (at 120 120)
    (property "Reference" "R1")
    (pad 1 smd rect
      (at 0 0)
      (size 0.5 0.5)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "NET1")
    )
    (pad 2 smd rect
      (at 1 0)
      (size 0.5 0.5)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "NET1")
    )
  )
)""")

        # This should NOT raise "Layer value not in stack" error
        # because layer stack is auto-detected
        router, net_map = load_pcb_for_routing(str(pcb_file), validate_drc=False)

        # Verify pads were loaded successfully
        assert len(router.pads) == 2
        assert "NET1" in net_map


class TestInsertSexpBeforeClosing:
    """Tests for _insert_sexp_before_closing (Issue #1108).

    The route command previously used rstrip(')') to strip the final closing
    parenthesis before inserting routes. This stripped ALL trailing ')' characters,
    not just one, causing unbalanced parentheses in the output PCB file.
    """

    def test_removes_only_last_closing_paren(self):
        """Verify only the last ')' is removed, not all trailing ones."""
        # This PCB has nested structure with ')' at end of inner elements
        pcb_content = '(kicad_pcb\n  (version 20240108)\n  (net 1 "VCC")\n)'
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = _insert_sexp_before_closing(pcb_content, route_sexp)

        # Should still contain the inner closing parens
        assert "(version 20240108)" in result
        assert '(net 1 "VCC")' in result
        assert "(segment" in result
        assert result.strip().endswith(")")

    def test_preserves_nested_structure(self):
        """Ensure deeply nested S-expressions are preserved."""
        pcb_content = "(kicad_pcb\n  (setup\n    (grid_origin 0 0)\n  )\n)"
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = _insert_sexp_before_closing(pcb_content, route_sexp)

        # The setup block should be fully preserved
        assert "(setup" in result
        assert "(grid_origin 0 0)" in result
        assert "(segment" in result
        # Validate parentheses are balanced
        assert _validate_sexp_parentheses(result)

    def test_balanced_parentheses_in_result(self):
        """The critical test: output must have balanced parentheses."""
        pcb_content = '(kicad_pcb\n  (version 20240108)\n  (generator "test")\n)'
        route_sexp = '(segment (start 1 2) (end 3 4) (width 0.25) (layer "F.Cu") (net 1))'

        result = _insert_sexp_before_closing(pcb_content, route_sexp)

        assert _validate_sexp_parentheses(result)

    def test_handles_pcb_ending_with_whitespace(self):
        """Handle PCB files that end with whitespace after the closing paren."""
        pcb_content = "(kicad_pcb\n  (version 20240108)\n)  \n\n"
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = _insert_sexp_before_closing(pcb_content, route_sexp)

        assert _validate_sexp_parentheses(result)
        assert "(segment" in result

    def test_regression_rstrip_bug(self):
        """Regression test for the rstrip(')') bug (Issue #1108).

        The old code used rstrip(')') which strips ALL trailing ')' characters.
        For PCB files ending with ')\\n)', this would strip both, breaking the
        S-expression structure.
        """
        # PCB content that ends with nested closing parens
        pcb_content = (
            '(kicad_pcb\n  (footprint "R0603"\n    (pad 1 smd rect (at 0 0) (size 0.6 0.5))\n  )\n)'
        )
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = _insert_sexp_before_closing(pcb_content, route_sexp)

        # The footprint block must remain intact
        assert '(footprint "R0603"' in result
        assert "(pad 1 smd rect" in result
        # Parentheses must be balanced
        assert _validate_sexp_parentheses(result)

    def test_multiple_sexp_fragments(self):
        """Test inserting multiple fragments (routes + zones)."""
        pcb_content = "(kicad_pcb\n  (version 20240108)\n)"
        combined = "(zone (net 1))\n  (segment (start 0 0) (end 10 10) (width 0.2))"

        result = _insert_sexp_before_closing(pcb_content, combined)

        assert "(zone" in result
        assert "(segment" in result
        assert _validate_sexp_parentheses(result)


class TestValidateSexpParentheses:
    """Tests for _validate_sexp_parentheses (Issue #1108)."""

    def test_balanced_simple(self):
        assert _validate_sexp_parentheses("(kicad_pcb)")

    def test_balanced_nested(self):
        assert _validate_sexp_parentheses('(kicad_pcb (version 1) (net 1 "VCC"))')

    def test_balanced_with_strings(self):
        """Parentheses inside quoted strings should be ignored."""
        assert _validate_sexp_parentheses('(property "Value" "Cap(100nF)")')

    def test_unbalanced_missing_close(self):
        assert not _validate_sexp_parentheses("(kicad_pcb (version 1)")

    def test_unbalanced_extra_close(self):
        assert not _validate_sexp_parentheses("(kicad_pcb))")

    def test_unbalanced_extra_open(self):
        assert not _validate_sexp_parentheses("((kicad_pcb)")

    def test_empty_string(self):
        assert _validate_sexp_parentheses("")

    def test_real_pcb_structure(self):
        """Test with a realistic PCB file structure."""
        pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "VCC")
  (footprint "R0603"
    (at 100 100)
    (pad 1 smd rect
      (at -0.5 0)
      (size 0.6 0.5)
      (layers "F.Cu")
      (net 1 "VCC")
    )
  )
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1))
)"""
        assert _validate_sexp_parentheses(pcb)


# ============================================================================
# TWO-PASS MERGE / EXISTING ROUTE OBSTACLE LOADING TESTS (Issue #1256)
# ============================================================================

# PCB fixture with pre-existing routed geometry (segments + vias) for
# multi-pass routing tests.  Board is 50x40mm at origin (100, 100).
_PCB_WITH_EXISTING_ROUTES = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET_A")
  (net 2 "NET_B")
  (net 3 "NET_C")
  (gr_rect (start 100 100) (end 150 140)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 115 115)
    (fp_text reference "U1" (at 0 -3.5) (layer "F.SilkS"))
    (pad "1" smd rect (at -2.7 -1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET_A"))
    (pad "2" smd rect (at -2.7 -0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "NET_B"))
    (pad "3" smd rect (at -2.7 0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "NET_C"))
    (pad "4" smd rect (at -2.7 1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "5" smd rect (at 2.7 1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "6" smd rect (at 2.7 0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "NET_C"))
    (pad "7" smd rect (at 2.7 -0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "NET_B"))
    (pad "8" smd rect (at 2.7 -1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET_A"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000200")
    (at 135 115)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "NET_A"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "NET_B"))
  )
  (segment (start 112.3000 113.0950) (end 120.0000 113.0950) (width 0.2500) (layer "F.Cu") (net 1) (uuid "seg-a1"))
  (segment (start 120.0000 113.0950) (end 134.4900 115.0000) (width 0.2500) (layer "F.Cu") (net 1) (uuid "seg-a2"))
  (via (at 125.0000 120.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-b1"))
  (segment (start 112.3000 114.3650) (end 125.0000 120.0000) (width 0.2500) (layer "F.Cu") (net 2) (uuid "seg-b1"))
)
"""


class TestParseVias:
    """Tests for the parse_vias function in optimizer/pcb.py."""

    def test_parse_vias_basic(self):
        """parse_vias extracts via coordinates, drill, diameter, net and layers."""
        from kicad_tools.router.optimizer.pcb import parse_vias

        vias_by_net = parse_vias(_PCB_WITH_EXISTING_ROUTES)
        assert "NET_B" in vias_by_net
        net_b_vias = vias_by_net["NET_B"]
        assert len(net_b_vias) == 1

        via = net_b_vias[0]
        assert via.x == pytest.approx(125.0, abs=0.01)
        assert via.y == pytest.approx(120.0, abs=0.01)
        assert via.diameter == pytest.approx(0.6, abs=0.01)
        assert via.drill == pytest.approx(0.3, abs=0.01)
        assert via.net == 2
        assert via.layers[0] == Layer.F_CU
        assert via.layers[1] == Layer.B_CU

    def test_parse_vias_empty(self):
        """parse_vias returns empty dict when no vias exist."""
        from kicad_tools.router.optimizer.pcb import parse_vias

        pcb_no_vias = """(kicad_pcb
  (net 0 "")
  (net 1 "A")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1))
)"""
        result = parse_vias(pcb_no_vias)
        assert result == {}

    def test_parse_vias_multiple_nets(self):
        """parse_vias groups vias by net name correctly."""
        from kicad_tools.router.optimizer.pcb import parse_vias

        pcb = """(kicad_pcb
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (via (at 105.0000 110.0000) (size 0.8000) (drill 0.4000) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
  (via (at 115.0000 120.0000) (size 0.8000) (drill 0.4000) (layers "F.Cu" "B.Cu") (net 2) (uuid "v2"))
  (via (at 125.0000 120.0000) (size 0.8000) (drill 0.4000) (layers "F.Cu" "B.Cu") (net 1) (uuid "v3"))
)"""
        result = parse_vias(pcb)
        assert len(result["VCC"]) == 2
        assert len(result["GND"]) == 1


class TestLoadExistingRoutes:
    """Tests for load_pcb_for_routing with load_existing_routes=True."""

    def _write_pcb(self, tmp_path, content=_PCB_WITH_EXISTING_ROUTES):
        pcb_file = tmp_path / "pass1_routed.kicad_pcb"
        pcb_file.write_text(content)
        return pcb_file

    def test_default_no_existing_routes(self, tmp_path):
        """Default load_existing_routes=False does not block route cells."""
        pcb_file = self._write_pcb(tmp_path)
        router, net_map = load_pcb_for_routing(str(pcb_file), validate_drc=False)
        # With default (False), the existing segment cells should NOT be
        # marked as blocked (only pad cells are blocked).
        # Verify at least that routing completed without error.
        assert router is not None
        assert "NET_A" in net_map

    def test_existing_routes_mark_grid_cells(self, tmp_path):
        """When load_existing_routes=True, existing segment cells are blocked."""
        pcb_file = self._write_pcb(tmp_path)
        router, net_map = load_pcb_for_routing(
            str(pcb_file), validate_drc=False, load_existing_routes=True
        )
        # The existing segment for NET_A goes from (112.3, 113.095)
        # to (120.0, 113.095).  Pick a point along that segment and
        # verify the grid cell is blocked.
        mid_x, mid_y = 116.0, 113.095
        gx, gy = router.grid.world_to_grid(mid_x, mid_y)
        layer_idx = router.grid.layer_to_index(Layer.F_CU.value)

        cell = router.grid.grid[layer_idx][gy][gx]
        assert cell.blocked, (
            "Grid cell along existing segment should be blocked when load_existing_routes=True"
        )

    def test_existing_via_marks_grid_cells(self, tmp_path):
        """When load_existing_routes=True, existing via cells are blocked."""
        pcb_file = self._write_pcb(tmp_path)
        router, net_map = load_pcb_for_routing(
            str(pcb_file), validate_drc=False, load_existing_routes=True
        )
        # The existing via for NET_B is at (125.0, 120.0)
        gx, gy = router.grid.world_to_grid(125.0, 120.0)
        # Via blocks all layers
        for layer_idx in range(router.grid.num_layers):
            cell = router.grid.grid[layer_idx][gy][gx]
            assert cell.blocked, (
                f"Grid cell at via position should be blocked on layer {layer_idx} "
                "when load_existing_routes=True"
            )

    def test_single_pass_unchanged(self, routing_test_pcb):
        """Regression: load_existing_routes=False behaves identically to before."""
        # Load with default (False)
        router1, net_map1 = load_pcb_for_routing(str(routing_test_pcb), validate_drc=False)
        # Load again explicitly False
        router2, net_map2 = load_pcb_for_routing(
            str(routing_test_pcb), validate_drc=False, load_existing_routes=False
        )
        assert net_map1 == net_map2
        assert len(router1.pads) == len(router2.pads)
        assert len(router1.nets) == len(router2.nets)


class TestMergeRoutesViaConflicts:
    """Tests for merge_routes_into_pcb with co-located via detection."""

    def test_merge_no_conflicts_default(self):
        """Default merge (detect_via_conflicts=False) appends without scanning."""
        original = """(kicad_pcb
  (net 0 "")
  (net 1 "A")
  (via (at 110.0000 110.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
)"""
        new_routes = '(via (at 120.0000 120.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 1) (uuid "v2"))'
        result = merge_routes_into_pcb(original, new_routes)
        # Both vias should be present
        assert result.count("(via") == 2

    def test_merge_detects_colocated_vias(self):
        """detect_via_conflicts=True removes co-located vias on different nets."""
        original = """(kicad_pcb
  (net 0 "")
  (net 1 "A")
  (net 2 "B")
  (via (at 110.0000 110.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
)"""
        # New route adds a via at the same location but on a different net
        new_routes = '(via (at 110.0000 110.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 2) (uuid "v2"))'
        result = merge_routes_into_pcb(
            original, new_routes, detect_via_conflicts=True, via_clearance=0.2
        )
        # The conflicting via (net 2) should have been removed; only net 1 remains
        assert result.count("(via") == 1
        assert "(net 1)" in result

    def test_merge_keeps_same_net_vias(self):
        """Co-located vias on the same net are not removed."""
        original = """(kicad_pcb
  (net 0 "")
  (net 1 "A")
  (via (at 110.0000 110.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
)"""
        # Another via at same position, same net
        new_routes = '(via (at 110.0000 110.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 1) (uuid "v2"))'
        result = merge_routes_into_pcb(
            original, new_routes, detect_via_conflicts=True, via_clearance=0.2
        )
        # Both vias should remain (same net, no conflict)
        assert result.count("(via") == 2

    def test_merge_conflict_within_clearance(self):
        """Vias within clearance distance on different nets are flagged."""
        original = """(kicad_pcb
  (net 0 "")
  (net 1 "A")
  (net 2 "B")
  (via (at 110.0000 110.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
)"""
        # Via 0.1mm away on a different net (within default 0.2mm clearance)
        new_routes = '(via (at 110.0500 110.0500) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 2) (uuid "v2"))'
        result = merge_routes_into_pcb(
            original, new_routes, detect_via_conflicts=True, via_clearance=0.2
        )
        assert result.count("(via") == 1
        assert "(net 1)" in result

    def test_merge_no_conflict_outside_clearance(self):
        """Vias outside clearance distance on different nets are kept."""
        original = """(kicad_pcb
  (net 0 "")
  (net 1 "A")
  (net 2 "B")
  (via (at 110.0000 110.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
)"""
        # Via 5mm away on different net (well outside clearance)
        new_routes = '(via (at 115.0000 115.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 2) (uuid "v2"))'
        result = merge_routes_into_pcb(
            original, new_routes, detect_via_conflicts=True, via_clearance=0.2
        )
        assert result.count("(via") == 2

    def test_backward_compatible_default(self):
        """Default detect_via_conflicts=False preserves exact existing behaviour."""
        original = """(kicad_pcb
  (net 0 "")
  (net 1 "A")
  (net 2 "B")
  (via (at 110.0000 110.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
)"""
        new_routes = '(via (at 110.0000 110.0000) (size 0.6000) (drill 0.3000) (layers "F.Cu" "B.Cu") (net 2) (uuid "v2"))'
        # Without flag, both vias stay (backward compat)
        result = merge_routes_into_pcb(original, new_routes)
        assert result.count("(via") == 2

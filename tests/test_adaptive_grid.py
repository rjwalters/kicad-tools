"""Tests for adaptive grid routing — fine grid near pads, coarse grid in channels.

Issue #1135: Adaptive grid routing for fine-pitch components.
"""

import math

import pytest

from kicad_tools.router.adaptive_grid import (
    AdaptiveGridResult,
    AdaptiveGridRouter,
    identify_fine_pitch_components,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.subgrid import SubGridResult, compute_subgrid_resolution


def make_pad(
    x: float,
    y: float,
    net: int,
    ref: str,
    pin: str,
    width: float = 0.3,
    height: float = 0.8,
    net_name: str = "",
    layer: Layer = Layer.F_CU,
    through_hole: bool = False,
) -> Pad:
    """Helper to create Pad objects."""
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=net_name or f"NET{net}",
        ref=ref,
        pin=pin,
        layer=layer,
        through_hole=through_hole,
    )


def make_grid_and_rules(
    width: float = 30.0,
    height: float = 30.0,
    resolution: float = 0.1,
    trace_width: float = 0.2,
    trace_clearance: float = 0.15,
) -> tuple[RoutingGrid, DesignRules]:
    """Create grid and rules for testing."""
    rules = DesignRules(
        grid_resolution=resolution,
        trace_width=trace_width,
        trace_clearance=trace_clearance,
    )
    grid = RoutingGrid(width=width, height=height, rules=rules)
    return grid, rules


class TestIdentifyFinePitchComponents:
    """Tests for identify_fine_pitch_components()."""

    def test_ssop_detected(self):
        """SSOP at 0.65mm pitch should be identified as fine-pitch."""
        pads = {
            ("U1", "1"): make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            ("U1", "2"): make_pad(x=1.65, y=1.0, net=2, ref="U1", pin="2"),
            ("U1", "3"): make_pad(x=2.30, y=1.0, net=3, ref="U1", pin="3"),
        }

        result = identify_fine_pitch_components(pads, coarse_resolution=0.1)

        assert "U1" in result
        assert result["U1"] < 0.1  # Fine resolution must be less than coarse

    def test_tssop_detected(self):
        """TSSOP at 0.5mm pitch should be identified as fine-pitch."""
        pads = {
            ("U2", "1"): make_pad(x=5.0, y=5.0, net=1, ref="U2", pin="1"),
            ("U2", "2"): make_pad(x=5.5, y=5.0, net=2, ref="U2", pin="2"),
        }

        result = identify_fine_pitch_components(pads, coarse_resolution=0.1)

        assert "U2" in result

    def test_through_hole_not_flagged(self):
        """Through-hole at 2.54mm pitch should NOT be identified as fine-pitch."""
        pads = {
            ("J1", "1"): make_pad(x=1.0, y=1.0, net=1, ref="J1", pin="1",
                                  through_hole=True, width=1.7, height=1.7),
            ("J1", "2"): make_pad(x=3.54, y=1.0, net=2, ref="J1", pin="2",
                                  through_hole=True, width=1.7, height=1.7),
        }

        result = identify_fine_pitch_components(pads, coarse_resolution=0.1)

        assert "J1" not in result

    def test_mixed_board(self):
        """Mixed board should only flag fine-pitch components."""
        pads = {
            ("U1", "1"): make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            ("U1", "2"): make_pad(x=1.65, y=1.0, net=2, ref="U1", pin="2"),
            ("R1", "1"): make_pad(x=10.0, y=10.0, net=3, ref="R1", pin="1"),
            ("R1", "2"): make_pad(x=12.0, y=10.0, net=4, ref="R1", pin="2"),
        }

        result = identify_fine_pitch_components(pads, coarse_resolution=0.1)

        assert "U1" in result
        assert "R1" not in result  # 2.0mm pitch is not fine

    def test_empty_pads(self):
        """Empty pad dict should return empty result."""
        result = identify_fine_pitch_components({}, coarse_resolution=0.1)
        assert len(result) == 0

    def test_single_pad_component(self):
        """Component with only one pad should not be flagged (no pitch)."""
        pads = [make_pad(x=1.0, y=1.0, net=1, ref="TP1", pin="1")]
        result = identify_fine_pitch_components(pads, coarse_resolution=0.1)
        assert "TP1" not in result

    def test_custom_threshold(self):
        """Custom fine_pitch_threshold should control detection sensitivity."""
        pads = {
            ("U1", "1"): make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            ("U1", "2"): make_pad(x=1.65, y=1.0, net=2, ref="U1", pin="2"),
        }

        # Threshold below 0.65mm should NOT flag U1
        result_strict = identify_fine_pitch_components(
            pads, coarse_resolution=0.1, fine_pitch_threshold=0.5
        )
        assert "U1" not in result_strict

        # Threshold above 0.65mm should flag U1
        result_loose = identify_fine_pitch_components(
            pads, coarse_resolution=0.1, fine_pitch_threshold=0.8
        )
        assert "U1" in result_loose


class TestAdaptiveGridRouter:
    """Tests for AdaptiveGridRouter."""

    def test_construction(self):
        """Router should be constructable with grid and rules."""
        grid, rules = make_grid_and_rules()
        router = AdaptiveGridRouter(grid, rules)

        assert router.grid is grid
        assert router.rules is rules
        assert router.fine_pitch_threshold == 0.8

    def test_custom_threshold(self):
        """Custom fine_pitch_threshold should be respected."""
        grid, rules = make_grid_and_rules()
        router = AdaptiveGridRouter(grid, rules, fine_pitch_threshold=0.5)
        assert router.fine_pitch_threshold == 0.5

    def test_phase1_detects_fine_pitch(self):
        """Phase 1 should detect and escape fine-pitch pads."""
        grid, rules = make_grid_and_rules()
        router = AdaptiveGridRouter(grid, rules)

        # SSOP-like pads at 0.65mm pitch
        pads = {}
        for i in range(4):
            key = ("U1", str(i + 1))
            pad = make_pad(
                x=5.0 + i * 0.65, y=5.0, net=i + 1, ref="U1", pin=str(i + 1)
            )
            pads[key] = pad
            grid.add_pad(pad)

        escape_result, escape_routes, fine_res = router._phase1_pad_escape(pads)

        assert "U1" in fine_res
        assert fine_res["U1"] < 0.1
        assert isinstance(escape_result, SubGridResult)

    def test_phase1_skips_on_grid(self):
        """Phase 1 should skip components where all pads are on-grid."""
        grid, rules = make_grid_and_rules()
        router = AdaptiveGridRouter(grid, rules)

        # 2.54mm pitch - all on grid
        pads = {
            ("J1", "1"): make_pad(x=5.0, y=5.0, net=1, ref="J1", pin="1",
                                  through_hole=True),
            ("J1", "2"): make_pad(x=7.54, y=5.0, net=2, ref="J1", pin="2",
                                  through_hole=True),
        }
        for p in pads.values():
            grid.add_pad(p)

        escape_result, escape_routes, fine_res = router._phase1_pad_escape(pads)

        assert len(fine_res) == 0
        assert len(escape_routes) == 0

    def test_route_adaptive_with_fn(self):
        """route_adaptive should work with a custom route function."""
        grid, rules = make_grid_and_rules()
        router = AdaptiveGridRouter(grid, rules)

        pads = {}
        for i in range(4):
            key = ("U1", str(i + 1))
            pad = make_pad(
                x=5.0 + i * 0.65, y=5.0, net=i + 1, ref="U1", pin=str(i + 1)
            )
            pads[key] = pad
            grid.add_pad(pad)

        nets = {
            1: [("U1", "1"), ("U1", "2")],
            3: [("U1", "3"), ("U1", "4")],
        }

        # Mock route function
        mock_routes = [
            Route(net=1, net_name="NET1", segments=[
                Segment(x1=5.0, y1=5.0, x2=5.7, y2=5.0, width=0.2,
                        layer=Layer.F_CU, net=1),
            ]),
        ]

        result = router.route_adaptive(nets, pads, route_fn=lambda: mock_routes)

        assert isinstance(result, AdaptiveGridResult)
        assert result.nets_attempted == 2
        assert result.nets_routed == 1
        assert result.coarse_resolution == 0.1

    def test_route_adaptive_no_fine_pitch(self):
        """Adaptive routing with no fine-pitch components should still work."""
        grid, rules = make_grid_and_rules()
        router = AdaptiveGridRouter(grid, rules)

        pads = {
            ("R1", "1"): make_pad(x=5.0, y=5.0, net=1, ref="R1", pin="1"),
            ("R1", "2"): make_pad(x=7.0, y=5.0, net=1, ref="R1", pin="2"),
        }
        for p in pads.values():
            grid.add_pad(p)

        nets = {1: [("R1", "1"), ("R1", "2")]}

        mock_routes = [
            Route(net=1, net_name="NET1", segments=[
                Segment(x1=5.0, y1=5.0, x2=7.0, y2=5.0, width=0.2,
                        layer=Layer.F_CU, net=1),
            ]),
        ]

        result = router.route_adaptive(nets, pads, route_fn=lambda: mock_routes)

        assert result.escaped_pads == 0
        assert result.nets_routed == 1
        assert len(result.fine_resolutions) == 0


class TestAdaptiveGridResult:
    """Tests for AdaptiveGridResult dataclass."""

    def test_default_values(self):
        """Default result should have zero values."""
        result = AdaptiveGridResult()

        assert result.escaped_pads == 0
        assert result.failed_escapes == 0
        assert len(result.all_routes) == 0
        assert result.total_time_ms == 0.0

    def test_all_routes_combines(self):
        """all_routes should combine escape and main routes."""
        escape = Route(net=1, net_name="NET1", segments=[
            Segment(x1=1.0, y1=1.0, x2=1.1, y2=1.0, width=0.2,
                    layer=Layer.F_CU, net=1),
        ])
        main = Route(net=1, net_name="NET1", segments=[
            Segment(x1=1.1, y1=1.0, x2=5.0, y2=5.0, width=0.2,
                    layer=Layer.F_CU, net=1),
        ])

        result = AdaptiveGridResult(
            escape_routes=[escape],
            main_routes=[main],
        )

        assert len(result.all_routes) == 2

    def test_total_time(self):
        """total_time_ms should sum both phases."""
        result = AdaptiveGridResult(
            phase1_time_ms=100.0,
            phase2_time_ms=200.0,
        )
        assert result.total_time_ms == 300.0

    def test_format_summary(self):
        """format_summary should produce readable output."""
        result = AdaptiveGridResult(
            coarse_resolution=0.1,
            fine_resolutions={"U1": 0.025},
            phase1_time_ms=50.0,
            phase2_time_ms=150.0,
            nets_attempted=10,
            nets_routed=8,
        )

        summary = result.format_summary()

        assert "0.100mm" in summary
        assert "0.0250mm" in summary
        assert "8/10" in summary

    def test_escaped_pads_with_result(self):
        """escaped_pads should reflect SubGridResult success_count."""
        from kicad_tools.router.subgrid import SubGridEscape

        subgrid_result = SubGridResult(
            escapes=[
                SubGridEscape(
                    pad=make_pad(x=1.65, y=1.0, net=1, ref="U1", pin="1"),
                    segment=Segment(
                        x1=1.65, y1=1.0, x2=1.7, y2=1.0,
                        width=0.2, layer=Layer.F_CU, net=1,
                    ),
                    grid_point=(17, 10),
                    snap_point=(1.7, 1.0),
                ),
            ],
            failed_pads=[
                make_pad(x=2.95, y=1.0, net=2, ref="U1", pin="2"),
            ],
        )

        result = AdaptiveGridResult(escape_result=subgrid_result)

        assert result.escaped_pads == 1
        assert result.failed_escapes == 1


class TestAdaptiveGridSSOP:
    """Integration test: SSOP-20 component with adaptive grid routing."""

    def _make_ssop20_pads(
        self, base_x: float = 10.0, base_y: float = 10.0
    ) -> dict[tuple[str, str], Pad]:
        """Create SSOP-20 like pads (10 per side, 0.65mm pitch)."""
        pads = {}

        # Left side
        for i in range(10):
            key = ("U1", str(i + 1))
            pads[key] = make_pad(
                x=base_x,
                y=base_y + i * 0.65,
                net=i + 1,
                ref="U1",
                pin=str(i + 1),
            )

        # Right side
        for i in range(10):
            key = ("U1", str(i + 11))
            pads[key] = make_pad(
                x=base_x + 6.0,
                y=base_y + i * 0.65,
                net=i + 11,
                ref="U1",
                pin=str(i + 11),
            )

        return pads

    def test_ssop20_escape_routing(self):
        """SSOP-20 should have off-grid pads escaped in Phase 1."""
        grid, rules = make_grid_and_rules(width=30.0, height=30.0)
        router = AdaptiveGridRouter(grid, rules)

        pads = self._make_ssop20_pads()
        for p in pads.values():
            grid.add_pad(p)

        escape_result, escape_routes, fine_res = router._phase1_pad_escape(pads)

        assert "U1" in fine_res
        assert fine_res["U1"] < 0.1
        assert escape_result.analysis is not None
        assert escape_result.analysis.has_off_grid_pads
        assert escape_result.success_count > 0

    def test_ssop20_full_adaptive(self):
        """Full adaptive routing should handle SSOP-20 with mock channel routing."""
        grid, rules = make_grid_and_rules(width=30.0, height=30.0)
        router = AdaptiveGridRouter(grid, rules)

        pads = self._make_ssop20_pads()
        for p in pads.values():
            grid.add_pad(p)

        nets = {i + 1: [("U1", str(i + 1))] for i in range(20)}

        mock_channel_routes = [
            Route(net=1, net_name="NET1", segments=[
                Segment(x1=10.0, y1=10.0, x2=16.0, y2=10.0,
                        width=0.2, layer=Layer.F_CU, net=1),
            ]),
        ]

        result = router.route_adaptive(
            nets, pads, route_fn=lambda: mock_channel_routes
        )

        assert isinstance(result, AdaptiveGridResult)
        assert result.escaped_pads > 0
        assert "U1" in result.fine_resolutions
        assert result.coarse_resolution == 0.1

    def test_mixed_board_ssop_and_tht(self):
        """Mixed board should escape SSOP pads but skip THT."""
        grid, rules = make_grid_and_rules(width=50.0, height=50.0)
        router = AdaptiveGridRouter(grid, rules)

        pads = self._make_ssop20_pads()

        # Add through-hole connector
        for i in range(4):
            key = ("J1", str(i + 1))
            pads[key] = make_pad(
                x=30.0 + i * 2.54,
                y=25.0,
                net=i + 30,
                ref="J1",
                pin=str(i + 1),
                through_hole=True,
                width=1.7,
                height=1.7,
            )

        for p in pads.values():
            grid.add_pad(p)

        _, _, fine_res = router._phase1_pad_escape(pads)

        assert "U1" in fine_res
        assert "J1" not in fine_res


class TestAdaptiveGridTimings:
    """Tests for timing attributes in AdaptiveGridResult."""

    def test_timing_recorded(self):
        """Phase timings should be recorded."""
        grid, rules = make_grid_and_rules()
        router = AdaptiveGridRouter(grid, rules)

        pads = {
            ("U1", "1"): make_pad(x=5.0, y=5.0, net=1, ref="U1", pin="1"),
            ("U1", "2"): make_pad(x=5.65, y=5.0, net=2, ref="U1", pin="2"),
        }
        for p in pads.values():
            grid.add_pad(p)

        nets = {1: [("U1", "1"), ("U1", "2")]}

        result = router.route_adaptive(nets, pads, route_fn=lambda: [])

        assert result.phase1_time_ms >= 0
        assert result.phase2_time_ms >= 0
        assert result.total_time_ms >= 0

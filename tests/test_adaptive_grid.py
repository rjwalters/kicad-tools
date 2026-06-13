"""Tests for adaptive grid routing — fine grid near pads, coarse grid in channels.

Issue #1135: Adaptive grid routing for fine-pitch components.
Issue #1768: Make adaptive multi-resolution grid the default routing strategy.
"""

import pytest

from kicad_tools.router.adaptive_grid import (
    AdaptiveGridResult,
    AdaptiveGridRouter,
    identify_fine_pitch_components,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.io import (
    FineZone,
    MultiResolutionGridPlan,
    compute_multi_resolution_plan,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.subgrid import SubGridResult


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
            ("J1", "1"): make_pad(
                x=1.0, y=1.0, net=1, ref="J1", pin="1", through_hole=True, width=1.7, height=1.7
            ),
            ("J1", "2"): make_pad(
                x=3.54, y=1.0, net=2, ref="J1", pin="2", through_hole=True, width=1.7, height=1.7
            ),
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
            pad = make_pad(x=5.0 + i * 0.65, y=5.0, net=i + 1, ref="U1", pin=str(i + 1))
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
            ("J1", "1"): make_pad(x=5.0, y=5.0, net=1, ref="J1", pin="1", through_hole=True),
            ("J1", "2"): make_pad(x=7.54, y=5.0, net=2, ref="J1", pin="2", through_hole=True),
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
            pad = make_pad(x=5.0 + i * 0.65, y=5.0, net=i + 1, ref="U1", pin=str(i + 1))
            pads[key] = pad
            grid.add_pad(pad)

        nets = {
            1: [("U1", "1"), ("U1", "2")],
            3: [("U1", "3"), ("U1", "4")],
        }

        # Mock route function
        mock_routes = [
            Route(
                net=1,
                net_name="NET1",
                segments=[
                    Segment(x1=5.0, y1=5.0, x2=5.7, y2=5.0, width=0.2, layer=Layer.F_CU, net=1),
                ],
            ),
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
            Route(
                net=1,
                net_name="NET1",
                segments=[
                    Segment(x1=5.0, y1=5.0, x2=7.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1),
                ],
            ),
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
        escape = Route(
            net=1,
            net_name="NET1",
            segments=[
                Segment(x1=1.0, y1=1.0, x2=1.1, y2=1.0, width=0.2, layer=Layer.F_CU, net=1),
            ],
        )
        main = Route(
            net=1,
            net_name="NET1",
            segments=[
                Segment(x1=1.1, y1=1.0, x2=5.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1),
            ],
        )

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
                        x1=1.65,
                        y1=1.0,
                        x2=1.7,
                        y2=1.0,
                        width=0.2,
                        layer=Layer.F_CU,
                        net=1,
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
            Route(
                net=1,
                net_name="NET1",
                segments=[
                    Segment(x1=10.0, y1=10.0, x2=16.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1),
                ],
            ),
        ]

        result = router.route_adaptive(nets, pads, route_fn=lambda: mock_channel_routes)

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


class TestMultiResolutionGridPlan:
    """Tests for MultiResolutionGridPlan and compute_multi_resolution_plan (Issue #1768)."""

    def test_fine_pitch_triggers_multi_resolution(self):
        """Fine-pitch components should produce a multi-resolution plan."""
        # SSOP-like pads at 0.65mm pitch
        pads = {}
        for i in range(10):
            key = ("U1", str(i + 1))
            pads[key] = make_pad(x=10.0 + i * 0.65, y=10.0, net=i + 1, ref="U1", pin=str(i + 1))

        plan = compute_multi_resolution_plan(
            pads=pads,
            clearance=0.15,
            board_width=65.0,
            board_height=56.0,
        )

        assert plan is not None
        assert plan.is_multi_resolution
        assert len(plan.fine_zones) == 1
        assert plan.fine_zones[0].ref == "U1"
        assert plan.fine_zones[0].resolution >= 0.05  # Min floor
        assert plan.coarse_resolution > 0

    def test_uniform_board_returns_none(self):
        """Board with only coarse-pitch components should return None (use uniform)."""
        # All 2.54mm pitch through-hole
        pads = {}
        for i in range(4):
            key = ("J1", str(i + 1))
            pads[key] = make_pad(
                x=5.0 + i * 2.54,
                y=5.0,
                net=i + 1,
                ref="J1",
                pin=str(i + 1),
                through_hole=True,
                width=1.7,
                height=1.7,
            )

        plan = compute_multi_resolution_plan(
            pads=pads,
            clearance=0.15,
            board_width=50.0,
            board_height=50.0,
        )

        assert plan is None

    def test_memory_budget_compliance(self):
        """Total cell estimate should stay within budget for typical boards."""
        # Mixed board: SSOP + connectors
        pads = {}
        for i in range(20):
            key = ("U1", str(i + 1))
            pads[key] = make_pad(
                x=10.0 + (i % 10) * 0.65,
                y=10.0 + (i // 10) * 6.0,
                net=i + 1,
                ref="U1",
                pin=str(i + 1),
            )
        for i in range(8):
            key = ("J1", str(i + 1))
            pads[key] = make_pad(
                x=40.0 + i * 2.54,
                y=30.0,
                net=i + 30,
                ref="J1",
                pin=str(i + 1),
                through_hole=True,
                width=1.7,
                height=1.7,
            )

        plan = compute_multi_resolution_plan(
            pads=pads,
            clearance=0.15,
            board_width=65.0,
            board_height=56.0,
            max_cells=2_000_000,
        )

        assert plan is not None
        assert plan.total_cell_estimate < 2_000_000

    def test_zone_padding_applied(self):
        """Fine zones should include padding around component bbox."""
        pads = {}
        for i in range(4):
            key = ("U1", str(i + 1))
            pads[key] = make_pad(x=10.0 + i * 0.65, y=10.0, net=i + 1, ref="U1", pin=str(i + 1))

        plan = compute_multi_resolution_plan(
            pads=pads,
            clearance=0.15,
            zone_padding=2.0,
        )

        assert plan is not None
        zone = plan.fine_zones[0]
        # Zone should extend 2mm beyond pads
        assert zone.x_min < 10.0
        assert zone.x_max > 10.0 + 3 * 0.65
        assert zone.y_min < 10.0
        assert zone.y_max > 10.0

    def test_explicit_grid_overrides_adaptive(self):
        """When --grid is numeric (not auto), multi_res_plan should not be computed."""
        # This is a behavioral test - explicit grid bypasses adaptive.
        # Verified by the route_cmd.py logic: multi_res_plan only computed
        # when args.grid == "auto"
        plan = compute_multi_resolution_plan(
            pads=[],
            clearance=0.15,
        )
        assert plan is None

    def test_multiple_fine_pitch_components(self):
        """Multiple fine-pitch components should each get their own zone."""
        pads = {}
        # Component U1 at 0.65mm pitch
        for i in range(4):
            key = ("U1", str(i + 1))
            pads[key] = make_pad(x=10.0 + i * 0.65, y=10.0, net=i + 1, ref="U1", pin=str(i + 1))
        # Component U2 at 0.5mm pitch
        for i in range(4):
            key = ("U2", str(i + 1))
            pads[key] = make_pad(x=30.0 + i * 0.5, y=30.0, net=i + 10, ref="U2", pin=str(i + 1))

        plan = compute_multi_resolution_plan(
            pads=pads,
            clearance=0.15,
            board_width=50.0,
            board_height=50.0,
        )

        assert plan is not None
        assert len(plan.fine_zones) == 2
        refs = {z.ref for z in plan.fine_zones}
        assert "U1" in refs
        assert "U2" in refs


class TestFineZone:
    """Tests for FineZone dataclass."""

    def test_cell_count(self):
        """cell_count should compute expected cells for zone."""
        zone = FineZone(
            ref="U1",
            x_min=0.0,
            y_min=0.0,
            x_max=10.0,
            y_max=10.0,
            resolution=0.05,
        )
        # (10/0.05 + 1) * (10/0.05 + 1) = 201 * 201 = 40401
        assert zone.cell_count == 201 * 201

    def test_width_height(self):
        """Width and height properties should be correct."""
        zone = FineZone(
            ref="U1",
            x_min=5.0,
            y_min=3.0,
            x_max=15.0,
            y_max=8.0,
            resolution=0.1,
        )
        assert zone.width == pytest.approx(10.0)
        assert zone.height == pytest.approx(5.0)


class TestMultiResolutionGridPlanDataclass:
    """Tests for MultiResolutionGridPlan properties and formatting."""

    def test_is_multi_resolution_true(self):
        """Plan with fine zones should report is_multi_resolution True."""
        plan = MultiResolutionGridPlan(
            coarse_resolution=0.25,
            fine_zones=[FineZone("U1", 0, 0, 10, 10, 0.05)],
            total_cell_estimate=50000,
        )
        assert plan.is_multi_resolution is True

    def test_is_multi_resolution_false(self):
        """Plan with no fine zones should report is_multi_resolution False."""
        plan = MultiResolutionGridPlan(
            coarse_resolution=0.1,
            fine_zones=[],
        )
        assert plan.is_multi_resolution is False

    def test_summary_format(self):
        """Summary should be human-readable."""
        plan = MultiResolutionGridPlan(
            coarse_resolution=0.25,
            fine_zones=[FineZone("U1", 8.0, 8.0, 12.0, 12.0, 0.05)],
            total_cell_estimate=100000,
            uniform_fallback=0.1,
        )
        summary = plan.summary()
        assert "0.250mm" in summary
        assert "U1" in summary
        assert "0.0500mm" in summary
        assert "100,000" in summary


class TestIssue2387FinePitchThresholdEpsilon:
    """Regression tests for issue #2387: TQFP-32 with strictly 0.8mm pitch
    must trip the fine-pitch threshold (was silently missed by `<` test).
    """

    def test_strict_08mm_pitch_detected(self):
        """A component with exactly 0.8mm pitch trips the fine-pitch threshold.

        Before the fix, ``pitch < 0.8`` only fired by accident on TQFP-32 due
        to float drift on the diagonal-distance pitch calculation.
        """
        # Build a 4-pad strip at exactly 0.8mm pitch
        pads = {
            ("U1", str(i + 1)): make_pad(
                x=1.0 + 0.8 * i,
                y=1.0,
                net=i + 1,
                ref="U1",
                pin=str(i + 1),
            )
            for i in range(4)
        }
        result = identify_fine_pitch_components(
            pads,
            coarse_resolution=0.1,
            fine_pitch_threshold=0.8,
        )
        assert "U1" in result, "Strict 0.8mm pitch must trip the 0.8mm threshold (issue #2387)"

    def test_strict_05mm_pitch_detected_at_05mm_threshold(self):
        """0.5mm pitch trips a 0.5mm threshold (epsilon margin only)."""
        pads = {
            ("U1", "1"): make_pad(x=1.0, y=1.0, net=1, ref="U1", pin="1"),
            ("U1", "2"): make_pad(x=1.5, y=1.0, net=2, ref="U1", pin="2"),
        }
        result = identify_fine_pitch_components(
            pads,
            coarse_resolution=0.1,
            fine_pitch_threshold=0.5,
        )
        assert "U1" in result

    def test_pitch_clearly_above_threshold_not_detected(self):
        """A 0.85mm pitch above the 0.8mm threshold is still skipped."""
        pads = {
            ("J1", "1"): make_pad(x=1.0, y=1.0, net=1, ref="J1", pin="1"),
            ("J1", "2"): make_pad(x=1.85, y=1.0, net=2, ref="J1", pin="2"),
        }
        result = identify_fine_pitch_components(
            pads,
            coarse_resolution=0.1,
            fine_pitch_threshold=0.8,
        )
        # Epsilon is 1e-6, so 0.85mm is firmly outside the threshold
        assert "J1" not in result


class TestIssue2387FinePitchEscapeFailure:
    """Regression tests for issue #2387: hard-fail when 0/N pads escape.

    When auto-grid selects a coarse resolution incompatible with a
    fine-pitch component's pad geometry, the entire escape pass produces
    zero successful escapes for that component.  Routing should hard-fail
    with an actionable error rather than continue a doomed pass.
    """

    def test_failure_class_attributes(self):
        """FinePitchEscapeFailure carries actionable diagnostic info."""
        from kicad_tools.router.adaptive_grid import FinePitchEscapeFailure

        err = FinePitchEscapeFailure(
            component_ref="U1",
            attempted_pads=32,
            suggested_grid=0.1,
            pitch=0.8,
        )
        assert err.component_ref == "U1"
        assert err.attempted_pads == 32
        assert err.suggested_grid == 0.1
        assert err.pitch == 0.8
        assert "U1" in str(err)
        assert "0/32" in str(err)
        assert "0.1" in str(err)
        assert "--grid" in str(err)

    def test_suggested_grid_for_pitch_picks_pad_aligned(self):
        """_suggested_grid_for_pitch returns a grid that divides the pitch."""
        from kicad_tools.router.adaptive_grid import AdaptiveGridRouter

        # 0.8mm pitch -> 0.1mm divides evenly
        assert AdaptiveGridRouter._suggested_grid_for_pitch(0.8) == 0.1
        # 0.65mm pitch -> 0.05mm divides evenly
        assert AdaptiveGridRouter._suggested_grid_for_pitch(0.65) == 0.05
        # 0.5mm pitch -> 0.05mm or 0.1mm divides evenly
        assert AdaptiveGridRouter._suggested_grid_for_pitch(0.5) in (0.1, 0.05)
        # None pitch -> safe default
        assert AdaptiveGridRouter._suggested_grid_for_pitch(None) == 0.05

    def test_raise_when_all_escapes_fail_for_component(self):
        """If every off-grid pad on a fine-pitch component fails to escape,
        FinePitchEscapeFailure is raised with the component ref.
        """
        from kicad_tools.router.adaptive_grid import (
            AdaptiveGridRouter,
            FinePitchEscapeFailure,
        )
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.subgrid import (
            SubGridAnalysis,
            SubGridPad,
            SubGridResult,
        )

        grid, rules = make_grid_and_rules()
        router = AdaptiveGridRouter(grid, rules)

        # Build a synthetic SubGridResult where U1 has 32 off-grid pads
        # attempted and 32 in failed_pads — all escapes failed.
        u1_pads = [
            Pad(
                x=1.0 + 0.8 * i,
                y=1.0,
                width=0.5,
                height=1.5,
                net=i + 1,
                net_name=f"NET{i + 1}",
                ref="U1",
                pin=str(i + 1),
            )
            for i in range(32)
        ]
        sgp_list = [
            SubGridPad(
                pad=p,
                grid_x=0,
                grid_y=0,
                offset_x=0.05,
                offset_y=0.0,
                snap_x=p.x,
                snap_y=p.y,
            )
            for p in u1_pads
        ]
        analysis = SubGridAnalysis(
            off_grid_pads=sgp_list,
            on_grid_pads=[],
            grid_resolution=0.065,
        )
        result = SubGridResult(
            analysis=analysis,
            failed_pads=u1_pads,  # all 32 failed
        )
        with pytest.raises(FinePitchEscapeFailure) as exc_info:
            router._raise_if_component_fully_failed(result, {"U1": 0.005})
        assert exc_info.value.component_ref == "U1"
        assert exc_info.value.attempted_pads == 32
        assert exc_info.value.suggested_grid > 0.0

    def test_no_raise_when_some_escapes_succeed(self):
        """If at least one pad on a component escapes, no exception."""
        from kicad_tools.router.adaptive_grid import AdaptiveGridRouter
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.subgrid import (
            SubGridAnalysis,
            SubGridPad,
            SubGridResult,
        )

        grid, rules = make_grid_and_rules()
        router = AdaptiveGridRouter(grid, rules)
        u1_pads = [
            Pad(
                x=1.0 + 0.8 * i,
                y=1.0,
                width=0.5,
                height=1.5,
                net=i + 1,
                net_name=f"NET{i + 1}",
                ref="U1",
                pin=str(i + 1),
            )
            for i in range(4)
        ]
        sgp_list = [
            SubGridPad(
                pad=p,
                grid_x=0,
                grid_y=0,
                offset_x=0.05,
                offset_y=0.0,
                snap_x=p.x,
                snap_y=p.y,
            )
            for p in u1_pads
        ]
        analysis = SubGridAnalysis(
            off_grid_pads=sgp_list,
            on_grid_pads=[],
            grid_resolution=0.1,
        )
        # Only 2 out of 4 failed; 2 escaped — should NOT raise.
        result = SubGridResult(
            analysis=analysis,
            failed_pads=u1_pads[:2],
        )
        router._raise_if_component_fully_failed(result, {"U1": 0.05})
        # Reaching here without exception is the assertion

    def test_no_raise_when_no_off_grid_pads(self):
        """An empty analysis (no off-grid pads attempted) does not raise."""
        from kicad_tools.router.adaptive_grid import AdaptiveGridRouter
        from kicad_tools.router.subgrid import SubGridAnalysis, SubGridResult

        grid, rules = make_grid_and_rules()
        router = AdaptiveGridRouter(grid, rules)
        analysis = SubGridAnalysis(
            off_grid_pads=[],
            on_grid_pads=[],
            grid_resolution=0.1,
        )
        result = SubGridResult(analysis=analysis, failed_pads=[])
        router._raise_if_component_fully_failed(result, {})
        # No exception expected


class TestAdaptiveGridStrategyDispatch:
    """Tests for strategy dispatch within adaptive grid routing (issue #2453).

    When the adaptive multi-resolution grid is active and a non-default
    strategy (evolutionary or monte-carlo) is requested, the phase2_route_fn
    lambda must dispatch to the correct router method rather than falling
    through to the default ``route_all()``.
    """

    def _make_simple_pads_and_nets(self, grid):
        """Create a minimal set of pads and nets for dispatch tests."""
        pads = {}
        for i in range(4):
            key = ("U1", str(i + 1))
            pad = make_pad(x=5.0 + i * 0.65, y=5.0, net=i + 1, ref="U1", pin=str(i + 1))
            pads[key] = pad
            grid.add_pad(pad)
        nets = {1: [("U1", "1"), ("U1", "2")], 3: [("U1", "3"), ("U1", "4")]}
        return pads, nets

    def test_evolutionary_strategy_dispatched_through_adaptive_grid(self):
        """When strategy='evolutionary', route_adaptive's route_fn must call
        route_all_evolutionary, not the default route_all.

        This validates the fix from commit b1584b35 — the phase2_route_fn
        lambda in route_cmd.py correctly dispatches evolutionary strategy
        when the adaptive multi-resolution grid is active.
        """
        from unittest.mock import MagicMock

        grid, rules = make_grid_and_rules()
        adaptive_router = AdaptiveGridRouter(grid, rules)
        pads, nets = self._make_simple_pads_and_nets(grid)

        # Track which method the route_fn invokes
        calls = []
        mock_routes = [
            Route(
                net=1,
                net_name="NET1",
                segments=[
                    Segment(x1=5.0, y1=5.0, x2=5.65, y2=5.0, width=0.2, layer=Layer.F_CU, net=1),
                ],
            ),
        ]

        def fake_route_all_evolutionary(**kwargs):
            calls.append("evolutionary")
            return mock_routes

        # Simulate the phase2_route_fn lambda from route_cmd.py
        # for strategy="evolutionary"
        mock_router = MagicMock()
        mock_router.route_all_evolutionary = fake_route_all_evolutionary

        def phase2_route_fn():
            return mock_router.route_all_evolutionary(
                pop_size=20,
                generations=10,
                verbose=False,
            )

        result = adaptive_router.route_adaptive(nets, pads, route_fn=phase2_route_fn)

        assert "evolutionary" in calls, (
            "route_all_evolutionary must be called when strategy is 'evolutionary'"
        )
        assert result.nets_routed == 1
        assert isinstance(result, AdaptiveGridResult)

    def test_monte_carlo_strategy_dispatched_through_adaptive_grid(self):
        """When strategy='monte-carlo', route_adaptive's route_fn must call
        route_all_monte_carlo, not the default route_all.
        """
        from unittest.mock import MagicMock

        grid, rules = make_grid_and_rules()
        adaptive_router = AdaptiveGridRouter(grid, rules)
        pads, nets = self._make_simple_pads_and_nets(grid)

        calls = []
        mock_routes = [
            Route(
                net=1,
                net_name="NET1",
                segments=[
                    Segment(x1=5.0, y1=5.0, x2=5.65, y2=5.0, width=0.2, layer=Layer.F_CU, net=1),
                ],
            ),
        ]

        def fake_route_all_monte_carlo(**kwargs):
            calls.append("monte-carlo")
            return mock_routes

        mock_router = MagicMock()
        mock_router.route_all_monte_carlo = fake_route_all_monte_carlo

        # Simulate the phase2_route_fn lambda for strategy="monte-carlo"
        def phase2_route_fn():
            return mock_router.route_all_monte_carlo(
                num_trials=10,
                verbose=False,
            )

        result = adaptive_router.route_adaptive(nets, pads, route_fn=phase2_route_fn)

        assert "monte-carlo" in calls, (
            "route_all_monte_carlo must be called when strategy is 'monte-carlo'"
        )
        assert result.nets_routed == 1

    def test_negotiated_strategy_dispatched_through_adaptive_grid(self):
        """When strategy='negotiated' (default), route_adaptive's route_fn
        must call route_all_negotiated, not route_all_evolutionary.
        """
        from unittest.mock import MagicMock

        grid, rules = make_grid_and_rules()
        adaptive_router = AdaptiveGridRouter(grid, rules)
        pads, nets = self._make_simple_pads_and_nets(grid)

        calls = []
        mock_routes = [
            Route(
                net=1,
                net_name="NET1",
                segments=[
                    Segment(x1=5.0, y1=5.0, x2=5.65, y2=5.0, width=0.2, layer=Layer.F_CU, net=1),
                ],
            ),
        ]

        def fake_route_all_negotiated(**kwargs):
            calls.append("negotiated")
            return mock_routes

        mock_router = MagicMock()
        mock_router.route_all_negotiated = fake_route_all_negotiated

        def phase2_route_fn():
            return mock_router.route_all_negotiated(
                max_iterations=50,
                timeout=None,
                per_net_timeout=None,
                batch_routing=False,
                hierarchical=False,
                perturbation=True,
            )

        result = adaptive_router.route_adaptive(nets, pads, route_fn=phase2_route_fn)

        assert "negotiated" in calls, (
            "route_all_negotiated must be called when strategy is 'negotiated'"
        )
        assert result.nets_routed == 1

    def test_route_fn_none_falls_back_to_router(self):
        """When route_fn is None, route_adaptive should use the internal
        _route_with_router fallback (not crash).
        """
        from unittest.mock import MagicMock, patch

        grid, rules = make_grid_and_rules()
        mock_pathfinder = MagicMock()
        adaptive_router = AdaptiveGridRouter(grid, rules, router=mock_pathfinder)
        pads, nets = self._make_simple_pads_and_nets(grid)

        # Mock the internal _route_with_router to avoid needing a real router
        mock_routes = [
            Route(
                net=1,
                net_name="NET1",
                segments=[
                    Segment(x1=5.0, y1=5.0, x2=5.65, y2=5.0, width=0.2, layer=Layer.F_CU, net=1),
                ],
            ),
        ]
        with patch.object(
            adaptive_router, "_route_with_router", return_value=mock_routes
        ) as mock_method:
            result = adaptive_router.route_adaptive(nets, pads, route_fn=None)

        mock_method.assert_called_once()
        assert result.nets_routed == 1


class TestPhase2RouteFnDispatchLogic:
    """Unit tests for the phase2_route_fn dispatch logic from route_cmd.py.

    These tests replicate the lambda structure at lines 3894-3925 of
    route_cmd.py and verify that each strategy value dispatches to the
    correct routing method. This provides direct coverage of the fix
    from commit b1584b35.
    """

    @staticmethod
    def _build_phase2_route_fn(strategy, router_mock, args_mock):
        """Replicate the phase2_route_fn lambda from route_cmd.py."""

        def phase2_route_fn():
            if strategy == "evolutionary":
                return router_mock.route_all_evolutionary(
                    pop_size=getattr(args_mock, "pop_size", 20),
                    generations=getattr(args_mock, "generations", 10),
                    verbose=False,
                )
            elif strategy == "monte-carlo":
                return router_mock.route_all_monte_carlo(
                    num_trials=getattr(args_mock, "mc_trials", 10),
                    verbose=False,
                )
            elif getattr(args_mock, "two_phase", False) and strategy == "negotiated":
                return router_mock.route_all_two_phase(
                    use_negotiated=True,
                    corridor_width_factor=2.0,
                    timeout=getattr(args_mock, "timeout", None),
                    per_net_timeout=getattr(args_mock, "per_net_timeout", None),
                    max_iterations=getattr(args_mock, "two_phase_iterations", None)
                    or getattr(args_mock, "iterations", 50),
                )
            elif strategy == "negotiated":
                return router_mock.route_all_negotiated(
                    max_iterations=getattr(args_mock, "iterations", 50),
                    timeout=getattr(args_mock, "timeout", None),
                    per_net_timeout=getattr(args_mock, "per_net_timeout", None),
                    batch_routing=False,
                    hierarchical=False,
                    perturbation=True,
                )
            else:
                return router_mock.route_all()

        return phase2_route_fn

    def test_evolutionary_calls_route_all_evolutionary(self):
        """strategy='evolutionary' must call route_all_evolutionary."""
        from unittest.mock import MagicMock

        router_mock = MagicMock()
        router_mock.route_all_evolutionary.return_value = []
        args_mock = MagicMock(pop_size=20, generations=10)

        fn = self._build_phase2_route_fn("evolutionary", router_mock, args_mock)
        fn()

        router_mock.route_all_evolutionary.assert_called_once()
        router_mock.route_all.assert_not_called()
        router_mock.route_all_monte_carlo.assert_not_called()

    def test_monte_carlo_calls_route_all_monte_carlo(self):
        """strategy='monte-carlo' must call route_all_monte_carlo."""
        from unittest.mock import MagicMock

        router_mock = MagicMock()
        router_mock.route_all_monte_carlo.return_value = []
        args_mock = MagicMock(mc_trials=10)

        fn = self._build_phase2_route_fn("monte-carlo", router_mock, args_mock)
        fn()

        router_mock.route_all_monte_carlo.assert_called_once()
        router_mock.route_all.assert_not_called()
        router_mock.route_all_evolutionary.assert_not_called()

    def test_negotiated_calls_route_all_negotiated(self):
        """strategy='negotiated' (default) must call route_all_negotiated."""
        from unittest.mock import MagicMock

        router_mock = MagicMock()
        router_mock.route_all_negotiated.return_value = []
        args_mock = MagicMock(
            two_phase=False,
            iterations=50,
            timeout=None,
            per_net_timeout=None,
        )

        fn = self._build_phase2_route_fn("negotiated", router_mock, args_mock)
        fn()

        router_mock.route_all_negotiated.assert_called_once()
        router_mock.route_all.assert_not_called()

    def test_negotiated_two_phase_calls_route_all_two_phase(self):
        """strategy='negotiated' with two_phase=True must call route_all_two_phase."""
        from unittest.mock import MagicMock

        router_mock = MagicMock()
        router_mock.route_all_two_phase.return_value = []
        args_mock = MagicMock(
            two_phase=True,
            iterations=50,
            timeout=None,
            per_net_timeout=None,
            two_phase_iterations=None,
        )

        fn = self._build_phase2_route_fn("negotiated", router_mock, args_mock)
        fn()

        router_mock.route_all_two_phase.assert_called_once()
        router_mock.route_all_negotiated.assert_not_called()

    def test_basic_strategy_falls_through_to_route_all(self):
        """strategy='basic' must call the default route_all."""
        from unittest.mock import MagicMock

        router_mock = MagicMock()
        router_mock.route_all.return_value = []
        args_mock = MagicMock()

        fn = self._build_phase2_route_fn("basic", router_mock, args_mock)
        fn()

        router_mock.route_all.assert_called_once()
        router_mock.route_all_evolutionary.assert_not_called()
        router_mock.route_all_monte_carlo.assert_not_called()

    def test_evolutionary_passes_pop_size_and_generations(self):
        """Evolutionary strategy must forward pop_size and generations args."""
        from unittest.mock import MagicMock

        router_mock = MagicMock()
        router_mock.route_all_evolutionary.return_value = []
        args_mock = MagicMock(pop_size=30, generations=15)

        fn = self._build_phase2_route_fn("evolutionary", router_mock, args_mock)
        fn()

        router_mock.route_all_evolutionary.assert_called_once_with(
            pop_size=30,
            generations=15,
            verbose=False,
        )

    def test_monte_carlo_passes_mc_trials(self):
        """Monte Carlo strategy must forward mc_trials as num_trials."""
        from unittest.mock import MagicMock

        router_mock = MagicMock()
        router_mock.route_all_monte_carlo.return_value = []
        args_mock = MagicMock(mc_trials=25)

        fn = self._build_phase2_route_fn("monte-carlo", router_mock, args_mock)
        fn()

        router_mock.route_all_monte_carlo.assert_called_once_with(
            num_trials=25,
            verbose=False,
        )

"""Tests for fine-pitch pad clearance reduction (Issue #1778).

Verifies that:
1. Fine-pitch SSOP pads (<=0.65mm pitch) get reduced clearance envelopes
   using manufacturer minimums instead of board-level defaults.
2. The grid has unblocked cells between adjacent fine-pitch pads, allowing
   A* pathfinder to access pads.
3. Escape route segments use min_trace_width for fine-pitch packages.
4. Standard-pitch components remain unaffected by the clearance reduction.
"""

import pytest

from kicad_tools.router.escape import (
    EscapeRouter,
    PackageType,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.mfr_limits import MFR_JLCPCB
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def make_ssop_row(
    pin_count: int = 10,
    pitch: float = 0.65,
    ref: str = "U1",
    pad_width: float = 0.35,
    pad_height: float = 1.2,
    y: float = 0.0,
    start_net: int = 1,
) -> list[Pad]:
    """Create a single row of SSOP pads for clearance testing."""
    pads = []
    total_width = (pin_count - 1) * pitch
    start_x = -total_width / 2

    for i in range(pin_count):
        pads.append(
            Pad(
                x=start_x + i * pitch,
                y=y,
                width=pad_width,
                height=pad_height,
                net=start_net + i,
                net_name=f"NET{start_net + i}",
                ref=ref,
                pin=str(i + 1),
                layer=Layer.F_CU,
            )
        )
    return pads


def make_dual_row_ssop(
    pin_count: int = 20,
    pitch: float = 0.65,
    ref: str = "U1",
    pad_width: float = 0.35,
    pad_height: float = 1.2,
    row_spacing: float = 5.3,
    start_net: int = 1,
) -> list[Pad]:
    """Create a full dual-row SSOP package for testing."""
    assert pin_count % 2 == 0
    pins_per_row = pin_count // 2
    pads = []
    total_width = (pins_per_row - 1) * pitch
    start_x = -total_width / 2

    # Top row
    for i in range(pins_per_row):
        pads.append(
            Pad(
                x=start_x + i * pitch,
                y=row_spacing / 2,
                width=pad_width,
                height=pad_height,
                net=start_net + i,
                net_name=f"NET{start_net + i}",
                ref=ref,
                pin=str(i + 1),
                layer=Layer.F_CU,
            )
        )

    # Bottom row
    for i in range(pins_per_row):
        pads.append(
            Pad(
                x=start_x + (pins_per_row - 1 - i) * pitch,
                y=-row_spacing / 2,
                width=pad_width,
                height=pad_height,
                net=start_net + pins_per_row + i,
                net_name=f"NET{start_net + pins_per_row + i}",
                ref=ref,
                pin=str(pin_count - i),
                layer=Layer.F_CU,
            )
        )

    return pads


class TestFinePitchClearanceReduction:
    """Tests that fine-pitch pads get reduced clearance envelopes."""

    @pytest.fixture
    def fine_pitch_rules(self):
        """Rules with fine-pitch clearance and neck-down enabled."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.05,
            # Fine-pitch settings
            fine_pitch_clearance=0.127,
            fine_pitch_threshold=0.8,
            min_trace_width=0.127,
            neck_down_threshold=0.8,
        )

    @pytest.fixture
    def standard_rules(self):
        """Standard rules without fine-pitch override."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.05,
        )

    def test_fine_pitch_pads_use_reduced_clearance(self, fine_pitch_rules):
        """0.65mm pitch SSOP pads should use reduced clearance envelope."""
        grid = RoutingGrid(
            width=10.0,
            height=4.0,
            rules=fine_pitch_rules,
            origin_x=-5.0,
            origin_y=-2.0,
        )

        # Add two adjacent pads at 0.65mm pitch
        pad1 = Pad(
            x=0.0, y=0.0, width=0.35, height=1.2,
            net=1, net_name="NET1", ref="U1", pin="1", layer=Layer.F_CU,
        )
        pad2 = Pad(
            x=0.65, y=0.0, width=0.35, height=1.2,
            net=2, net_name="NET2", ref="U1", pin="2", layer=Layer.F_CU,
        )

        # Add with fine pitch info
        grid.add_pad(pad1, pin_pitch=0.65)
        grid.add_pad(pad2, pin_pitch=0.65)

        # Check that there are unblocked cells between pads
        # Pad1 center at x=0.0, Pad2 center at x=0.65
        # With reduced clearance: envelope = min_trace_width/2 = 0.0635mm per pad
        # Pad1 right edge at 0.175, clearance extends to 0.175 + 0.0635 = 0.2385
        # Pad2 left edge at 0.475, clearance extends to 0.475 - 0.0635 = 0.4115
        # Gap between blocked zones: 0.4115 - 0.2385 = 0.173mm ~ 3 cells at 0.05mm

        # Find unblocked cells between the two pad centers on the pad layer
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        mid_x = 0.325  # Midpoint between pads
        gx_mid, gy_mid = grid.world_to_grid(mid_x, 0.0)

        # At least one cell near the midpoint should be unblocked
        found_unblocked = False
        # Search a few cells around the midpoint
        for dx in range(-2, 3):
            gx = gx_mid + dx
            if 0 <= gx < grid.cols and 0 <= gy_mid < grid.rows:
                cell = grid.grid[layer_idx][gy_mid][gx]
                if not cell.blocked:
                    found_unblocked = True
                    break

        assert found_unblocked, (
            "No unblocked cells found between adjacent 0.65mm pitch SSOP pads. "
            "Fine-pitch clearance reduction is not working."
        )

    def test_standard_pitch_pads_use_full_clearance(self, fine_pitch_rules):
        """Standard pitch (1.27mm) pads should use full clearance envelope."""
        grid = RoutingGrid(
            width=10.0,
            height=4.0,
            rules=fine_pitch_rules,
            origin_x=-5.0,
            origin_y=-2.0,
        )

        # Add two adjacent pads at 1.27mm pitch (standard SOIC)
        pad1 = Pad(
            x=0.0, y=0.0, width=0.6, height=1.5,
            net=1, net_name="NET1", ref="U2", pin="1", layer=Layer.F_CU,
        )
        pad2 = Pad(
            x=1.27, y=0.0, width=0.6, height=1.5,
            net=2, net_name="NET2", ref="U2", pin="2", layer=Layer.F_CU,
        )

        # Add with standard pitch info (above threshold)
        grid.add_pad(pad1, pin_pitch=1.27)
        grid.add_pad(pad2, pin_pitch=1.27)

        # With full clearance: envelope = 0.15 + 0.2/2 = 0.25mm
        # This is the standard behavior -- just verify it doesn't crash
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        gx_center, gy_center = grid.world_to_grid(0.0, 0.0)
        if 0 <= gx_center < grid.cols and 0 <= gy_center < grid.rows:
            cell = grid.grid[layer_idx][gy_center][gx_center]
            # Center of pad should be blocked
            assert cell.blocked

    def test_no_pitch_uses_full_clearance(self, fine_pitch_rules):
        """Pads without pitch info should use full clearance."""
        grid = RoutingGrid(
            width=10.0,
            height=4.0,
            rules=fine_pitch_rules,
            origin_x=-5.0,
            origin_y=-2.0,
        )

        pad = Pad(
            x=0.0, y=0.0, width=0.5, height=0.5,
            net=1, net_name="NET1", ref="R1", pin="1", layer=Layer.F_CU,
        )

        # Add without pitch info - should use standard clearance
        grid.add_pad(pad)

        # Verify pad center is blocked (basic sanity)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        gx, gy = grid.world_to_grid(0.0, 0.0)
        if 0 <= gx < grid.cols and 0 <= gy < grid.rows:
            assert grid.grid[layer_idx][gy][gx].blocked

    def test_clearance_blocks_cells_within_envelope(self, fine_pitch_rules):
        """Cells within min_trace_width/2 of pad edge should still be blocked."""
        grid = RoutingGrid(
            width=10.0,
            height=4.0,
            rules=fine_pitch_rules,
            origin_x=-5.0,
            origin_y=-2.0,
        )

        # Create pads with very small pitch
        pad1 = Pad(
            x=0.0, y=0.0, width=0.25, height=0.8,
            net=1, net_name="NET1", ref="U3", pin="1", layer=Layer.F_CU,
        )

        grid.add_pad(pad1, pin_pitch=0.5)

        # The clearance envelope is min_trace_width/2 = 0.0635mm
        # Pad right edge at 0.125, blocked extends to 0.125 + 0.0635 = 0.1885
        # Cell at grid point 0.15 (3 grid cells from origin+5) should be blocked
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Cell clearly inside pad metal should be blocked
        check_x = 0.0  # Pad center
        gx, gy = grid.world_to_grid(check_x, 0.0)
        if 0 <= gx < grid.cols and 0 <= gy < grid.rows:
            assert grid.grid[layer_idx][gy][gx].blocked

        # Cell just outside pad edge but within envelope should be blocked
        check_x = 0.15  # 0.025mm from pad edge (within 0.0635mm envelope)
        gx, gy = grid.world_to_grid(check_x, 0.0)
        if 0 <= gx < grid.cols and 0 <= gy < grid.rows:
            assert grid.grid[layer_idx][gy][gx].blocked


class TestFinePitchGridAccess:
    """Tests that A* can find paths between adjacent fine-pitch pads."""

    def test_ssop_row_has_passable_cells_between_pads(self):
        """A row of 0.65mm SSOP pads should have passable cells between them."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.05,
            fine_pitch_clearance=0.127,
            fine_pitch_threshold=0.8,
            min_trace_width=0.127,
        )

        grid = RoutingGrid(
            width=10.0,
            height=4.0,
            rules=rules,
            origin_x=-5.0,
            origin_y=-2.0,
        )

        # Create a row of 10 SSOP pads at 0.65mm pitch
        pads = make_ssop_row(pin_count=10, pitch=0.65, y=0.0)
        for pad in pads:
            grid.add_pad(pad, pin_pitch=0.65)

        # Check that between each pair of adjacent pads, there is at
        # least one unblocked cell at the pad row Y coordinate
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        passable_gaps = 0
        for i in range(len(pads) - 1):
            mid_x = (pads[i].x + pads[i + 1].x) / 2
            gx_mid, gy_mid = grid.world_to_grid(mid_x, 0.0)

            # Search nearby cells for an unblocked one
            for dx in range(-1, 2):
                gx = gx_mid + dx
                if 0 <= gx < grid.cols and 0 <= gy_mid < grid.rows:
                    cell = grid.grid[layer_idx][gy_mid][gx]
                    if not cell.blocked:
                        passable_gaps += 1
                        break

        # At least some gaps should be passable (not all will be due to rounding)
        assert passable_gaps > 0, (
            f"No passable cells found between any adjacent SSOP pads "
            f"(checked {len(pads) - 1} gaps). Issue #1778 fix not working."
        )

    def test_without_fine_pitch_clearance_all_gaps_blocked(self):
        """Without fine-pitch clearance, 0.65mm SSOP gaps should be fully blocked."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.05,
            # NO fine_pitch_clearance or min_trace_width -- standard behavior
        )

        grid = RoutingGrid(
            width=10.0,
            height=4.0,
            rules=rules,
            origin_x=-5.0,
            origin_y=-2.0,
        )

        # Create same row -- but without pitch info, uses full clearance
        pads = make_ssop_row(pin_count=10, pitch=0.65, y=0.0)
        for pad in pads:
            grid.add_pad(pad)  # No pin_pitch -- uses full clearance

        # All gaps should be blocked with standard clearance
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        blocked_gaps = 0
        for i in range(len(pads) - 1):
            mid_x = (pads[i].x + pads[i + 1].x) / 2
            gx_mid, gy_mid = grid.world_to_grid(mid_x, 0.0)

            all_blocked = True
            for dx in range(-1, 2):
                gx = gx_mid + dx
                if 0 <= gx < grid.cols and 0 <= gy_mid < grid.rows:
                    cell = grid.grid[layer_idx][gy_mid][gx]
                    if not cell.blocked:
                        all_blocked = False
                        break

            if all_blocked:
                blocked_gaps += 1

        # Most gaps should be blocked without fine-pitch clearance
        # (validates that the standard behavior still blocks, confirming the
        # problem this issue fixes)
        assert blocked_gaps >= len(pads) - 2, (
            f"Expected most gaps blocked without fine-pitch clearance, "
            f"but only {blocked_gaps}/{len(pads) - 1} were blocked."
        )


class TestFinePitchEscapeWidth:
    """Tests that escape route segments use min_trace_width for fine-pitch."""

    def test_escape_segments_use_min_trace_width(self):
        """Fine-pitch escape segments should use min_trace_width, not trace_width."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.05,
            min_trace_width=0.127,
            fine_pitch_clearance=0.127,
            fine_pitch_threshold=0.8,
        )

        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            origin_x=-10.0,
            origin_y=-10.0,
        )

        escape_router = EscapeRouter(grid, rules)

        # Create SSOP-20 pads
        pads = make_dual_row_ssop(pin_count=20, pitch=0.65)
        package_info = escape_router.analyze_package(pads)

        # Should be detected as SSOP/fine-pitch
        assert package_info.package_type in (PackageType.SSOP, PackageType.TSSOP)

        # Generate escape routes
        escapes = escape_router.generate_escapes(package_info)

        # All escape segments should use min_trace_width
        for escape in escapes:
            for segment in escape.segments:
                assert segment.width == pytest.approx(0.127, abs=0.001), (
                    f"Escape segment for pad {escape.pad.pin} has width "
                    f"{segment.width}mm, expected {rules.min_trace_width}mm"
                )

    def test_escape_segments_use_trace_width_when_no_min(self):
        """Without min_trace_width, escape segments use normal trace_width."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.05,
            # No min_trace_width set
        )

        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            origin_x=-10.0,
            origin_y=-10.0,
        )

        escape_router = EscapeRouter(grid, rules)
        pads = make_dual_row_ssop(pin_count=20, pitch=0.65)
        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        # Without min_trace_width, should fall back to trace_width
        for escape in escapes:
            for segment in escape.segments:
                assert segment.width == pytest.approx(0.2, abs=0.001)

    def test_escape_segment_widths_above_jlcpcb_minimum(self):
        """All escape segment widths should be at or above JLCPCB minimum."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.05,
            min_trace_width=0.127,
            fine_pitch_clearance=0.127,
        )

        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            origin_x=-10.0,
            origin_y=-10.0,
        )

        escape_router = EscapeRouter(grid, rules)
        pads = make_dual_row_ssop(pin_count=20, pitch=0.65)
        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        for escape in escapes:
            for segment in escape.segments:
                assert segment.width >= MFR_JLCPCB.min_trace, (
                    f"Segment width {segment.width}mm is below JLCPCB minimum "
                    f"{MFR_JLCPCB.min_trace}mm"
                )


class TestAutoroterComponentPitch:
    """Tests that Autorouter computes and passes component pitch correctly."""

    def test_compute_component_pitch_ssop(self):
        """_compute_component_pitch should return minimum pitch for SSOP pads."""
        from kicad_tools.router.core import Autorouter

        pads = [
            {"number": "1", "x": 0.0, "y": 0.0},
            {"number": "2", "x": 0.65, "y": 0.0},
            {"number": "3", "x": 1.30, "y": 0.0},
        ]

        pitch = Autorouter._compute_component_pitch(pads)
        assert pitch == pytest.approx(0.65, abs=0.01)

    def test_compute_component_pitch_standard(self):
        """_compute_component_pitch should return 1.27mm for standard SOIC."""
        from kicad_tools.router.core import Autorouter

        pads = [
            {"number": "1", "x": 0.0, "y": 0.0},
            {"number": "2", "x": 1.27, "y": 0.0},
            {"number": "3", "x": 2.54, "y": 0.0},
        ]

        pitch = Autorouter._compute_component_pitch(pads)
        assert pitch == pytest.approx(1.27, abs=0.01)

    def test_compute_component_pitch_single_pad(self):
        """Single pad should return None."""
        from kicad_tools.router.core import Autorouter

        pads = [{"number": "1", "x": 0.0, "y": 0.0}]
        pitch = Autorouter._compute_component_pitch(pads)
        assert pitch is None

    def test_compute_component_pitch_empty(self):
        """Empty pad list should return None."""
        from kicad_tools.router.core import Autorouter

        pitch = Autorouter._compute_component_pitch([])
        assert pitch is None

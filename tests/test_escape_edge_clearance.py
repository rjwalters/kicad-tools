"""Tests for escape router edge clearance clamping (Issue #2136)."""

import pytest

from kicad_tools.router.escape import (
    EscapeRoute,
    EscapeRouter,
    PackageType,
    get_package_info,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment, Via
from kicad_tools.router.rules import DesignRules


# ==============================================================================
# Helpers
# ==============================================================================


def create_qfp_pads(pins_per_side: int, pitch: float = 0.5, ref: str = "U1") -> list[Pad]:
    """Create pads simulating a QFP package with pins on all 4 sides."""
    pads = []
    net = 1
    half_width = (pins_per_side - 1) * pitch / 2 + 1.0

    # North edge (top)
    for i in range(pins_per_side):
        pads.append(
            Pad(
                x=-half_width + 1.0 + i * pitch,
                y=half_width,
                width=0.3,
                height=0.3,
                net=net,
                net_name=f"NET_{net}",
                layer=Layer.F_CU,
                ref=ref,
                through_hole=False,
            )
        )
        net += 1

    # South edge (bottom)
    for i in range(pins_per_side):
        pads.append(
            Pad(
                x=-half_width + 1.0 + i * pitch,
                y=-half_width,
                width=0.3,
                height=0.3,
                net=net,
                net_name=f"NET_{net}",
                layer=Layer.F_CU,
                ref=ref,
                through_hole=False,
            )
        )
        net += 1

    # East edge (right)
    for i in range(pins_per_side):
        pads.append(
            Pad(
                x=half_width,
                y=-half_width + 1.0 + i * pitch,
                width=0.3,
                height=0.3,
                net=net,
                net_name=f"NET_{net}",
                layer=Layer.F_CU,
                ref=ref,
                through_hole=False,
            )
        )
        net += 1

    # West edge (left)
    for i in range(pins_per_side):
        pads.append(
            Pad(
                x=-half_width,
                y=-half_width + 1.0 + i * pitch,
                width=0.3,
                height=0.3,
                net=net,
                net_name=f"NET_{net}",
                layer=Layer.F_CU,
                ref=ref,
                through_hole=False,
            )
        )
        net += 1

    return pads


def make_rules() -> DesignRules:
    """Create standard design rules for testing."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.25,
    )


def make_grid(width: float = 30.0, height: float = 30.0) -> RoutingGrid:
    """Create a simple routing grid for testing."""
    return RoutingGrid(width, height, make_rules(), origin_x=0, origin_y=0)


# ==============================================================================
# Test: _clamp_to_edge_clearance
# ==============================================================================


class TestClampToEdgeClearance:
    """Tests for the _clamp_to_edge_clearance helper method."""

    def test_no_clamping_when_not_configured(self):
        """Points pass through unchanged when edge clearance is not set."""
        grid = make_grid()
        rules = make_rules()
        router = EscapeRouter(grid, rules)  # No edge_clearance or board_bounds

        assert router._clamp_to_edge_clearance(5.0, 5.0) == (5.0, 5.0)
        assert router._clamp_to_edge_clearance(100.0, 100.0) == (100.0, 100.0)

    def test_no_clamping_when_only_edge_clearance_set(self):
        """Points pass through when edge_clearance is set but board_bounds is None."""
        grid = make_grid()
        rules = make_rules()
        router = EscapeRouter(grid, rules, edge_clearance=0.3)

        assert router._clamp_to_edge_clearance(0.0, 0.0) == (0.0, 0.0)

    def test_no_clamping_when_only_board_bounds_set(self):
        """Points pass through when board_bounds is set but edge_clearance is None."""
        grid = make_grid()
        rules = make_rules()
        router = EscapeRouter(grid, rules, board_bounds=(0.0, 0.0, 30.0, 30.0))

        assert router._clamp_to_edge_clearance(-1.0, -1.0) == (-1.0, -1.0)

    def test_clamping_within_bounds(self):
        """Points inside the clearance zone are not modified."""
        grid = make_grid()
        rules = make_rules()
        router = EscapeRouter(
            grid, rules,
            edge_clearance=0.5,
            board_bounds=(0.0, 0.0, 30.0, 30.0),
        )

        # Well inside the board
        assert router._clamp_to_edge_clearance(15.0, 15.0) == (15.0, 15.0)
        # At the clearance boundary
        assert router._clamp_to_edge_clearance(0.5, 0.5) == (0.5, 0.5)
        assert router._clamp_to_edge_clearance(29.5, 29.5) == (29.5, 29.5)

    def test_clamping_at_board_edge(self):
        """Points exactly on the board edge are clamped inward."""
        grid = make_grid()
        rules = make_rules()
        router = EscapeRouter(
            grid, rules,
            edge_clearance=0.5,
            board_bounds=(0.0, 0.0, 30.0, 30.0),
        )

        # On the left edge -> clamped to 0.5
        x, y = router._clamp_to_edge_clearance(0.0, 15.0)
        assert x == pytest.approx(0.5)
        assert y == pytest.approx(15.0)

        # On the right edge -> clamped to 29.5
        x, y = router._clamp_to_edge_clearance(30.0, 15.0)
        assert x == pytest.approx(29.5)

        # On the top edge -> clamped to 29.5
        x, y = router._clamp_to_edge_clearance(15.0, 30.0)
        assert y == pytest.approx(29.5)

        # On the bottom edge -> clamped to 0.5
        x, y = router._clamp_to_edge_clearance(15.0, 0.0)
        assert y == pytest.approx(0.5)

    def test_clamping_outside_board(self):
        """Points outside the board are clamped to the clearance boundary."""
        grid = make_grid()
        rules = make_rules()
        router = EscapeRouter(
            grid, rules,
            edge_clearance=0.3,
            board_bounds=(0.0, 0.0, 20.0, 20.0),
        )

        x, y = router._clamp_to_edge_clearance(-5.0, -5.0)
        assert x == pytest.approx(0.3)
        assert y == pytest.approx(0.3)

        x, y = router._clamp_to_edge_clearance(25.0, 25.0)
        assert x == pytest.approx(19.7)
        assert y == pytest.approx(19.7)

    def test_clamping_with_nonzero_board_origin(self):
        """Edge clearance works with boards not starting at (0, 0)."""
        grid = make_grid()
        rules = make_rules()
        router = EscapeRouter(
            grid, rules,
            edge_clearance=1.0,
            board_bounds=(10.0, 10.0, 50.0, 40.0),
        )

        # Near left edge of board
        x, y = router._clamp_to_edge_clearance(10.0, 25.0)
        assert x == pytest.approx(11.0)
        assert y == pytest.approx(25.0)

        # Near right edge
        x, y = router._clamp_to_edge_clearance(50.0, 25.0)
        assert x == pytest.approx(49.0)


# ==============================================================================
# Test: generate_escapes with edge clearance
# ==============================================================================


class TestGenerateEscapesEdgeClearance:
    """Tests that generate_escapes clamps escape routes when edge clearance is set."""

    def test_escape_points_within_edge_clearance_zone(self):
        """All escape points must stay within the board edge clearance zone."""
        grid = make_grid(width=40.0, height=40.0)
        rules = make_rules()
        board_bounds = (0.0, 0.0, 40.0, 40.0)
        edge_clearance = 0.5

        router = EscapeRouter(
            grid, rules,
            edge_clearance=edge_clearance,
            board_bounds=board_bounds,
        )

        # Place a QFP near the board edge
        pads = create_qfp_pads(pins_per_side=4, pitch=0.5)
        # Shift all pads close to top-right corner
        for i, pad in enumerate(pads):
            pads[i] = Pad(
                x=pad.x + 37.0,
                y=pad.y + 37.0,
                width=pad.width,
                height=pad.height,
                net=pad.net,
                net_name=pad.net_name,
                layer=pad.layer,
                ref=pad.ref,
                through_hole=pad.through_hole,
            )

        package = router.analyze_package(pads)
        escapes = router.generate_escapes(package)

        min_x = board_bounds[0] + edge_clearance
        min_y = board_bounds[1] + edge_clearance
        max_x = board_bounds[2] - edge_clearance
        max_y = board_bounds[3] - edge_clearance

        for escape in escapes:
            ex, ey = escape.escape_point
            assert ex >= min_x - 1e-6, (
                f"Escape point x={ex} violates left edge clearance (min={min_x})"
            )
            assert ey >= min_y - 1e-6, (
                f"Escape point y={ey} violates bottom edge clearance (min={min_y})"
            )
            assert ex <= max_x + 1e-6, (
                f"Escape point x={ex} violates right edge clearance (max={max_x})"
            )
            assert ey <= max_y + 1e-6, (
                f"Escape point y={ey} violates top edge clearance (max={max_y})"
            )

    def test_no_clamping_when_package_is_centered(self):
        """Escape routes for a centered package should not be clamped."""
        grid = make_grid(width=40.0, height=40.0)
        rules = make_rules()
        board_bounds = (0.0, 0.0, 40.0, 40.0)

        # Without edge clearance
        router_no_ec = EscapeRouter(grid, rules)
        # With edge clearance
        router_with_ec = EscapeRouter(
            grid, rules,
            edge_clearance=0.3,
            board_bounds=board_bounds,
        )

        # Place QFP in the center (well away from edges)
        pads = create_qfp_pads(pins_per_side=4, pitch=0.5)
        for i, pad in enumerate(pads):
            pads[i] = Pad(
                x=pad.x + 20.0,
                y=pad.y + 20.0,
                width=pad.width,
                height=pad.height,
                net=pad.net,
                net_name=pad.net_name,
                layer=pad.layer,
                ref=pad.ref,
                through_hole=pad.through_hole,
            )

        package_no_ec = router_no_ec.analyze_package(pads)
        escapes_no_ec = router_no_ec.generate_escapes(package_no_ec)

        package_with_ec = router_with_ec.analyze_package(pads)
        escapes_with_ec = router_with_ec.generate_escapes(package_with_ec)

        # Centered escape points should be identical (no clamping needed)
        for e_no, e_with in zip(escapes_no_ec, escapes_with_ec, strict=True):
            assert e_no.escape_point[0] == pytest.approx(e_with.escape_point[0], abs=1e-6)
            assert e_no.escape_point[1] == pytest.approx(e_with.escape_point[1], abs=1e-6)

    def test_via_positions_clamped(self):
        """Via positions in escape routes must respect edge clearance."""
        grid = make_grid(width=40.0, height=40.0)
        rules = make_rules()
        board_bounds = (0.0, 0.0, 40.0, 40.0)
        edge_clearance = 1.0

        router = EscapeRouter(
            grid, rules,
            edge_clearance=edge_clearance,
            board_bounds=board_bounds,
        )

        # Place pads near the edge
        pads = create_qfp_pads(pins_per_side=4, pitch=0.5)
        for i, pad in enumerate(pads):
            pads[i] = Pad(
                x=pad.x + 38.0,
                y=pad.y + 38.0,
                width=pad.width,
                height=pad.height,
                net=pad.net,
                net_name=pad.net_name,
                layer=pad.layer,
                ref=pad.ref,
                through_hole=pad.through_hole,
            )

        package = router.analyze_package(pads)
        escapes = router.generate_escapes(package)

        max_x = board_bounds[2] - edge_clearance
        max_y = board_bounds[3] - edge_clearance

        for escape in escapes:
            if escape.via_pos is not None:
                vx, vy = escape.via_pos
                assert vx <= max_x + 1e-6, (
                    f"Via x={vx} violates right edge clearance (max={max_x})"
                )
                assert vy <= max_y + 1e-6, (
                    f"Via y={vy} violates top edge clearance (max={max_y})"
                )

    def test_segment_endpoints_clamped(self):
        """Non-origin segment endpoints must respect edge clearance."""
        grid = make_grid(width=40.0, height=40.0)
        rules = make_rules()
        board_bounds = (0.0, 0.0, 40.0, 40.0)
        edge_clearance = 1.0

        router = EscapeRouter(
            grid, rules,
            edge_clearance=edge_clearance,
            board_bounds=board_bounds,
        )

        # Place near edge
        pads = create_qfp_pads(pins_per_side=4, pitch=0.5)
        for i, pad in enumerate(pads):
            pads[i] = Pad(
                x=pad.x + 38.0,
                y=pad.y + 38.0,
                width=pad.width,
                height=pad.height,
                net=pad.net,
                net_name=pad.net_name,
                layer=pad.layer,
                ref=pad.ref,
                through_hole=pad.through_hole,
            )

        package = router.analyze_package(pads)
        escapes = router.generate_escapes(package)

        max_x = board_bounds[2] - edge_clearance
        max_y = board_bounds[3] - edge_clearance

        for escape in escapes:
            for seg in escape.segments:
                # x2, y2 of all segments should be clamped
                assert seg.x2 <= max_x + 1e-6, (
                    f"Segment endpoint x2={seg.x2} violates edge clearance"
                )
                assert seg.y2 <= max_y + 1e-6, (
                    f"Segment endpoint y2={seg.y2} violates edge clearance"
                )


# ==============================================================================
# Test: MfrLimits integration with edge clearance
# ==============================================================================


class TestMfrEdgeClearanceIntegration:
    """Tests that manufacturer limits provide edge clearance values."""

    def test_all_manufacturers_have_nonzero_edge_clearance(self):
        """All built-in manufacturers should have positive edge clearance."""
        from kicad_tools.router.mfr_limits import MFR_LIMITS

        for name, mfr in MFR_LIMITS.items():
            assert mfr.min_edge_clearance > 0, (
                f"Manufacturer '{name}' has zero edge clearance"
            )

    def test_jlcpcb_edge_clearance_matches_dru(self):
        """JLCPCB edge clearance should match the .kicad_dru file value."""
        from kicad_tools.router.mfr_limits import MFR_JLCPCB

        assert MFR_JLCPCB.min_edge_clearance == pytest.approx(0.3)

    def test_oshpark_edge_clearance_matches_dru(self):
        """OSHPark edge clearance should match the .kicad_dru file value."""
        from kicad_tools.router.mfr_limits import MFR_OSHPARK

        assert MFR_OSHPARK.min_edge_clearance == pytest.approx(0.381)

    def test_pcbway_edge_clearance_matches_dru(self):
        """PCBWay edge clearance should match the .kicad_dru file value."""
        from kicad_tools.router.mfr_limits import MFR_PCBWAY

        assert MFR_PCBWAY.min_edge_clearance == pytest.approx(0.25)

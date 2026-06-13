"""Tests for escape endpoint routing (Issue #2401).

Verifies that after escape routing, the main routing pipeline uses escape
endpoint coordinates (not original pad centers) as A* routing targets, and
that RSMT edges connect escape endpoints correctly.
"""

import pytest

from kicad_tools.router.escape import EscapeRoute
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules


def _make_pad(
    x: float,
    y: float,
    net: int,
    ref: str = "U1",
    pin: str = "1",
    net_name: str = "",
    layer: Layer = Layer.F_CU,
) -> Pad:
    """Create a test pad."""
    return Pad(
        x=x,
        y=y,
        width=0.3,
        height=0.3,
        net=net,
        net_name=net_name or f"NET_{net}",
        layer=layer,
        ref=ref,
        pin=pin,
    )


def _make_escape_route(
    pad: Pad, escape_x: float, escape_y: float, escape_layer: Layer | None = None
) -> EscapeRoute:
    """Create a minimal EscapeRoute for testing."""
    from kicad_tools.router.escape import EscapeDirection

    elayer = escape_layer or pad.layer
    seg = Segment(
        x1=pad.x,
        y1=pad.y,
        x2=escape_x,
        y2=escape_y,
        width=0.15,
        layer=elayer,
        net=pad.net,
        net_name=pad.net_name,
    )
    return EscapeRoute(
        pad=pad,
        direction=EscapeDirection.NORTH,
        escape_point=(escape_x, escape_y),
        escape_layer=elayer,
        segments=[seg],
    )


class TestEscapePadOverrides:
    """Test that _escape_pad_overrides are built correctly."""

    def test_generate_escape_routes_builds_overrides(self):
        """After generate_escape_routes(), _escape_pad_overrides maps
        escaped pad keys to virtual pads at escape endpoint coordinates."""
        from kicad_tools.router.core import Autorouter

        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
        )

        router = Autorouter(width=30.0, height=30.0, rules=rules)

        # Create a dense SSOP-like package (0.65mm pitch, dual-row)
        # Needs >= 4 pads, dual-row, fine pitch
        pads = []
        net = 1
        for i in range(10):
            pads.append(
                {
                    "x": 10.0 + i * 0.65,
                    "y": 13.0,
                    "width": 0.3,
                    "height": 0.8,
                    "net": net,
                    "net_name": f"NET_{net}",
                    "layer": Layer.F_CU,
                    "number": str(i + 1),
                }
            )
            net += 1
        for i in range(10):
            pads.append(
                {
                    "x": 10.0 + i * 0.65,
                    "y": 17.0,
                    "width": 0.3,
                    "height": 0.8,
                    "net": net,
                    "net_name": f"NET_{net}",
                    "layer": Layer.F_CU,
                    "number": str(i + 11),
                }
            )
            net += 1

        router.add_component("U1", pads)

        # Detect and generate escape routes
        dense = router.detect_dense_packages()

        if not dense:
            pytest.skip("Package not detected as dense -- test data may need adjustment")

        escape_routes = router.generate_escape_routes(dense)

        # Verify that overrides were built
        assert len(router._escape_pad_overrides) > 0, (
            "Expected escape pad overrides to be populated after escape routing"
        )

        # Verify each override has escape endpoint coordinates (not original pad)
        for pad_key, virtual_pad in router._escape_pad_overrides.items():
            original_pad = router.pads[pad_key]
            # Virtual pad should be at escape endpoint, not original position
            # (escape routes move pads perpendicular to the row, so at least
            # one coordinate should differ)
            moved = (
                abs(virtual_pad.x - original_pad.x) > 0.01
                or abs(virtual_pad.y - original_pad.y) > 0.01
            )
            assert moved, (
                f"Virtual pad for {pad_key} at ({virtual_pad.x}, {virtual_pad.y}) "
                f"matches original ({original_pad.x}, {original_pad.y}) -- "
                f"escape endpoint not applied"
            )
            # Net and ref/pin should be preserved
            assert virtual_pad.net == original_pad.net
            assert virtual_pad.ref == original_pad.ref
            assert virtual_pad.pin == original_pad.pin


class TestRSMTUsesEscapeEndpoints:
    """Test that build_rsmt uses virtual pad positions."""

    def test_rsmt_edges_use_escape_coordinates(self):
        """build_rsmt extracts (p.x, p.y) from pad objects, so virtual
        pads at escape endpoints should produce RSMT edges connecting
        escape endpoints, not original positions."""
        from kicad_tools.router.algorithms.steiner import build_rsmt

        # Original pad positions
        pad1 = _make_pad(0.0, 0.0, net=1, ref="U1", pin="1")
        pad2 = _make_pad(10.0, 0.0, net=1, ref="U1", pin="2")
        pad3 = _make_pad(5.0, 10.0, net=1, ref="U1", pin="3")

        # Virtual pads at escape endpoints (shifted)
        vpad1 = _make_pad(1.0, 1.0, net=1, ref="U1", pin="1")
        vpad2 = _make_pad(9.0, 1.0, net=1, ref="U1", pin="2")
        vpad3 = _make_pad(5.0, 9.0, net=1, ref="U1", pin="3")

        # Build RSMT with virtual pads
        extended_pads, edges = build_rsmt([vpad1, vpad2, vpad3])

        # All RSMT terminal positions should be at virtual pad coordinates
        for i in range(3):
            assert extended_pads[i].x == [vpad1, vpad2, vpad3][i].x
            assert extended_pads[i].y == [vpad1, vpad2, vpad3][i].y

        # Ensure we got edges
        assert len(edges) >= 2, "Expected at least 2 edges for 3-terminal RSMT"


class TestMixedEscapedAndOriginalPads:
    """Test handling of nets where some pads have escape routes and others do not."""

    def test_mixed_pads_resolved_correctly(self):
        """When a net has some pads with escape routes and some without,
        the override dict correctly returns virtual pads for escaped ones
        and original pads for non-escaped ones."""
        from kicad_tools.router.core import Autorouter

        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
        )

        router = Autorouter(width=30.0, height=30.0, rules=rules)

        # Set up a scenario with mixed pads:
        # Pad A is in the override dict, Pad B is not
        pad_a = _make_pad(5.0, 5.0, net=1, ref="U1", pin="1")
        pad_b = _make_pad(20.0, 20.0, net=1, ref="R1", pin="1")

        router.pads[("U1", "1")] = pad_a
        router.pads[("R1", "1")] = pad_b
        router.nets[1] = [("U1", "1"), ("R1", "1")]

        # Override only pad A
        virtual_a = _make_pad(6.0, 6.0, net=1, ref="U1", pin="1")
        router._escape_pad_overrides[("U1", "1")] = virtual_a

        # Simulate what _route_net_with_corridor does
        pad_keys = router.nets[1]
        pad_objs = [router._escape_pad_overrides.get(p, router.pads[p]) for p in pad_keys]

        # Pad A should be the virtual pad
        assert pad_objs[0].x == 6.0
        assert pad_objs[0].y == 6.0

        # Pad B should be the original pad
        assert pad_objs[1].x == 20.0
        assert pad_objs[1].y == 20.0


class TestEscapeLayerPreserved:
    """Test that virtual pads reflect the escape layer, not original pad layer."""

    def test_virtual_pad_uses_escape_layer(self):
        """When escape routes transition to a different layer, the virtual
        pad at the escape endpoint should be on the escape layer."""
        from kicad_tools.router.core import Autorouter

        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
        )

        router = Autorouter(width=30.0, height=30.0, rules=rules)

        # Create a pad on F.Cu
        pad = _make_pad(10.0, 10.0, net=1, ref="U1", pin="1", layer=Layer.F_CU)
        router.pads[("U1", "1")] = pad

        # Simulate escape route that transitions to B.Cu
        escape = _make_escape_route(pad, 10.0, 12.0, escape_layer=Layer.B_CU)

        # Build override as generate_escape_routes would
        ep_x, ep_y = escape.escape_point
        virtual_pad = Pad(
            x=ep_x,
            y=ep_y,
            width=pad.width,
            height=pad.height,
            net=pad.net,
            net_name=pad.net_name,
            layer=escape.escape_layer,
            ref=pad.ref,
            pin=pad.pin,
            through_hole=pad.through_hole,
            drill=pad.drill,
        )
        router._escape_pad_overrides[("U1", "1")] = virtual_pad

        # Verify layer is B.Cu (escape layer), not F.Cu (original)
        resolved = router._escape_pad_overrides.get(("U1", "1"), router.pads[("U1", "1")])
        assert resolved.layer == Layer.B_CU, f"Expected escape layer B.Cu but got {resolved.layer}"

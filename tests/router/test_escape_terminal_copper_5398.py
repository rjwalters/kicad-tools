"""An escaped routing terminal may only claim copper that actually exists.

Issue #5398.  The main router replaces an escaped pad with a virtual
terminal at the escape endpoint (Issue #2401 / #3183).  Both A* backends
derive a pad-metal region from that terminal's ``width``/``height`` and
waive the blocked-cell / clearance-only checks inside it ("allow entry into
own pad's metal area").  Copying the escaped pad's full outline to the
shifted endpoint therefore synthesized pad metal over bare board, and the
search used that waiver to emit copper the exact post-route validator then
rejected -- the observed board-05 ISENSE_B- rejection placed a 0.2 mm trace
0.119 mm from a foreign GATE_BL via against a 0.15 mm rule, burning every
resume attempt before the slow Python fallback.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import get_backend_info
from kicad_tools.router.escape import (
    EscapeDirection,
    EscapeRoute,
    escape_endpoint_copper_extent,
    escape_endpoint_pad,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

NET = 1
NET_NAME = "ISENSE_B-"
FOREIGN_NET = 2
FOREIGN_NET_NAME = "GATE_BL"


def _aabb(x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
    return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)


def _segment_aabb(seg: Segment) -> tuple[float, float, float, float]:
    half = seg.width / 2
    return (
        min(seg.x1, seg.x2) - half,
        min(seg.y1, seg.y2) - half,
        max(seg.x1, seg.x2) + half,
        max(seg.y1, seg.y2) + half,
    )


def _covered(inner, outers) -> bool:
    """True when every corner of ``inner`` lies inside some ``outers`` box."""
    x1, y1, x2, y2 = inner
    for px, py in ((x1, y1), (x2, y1), (x1, y2), (x2, y2)):
        if not any(
            ox1 - 1e-9 <= px <= ox2 + 1e-9 and oy1 - 1e-9 <= py <= oy2 + 1e-9
            for ox1, oy1, ox2, oy2 in outers
        ):
            return False
    return True


def _dense_package_router() -> Autorouter:
    """A dense dual-row package with tall pins, as on the board-05 sense cluster."""
    rules = DesignRules(trace_width=0.2, trace_clearance=0.2, via_drill=0.3, via_diameter=0.6)
    router = Autorouter(width=30.0, height=30.0, rules=rules)
    pads = []
    net = 1
    for row_y in (13.0, 17.0):
        for column in range(10):
            pads.append(
                {
                    "number": str(net),
                    "x": 10.0 + column * 0.65,
                    "y": row_y,
                    "width": 0.3,
                    "height": 1.55,
                    "net": net,
                    "net_name": f"NET_{net}",
                    "layer": Layer.F_CU,
                }
            )
            net += 1
    router.add_component("U1", pads)
    return router


def test_escape_terminals_never_claim_absent_copper():
    """Every escape override's metal region lies inside real committed copper."""
    router = _dense_package_router()
    packages = router.detect_dense_packages()
    assert packages, "fixture must be classified dense so escapes are generated"
    routes = router.generate_escape_routes(packages)
    assert router._escape_pad_overrides, "escape overrides must be populated"

    copper_by_net: dict[int, list[tuple[float, float, float, float]]] = {}
    for route in routes:
        for seg in route.segments:
            copper_by_net.setdefault(seg.net, []).append(_segment_aabb(seg))
        for via in route.vias:
            copper_by_net.setdefault(via.net, []).append(
                _aabb(via.x, via.y, via.diameter, via.diameter)
            )

    for (ref, pin), terminal in sorted(router._escape_pad_overrides.items()):
        physical = router.pads[(ref, pin)]
        real_copper = [_aabb(physical.x, physical.y, physical.width, physical.height)]
        real_copper += copper_by_net.get(physical.net, [])
        claimed = _aabb(terminal.x, terminal.y, terminal.width, terminal.height)
        assert _covered(claimed, real_copper), (
            f"{ref}.{pin} escape terminal claims metal at {claimed} outside the "
            f"escaped pad and its committed escape conductor {real_copper}"
        )


def test_escape_terminal_extent_uses_conductor_not_pad_outline():
    """A moved endpoint is sized by the escape conductor, clamped to the pad."""
    pad = Pad(10.0, 9.5, 0.3, 3.0, NET, NET_NAME, ref="U3", pin="39")
    stub = Segment(10.0, 10.5, 10.0, 11.475, 0.2, Layer.F_CU, NET, NET_NAME)
    escape = EscapeRoute(
        pad=pad,
        direction=EscapeDirection.NORTH,
        escape_point=(10.0, 11.475),
        escape_layer=Layer.F_CU,
        segments=[stub],
    )
    assert escape_endpoint_copper_extent(escape) == pytest.approx(0.2)

    terminal = escape_endpoint_pad(pad, escape, fallback_width=0.2)
    assert (terminal.x, terminal.y) == (10.0, 11.475)
    assert (terminal.width, terminal.height) == pytest.approx((0.2 / math.sqrt(2),) * 2)
    # Identity is preserved so (ref, pin) still resolves to the physical pad.
    assert (terminal.ref, terminal.pin, terminal.net) == ("U3", "39", NET)
    # The old behaviour copied the pad outline to the endpoint, claiming metal
    # 1.5 mm beyond it; the conductor-sized terminal reaches only 0.1 mm.
    copied_outline_reach = escape.escape_point[1] + pad.height / 2
    assert terminal.y + terminal.height / 2 == pytest.approx(11.475 + 0.1 / math.sqrt(2))
    assert terminal.y + terminal.height / 2 < copied_outline_reach - 1.0


def test_in_pad_rescue_terminal_uses_via_annulus():
    """A via-in-pad rescue terminal is the via's annular ring, not the pad."""
    pad = Pad(10.0, 9.5, 0.3, 1.55, NET, NET_NAME, ref="U3", pin="39")
    via = Via(10.0, 9.5, 0.2, 0.3, (Layer.F_CU, Layer.B_CU), NET, NET_NAME, in_pad=True)
    escape = EscapeRoute(
        pad=pad,
        direction=EscapeDirection.VIA_DOWN,
        escape_point=(10.0, 9.5),
        escape_layer=Layer.B_CU,
        segments=[],
        via=via,
    )
    assert escape_endpoint_copper_extent(escape) == pytest.approx(0.3)
    terminal = escape_endpoint_pad(pad, escape, fallback_width=0.2)
    assert (terminal.width, terminal.height) == pytest.approx((0.3 / math.sqrt(2),) * 2)
    assert terminal.layer is Layer.B_CU


def test_terminal_without_committed_conductor_keeps_pad_copper():
    """An unmoved endpoint keeps the authored pad geometry (real metal)."""
    pad = Pad(10.0, 9.5, 0.3, 1.55, NET, NET_NAME, ref="U3", pin="39", shape="roundrect")
    escape = EscapeRoute(
        pad=pad,
        direction=EscapeDirection.NORTH,
        escape_point=(10.0, 9.5),
        escape_layer=Layer.F_CU,
    )
    assert escape_endpoint_copper_extent(escape) is None
    terminal = escape_endpoint_pad(pad, escape, fallback_width=0.2)
    assert (terminal.width, terminal.height, terminal.shape) == (0.3, 1.55, "roundrect")


def test_coarse_grid_does_not_enlarge_terminal_copper():
    """Grid resolution cannot enlarge a clearance waiver beyond metal."""
    pad = Pad(10.0, 9.5, 0.3, 3.0, NET, NET_NAME, ref="U3", pin="39")
    stub = Segment(10.0, 10.5, 10.0, 11.475, 0.05, Layer.F_CU, NET, NET_NAME)
    escape = EscapeRoute(
        pad=pad,
        direction=EscapeDirection.NORTH,
        escape_point=(10.0, 11.475),
        escape_layer=Layer.F_CU,
        segments=[stub],
    )
    terminal = escape_endpoint_pad(pad, escape, fallback_width=0.2, min_extent=0.127)
    assert (terminal.width, terminal.height) == pytest.approx((0.05 / math.sqrt(2),) * 2)


@pytest.mark.parametrize("force_python", [True, False])
def test_escaped_terminal_does_not_waive_foreign_via_clearance(force_python):
    """Emitted copper clears a foreign via that sits in the escaped pad's shadow.

    The foreign via is placed where the *copied pad outline* would have
    covered it: a straight run from the escape endpoint to the sink passes
    0.10 mm from its copper, against a 0.15 mm rule.  With the terminal
    sized to the escape conductor the search has to route around it.
    """
    if not force_python and not get_backend_info()["available"]:
        pytest.skip("C++ extension unavailable")

    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
    )
    router = Autorouter(
        20,
        20,
        rules=rules,
        force_python=force_python,
        physics_enabled=False,
        per_net_iterations=20000,
    )
    router.add_component(
        "U3",
        [
            {
                "number": "39",
                "x": 10.0,
                "y": 9.5,
                "width": 0.3,
                "height": 3.0,
                "net": NET,
                "net_name": NET_NAME,
                "layer": Layer.F_CU,
            }
        ],
    )
    router.add_component(
        "R11",
        [
            {
                "number": "2",
                "x": 10.0,
                "y": 15.0,
                "width": 0.8,
                "height": 0.8,
                "net": NET,
                "net_name": NET_NAME,
                "layer": Layer.F_CU,
            }
        ],
    )

    # Committed escape stub out of the tall pad, and its endpoint terminal.
    stub = Segment(10.0, 10.5, 10.0, 11.475, 0.2, Layer.F_CU, NET, NET_NAME)
    stub_route = Route(NET, NET_NAME, [stub], is_escape=True)
    router._mark_route(stub_route)
    router.routes.append(stub_route)
    escape = EscapeRoute(
        pad=router.pads[("U3", "39")],
        direction=EscapeDirection.NORTH,
        escape_point=(10.0, 11.475),
        escape_layer=Layer.F_CU,
        segments=[stub],
    )
    router._escape_pad_overrides[("U3", "39")] = router._build_escape_endpoint_pad(
        router.pads[("U3", "39")], escape
    )

    foreign_via = Via(
        10.5,
        12.2,
        rules.via_drill,
        rules.via_diameter,
        (Layer.F_CU, Layer.B_CU),
        FOREIGN_NET,
        FOREIGN_NET_NAME,
    )
    foreign_route = Route(FOREIGN_NET, FOREIGN_NET_NAME, [], [foreign_via])
    router._mark_route(foreign_route)
    router.routes.append(foreign_route)

    routes = router.route_net(NET)
    assert routes, "the escaped sense terminal must still reach its sink"

    via_copper = Point(foreign_via.x, foreign_via.y).buffer(foreign_via.diameter / 2)
    emitted = [seg for route in routes for seg in route.segments]
    assert emitted, "route must emit copper"
    metal = unary_union([LineString([seg.start, seg.end]).buffer(seg.width / 2) for seg in emitted])
    assert metal.distance(via_copper) >= rules.trace_clearance - 1e-6, (
        "emitted copper is closer to the foreign via than the authored clearance; "
        "the escaped terminal is still waiving checks over copper that does not exist"
    )


@pytest.mark.parametrize("rotation", [0.0, 45.0, 90.0])
@pytest.mark.parametrize("local_y", [0.2, 0.75, 0.8])
def test_shifted_terminal_is_contained_in_authored_rectangle(rotation, local_y):
    theta = math.radians(rotation)
    pad = Pad(10, 9.5, 0.3, 1.55, NET, NET_NAME, shape="rect", rotation=rotation)
    escape = EscapeRoute(
        pad=pad,
        direction=EscapeDirection.NORTH,
        escape_point=(pad.x + local_y * math.sin(theta), pad.y + local_y * math.cos(theta)),
        escape_layer=pad.layer,
    )
    if local_y > pad.height / 2:
        with pytest.raises(ValueError, match="no committed copper"):
            escape_endpoint_pad(pad, escape, fallback_width=0.2, min_extent=0.127)
        return
    terminal = escape_endpoint_pad(pad, escape, fallback_width=0.2, min_extent=0.127)
    dx, dy = terminal.x - pad.x, terminal.y - pad.y
    x = abs(dx * math.cos(theta) - dy * math.sin(theta))
    y = abs(dx * math.sin(theta) + dy * math.cos(theta))
    if y > pad.height / 2:
        assert terminal.width == terminal.height == 0
    else:
        assert terminal.width / 2 <= pad.width / 2 - x
        assert terminal.height / 2 <= pad.height / 2 - y
        assert terminal.shape == "rect"


@pytest.mark.parametrize("layer", [Layer.F_CU, Layer.B_CU])
def test_missing_conductor_never_invents_copper(layer):
    pad = Pad(10, 9.5, 0.3, 1.55, NET, NET_NAME, shape="rect")
    escape = EscapeRoute(pad, EscapeDirection.NORTH, (10, 11), layer)
    with pytest.raises(ValueError, match="no committed copper"):
        escape_endpoint_pad(pad, escape, fallback_width=0.2, min_extent=0.127)


def test_endpoint_on_other_layer_cannot_copy_surface_pad():
    pad = Pad(10, 9.5, 0.3, 1.55, NET, NET_NAME, shape="rect")
    escape = EscapeRoute(pad, EscapeDirection.VIA_DOWN, (10, 9.5), Layer.B_CU)
    with pytest.raises(ValueError, match="no committed copper"):
        escape_endpoint_pad(pad, escape, fallback_width=0.2)


def test_offset_conductor_cap_is_not_translated():
    pad = Pad(10, 9.5, 0.3, 1.55, NET, NET_NAME)
    stub = Segment(10, 10.5, 10, 11.5, 0.05, Layer.F_CU, NET, NET_NAME)
    escape = EscapeRoute(pad, EscapeDirection.NORTH, (10, 11.5005), Layer.F_CU, segments=[stub])
    terminal = escape_endpoint_pad(pad, escape, fallback_width=0.2)
    assert terminal.width / 2 + abs(terminal.y - stub.y2) <= stub.width / 2


def test_through_via_terminal_on_inner_layer_uses_real_copper():
    pad = Pad(10, 9.5, 0.3, 1.55, NET, NET_NAME)
    via = Via(10, 9.5, 0.2, 0.3, (Layer.F_CU, Layer.B_CU), NET, NET_NAME)
    escape = EscapeRoute(pad, EscapeDirection.VIA_DOWN, (10, 9.5), Layer.IN1_CU, via=via)
    terminal = escape_endpoint_pad(pad, escape, fallback_width=0.2)
    assert terminal.width == terminal.height == via.diameter / math.sqrt(2)
    assert terminal.layer is Layer.IN1_CU


def test_router_rejects_unbacked_terminal_without_losing_physical_pad():
    router = Autorouter(20, 20, force_python=True)
    pad = Pad(10, 9.5, 0.3, 1.55, NET, NET_NAME, ref="U3", pin="39")
    escape = EscapeRoute(pad, EscapeDirection.NORTH, (10, 11), Layer.F_CU)
    terminal = router._build_escape_endpoint_pad(pad, escape)
    assert terminal is pad
    assert (terminal.x, terminal.y) != escape.escape_point


@pytest.mark.parametrize("force_python", [True, False])
@pytest.mark.parametrize("narrow_off_grid", [False, True])
def test_endpoint_waiver_corner_routes_without_native_retry_fallback(force_python, narrow_off_grid):
    """A valid foreign via near the cap corner must not poison native seeds.

    The narrow control has no grid center inside the endpoint square. It
    must connect using off-grid handling, without inventing a larger waiver.
    """
    if not force_python and not get_backend_info()["available"]:
        pytest.skip("C++ extension unavailable")
    width = 0.05 if narrow_off_grid else 0.2
    endpoint = 10.085 if narrow_off_grid else 10.025
    rules = DesignRules(
        trace_width=width,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        grid_resolution=0.127 if narrow_off_grid else 0.025,
    )
    router = Autorouter(
        20,
        20,
        force_python=force_python,
        physics_enabled=False,
        rules=rules,
        per_net_iterations=20000,
    )
    for ref, x, y in [("U1", 8, endpoint), ("R1", 15, 15)]:
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "DATA",
                }
            ],
        )
    pad = router.pads[("U1", "1")]
    seg = Segment(8, endpoint, endpoint, endpoint, width, Layer.F_CU, 1, "DATA")
    stub = Route(1, "DATA", [seg], is_escape=True)
    router._mark_route(stub)
    router.routes.append(stub)
    escape = EscapeRoute(
        pad, EscapeDirection.EAST, (endpoint, endpoint), Layer.F_CU, segments=[seg]
    )
    terminal = router._build_escape_endpoint_pad(pad, escape)
    router._escape_pad_overrides[("U1", "1")] = terminal
    if force_python:
        bounds = router.router._get_pad_metal_bounds(terminal, width)
    else:
        native_bounds = router.router._compute_pad_bounds(terminal, width)
        bounds = (
            native_bounds.metal_gx1,
            native_bounds.metal_gy1,
            native_bounds.metal_gx2,
            native_bounds.metal_gy2,
        )
    x1, y1, x2, y2 = bounds
    if narrow_off_grid:
        assert x1 > x2 or y1 > y2, "control must have no grid center in the real metal"
    for gx in range(x1, x2 + 1):
        for gy in range(y1, y2 + 1):
            x, y = router.grid.grid_to_world(gx, gy)
            assert math.hypot(x - terminal.x, y - terminal.y) <= width / 2
    if narrow_off_grid and not force_python:
        generic = router.router._compute_pad_bounds(replace(terminal, escape_terminal=False), width)
        assert generic.metal_gx1 <= generic.metal_gx2
        assert generic.metal_gy1 <= generic.metal_gy2
    # All corners of the actual rectangular waiver fit within the cap.
    assert math.hypot(terminal.width / 2, terminal.height / 2) <= width / 2
    via = Via(endpoint + 0.4, endpoint + 0.4, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "OTHER")
    foreign = Route(2, "OTHER", [], [via])
    router._mark_route(foreign)
    router.routes.append(foreign)
    routes = router.route_net(1)
    assert routes, "the real off-grid terminal must connect to its sink"
    if not force_python:
        stats = router.backend_info["fallback_stats"]
        if narrow_off_grid:
            assert stats["fallback_count"] == 1
            assert "exhausted 5 resume attempts" not in str(stats["fallback_reasons"])
        else:
            assert stats["fallback_count"] == 0
    emitted = [s for route in routes for s in route.segments]
    copper = unary_union([LineString([s.start, s.end]).buffer(s.width / 2) for s in emitted])
    assert copper.distance(Point(via.x, via.y).buffer(via.diameter / 2)) >= rules.trace_clearance
    # New route copper contacts both the committed stub and the target pad.
    assert copper.intersects(LineString([seg.start, seg.end]).buffer(seg.width / 2))
    assert copper.intersects(Point(15, 15).buffer(0.4))


@pytest.mark.parametrize("force_python", [True, False])
def test_fine_pitch_escape_keeps_conductor_seeds_without_disabling_pad_inset(force_python):
    """Board 04: pad-tail erosion must not erase a committed escape endpoint."""
    if not force_python and not get_backend_info()["available"]:
        pytest.skip("C++ extension unavailable")
    rules = DesignRules(trace_width=0.2, grid_resolution=0.05, strict_pad_clearance=True)
    router = Autorouter(20, 20, rules=rules, force_python=force_python, physics_enabled=False)
    physical = Pad(8, 10, 0.3, 1.55, NET, NET_NAME, ref="U2", pin="5")
    stub = Segment(8, 10, 10.025, 10.025, 0.2, Layer.F_CU, NET, NET_NAME)
    escape = EscapeRoute(physical, EscapeDirection.EAST, stub.end, Layer.F_CU, segments=[stub])
    terminal = router._build_escape_endpoint_pad(physical, escape)

    def bounds(pad, width):
        if force_python:
            return router.router._get_pad_metal_bounds(pad, width)
        b = router.router._compute_pad_bounds(pad, width)
        return b.metal_gx1, b.metal_gy1, b.metal_gx2, b.metal_gy2

    x1, y1, x2, y2 = bounds(terminal, rules.trace_width)
    assert x1 <= x2 and y1 <= y2, "real escape copper contains legal grid seeds"
    for gx in range(x1, x2 + 1):
        for gy in range(y1, y2 + 1):
            x, y = router.grid.grid_to_world(gx, gy)
            assert math.hypot(x - terminal.x, y - terminal.y) <= stub.width / 2

    # Real-pad tails still need the strict trace-width inset.
    raw = bounds(physical, None)
    inset = bounds(physical, rules.trace_width)
    assert inset[0] >= raw[0] and inset[1] >= raw[1]
    assert inset[2] <= raw[2] and inset[3] <= raw[3]
    assert inset != raw

    unmoved = EscapeRoute(physical, EscapeDirection.EAST, (physical.x, physical.y), physical.layer)
    authored = router._build_escape_endpoint_pad(physical, unmoved)
    assert not authored.escape_terminal
    assert bounds(authored, rules.trace_width) == inset

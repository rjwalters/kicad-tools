"""Regression tests for Issue #6315: clearance-cell ownership between adjacent
fine-pitch pads of different nets.

Board 05's ``PHASE_C`` (U2.9) was unroutable whenever ``PHASE_B`` (U2.8, the
pad 0.65 mm above it) was routed in the same run, even on an empty board and
even with ``PHASE_C`` routed first.  Both nets are 0.4 mm wide.  The cause was
grid ownership, not routing order:

* ``RoutingGrid._add_pad_unsafe`` gave every clearance cell to the FIRST pad
  that touched it -- including the neighbour's own metal rows -- so the whole
  gap between U2.8 and U2.9, and U2.9's top metal row, belonged to
  ``PHASE_B``; and
* the C++ A* trace disc treats every foreign-owned blocked cell as copper, so
  a 0.4 mm ``PHASE_C`` trace had no legal cell to leave its own pad from,
  although a trace on U2.9's centreline clears both neighbours.

The fix: a pad's own metal belongs to it whatever the add order; a halo cell
reached by two different signal nets' pads is owned by the NEAREST pad and
recorded as shared; and the C++ A* resolves pad-derived cells with the exact
segment-vs-pad clearance test instead of trusting ``cell.net``.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available; run `kct build-native`",
)

# Board 05 U2 (TI PWP0028C) geometry: 1.575 x 0.4 mm pads at 0.65 mm pitch,
# 0.065 mm grid, 0.4 mm wide phase traces.
PITCH = 0.65
PAD_W = 1.575
PAD_H = 0.4
RESOLUTION = 0.065
WIDE_TRACE = 0.4
WIDE_CLEARANCE = 0.2
PAD_X = 5.0
PAD_Y0 = 5.0


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=RESOLUTION,
    )


def _u2_pads(net_a: int = 1, net_b: int = 2) -> list[Pad]:
    """Four U2-style pads: unconnected, net A, net B, unconnected."""
    nets = [0, net_a, net_b, 0]
    return [
        Pad(
            x=PAD_X,
            y=PAD_Y0 + i * PITCH,
            width=PAD_W,
            height=PAD_H,
            net=net,
            net_name={0: "", net_a: "PHASE_B", net_b: "PHASE_C"}[net],
            layer=Layer.F_CU,
            ref="U2",
            pin=str(i + 7),
        )
        for i, net in enumerate(nets)
    ]


def _grid_with_pads(order: list[int]) -> tuple[RoutingGrid, list[Pad]]:
    grid = RoutingGrid(
        width=12.0,
        height=12.0,
        rules=_rules(),
        layer_stack=LayerStack.two_layer(),
    )
    pads = _u2_pads()
    for i in order:
        grid.add_pad(pads[i], pin_pitch=PITCH)
    return grid, pads


def _metal_cells(grid: RoutingGrid, pad: Pad) -> list[tuple[int, int]]:
    res = grid.resolution
    gx1 = math.ceil((pad.x - pad.width / 2 - grid.origin_x) / res)
    gx2 = math.floor((pad.x + pad.width / 2 - grid.origin_x) / res)
    gy1 = math.ceil((pad.y - pad.height / 2 - grid.origin_y) / res)
    gy2 = math.floor((pad.y + pad.height / 2 - grid.origin_y) / res)
    return [(gy, gx) for gy in range(gy1, gy2 + 1) for gx in range(gx1, gx2 + 1)]


def _window(grid: RoutingGrid, pads: list[Pad]):
    res = grid.resolution
    gx1 = math.floor((PAD_X - PAD_W / 2 - 1.0 - grid.origin_x) / res)
    gx2 = math.ceil((PAD_X + PAD_W / 2 + 1.0 - grid.origin_x) / res)
    gy1 = math.floor((pads[0].y - 1.0 - grid.origin_y) / res)
    gy2 = math.ceil((pads[-1].y + 1.0 - grid.origin_y) / res)
    return (slice(gy1, gy2 + 1), slice(gx1, gx2 + 1))


class TestPadHaloOwnership:
    """Grid-level ownership rules (Python ``RoutingGrid``, no routing)."""

    @pytest.mark.parametrize("order", [[0, 1, 2, 3], [3, 2, 1, 0], [2, 0, 3, 1]])
    def test_pad_metal_belongs_to_its_own_pad(self, order: list[int]) -> None:
        grid, pads = _grid_with_pads(order)
        for pad in pads:
            nets = {int(grid._net[0, gy, gx]) for gy, gx in _metal_cells(grid, pad)}
            assert nets == {pad.net}, (
                f"U2.{pad.pin} metal carries nets {nets}, expected only {pad.net} (order {order})"
            )

    def test_gap_between_two_signal_pads_is_shared(self) -> None:
        grid, pads = _grid_with_pads([0, 1, 2, 3])
        assert grid._shared_pad_halo, "the U2.8/U2.9 clearance gap must be recorded as shared"
        # Every shared cell lies between the two signal pads' metal.
        res = grid.resolution
        top = pads[1].y + PAD_H / 2
        bottom = pads[2].y - PAD_H / 2
        for key in grid._shared_pad_halo:
            gy = (key // grid.cols) % grid.rows
            wy = grid.origin_y + gy * res
            assert top - res <= wy <= bottom + res

    def test_gap_owner_is_the_nearest_pad(self) -> None:
        """Neither net owns the whole gap: each owns the half next to it."""
        grid, pads = _grid_with_pads([0, 1, 2, 3])
        owners = {int(grid._net.flat[key]) for key in grid._shared_pad_halo}
        assert owners == {pads[1].net, pads[2].net}

    def test_ownership_does_not_depend_on_pad_order(self) -> None:
        """Reverse-order self-check: the same grid whichever pad is added first."""
        grid_fwd, pads = _grid_with_pads([0, 1, 2, 3])
        grid_rev, _ = _grid_with_pads([3, 2, 1, 0])
        win = _window(grid_fwd, pads)
        for name in ("_net", "_blocked", "_pad_blocked", "_is_obstacle"):
            fwd = getattr(grid_fwd, name)[0][win]
            rev = getattr(grid_rev, name)[0][win]
            assert (fwd == rev).all(), f"{name} differs between pad add orders"
        assert grid_fwd._shared_pad_halo == grid_rev._shared_pad_halo


# ---------------------------------------------------------------------------
# End-to-end on the C++ backend
# ---------------------------------------------------------------------------


def _pad_edge_gap(pad: Pad, x1: float, y1: float, x2: float, y2: float) -> float:
    """Distance from segment centreline to an axis-aligned rectangular pad."""
    hw, hh = pad.width / 2, pad.height / 2
    best = math.inf
    steps = 200
    for i in range(steps + 1):
        t = i / steps
        px, py = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        dx = max(abs(px - pad.x) - hw, 0.0)
        dy = max(abs(py - pad.y) - hh, 0.0)
        best = min(best, math.hypot(dx, dy))
    return best


def _route_two_wide_nets(order: tuple[str, str]):
    from kicad_tools.router.core import Autorouter

    ar = Autorouter(width=12.0, height=12.0, rules=_rules())
    for name in ("PHASE_B", "PHASE_C"):
        ar.net_class_map[name] = NetClassRouting(
            name="HighCurrent", trace_width=WIDE_TRACE, clearance=WIDE_CLEARANCE
        )
    u2 = [
        {
            "number": p.pin,
            "x": p.x,
            "y": p.y,
            "width": p.width,
            "height": p.height,
            "net": p.net,
            "net_name": p.net_name,
            "layer": Layer.F_CU,
        }
        for p in _u2_pads()
    ]
    ar.add_component("U2", u2)
    ar.add_component(
        "J2",
        [
            {
                "number": "1",
                "x": 1.0,
                "y": 3.0,
                "width": 1.0,
                "height": 1.0,
                "net": 1,
                "net_name": "PHASE_B",
                "layer": Layer.F_CU,
            },
            {
                "number": "2",
                "x": 1.0,
                "y": 8.5,
                "width": 1.0,
                "height": 1.0,
                "net": 2,
                "net_name": "PHASE_C",
                "layer": Layer.F_CU,
            },
        ],
    )
    if ar.backend_info.get("active") != "cpp":
        pytest.skip("Autorouter did not select the C++ backend")
    routes = {}
    net_ids = {"PHASE_B": 1, "PHASE_C": 2}
    for name in order:
        got = ar.route_net(net_ids[name])
        routes[name] = got
    return ar, routes


@requires_cpp
class TestTwoWideNetsOnAdjacentFinePitchPins:
    """AC 2: two 0.4 mm nets on adjacent 0.65 mm-pitch pins both route, legally."""

    @pytest.mark.parametrize("order", [("PHASE_B", "PHASE_C"), ("PHASE_C", "PHASE_B")])
    def test_both_nets_route_with_legal_clearance(self, order: tuple[str, str]) -> None:
        ar, routes = _route_two_wide_nets(order)
        for name in order:
            assert routes[name], f"{name} did not route (order {order})"

        pads = list(ar.pads.values())
        for route in ar.routes:
            for seg in route.segments:
                for pad in pads:
                    if pad.net == route.net or pad.layer != seg.layer:
                        continue
                    gap = _pad_edge_gap(pad, seg.x1, seg.y1, seg.x2, seg.y2) - seg.width / 2
                    assert gap >= WIDE_CLEARANCE - 1e-3, (
                        f"net {route.net} segment ({seg.x1:.3f},{seg.y1:.3f})-"
                        f"({seg.x2:.3f},{seg.y2:.3f}) is {gap:.3f} mm from "
                        f"{pad.ref}.{pad.pin} (needs {WIDE_CLEARANCE})"
                    )

    def test_cpp_grid_flags_pad_cells_and_shared_halo(self) -> None:
        ar, _ = _route_two_wide_nets(("PHASE_B", "PHASE_C"))
        grid = ar.grid
        cpp = getattr(grid, "_cpp_grid", None)
        if cpp is None:
            pytest.skip("no paired C++ grid")
        assert grid._shared_pad_halo
        key = next(iter(grid._shared_pad_halo))
        gx = key % grid.cols
        gy = (key // grid.cols) % grid.rows
        layer = key // (grid.cols * grid.rows)
        cell = cpp._impl.at(gx, gy, layer)
        assert cell.pad_cell
        assert cell.shared_pad_halo


@requires_cpp
def test_pad_geometry_clear_matches_exact_geometry() -> None:
    """``Grid3D.pad_geometry_clear``: centreline exit legal, off-centre not."""
    grid, pads = _grid_with_pads([0, 1, 2, 3])
    from kicad_tools.router.cpp_backend import CppGrid

    cpp = CppGrid.from_routing_grid(grid)
    impl = cpp._impl
    for pad in pads:
        grid_layer = 0
        impl.add_pad(
            pad.x, pad.y, pad.width, pad.height, pad.net, grid_layer, 0, 0.0, False, 0.0, False
        )
    pc = pads[2]  # net 2, between net 1 above and net 0 below
    half = WIDE_TRACE / 2
    # A 0.4 mm trace on U2.9's centreline, leaving to the left: 0.25 mm from
    # both neighbours' metal, legal at 0.2 mm.
    assert impl.pad_geometry_clear(
        pc.x - 1.5, pc.y, pc.x - 0.5, pc.y, 0, pc.net, half, WIDE_CLEARANCE
    )
    # Shifted 0.1 mm toward U2.8 it is only 0.15 mm from U2.8's metal.
    assert not impl.pad_geometry_clear(
        pc.x - 1.5, pc.y - 0.1, pc.x - 0.5, pc.y - 0.1, 0, pc.net, half, WIDE_CLEARANCE
    )


# ---------------------------------------------------------------------------
# Give-up message: a via in the net's OWN pad is not a via-via block
# ---------------------------------------------------------------------------


@requires_cpp
def test_via_in_own_pad_is_not_reported_as_via_via_block() -> None:
    """A net that cannot leave its own SMD pad (via-in-pad disallowed) must not
    fail with ``FAILURE_VIA_VIA_BLOCKED`` naming its own net -- board 05 printed
    "all via candidates blocked by stored-via geometry (blocking net PHASE_C)"
    for ``PHASE_C`` itself."""
    from kicad_tools.router import router_cpp
    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder

    rules = DesignRules(
        trace_width=0.25,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
        manufacturer="jlcpcb",
    )
    grid = RoutingGrid(
        width=6.0, height=6.0, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    start = Pad(x=2.0, y=2.0, width=0.8, height=0.8, layer=Layer.F_CU, net=1, net_name="N1")
    end = Pad(x=4.5, y=4.5, width=0.8, height=0.8, layer=Layer.B_CU, net=1, net_name="N1")
    grid.add_pad(start)
    grid.add_pad(end)
    cpp_grid = CppGrid.from_routing_grid(grid)
    if cpp_grid._impl.pad_count == 0:
        for p, layer in ((start, 0), (end, 3)):
            cpp_grid._impl.add_pad(
                p.x, p.y, p.width, p.height, p.net, layer, 0, 0.0, False, 0.0, False
            )
    # Wall the start pad in on F.Cu (net-0 static obstacle ring) so its only
    # way out is a via -- which would sit in its own pad.
    gx0, gy0 = grid.world_to_grid(start.x, start.y)
    for dx in range(-8, 9):
        for dy in range(-8, 9):
            if max(abs(dx), abs(dy)) in (7, 8):
                cpp_grid._impl.mark_blocked(gx0 + dx, gy0 + dy, 0, 0, True, False)

    pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    pathfinder.set_routable_layers(cpp_grid.get_routable_indices())
    pathfinder.route(start, end, negotiated_mode=False)
    info = pathfinder.get_last_failure_info()
    if info is None:
        return  # no failure diagnostic at all -- certainly not a self-block
    assert not (
        info["failure_reason"] == router_cpp.FAILURE_VIA_VIA_BLOCKED
        and info["blocking_via_net"] == start.net
    ), f"own pad reported as a via-via blocker: {info}"


# ---------------------------------------------------------------------------
# AC 3: classifier criterion
# ---------------------------------------------------------------------------


def _sparse_decide(search_failure):
    from kicad_tools.router.stuck_classifier import _decide

    return _decide(
        net_name="PHASE_C",
        net_number=30,
        pad_names=["U2.9"],
        blockers=[],
        worst_open_arc=12,
        local_congestion=0,
        nearest_blocker_mm=None,
        congestion_threshold=6,
        dense_cluster_threshold=12,
        search_failure=search_failure,
    )


def test_classifier_geometry_only_still_budget_starved() -> None:
    from kicad_tools.router.stuck_classifier import StuckClass

    assert _sparse_decide(None).classification is StuckClass.BUDGET_STARVED


@pytest.mark.parametrize("reason", ["no_path", "via_via_blocked", "pairwise_blocked"])
def test_classifier_geometric_search_failure_is_not_budget_starved(reason: str) -> None:
    from kicad_tools.router.stuck_classifier import StuckClass

    diag = _sparse_decide(reason)
    assert diag.classification is not StuckClass.BUDGET_STARVED
    assert diag.classification is StuckClass.ESCAPE_BLOCKED
    assert reason in diag.evidence


@pytest.mark.parametrize("reason", ["timeout", "iteration_limit", "deadline-exceeded"])
def test_classifier_budget_search_failure_stays_budget_starved(reason: str) -> None:
    from kicad_tools.router.stuck_classifier import StuckClass

    assert _sparse_decide(reason).classification is StuckClass.BUDGET_STARVED


def test_normalize_search_failure_maps_cpp_codes() -> None:
    from kicad_tools.router.stuck_classifier import (
        is_budget_search_failure,
        normalize_search_failure,
    )

    assert normalize_search_failure(None) is None
    assert normalize_search_failure(0) is None
    assert normalize_search_failure(1) == "no_path"
    assert normalize_search_failure(3) == "timeout"
    assert normalize_search_failure(5) == "via_via_blocked"
    assert is_budget_search_failure("iteration_limit")
    assert not is_budget_search_failure("no_path")

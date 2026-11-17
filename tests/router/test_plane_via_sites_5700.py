"""A plane-net pad's stitch-via site is the router's obstacle (Issue #5700).

Board 06 / diff-pair ``pours=BROKEN`` (Blocker 2 of #5673): once the
exact-disc halo (#5660) stopped over-blocking diagonals, signal routing
legally dropped a via 0.806 mm from ``J1.A4`` (``VBUS_USB``).  With J1's own
neighbouring pads and two passing traces it sealed the pad in a pocket with
no legal via site, and every post-route stitch/repair strategy failed --
correctly, since there was nothing left to find.  The router had no notion
that a plane-net pad (skipped by the trace router) still *owes* a via site.

``reserve_plane_via_sites`` states that invariant before routing: it picks a
legal site per plane-net SMD pad and marks it (and the pad-to-site stub) as
net-0 static obstacles, which both the Python and the C++ pathfinder honour.

The fixture below is a slice of board 06's J1: 0.3 x 0.6 mm pads at 1.0 mm
pitch in two rows 2.0 mm apart, with the plane pad flanked by signal pads.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.plane_via_sites import (
    PlaneViaSiteRules,
    reserve_plane_via_sites,
)
from kicad_tools.router.primitives import Layer, Pad, Route, Segment
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not built")

PLANE = "VBUS_USB"
SIGNAL_NET = 1


def _rules() -> DesignRules:
    # Board 06's router rules (``route_pcb`` in its recipe).
    return DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=0.25,
        via_diameter=0.45,
    )


def _pad(ref: str, pin: str, x: float, y: float, net: int, net_name: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.3,
        height=0.6,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
    )


def _j1_slice() -> RoutingGrid:
    """J1's A3/A4/A5 over B10/B9/B8, plane pads rewritten to net 0.

    ``skip_nets`` gives plane pads net 0 while keeping ``net_name`` -- the
    reservation must find them by name.
    """
    grid = RoutingGrid(
        width=20, height=20, rules=_rules(), layer_stack=LayerStack.four_layer_all_signal()
    )
    for pad in (
        _pad("J1", "A3", 9.0, 10.0, SIGNAL_NET, "USB3_TX1-"),
        _pad("J1", "A4", 10.0, 10.0, 0, PLANE),
        _pad("J1", "A5", 11.0, 10.0, 2, "USB_CC1"),
        _pad("J1", "B10", 9.0, 12.0, 3, "USB3_RX1-"),
        _pad("J1", "B9", 10.0, 12.0, 0, PLANE),
        _pad("J1", "B8", 11.0, 12.0, 4, "+3V3_SIG"),
    ):
        grid.add_pad(pad)
    return grid


def test_every_plane_pad_gets_a_legal_site_outside_the_row_channel() -> None:
    grid = _j1_slice()
    result = reserve_plane_via_sites(grid, [PLANE])

    assert result.unreserved == []
    assert sorted(p for s in result.sites for p in s.pads) == ["J1.A4", "J1.B9"]
    rules = PlaneViaSiteRules()
    via_r = rules.via_diameter / 2
    for site in result.sites:
        for pad in grid._pads:
            gap = math.hypot(pad.x - site.x, pad.y - site.y) - math.hypot(0.15, 0.3)
            floor = via_r if pad.net_name == PLANE else via_r + rules.clearance
            assert gap >= floor - 1e-9, (site, pad.ref, pad.pin)
    # Outward from the component, not into the 10..12 mm channel between the
    # rows, which is where J1's signal pins cross.
    (a4,) = [s for s in result.sites if "J1.A4" in s.pads]
    (b9,) = [s for s in result.sites if "J1.B9" in s.pads]
    assert a4.y < 10.0
    assert b9.y > 12.0


def _encroaching_cells(grid: RoutingGrid, site, copper_radius: float) -> list[tuple[int, int]]:
    """Cells where signal copper of ``copper_radius`` would breach the site.

    A cell encroaches when copper centred there comes within the site rules'
    clearance of the reserved via's copper -- exactly what walled J1.A4 in.
    """
    rules = PlaneViaSiteRules()
    reach = rules.via_diameter / 2 + rules.clearance + copper_radius
    sx, sy = grid.world_to_grid(site.x, site.y)
    span = int(math.ceil(reach / grid.resolution))
    cells = []
    for gy in range(sy - span, sy + span + 1):
        for gx in range(sx - span, sx + span + 1):
            wx, wy = grid.grid_to_world(gx, gy)
            if math.hypot(wx - site.x, wy - site.y) < reach - 1e-9:
                cells.append((gx, gy))
    return cells


def _a4_site():
    return next(s for s in reserve_plane_via_sites(_j1_slice(), [PLANE]).sites if "J1.A4" in s.pads)


def test_reserved_site_refuses_signal_copper_it_would_otherwise_accept() -> None:
    """The invariant, with its premise: the site's space WAS open to routing.

    Without the "before" half this would prove nothing -- cells that were
    already blocked would be refused whether or not the reservation ran.
    """
    rules = _rules()
    site = _a4_site()  # planned on a scratch copy of the same board
    grid = _j1_slice()
    via_cells = _encroaching_cells(grid, site, rules.via_diameter / 2)
    trace_cells = _encroaching_cells(grid, site, rules.trace_width / 2)

    router = Router(grid, rules)
    open_via = [c for c in via_cells if not router._is_via_blocked(*c, 0, SIGNAL_NET)]
    open_trace = [c for c in trace_cells if not router._is_trace_blocked(*c, 0, SIGNAL_NET)]
    assert open_via, "premise: a signal via could have eaten the site pre-reservation"
    assert open_trace, "premise: a signal trace could have eaten the site pre-reservation"

    reserve_plane_via_sites(grid, [PLANE])
    router = Router(grid, rules)
    assert [c for c in via_cells if not router._is_via_blocked(*c, 0, SIGNAL_NET)] == []
    for layer in range(grid.num_layers):
        # A through via needs the site on every layer, so every layer is held.
        leaked = [c for c in trace_cells if not router._is_trace_blocked(*c, layer, SIGNAL_NET)]
        assert leaked == [], layer


@requires_cpp
def test_cpp_grid_attached_before_reservation_sees_it() -> None:
    """Production order: the C++ mirror exists before the reservation runs."""
    rules = _rules()
    site = _a4_site()
    grid = _j1_slice()
    cpp_grid = CppGrid.from_routing_grid(grid)
    via_cells = _encroaching_cells(grid, site, rules.via_diameter / 2)
    trace_cells = _encroaching_cells(grid, site, rules.trace_width / 2)

    before = CppPathfinder(cpp_grid, rules)
    assert [c for c in via_cells if not before._impl.is_via_blocked(*c, SIGNAL_NET, False)]
    assert [c for c in trace_cells if not before._impl.is_trace_blocked(*c, 0, SIGNAL_NET, False)]

    result = reserve_plane_via_sites(grid, [PLANE])
    assert result.cells_blocked > 0
    after = CppPathfinder(cpp_grid, rules)
    for allow_sharing in (False, True):  # standard and negotiated modes
        leaked = [
            c for c in via_cells if not after._impl.is_via_blocked(*c, SIGNAL_NET, allow_sharing)
        ]
        assert leaked == [], allow_sharing
        for layer in range(grid.num_layers):
            leaked = [
                c
                for c in trace_cells
                if not after._impl.is_trace_blocked(*c, layer, SIGNAL_NET, allow_sharing)
            ]
            assert leaked == [], (allow_sharing, layer)


def test_rip_up_does_not_erase_the_reservation() -> None:
    grid = _j1_slice()
    result = reserve_plane_via_sites(grid, [PLANE])
    reserved = {
        (layer, gy, gx)
        for layer in range(grid.num_layers)
        for gy in range(grid.rows)
        for gx in range(grid.cols)
        if grid.cell_at(layer, gy, gx).blocked
    }
    (a4,) = [s for s in result.sites if "J1.A4" in s.pads]
    # A signal trace grazing the site: its halo overlaps reserved cells.
    route = Route(net=SIGNAL_NET, net_name="USB3_TX1-")
    route.segments.append(
        Segment(
            a4.x - 3.0,
            a4.y - 0.5,
            a4.x + 3.0,
            a4.y - 0.5,
            0.15,
            Layer.F_CU,
            SIGNAL_NET,
            "USB3_TX1-",
        )
    )
    grid.mark_route(route, max_trace_width=0.15)
    grid.unmark_route(route, max_trace_width=0.15)
    still = {key for key in reserved if grid.cell_at(*key).blocked}
    assert still == reserved


def test_walled_in_pad_is_reported_not_forced() -> None:
    """No legal site pre-route: report it and leave the grid alone."""
    grid = RoutingGrid(
        width=20, height=20, rules=_rules(), layer_stack=LayerStack.four_layer_all_signal()
    )
    grid.add_pad(_pad("U9", "1", 10.0, 10.0, 0, PLANE))
    for k in range(12):
        angle = 2 * math.pi * k / 12
        grid.add_pad(
            _pad(
                "U9",
                str(k + 2),
                10.0 + 0.8 * math.cos(angle),
                10.0 + 0.8 * math.sin(angle),
                10 + k,
                f"S{k}",
            )
        )
    result = reserve_plane_via_sites(grid, [PLANE], PlaneViaSiteRules(max_reach=1.0))
    assert result.unreserved == ["U9.1"]
    assert result.sites == []
    assert result.cells_blocked == 0


def test_only_the_listed_nets_are_reserved() -> None:
    grid = _j1_slice()
    result = reserve_plane_via_sites(grid, ["GND"])
    assert result.sites == [] and result.unreserved == [] and result.cells_blocked == 0


def test_same_net_neighbours_share_one_site() -> None:
    grid = RoutingGrid(
        width=20, height=20, rules=_rules(), layer_stack=LayerStack.four_layer_all_signal()
    )
    grid.add_pad(_pad("J2", "1", 10.0, 10.0, 0, PLANE))
    grid.add_pad(_pad("J2", "2", 10.8, 10.0, 0, PLANE))
    result = reserve_plane_via_sites(grid, [PLANE])
    assert len(result.sites) == 1
    assert result.sites[0].pads == ("J2.1", "J2.2")

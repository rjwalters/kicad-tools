"""Regression tests for Issue #9734: board 05 VCP demoted by a float32
coordinate round-trip at the C++ backend boundary.

Root cause (measured on the seed-42 board-05 unattended route): the C++
``Segment``/``Via`` structs store float32 coordinates, whose ulp at board
scale (~1.5e-5 mm at 130 mm) round-trips a grid-exact ``132.855`` as
``132.854996``.  The finalize seg-seg SHORT gate
(:func:`demote_seg_seg_finalize`, threshold ``(w_a + w_b) / 2 +
trace_clearance``) measures exact float64 distances on those corrupted
coordinates, so a pair DESIGNED to sit exactly at the threshold -- VCP's
non-rippable escape stub against VM's committed F.Cu trace, centerline
gap nominally 0.325 mm = 0.075 + 0.100 + 0.150 -- lands 3.05 nm short and
VCP is demoted to unrouted; the chained lattice pass then declines
``no-path`` and ``kct route`` exits 3.

The fix snaps every C++-emitted coordinate back onto the 4-decimal
(0.1 um) lattice at the backend boundary -- the same contract
``RoutingGrid.grid_to_world`` applies to every Python-backend cell
coordinate -- so both backends emit identical, lattice-exact geometry.

This file pins both sides of that contract:

1. :class:`TestDemoteSegSegFinalizeVcpShape` -- the detection side: the
   EXACT float32-corrupted pair geometry demotes VCP (victim 25, the
   greedy cover's tie-break), the 4-decimal lattice-exact twin at exactly
   the required pitch does NOT demote (the existing 1e-6 float-noise
   guard), and a genuinely sub-clearance pair still demotes (no loosened
   short detection).
2. :class:`TestCppBackendCoordinateSnap` -- the producer side: routes
   emitted by the C++ pathfinder on a grid whose origin puts cell
   coordinates at board scale carry only 4-decimal coordinates (segments
   and vias), matching the Python backend.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.core import Autorouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP, DesignRules

requires_cpp = pytest.mark.requires_router_cpp

# The exact seed-42 geometry (dumped from the finalize call of the failing
# run): VM's committed horizontal (net 26, rippable) vs VCP's non-rippable
# escape stub (net 25), F.Cu, trace_clearance 0.150.  Every coordinate is
# the float32 round-trip of a 4-decimal grid value (132.854996 =
# float32(132.855), 97.339996 = float32(97.340), ...), which puts the
# centerline gap 3.05 nm under the 0.325 mm requirement.
VCP_NET = 25
VM_NET = 26

VM_HOR_FLOAT32 = ((132.854996, 97.339996), (132.790009, 97.339996))
VCP_STUB_FLOAT32 = ((132.854996, 97.014999), (132.790009, 96.949997))

# The same pair snapped back onto the 4-decimal lattice: centerline gap
# exactly 0.325 mm == (0.150 + 0.200) / 2 + 0.150.
VM_HOR_CLEAN = ((132.855, 97.34), (132.79, 97.34))
VCP_STUB_CLEAN = ((132.855, 97.015), (132.79, 96.95))


def _make_rules() -> DesignRules:
    """Board-05's clearance regime for the failing pair (0.15/0.15)."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.065,
    )


def _make_autorouter() -> Autorouter:
    """Autorouter whose grid covers the real board-05 locus (~132, ~97)."""
    return Autorouter(
        width=20.0,
        height=20.0,
        origin_x=130.0,
        origin_y=90.0,
        rules=_make_rules(),
        layer_stack=LayerStack.two_layer(),
    )


def _seg(pts: tuple[tuple[float, float], tuple[float, float]], net: int, width: float) -> Segment:
    return Segment(
        x1=pts[0][0],
        y1=pts[0][1],
        x2=pts[1][0],
        y2=pts[1][1],
        width=width,
        layer=Layer.F_CU,
        net=net,
    )


def _board(
    ar: Autorouter,
    stub_pts: tuple[tuple[float, float], tuple[float, float]],
    vm_pts: tuple[tuple[float, float], tuple[float, float]] = VM_HOR_CLEAN,
) -> dict[int, list[Route]]:
    """Mirror the failing commit: VM's trace and VCP's negotiated leg are
    net_routes members; VCP's escape stub lives only in ``ar.routes``
    (non-rippable infrastructure, joined into the finalize gate's foreign
    universe via ``_collect_extra_routes_for_revalidation``)."""
    vm_route = Route(
        net=VM_NET,
        net_name="VM",
        segments=[_seg(vm_pts, VM_NET, 0.2)],
        vias=[],
    )
    vcp_route = Route(
        net=VCP_NET,
        net_name="VCP",
        segments=[
            # VCP's negotiated leg, far from the locus and clean.
            Segment(
                x1=138.0,
                y1=98.0,
                x2=136.0,
                y2=98.0,
                width=0.15,
                layer=Layer.F_CU,
                net=VCP_NET,
            )
        ],
        vias=[],
    )
    vcp_stub = Route(
        net=VCP_NET,
        net_name="VCP",
        segments=[_seg(stub_pts, VCP_NET, 0.15)],
        vias=[],
    )
    for route in (vm_route, vcp_route, vcp_stub):
        ar.grid.mark_route(route)
        ar.grid.mark_route_usage(route)
        ar.routes.append(route)
    return {VCP_NET: [vcp_route], VM_NET: [vm_route]}


class TestDemoteSegSegFinalizeVcpShape:
    """The finalize gate's verdict on the three shapes of the #9734 locus."""

    def test_float32_roundtrip_pair_demotes_vcp(self) -> None:
        """The exact demoted shape: 3.05 nm under the 0.325 mm requirement
        (float32 round-trip of lattice-exact coordinates) is a real SHORT
        as shipped -- the gate must keep refusing it."""
        ar = _make_autorouter()
        neg = NegotiatedRouter(ar.grid, Router(ar.grid, ar.rules), ar.rules, DEFAULT_NET_CLASS_MAP)
        net_routes = _board(ar, VCP_STUB_FLOAT32, VM_HOR_FLOAT32)

        demoted = ar._demote_seg_seg_overlap_nets(net_routes, neg, finalize=True)

        assert demoted == [VCP_NET]
        assert net_routes[VCP_NET] == []
        assert net_routes[VM_NET], "VM must stay committed"

    def test_lattice_exact_twin_not_demoted(self) -> None:
        """The same pair snapped to the 4-decimal lattice sits exactly AT
        the required pitch (0.325) -- the float-noise guard keeps it clean,
        which is what the backend coordinate snap restores."""
        ar = _make_autorouter()
        neg = NegotiatedRouter(ar.grid, Router(ar.grid, ar.rules), ar.rules, DEFAULT_NET_CLASS_MAP)
        net_routes = _board(ar, VCP_STUB_CLEAN)

        demoted = ar._demote_seg_seg_overlap_nets(net_routes, neg, finalize=True)

        assert demoted == []
        assert net_routes[VCP_NET] and net_routes[VM_NET]

    def test_genuinely_sub_clearance_pair_still_demotes(self) -> None:
        """Edge case: a real 0.085 mm clearance deficit (far beyond any
        float noise) still demotes -- the fix must not loosen the gate."""
        ar = _make_autorouter()
        neg = NegotiatedRouter(ar.grid, Router(ar.grid, ar.rules), ar.rules, DEFAULT_NET_CLASS_MAP)
        genuinely_short = ((132.855, 97.1249), (132.79, 96.95))
        net_routes = _board(ar, genuinely_short)

        demoted = ar._demote_seg_seg_overlap_nets(net_routes, neg, finalize=True)

        assert demoted == [VCP_NET]
        assert net_routes[VCP_NET] == []


class TestCppBackendCoordinateSnap:
    """The producer side: C++-emitted geometry lands on the 0.1 um lattice.

    The grid origin (130, 95) puts every cell coordinate at board scale,
    where the float32 round-trip is visible in the 6th decimal
    (float32(132.855) = 132.854996) -- exactly the failing board-05
    regime.  Requires the built router_cpp extension (#9705).
    """

    def _make_pathfinder(self, resolution: float):
        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder

        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            via_clearance=0.15,
            grid_resolution=resolution,
        )
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            origin_x=130.0,
            origin_y=95.0,
            layer_stack=LayerStack.two_layer(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())
        return pathfinder

    @requires_cpp
    def test_segment_coordinates_are_lattice_exact(self) -> None:
        pathfinder = self._make_pathfinder(0.065)
        start = Pad(
            x=130.65,
            y=97.015,
            width=0.6,
            height=0.6,
            net=25,
            net_name="VCP",
            layer=Layer.F_CU,
        )
        end = Pad(
            x=139.4,
            y=97.34,
            width=0.6,
            height=0.6,
            net=25,
            net_name="VCP",
            layer=Layer.F_CU,
        )

        route = pathfinder.route(start, end)
        assert route is not None, "C++ pathfinder must route the open corridor"
        assert route.segments, "route must carry segments"
        for seg in route.segments:
            for value in (seg.x1, seg.y1, seg.x2, seg.y2):
                assert value == round(value, 4), (
                    "Issue #9734: C++-emitted segment coordinates must be "
                    f"4-decimal lattice-exact, got {value!r} -- a float32 "
                    "round-trip left sub-clearance noise on the board"
                )

    @requires_cpp
    def test_via_coordinates_are_lattice_exact(self) -> None:
        pathfinder = self._make_pathfinder(0.065)
        start = Pad(
            x=130.65,
            y=97.015,
            width=0.6,
            height=0.6,
            net=25,
            net_name="VCP",
            layer=Layer.F_CU,
        )
        end = Pad(
            x=130.65,
            y=97.015,
            width=0.6,
            height=0.6,
            net=25,
            net_name="VCP",
            layer=Layer.B_CU,
        )

        route = pathfinder.route(start, end)
        assert route is not None and route.vias, "layer change must drop a via"
        for via in route.vias:
            assert via.x == round(via.x, 4), f"via x {via.x!r} is not lattice-exact"
            assert via.y == round(via.y, 4), f"via y {via.y!r} is not lattice-exact"

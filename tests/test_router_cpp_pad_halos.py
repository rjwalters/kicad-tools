"""Pad padding is not copper to inflate again during native radius checks."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available, router_cpp
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="Native router required")


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("radius", [0, 3])
def test_padding_is_not_inflated_but_copper_and_keepouts_are(sharing, radius):
    grid = router_cpp.Grid3D(40, 40, 2, 0.1)
    rules = router_cpp.DesignRules()
    rules.grid_resolution = 0.1
    finder = router_cpp.Pathfinder(grid, rules, True)
    grid.mark_blocked(21, 20, 0, 2, False, False, True)
    assert not finder.is_trace_blocked(20, 20, 0, 1, sharing, radius)
    assert not finder.is_via_blocked(20, 20, 1, sharing, radius)
    # Overlapping non-pad keepouts must revoke the exemption.
    grid.mark_blocked(21, 20, 0, 2)
    grid.mark_blocked(21, 20, 0, 2, False, False, True)
    assert finder.is_trace_blocked(20, 20, 0, 1, sharing, radius)
    assert finder.is_via_blocked(20, 20, 1, sharing, radius)


@pytest.mark.parametrize(
    "metal,obstacle,usage", [(True, False, 0), (False, True, 0), (False, False, 1)]
)
def test_halo_provenance_does_not_exempt_copper(metal, obstacle, usage):
    grid = router_cpp.Grid3D(40, 40, 2, 0.1)
    rules = router_cpp.DesignRules()
    rules.grid_resolution = 0.1
    finder = router_cpp.Pathfinder(grid, rules, True)
    grid.mark_blocked(21, 20, 0, 2, obstacle, metal, True)
    grid.at(21, 20, 0).usage_count = usage
    assert finder.is_trace_blocked(20, 20, 0, 1, False, 3)
    assert finder.is_via_blocked(20, 20, 1, False, 3)


@pytest.mark.parametrize("incremental", [False, True])
def test_pad_provenance_survives_bulk_and_incremental_sync(incremental):
    rules = DesignRules(grid_resolution=0.1)
    grid = RoutingGrid(width=10, height=10, rules=rules, layer_stack=LayerStack.two_layer())
    cpp = CppGrid.from_routing_grid(grid) if incremental else None
    grid.add_pad(
        Pad(x=5, y=5, width=0.45, height=0.45, net=0, net_name="GND", layer=Layer.F_CU),
        pin_pitch=1.27,
    )
    if cpp is None:
        cpp = CppGrid.from_routing_grid(grid)
    halo_cells = [
        (l, y, x)
        for l, y, x in grid._pad_halo_cells
        if not grid._pad_blocked[l, y, x] and not grid._is_obstacle[l, y, x]
    ]
    assert halo_cells
    assert all(cpp._impl.at(x, y, l).pad_halo_only for l, y, x in halo_cells)
    l, y, x = halo_cells[0]
    grid.cell_at(l, y, x).blocked = True
    assert (l, y, x) not in grid._pad_halo_cells


@pytest.mark.parametrize("keepout_first", [False, True])
def test_python_keepout_overlap_stays_blocked_after_bulk_sync(keepout_first):
    rules = DesignRules(grid_resolution=0.1)
    grid = RoutingGrid(width=10, height=10, rules=rules, layer_stack=LayerStack.two_layer())
    x, y = grid.world_to_grid(5.4, 5)
    if keepout_first:
        grid.cell_at(0, y, x).blocked = True
    grid.add_pad(
        Pad(x=5, y=5, width=0.45, height=0.45, net=0, net_name="GND", layer=Layer.F_CU),
        pin_pitch=1.27,
    )
    if not keepout_first:
        grid.cell_at(0, y, x).blocked = True
    cpp = CppGrid.from_routing_grid(grid)
    assert cpp._impl.at(x, y, 0).blocked
    assert not cpp._impl.at(x, y, 0).pad_halo_only


def test_plane_padding_does_not_bypass_native39_stored_via_guard():
    grid = router_cpp.Grid3D(40, 40, 2, 0.1)
    rules = router_cpp.DesignRules()
    rules.grid_resolution = 0.1
    finder = router_cpp.Pathfinder(grid, rules, True)
    # A halo-only raster cell is passable, but the indexed physical via at
    # the same location remains forbidden even without a registered mark.
    grid.mark_blocked(20, 20, 0, 2, False, False, True)
    grid.add_stored_via(2.0, 2.0, 0.3, 0.6, 2)
    assert finder.is_via_blocked(20, 20, 1, False, 3)


@pytest.mark.parametrize("kind", ["segment", "via"])
@pytest.mark.parametrize("radius", [0, 3])
@pytest.mark.parametrize("route_first", [False, True])
@pytest.mark.parametrize("partner", [False, True])
@pytest.mark.parametrize("sharing", [False, True])
def test_native_route_marks_revoke_padding_in_both_trace_kernels(
    kind, radius, route_first, partner, sharing
):
    grid = router_cpp.Grid3D(40, 40, 2, 0.1)
    rules = router_cpp.DesignRules()
    rules.grid_resolution = 0.1
    rules.trace_width = 0.2
    rules.trace_clearance = 0.2
    finder = router_cpp.Pathfinder(grid, rules, True)
    route_net = 2 if partner else 3

    def trace_blocked():
        # dx=2 is in the tighter partner-radius slack ring. Static pad
        # provenance must not turn partner copper into an exempt halo.
        return finder.is_trace_blocked(20, 20, 0, 1, sharing, radius, 2 if partner else -1, 1)

    def mark():
        if kind == "segment":
            grid.mark_segment(22, 20, 22, 20, 0, route_net, 0)
            grid.add_stored_segment(2.2, 2.0, 2.2, 2.0, 0.5, 0, route_net)
        else:
            grid.mark_via(22, 20, route_net, 0)
            grid.add_stored_via(2.2, 2.0, 0.3, 0.6, route_net)

    if route_first:
        mark()
    grid.mark_blocked(22, 20, 0, 2, False, False, True)
    if not route_first:
        assert not trace_blocked()
        mark()
    # Native route markers do not have to set usage_count: the provenance
    # lifecycle must protect real occupancy independently of that counter.
    assert grid.at(22, 20, 0).usage_count == 0
    assert not grid.at(22, 20, 0).pad_halo_only
    assert trace_blocked()
    assert finder.is_via_blocked(20, 20, 1, sharing, radius)
    # A later pad sync cannot re-grant relief over routed occupancy.
    grid.mark_blocked(22, 20, 0, 2, False, False, True)
    assert not grid.at(22, 20, 0).pad_halo_only
    if kind == "segment":
        grid.unmark_segment(22, 20, 22, 20, 0, route_net, 0)
    else:
        grid.unmark_via(22, 20, route_net, 0)
    assert grid.at(22, 20, 0).blocked
    assert not grid.at(22, 20, 0).pad_halo_only


@pytest.mark.parametrize("kind", ["segment", "via"])
@pytest.mark.parametrize("radius", [0, 3])
@pytest.mark.parametrize("soft", [False, True])
def test_reserved_route_overlap_cannot_retain_padding_relief(kind, radius, soft):
    grid = router_cpp.Grid3D(40, 40, 2, 0.1)
    rules = router_cpp.DesignRules()
    rules.grid_resolution = 0.1
    rules.trace_width = 0.2
    rules.trace_clearance = 0.2
    finder = router_cpp.Pathfinder(grid, rules, True)
    grid.mark_blocked(22, 20, 0, 2, False, False, True)
    # A hard reservation permits its owner (query net1); a foreign soft
    # reservation permits lateral queries too. Route net3 owns neither.
    owner = 9 if soft else 1
    grid.reserve_cell(22, 20, 0, [owner], soft)
    assert not finder.is_trace_blocked(20, 20, 0, 1, False, radius)
    before = grid.at(22, 20, 0)
    original = (before.net, before.original_net, before.reserved_count, before.reserved_soft)
    if kind == "segment":
        grid.mark_segment(22, 20, 22, 20, 0, 3, 0)
        grid.add_stored_segment(2.2, 2.0, 2.2, 2.0, 0.5, 0, 3)
    else:
        grid.mark_via(22, 20, 3, 0)
        grid.add_stored_via(2.2, 2.0, 0.3, 0.6, 3)
    cell = grid.at(22, 20, 0)
    assert (cell.net, cell.original_net, cell.reserved_count, cell.reserved_soft) == original
    assert cell.blocked and cell.static_blocked
    assert cell.usage_count == 0
    assert not cell.pad_halo_only
    assert finder.is_trace_blocked(20, 20, 0, 1, False, radius)
    assert finder.is_trace_blocked(20, 20, 0, 1, True, radius)
    # Replay of the static pad cannot resurrect relief after a skipped mark.
    grid.mark_blocked(22, 20, 0, 2, False, False, True)
    assert not grid.at(22, 20, 0).pad_halo_only


@pytest.mark.parametrize("kind", ["segment", "via"])
@pytest.mark.parametrize("soft", [False, True])
def test_bulk_reconstruction_does_not_restore_reserved_route_halo(kind, soft):
    from kicad_tools.router.primitives import Segment, Via

    rules = DesignRules(grid_resolution=0.1)
    grid = RoutingGrid(width=10, height=10, rules=rules, layer_stack=LayerStack.two_layer())
    grid.add_pad(
        Pad(x=5, y=5, width=0.45, height=0.45, net=0, net_name="GND", layer=Layer.F_CU),
        pin_pitch=1.27,
    )
    x, y = grid.world_to_grid(5.4, 5)
    assert (0, y, x) in grid._pad_halo_cells
    grid.reserve_corridor_cells(0, {(x, y)}, {1}, soft=soft)
    if kind == "segment":
        grid._mark_segment(Segment(5.4, 5, 5.4, 5, 0.2, Layer.F_CU, 3))
    else:
        grid._mark_via(Via(5.4, 5, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 3))
    cpp = CppGrid.from_routing_grid(grid)
    cell = cpp._impl.at(x, y, 0)
    assert cell.blocked and not cell.pad_halo_only
    assert cell.reserved_count == 1 and cell.reserved_soft == soft


@pytest.mark.parametrize("kind", ["segment", "via"])
@pytest.mark.parametrize("radius", [0, 3])
@pytest.mark.parametrize("soft", [False, True])
def test_route_before_reserved_pad_requires_physical_clearance(kind, radius, soft):
    grid = router_cpp.Grid3D(40, 40, 2, 0.1)
    rules = router_cpp.DesignRules()
    rules.grid_resolution = 0.1
    rules.trace_width = rules.trace_clearance = 0.2
    finder = router_cpp.Pathfinder(grid, rules, True)
    grid.reserve_cell(22, 20, 0, [9 if soft else 1], soft)
    if kind == "segment":
        grid.mark_segment(22, 20, 22, 20, 0, 3, 0)
        grid.add_stored_segment(2.2, 2, 2.2, 2, 0.5, 0, 3)
    else:
        grid.mark_via(22, 20, 3, 0)
        grid.add_stored_via(2.2, 2, 0.3, 0.6, 3)
    grid.mark_blocked(22, 20, 0, 2, False, False, True)
    # Hard reservations (and soft reservations for via marks) skip raster
    # occupancy, so this later pad CAN regrant provenance. Physical safety
    # must not depend on that flag or route_cell_has_geometry coverage.
    if not soft or kind == "via":
        assert grid.at(22, 20, 0).pad_halo_only
    for sharing in (False, True):
        assert finder.is_trace_blocked(20, 20, 0, 1, sharing, radius)


@pytest.mark.parametrize("kind", ["segment", "via"])
@pytest.mark.parametrize("radius", [0, 3])
def test_pad_relief_allows_physically_separated_stored_copper(kind, radius):
    grid = router_cpp.Grid3D(40, 40, 2, 0.1)
    rules = router_cpp.DesignRules()
    rules.grid_resolution = 0.1
    rules.trace_width = rules.trace_clearance = 0.2
    finder = router_cpp.Pathfinder(grid, rules, True)
    grid.mark_blocked(22, 20, 0, 2, False, False, True)
    if kind == "segment":
        grid.add_stored_segment(3.2, 2, 3.2, 2.5, 0.5, 0, 3)
    else:
        grid.add_stored_via(3.2, 2, 0.3, 0.6, 3)
    for sharing in (False, True):
        assert not finder.is_trace_blocked(20, 20, 0, 1, sharing, radius)


@pytest.mark.parametrize("kind", ["segment", "via"])
@pytest.mark.parametrize("soft", [False, True])
@pytest.mark.parametrize("radius", [0, 3])
def test_bulk_route_before_pad_keeps_physical_veto(kind, soft, radius):
    from kicad_tools.router.cpp_backend import CppPathfinder
    from kicad_tools.router.primitives import Route, Segment, Via

    rules = DesignRules(grid_resolution=0.1)
    grid = RoutingGrid(width=10, height=10, rules=rules, layer_stack=LayerStack.two_layer())
    x, y = grid.world_to_grid(5.4, 5)
    grid.reserve_corridor_cells(0, {(x, y)}, {9 if soft else 1}, soft=soft)
    route = Route(3, "FOREIGN")
    if kind == "segment":
        route.segments.append(Segment(5.4, 5, 5.4, 5, 0.2, Layer.F_CU, 3))
    else:
        route.vias.append(Via(5.4, 5, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 3))
    grid.mark_route(route)
    grid.add_pad(
        Pad(x=5, y=5, width=0.45, height=0.45, net=0, net_name="GND", layer=Layer.F_CU),
        pin_pitch=1.27,
    )
    cpp = CppGrid.from_routing_grid(grid)
    finder = CppPathfinder(cpp, rules)
    finder._sync_stored_routes(grid)
    assert cpp._synced_route_count == 1
    # Query just outside the original pad envelope; physical foreign copper
    # overlaps the emitted trace even if the reserved cell has pad provenance.
    for sharing in (False, True):
        assert finder._impl.is_trace_blocked(x + 2, y, 0, 1, sharing, radius)


@pytest.mark.parametrize("radius", [0, 3])
def test_pad_relief_checks_swept_emitted_width_and_fill_clearance(radius):
    grid = router_cpp.Grid3D(40, 40, 2, 0.1)
    rules = router_cpp.DesignRules()
    rules.grid_resolution = 0.1
    rules.trace_width = rules.trace_clearance = 0.2
    finder = router_cpp.Pathfinder(grid, rules, True)
    grid.mark_blocked(22, 20, 0, 2, False, False, True)
    grid.add_stored_segment(1.5, 2.5, 1.5, 2.5, 0.2, 0, 3)

    def blocked():
        return finder.is_trace_blocked(20, 20, 0, 1, False, radius, -1, 0, 10, 20)

    # Default swept clearance is .3 mm; increasing emitted width or the
    # fill-context clearance must each reject this .5-mm center separation.
    assert not blocked()
    finder.set_search_pair_widths(0.35, 0.3)
    assert blocked()
    finder.set_search_pair_widths(0.1, 0.3)
    finder.set_search_fill_clearances(0.4, 0.2)
    assert blocked()

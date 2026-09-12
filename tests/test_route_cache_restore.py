"""Cached copper must restore the occupancy read by finalization passes."""

from kicad_tools.router import Autorouter, DesignRules, Route, RoutingCache, Segment
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import make_collision_checker


def test_cache_restore_matches_cold_collision_state_and_preserves_keepouts():
    from kicad_tools.cli.route_cmd import _restore_route_grid

    def board():
        router = Autorouter(10, 10, rules=DesignRules(grid_resolution=0.1), force_python=True)
        router.grid.add_keepout(7, 7, 8, 8, [Layer.F_CU])
        return router

    route = Route(1, "one", [Segment(2, 5, 6, 5, 0.2, Layer.F_CU, 1, "one")])
    cold, warm = board(), board()
    cold._mark_route(route)
    cold.grid.mark_route_usage(route)
    cold.routes = [route]
    restored = route.copy_geometry()
    _restore_route_grid(warm, [restored])
    crossing = (4, 3, 4, 6, Layer.F_CU, 0.2, 2)
    clear = (1, 1, 5, 1, Layer.F_CU, 0.2, 2)
    keepout = (7.5, 6, 7.5, 9, Layer.F_CU, 0.2, 2)
    for candidate in (crossing, clear, keepout):
        assert make_collision_checker(warm.grid).path_is_clear(
            *candidate
        ) == make_collision_checker(cold.grid).path_is_clear(*candidate)
    assert not make_collision_checker(warm.grid).path_is_clear(*crossing)
    gx, gy = warm.grid.world_to_grid(7.5, 7.5)
    assert warm.grid.grid[warm.grid.layer_to_index(Layer.F_CU.value)][gy][gx].blocked
    assert make_collision_checker(warm.grid).path_is_clear(*clear)
    assert warm.grid.routes == warm.routes == [restored]
    # Loading a second snapshot must replace occupancy, not double its count.
    _restore_route_grid(warm, [restored.copy_geometry()])
    assert len(warm.grid.routes) == 1
    assert warm.grid.get_total_overflow() == cold.grid.get_total_overflow()


def test_cache_roundtrip_preserves_escape_classification(tmp_path):
    cache = RoutingCache(cache_dir=tmp_path)
    route = Route(1, "escape", is_escape=True)
    assert cache.deserialize_routes(cache.serialize_routes([route]))[0].is_escape


def test_restore_removes_stale_grid_copper_and_usage():
    from kicad_tools.cli.route_cmd import _restore_route_grid

    router = Autorouter(10, 10, rules=DesignRules(grid_resolution=0.1), force_python=True)
    stale = Route(1, "old", [Segment(2, 5, 6, 5, 0.2, Layer.F_CU, 1, "old")])
    chosen = Route(2, "chosen", [Segment(2, 2, 6, 2, 0.2, Layer.F_CU, 2, "chosen")])
    router._mark_route(stale)
    router.grid.mark_route_usage(stale)
    router.grid.mark_route_usage(stale)
    # The selected best iteration differs from the last grid-marked iteration.
    router.routes = [chosen]
    _restore_route_grid(router, router.routes)
    assert router.grid.routes == [chosen]
    checker = make_collision_checker(router.grid)
    assert checker.path_is_clear(4, 4, 4, 6, Layer.F_CU, 0.2, 3)
    assert not checker.path_is_clear(4, 1, 4, 3, Layer.F_CU, 0.2, 3)
    assert router.grid.get_total_overflow() == 0
    assert len(router.router._routed_segments) == 1

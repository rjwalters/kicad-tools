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


def test_restore_drops_discarded_net_cell_ownership_residue():
    """Issue #5274: pre-post-pass occupancy must be a function of the final copper.

    ``_mark_segment`` claims a contested cell for whichever net blocks it
    FIRST, and ``_unmark_segment`` only releases a cell it still owns.  So
    when the first claimant's copper is later discarded (negotiated rip-up,
    best-iteration rollback, cache replay of a different snapshot) its cell
    ownership survives as residue that no per-route unmark can clear -- the
    discarded net has no geometry left to unmark with.

    That residue made the grid handed to the optimizer a function of the run's
    routing HISTORY rather than of its final route set: a cold run and a warm
    cache replay of the same routes disagreed about which cells were own-net,
    and the optimizer's collision checker (own-net cells are passable) merged a
    different number of collinear runs.  ``_restore_route_grid`` must leave the
    same occupancy either way.
    """
    import numpy as np

    from kicad_tools.cli.route_cmd import _restore_route_grid

    def board():
        return Autorouter(10, 10, rules=DesignRules(grid_resolution=0.1), force_python=True)

    # net 1 and net 2 cross, so their clearance envelopes contest cells.
    discarded = Route(1, "discarded", [Segment(2, 5, 8, 5, 0.2, Layer.F_CU, 1, "discarded")])
    kept = Route(2, "kept", [Segment(5, 2, 5, 8, 0.2, Layer.F_CU, 2, "kept")])

    with_history = board()
    with_history._mark_route(discarded)
    with_history._mark_route(kept)
    # The discarded net loses its copper; only ``kept`` survives the rollback.
    with_history.routes = [kept]
    _restore_route_grid(with_history, with_history.routes)

    replayed = board()
    _restore_route_grid(replayed, [kept.copy_geometry()])

    assert np.array_equal(
        np.asarray(with_history.grid._blocked), np.asarray(replayed.grid._blocked)
    )
    assert np.array_equal(np.asarray(with_history.grid._net), np.asarray(replayed.grid._net)), (
        "discarded-net cell ownership leaked past _restore_route_grid"
    )
    # The surviving net owns the crossing it now solely occupies.
    gx, gy = replayed.grid.world_to_grid(5, 5)
    layer = replayed.grid.layer_to_index(Layer.F_CU.value)
    assert replayed.grid.grid[layer][gy][gx].net == 2


def test_resync_captures_static_baseline_before_first_route_mark():
    """Issue #5274: cache replay marks copper without ever calling ``mark_route``.

    ``restore_route_snapshot`` marks cells through ``resync_route_occupancy``,
    which used to skip the static-blockage snapshot that ``mark_route`` takes.
    With no snapshot, the next rip-up FREES statically blocked pad/edge
    clearance halos outright instead of restoring their static owner -- so a
    warm run silently lost keepout copper a cold run kept.
    """
    router = Autorouter(10, 10, rules=DesignRules(grid_resolution=0.1), force_python=True)
    router.grid.add_keepout(7, 7, 8, 8, [Layer.F_CU])
    assert router.grid._static_blocked is None

    route = Route(1, "one", [Segment(7.5, 6, 7.5, 9, 0.2, Layer.F_CU, 1, "one")])
    router.restore_route_snapshot([route])
    assert router.grid._static_blocked is not None

    router.grid.unmark_route(route)
    layer = router.grid.layer_to_index(Layer.F_CU.value)
    gx, gy = router.grid.world_to_grid(7.5, 7.5)
    assert router.grid.grid[layer][gy][gx].blocked, "keepout freed by a post-replay rip-up"

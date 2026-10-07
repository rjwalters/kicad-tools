"""The relaxed rip-up blocker search sees past committed copper (Issue #6009).

``NegotiatedRouter.find_blocking_nets_relaxed`` re-runs a failing net's own A*
with every committed route lifted, then names the nets whose copper lay on the
relaxed path.  Before #6009 the lift only cleared the Python ``_blocked`` /
``_net`` planes: the paired C++ grid and the segment / via R-trees still held
every trace, so on *both* backends the relaxed search failed and returned no
blockers at all -- neighbourhood rip-up and ``--ripup-strategy sequential-n1``
never had anything to rip.

Pinned here, on both pathfinder backends:

* the two-net crossing fixture from ``test_unrouted_cause`` -- the loser's
  relaxed search finds a path and names the winner as its only blocker;
* the grid (every Python ndarray, every C++ cell field, ``grid.routes`` order),
  the access-witness journal and the pathfinder's crossing-cost cache come back
  exactly, including when the search raises mid-lift.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.layers import Layer
from tests.test_unrouted_cause import (
    BACKENDS,
    _assert_snapshots_equal,
    _crossing_router,
    _dense_router,
    _full_grid_snapshot,
    _route,
)


def _neg(router):
    return NegotiatedRouter(router.grid, router.router, router.rules, router.net_class_map)


def _pads(router, net):
    return [router.pads[key] for key in router.nets[net]]


@pytest.mark.parametrize("force_python", BACKENDS)
def test_relaxed_search_names_the_crossing_winner(force_python: bool) -> None:
    router = _crossing_router(force_python)
    unrouted = _route(router)
    assert len(unrouted) == 1, "fixture sanity: exactly one of the crossing nets must fail"
    [loser] = unrouted
    winner = ({1, 2} - {loser}).pop()

    scores = _neg(router).find_blocking_nets_relaxed(
        [loser], {loser: _pads(router, loser)}, per_net_timeout=5.0
    )

    assert scores == {winner: 1}


@pytest.mark.parametrize("force_python", BACKENDS)
def test_pathfinder_relaxed_search_inside_the_lift(force_python: bool) -> None:
    """The issue's reproduction: route + per-pathfinder relaxed search."""
    router = _crossing_router(force_python)
    [loser] = _route(router)
    winner = ({1, 2} - {loser}).pop()
    src, dst = _pads(router, loser)[:2]

    with router.grid.temporarily_unblock_routed_nets(router.router) as lift:
        solo = router.router.route(src, dst, per_net_timeout=5.0)
        copper = lift.copper_net
        blockers = router.router.find_blocking_nets_relaxed(src, dst, copper != 0, copper, 5.0)

    assert solo is not None, "the loser must route once the winner's copper is lifted"
    assert blockers == {winner}


@pytest.mark.parametrize("force_python", BACKENDS)
def test_relaxed_search_restores_the_grid_exactly(force_python: bool) -> None:
    router = _dense_router(force_python)
    unrouted = _route(router)
    assert len(router.grid.routes) >= 2, "fixture sanity: several overlapping routes"
    assert unrouted, "fixture sanity: something is left to search"
    journal: list[tuple[str, object]] = []

    def observer(event: str, route: object) -> None:
        journal.append((event, route))

    router.grid.commit_observer = observer
    crossing_cache = list(getattr(router.router, "_routed_segments", []))
    generation = router.grid.occupancy_generation
    before = _full_grid_snapshot(router)

    _neg(router).find_blocking_nets_relaxed(
        sorted(unrouted),
        {net: _pads(router, net) for net in unrouted},
        per_net_timeout=2.0,
    )

    _assert_snapshots_equal(before, _full_grid_snapshot(router))
    assert router.grid.occupancy_generation > generation, "occupancy caches not invalidated"
    assert journal == [], "the lift leaked events into the witness journal"
    assert router.grid.commit_observer is observer
    if hasattr(router.router, "_routed_segments"):
        assert sorted(router.router._routed_segments) == sorted(crossing_cache)


@pytest.mark.parametrize("force_python", BACKENDS)
def test_grid_restored_exactly_when_relaxed_search_raises(
    force_python: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _dense_router(force_python)
    unrouted = _route(router)
    assert len(router.grid.routes) >= 2, "fixture sanity: several overlapping routes"
    before = _full_grid_snapshot(router)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(router.router, "find_blocking_nets_relaxed", boom)
    with pytest.raises(RuntimeError, match="boom"):
        _neg(router).find_blocking_nets_relaxed(
            sorted(unrouted), {net: _pads(router, net) for net in unrouted}
        )

    _assert_snapshots_equal(before, _full_grid_snapshot(router))


@pytest.mark.parametrize("force_python", BACKENDS)
def test_grid_restored_exactly_when_the_lift_itself_raises(
    force_python: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure partway through the lift must not leave routes off the grid."""
    router = _dense_router(force_python)
    _route(router)
    grid = router.grid
    assert len(grid.routes) >= 2, "fixture sanity: several overlapping routes"
    before = _full_grid_snapshot(router)
    real_resync = grid.resync_route_occupancy
    calls = {"n": 0}

    def flaky_resync(replacements, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Lift half the routes for real, then fail.
            real_resync(replacements[: len(replacements) // 2], *args, **kwargs)
            raise RuntimeError("lift failed")
        return real_resync(replacements, *args, **kwargs)

    monkeypatch.setattr(grid, "resync_route_occupancy", flaky_resync)
    with pytest.raises(RuntimeError, match="lift failed"), grid.temporarily_unblock_routed_nets():
        pytest.fail("the body must not run when the lift fails")

    monkeypatch.undo()
    _assert_snapshots_equal(before, _full_grid_snapshot(router))


# ---------------------------------------------------------------------------
# Keepout rule areas (#6008) survive the lift
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("force_python", BACKENDS)
@pytest.mark.parametrize("filtered", [False, True], ids=["all-nets", "net-filtered"])
def test_lift_keeps_keepout_rule_areas(tmp_path, force_python: bool, filtered: bool) -> None:
    """A lifted grid must still refuse a route through a keepout.

    A front-only ``(tracks not_allowed)`` wall pushes both nets onto B.Cu, so
    there is committed copper to lift.  An all-nets wall is stamped as static
    net-0 cells; a ``spatial_keepouts``-filtered one lives only in
    ``grid._rule_area_keepouts`` (and the C++ grid's area list).  Inside the
    lift both kinds must still hold, and afterwards the grid is unchanged.
    """
    from kicad_tools.router.rules import NetClassRouting
    from tests.test_grid_keepout_rule_areas_6008 import (
        WALL_X0,
        WALL_X1,
        _board,
        _crosses_band,
        _load,
        _wall,
    )
    from tests.test_grid_keepout_rule_areas_6008 import _route as _route_board

    router = _load(
        tmp_path, _board(_wall(layers='"F.Cu"', vias="allowed"), two_nets=True), force_python
    )
    if filtered:
        router.net_class_map["/HV_A"] = NetClassRouting(name="HV_A")
        router.net_class_map["/HV_B"] = NetClassRouting(name="HV_B")
        router._spatial_keepout_filters = {"wall": {"only_classes": ["HV_A", "HV_B"]}}
    _route_board(router)
    grid = router.grid
    assert {r.net for r in grid.routes if r.segments} == {1, 2}, "fixture: both nets route"
    (area,) = grid._rule_area_keepouts
    assert area.static_tracks is not filtered
    areas_before = list(grid._rule_area_keepouts)
    cpp = getattr(grid, "_cpp_grid", None)
    cpp_areas_before = cpp._impl.rule_area_keepout_count() if cpp is not None else None
    wx, wy = grid.world_to_grid((WALL_X0 + WALL_X1) / 2, 108.0)
    before = _full_grid_snapshot(router)

    src, dst = _pads(router, 1)[:2]
    with grid.temporarily_unblock_routed_nets(router.router) as lift:
        assert lift.lifted, "fixture: there is copper to lift"
        if not filtered:
            assert grid._blocked[0, wy, wx] and grid._net[0, wy, wx] == 0
            assert lift.copper_net[0, wy, wx] == 0
            if cpp is not None:
                cell = cpp._impl.at(wx, wy, 0)
                assert cell.blocked and cell.net == 0
        assert grid._rule_area_keepouts == areas_before
        if cpp is not None:
            assert cpp._impl.rule_area_keepout_count() == cpp_areas_before
        solo = router.router.route(src, dst, per_net_timeout=10.0)

    assert solo is not None, "the back layer is open -- the lifted net must route"
    for seg in solo.segments:
        if seg.layer == Layer.F_CU:
            assert not _crosses_band(seg, WALL_X0, WALL_X1, seg.width / 2), seg
    _assert_snapshots_equal(before, _full_grid_snapshot(router))
    assert grid._rule_area_keepouts == areas_before

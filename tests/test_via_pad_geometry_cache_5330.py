"""The vectorized non-through-hole pad cache must not survive in-place pad edits.

PR #5330 review (issue #5240): ``Router._non_th_pad_geometry`` caches
``(x, y, half_w, half_h, cos, sin)`` arrays for the ``not
self._allow_smd_vias`` sweep in ``Router._check_via_placement_cached``.  It
was originally keyed on pad *count* alone, justified by "pads are only ever
appended via ``RoutingGrid.add_pad``, never removed or reordered".  That is
true of removal/reordering but says nothing about *mutation*:
``PlacementDeltaFeedbackLoop._apply_delta_to_router_pads`` /
``_restore_router_pads`` rewrite ``Pad.x``/``Pad.y``/``Pad.layer`` in place,
on the very ``Pad`` objects the grid holds, with no count change, while the
loop reuses a single ``Autorouter`` (and therefore a single pathfinder
``Router``) across every iteration.  A warmed cache then kept answering with
the pad's OLD position for the rest of the session -- so a via could be
admitted on top of a pad that had moved under it.

These tests pin the invalidation contract: the pad geometry cache is dropped
by ``clear_via_cache()`` (the same trigger the sibling ``_via_cache`` already
uses at the start of every ``route_net``/``_astar_search``), by the explicit
``invalidate_pad_geometry_cache()`` hook, and by the placement-feedback pad
mutators themselves.
"""

from typing import Any

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.placement_delta import PlacementDelta
from kicad_tools.router.placement_feedback import PlacementDeltaFeedbackLoop
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# A cell far from where any pad is initially placed, so the coarse grid's
# obstacle map (populated once by ``add_pad`` and deliberately NOT updated by
# an in-place pad move) never masks what the pad sweep decides.
FREE_WX, FREE_WY = 20.0, 10.0
HOME_WX, HOME_WY = 10.0, 10.0


def _router_with_pads(*pads: Pad) -> tuple[Router, RoutingGrid]:
    """jlcpcb (no via-in-pad) router holding the given SMD pads."""
    rules = DesignRules(grid_resolution=0.1, manufacturer="jlcpcb")
    grid = RoutingGrid(width=40, height=40, rules=rules, layer_stack=LayerStack.two_layer())
    for pad in pads:
        grid.add_pad(pad)
    router = Router(grid, rules)
    assert not router._allow_smd_vias, "jlcpcb must disallow via-in-pad for this regression"
    return router, grid


def test_pad_moved_in_place_is_seen_after_clear_via_cache() -> None:
    """Judge's PR #5330 repro: a pad translated onto a warmed cell must block it.

    Before the fix this returned ``True`` (stale cached geometry still had the
    pad at its old position) even though ``clear_via_cache()`` -- which every
    ``route_net``/``_astar_search`` call runs precisely because "grid state may
    have changed" -- had just been invoked.
    """
    pad = Pad(x=HOME_WX, y=HOME_WY, width=1.0, height=1.0, net=1, net_name="N1")
    router, grid = _router_with_pads(pad)
    gx, gy = grid.world_to_grid(FREE_WX, FREE_WY)

    # Warm both caches while the pad is far away: the via is placeable.
    assert router._check_via_placement_cached(gx, gy, net=2, allow_sharing=False) is True

    # In-place move onto the via cell -- same Pad object, same pad count,
    # exactly what _apply_delta_to_router_pads does for a translate delta.
    pad.x, pad.y = FREE_WX, FREE_WY
    router.clear_via_cache()

    assert router._check_via_placement_cached(gx, gy, net=2, allow_sharing=False) is False


def test_pad_rotated_in_place_is_seen_after_clear_via_cache() -> None:
    """Rotation is cached geometry too (cos/sin), not just position."""
    # Long, thin pad centred on the free cell's column: its long axis runs
    # along X, so a point 1.4mm ABOVE its centre is clear of it.  Rotating it
    # 90 degrees swings the long axis onto Y and swallows that point.
    pad = Pad(x=FREE_WX, y=FREE_WY - 1.4, width=4.0, height=0.4, net=1, net_name="N1")
    router, grid = _router_with_pads(pad)
    gx, gy = grid.world_to_grid(FREE_WX, FREE_WY)

    assert router._check_via_placement_cached(gx, gy, net=2, allow_sharing=False) is True

    pad.rotation = 90.0
    router.clear_via_cache()

    assert router._check_via_placement_cached(gx, gy, net=2, allow_sharing=False) is False


def test_invalidate_pad_geometry_cache_rebuilds_arrays() -> None:
    """The explicit hook drops the arrays; the next query rebuilds them.

    Checked at the cache level (not through ``_check_via_placement_cached``)
    because the hook deliberately does NOT touch the sibling per-cell
    ``_via_cache`` -- ``clear_via_cache`` is the entry point that clears both.
    """
    pad = Pad(x=HOME_WX, y=HOME_WY, width=1.0, height=1.0, net=1, net_name="N1")
    router, _grid = _router_with_pads(pad)

    xs, ys = router._non_th_pad_geometry()[:2]
    assert (xs[0], ys[0]) == (HOME_WX, HOME_WY)
    assert router._non_th_pad_cache is not None

    pad.x, pad.y = FREE_WX, FREE_WY

    # Pad-count-only keying: the stale arrays are still handed back.
    stale_xs, stale_ys = router._non_th_pad_geometry()[:2]
    assert (stale_xs[0], stale_ys[0]) == (HOME_WX, HOME_WY)

    router.invalidate_pad_geometry_cache()
    assert router._non_th_pad_cache is None

    fresh_xs, fresh_ys = router._non_th_pad_geometry()[:2]
    assert (fresh_xs[0], fresh_ys[0]) == (FREE_WX, FREE_WY)


def test_clear_via_cache_drops_both_caches() -> None:
    """``clear_via_cache`` is the shared invalidation trigger for both caches."""
    pad = Pad(x=HOME_WX, y=HOME_WY, width=1.0, height=1.0, net=1, net_name="N1")
    router, grid = _router_with_pads(pad)
    gx, gy = grid.world_to_grid(FREE_WX, FREE_WY)

    router._check_via_placement_cached(gx, gy, net=2, allow_sharing=False)
    assert router._via_cache
    assert router._non_th_pad_cache is not None

    router.clear_via_cache()

    assert not router._via_cache
    assert router._non_th_pad_cache is None


def test_cache_still_rebuilds_when_a_pad_is_appended() -> None:
    """The pad-count guard (the cheap extra check) must still work."""
    pad = Pad(x=HOME_WX, y=HOME_WY, width=1.0, height=1.0, net=1, net_name="N1")
    router, grid = _router_with_pads(pad)
    gx, gy = grid.world_to_grid(FREE_WX, FREE_WY)

    assert router._check_via_placement_cached(gx, gy, net=2, allow_sharing=False) is True

    grid.add_pad(Pad(x=FREE_WX, y=FREE_WY, width=1.0, height=1.0, net=1, net_name="N1"))
    router.clear_via_cache()  # per-cell via cache; the pad-count guard covers the rest

    assert router._check_via_placement_cached(gx, gy, net=2, allow_sharing=False) is False


class _FakeAutorouter:
    """Minimal Autorouter stand-in: the pathfinder plus its flat pad list."""

    def __init__(self, router: Any, pads: list[Pad]) -> None:
        self.router = router
        self.all_pads = pads


def _loop_for(router: Any, pads: list[Pad]) -> PlacementDeltaFeedbackLoop:
    return PlacementDeltaFeedbackLoop(
        _FakeAutorouter(router, pads),  # type: ignore[arg-type]
        pcb=None,
        verbose=False,
    )


def test_apply_delta_to_router_pads_invalidates_pad_geometry() -> None:
    """The real mutation site drops the cache without needing a route call."""
    pad = Pad(x=HOME_WX, y=HOME_WY, width=1.0, height=1.0, net=1, net_name="N1", ref="U1")
    router, grid = _router_with_pads(pad)
    gx, gy = grid.world_to_grid(FREE_WX, FREE_WY)

    assert router._check_via_placement_cached(gx, gy, net=2, allow_sharing=False) is True

    loop = _loop_for(router, [pad])
    loop._apply_delta_to_router_pads(
        PlacementDelta(
            net_name="N1",
            target_ref="U1",
            kind="translate",
            dx=FREE_WX - HOME_WX,
            dy=FREE_WY - HOME_WY,
        )
    )
    assert (pad.x, pad.y) == (FREE_WX, FREE_WY)
    assert router._non_th_pad_cache is None

    # No explicit clear_via_cache() here on purpose: the mutator's own hook is
    # what must make the next check see the pad's new position.
    assert router._check_via_placement_cached(gx, gy, net=2, allow_sharing=False) is False


def test_restore_router_pads_invalidates_pad_geometry() -> None:
    """Reverting a delta is an in-place mutation too, and must invalidate."""
    pad = Pad(x=HOME_WX, y=HOME_WY, width=1.0, height=1.0, net=1, net_name="N1", ref="U1")
    router, grid = _router_with_pads(pad)
    gx, gy = grid.world_to_grid(FREE_WX, FREE_WY)

    loop = _loop_for(router, [pad])
    snapshot = loop._snapshot_router_pads()

    pad.x, pad.y = FREE_WX, FREE_WY
    router.clear_via_cache()
    assert router._check_via_placement_cached(gx, gy, net=2, allow_sharing=False) is False

    loop._restore_router_pads(snapshot)
    assert (pad.x, pad.y) == (HOME_WX, HOME_WY)
    assert router._non_th_pad_cache is None
    assert router._check_via_placement_cached(gx, gy, net=2, allow_sharing=False) is True


def test_invalidate_router_pad_geometry_tolerates_missing_hooks() -> None:
    """Backends without the hooks (C++ pathfinder, test doubles) are a no-op."""

    class _Bare:
        pass

    class _NoPathfinder:
        router = None
        all_pads: list[Any] = []

    loop = PlacementDeltaFeedbackLoop(_NoPathfinder(), pcb=None, verbose=False)  # type: ignore[arg-type]
    loop._invalidate_router_pad_geometry()  # must not raise

    loop2 = _loop_for(_Bare(), [])
    loop2._invalidate_router_pad_geometry()  # must not raise

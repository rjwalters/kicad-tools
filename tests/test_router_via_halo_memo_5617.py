"""Equivalence pins for the route-halo via-probe memo (Issue #5617).

Background
----------
``Router._is_via_blocked`` opens with a route-halo probe --
``_via_halo_clear([], layer, gx, gy, net, require_geometry=False)`` -- and a
py-spy profile of the Diff-Pair regression job's re-route step attributed
**18.2 % of phase 4** ("Routing nets...", itself 66.5 % of the step) to it.
The sibling ``Router._via_cache`` cannot absorb any of that: it is bypassed
whenever ``allow_sharing`` is set, which is exactly the negotiated mode the
pure-Python A* fallback runs in.

``Router._via_halo_clear_cached`` memoises the probe on ``(gx, gy, net)``,
which is sound only because of three properties this module pins down:

1. **The probe ignores ``layer``.**  It passes an EMPTY ``cells`` list (so
   ``RouteHaloGeometry.cell_known`` is never consulted) and builds a ``Via``
   whose layer span is always ``(layer 0, layer n-1)``.  So the per-layer loop
   in ``_check_via_placement_cached`` was recomputing one identical answer once
   per non-plane layer.
2. **``RouteHaloGeometry.state_version`` tracks every geometry change.**  It
   bumps on each actual ``_refresh`` rebuild, and a rebuild is forced by any
   ``record()`` (via the ``_generation = -1`` sentinel) or any
   ``grid.occupancy_generation`` change.  A token mismatch drops the memo
   wholesale.
3. **``clear_via_cache()`` drops the memo too**, so the router's rule
   configuration -- the other input ``RouteHaloGeometry.clear`` reads -- cannot
   be served stale across routes either.

Sibling of ``test_router_via_kernel_memo_5617.py`` (the kernel-side fast paths
of the same function) and ``test_route_halo_bounds_pruning.py`` (#5240's
bounding-box prune inside ``clear``).
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Layer, Route, Via
from kicad_tools.router.rules import DesignRules


def _context() -> tuple[RoutingGrid, Router]:
    """A four-layer board with one stored via at grid (60, 60) on net 2."""
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.127,
    )
    grid = RoutingGrid(
        width=20, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    x, y = grid.grid_to_world(60, 60)
    route = Route(net=2, net_name="N2")
    route.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2"))
    grid.mark_route(route)
    router = Router(grid, rules)
    router.set_net_name_to_id({"N1": 1, "N2": 2})
    return grid, router


def _uncached(router: Router, layer: int, gx: int, gy: int, net: int) -> bool:
    """The exact pre-memo expression ``_is_via_blocked`` used to evaluate."""
    return router._via_halo_clear([], layer, gx, gy, net, require_geometry=False)


# A window that straddles the stored via, so the sweep below sees BOTH
# verdicts rather than a constant.
_SWEEP = [(gx, gy) for gx in range(54, 68) for gy in range(54, 68)]


def test_probe_window_is_not_degenerate():
    """Guard: the sweep below is only meaningful if it sees both verdicts."""
    _, router = _context()
    verdicts = {_uncached(router, 2, gx, gy, 1) for gx, gy in _SWEEP}
    assert verdicts == {True, False}


def test_memo_matches_uncached_verdict_everywhere():
    """Every memoised verdict equals the freshly-computed one."""
    grid, router = _context()
    halo = grid._route_halo
    for gx, gy in _SWEEP:
        expected = _uncached(router, 2, gx, gy, 1)
        # First call populates, second is served from the memo; both must
        # agree with the uncached computation.
        assert router._via_halo_clear_cached(halo, 2, gx, gy, 1) is expected
        assert router._via_halo_clear_cached(halo, 2, gx, gy, 1) is expected


def test_memo_is_layer_independent_and_computes_once():
    """The probe's answer does not depend on ``layer`` -- so neither does the key.

    This is the property that makes ``_check_via_placement_cached``'s
    per-non-plane-layer loop redundant work rather than distinct work.
    """
    grid, router = _context()
    halo = grid._route_halo
    layers = range(grid.num_layers)

    # Property: identical verdicts across layers, computed WITHOUT the memo.
    for gx, gy in _SWEEP:
        answers = {_uncached(router, layer, gx, gy, 1) for layer in layers}
        assert len(answers) == 1, f"probe became layer-dependent at ({gx}, {gy})"

    # And the memo collapses the repeat into a single computation.
    calls: list[tuple[int, int, int]] = []
    original = router._via_halo_clear

    def counting(cells, layer, gx, gy, net, *, require_geometry=True):
        calls.append((layer, gx, gy))
        return original(cells, layer, gx, gy, net, require_geometry=require_geometry)

    router._via_halo_clear = counting  # type: ignore[method-assign]
    for layer in layers:
        router._via_halo_clear_cached(halo, layer, 55, 56, 1)
    assert len(calls) == 1


def test_memo_drops_on_halo_geometry_change():
    """A new via near the probe flips the verdict -- the memo must not hide it."""
    grid, router = _context()
    halo = grid._route_halo

    probe = (90, 90)
    before = router._via_halo_clear_cached(halo, 2, *probe, 1)
    assert before is _uncached(router, 2, *probe, 1)

    # Land foreign copper right on the probe point.
    x, y = grid.grid_to_world(*probe)
    blocker = Route(net=2, net_name="N2")
    blocker.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2"))
    grid.mark_route(blocker)

    after = _uncached(router, 2, *probe, 1)
    assert after is not before, "fixture no longer exercises an invalidation"
    assert router._via_halo_clear_cached(halo, 2, *probe, 1) is after


def test_state_version_bumps_exactly_on_rebuild():
    grid, router = _context()
    halo = grid._route_halo

    first = halo.state_version
    assert halo.state_version == first, "reading the token must not bump it"

    grid.bump_occupancy_generation()
    second = halo.state_version
    assert second > first

    # ``record`` writes the -1 sentinel, which forces the next refresh.
    halo.record((0, 1, 0, 1, 1, 2, 2, 0.2), 3, True)
    third = halo.state_version
    assert third > second
    assert halo.state_version == third


def test_clear_via_cache_drops_the_memo():
    grid, router = _context()
    halo = grid._route_halo
    router._via_halo_clear_cached(halo, 2, 55, 56, 1)
    assert router._via_halo_cache

    router.clear_via_cache()
    assert not router._via_halo_cache
    assert router._via_halo_cache_token is None


def test_memo_cap_is_a_memory_backstop_only(monkeypatch):
    """Hitting the cap clears the memo; verdicts are unaffected."""
    grid, router = _context()
    halo = grid._route_halo
    monkeypatch.setattr(Router, "_VIA_HALO_CACHE_MAX", 4, raising=True)

    for i, (gx, gy) in enumerate(_SWEEP[:12]):
        expected = _uncached(router, 2, gx, gy, 1)
        assert router._via_halo_clear_cached(halo, 2, gx, gy, 1) is expected
        assert len(router._via_halo_cache) <= 4, f"cap breached after {i + 1} inserts"


@pytest.mark.parametrize("sharing", [False, True])
def test_is_via_blocked_verdicts_unchanged_by_the_memo(sharing):
    """End-to-end: ``_is_via_blocked`` answers the same with and without it."""
    grid, router = _context()

    def uncached_probe(halo, layer, gx, gy, net):
        return _uncached(router, layer, gx, gy, net)

    reference = {}
    router._via_halo_clear_cached = uncached_probe  # type: ignore[method-assign]
    for gx, gy in _SWEEP:
        reference[(gx, gy)] = router._is_via_blocked(gx, gy, 2, 1, sharing, radius=4)

    grid2, router2 = _context()
    assert grid2 is not grid
    for gx, gy in _SWEEP:
        assert router2._is_via_blocked(gx, gy, 2, 1, sharing, radius=4) is reference[(gx, gy)]

    assert set(reference.values()) == {True, False}, "sweep must see both verdicts"

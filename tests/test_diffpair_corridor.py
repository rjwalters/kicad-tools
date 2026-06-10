"""Tests for the Issue #3439 corridor-bounded coupled diff-pair search.

Background: ``CoupledPathfinder.route_coupled`` is a pure-Python A* over
the joint ``(P-pos, N-pos)`` product state space.  On board 07's
4-layer 110x95 mm grid every declared pair blew its 60 s per-pair
budget at ~14k iterations (pure-Python speed), so coupled routing was
structurally intractable and the recipe had to disable
``--differential-pairs`` entirely.

Issue #3439 adds a corridor-bounded search mode:

1. ``build_corridor_mask`` dilates a single-ended guide route (found by
   the C++-accelerated per-net pathfinder) into a layer-agnostic
   spatial corridor.
2. ``CoupledPathfinder.route_coupled`` accepts a ``corridor`` kwarg and
   prunes any neighbor state whose P or N head leaves the corridor
   (endpoint cells exempt).
3. ``DiffPairRouter.route_differential_pair_coupled`` routes the P side
   single-ended (WITHOUT committing it), builds the corridor, attempts
   the coupled search inside it with half the per-pair budget, and
   falls back to the legacy unconstrained search when the corridor
   attempt fails.

The corridor reduces the joint search space from the full grid product
to a near-1D tube, which the pure-Python search completes in seconds.
"""

from __future__ import annotations

import logging
import time

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.diffpair_routing import (
    CoupledPathfinder,
    build_corridor_mask,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

# ---------------------------------------------------------------------------
# build_corridor_mask
# ---------------------------------------------------------------------------


def _make_grid(width: float = 12.7, height: float = 12.7) -> RoutingGrid:
    rules = DesignRules()
    return RoutingGrid(width=width, height=height, rules=rules)


def _straight_guide_route(grid: RoutingGrid) -> Route:
    """A single horizontal segment from (2, 5) to (10, 5) mm."""
    route = Route(net=1, net_name="GUIDE")
    route.segments.append(
        Segment(
            x1=2.0,
            y1=5.0,
            x2=10.0,
            y2=5.0,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="GUIDE",
        )
    )
    return route


def test_corridor_mask_covers_guide_path():
    """Every rasterized guide cell is inside the corridor."""
    grid = _make_grid()
    route = _straight_guide_route(grid)
    corridor = build_corridor_mask(grid, route, radius_cells=2)

    gx1, gy1 = grid.world_to_grid(2.0, 5.0)
    gx2, gy2 = grid.world_to_grid(10.0, 5.0)
    for x in range(gx1, gx2 + 1):
        assert (x, gy1) in corridor, f"guide cell ({x},{gy1}) missing from corridor"


def test_corridor_mask_dilates_by_radius():
    """Cells within ``radius_cells`` (Chebyshev) of the guide are
    included; cells beyond it are excluded."""
    grid = _make_grid()
    route = _straight_guide_route(grid)
    radius = 3
    corridor = build_corridor_mask(grid, route, radius_cells=radius)

    gx, gy = grid.world_to_grid(6.0, 5.0)  # mid-path cell
    assert (gx, gy + radius) in corridor
    assert (gx, gy - radius) in corridor
    assert (gx, gy + radius + 1) not in corridor
    assert (gx, gy - radius - 1) not in corridor


def test_corridor_mask_clamps_to_grid_bounds():
    """Dilation near the board edge never produces out-of-bounds cells."""
    grid = _make_grid()
    route = Route(net=1, net_name="EDGE")
    route.segments.append(
        Segment(
            x1=0.1,
            y1=0.1,
            x2=1.0,
            y2=0.1,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="EDGE",
        )
    )
    corridor = build_corridor_mask(grid, route, radius_cells=5)
    for x, y in corridor:
        assert 0 <= x < grid.cols
        assert 0 <= y < grid.rows


def test_corridor_mask_includes_extra_cells():
    """``extra_cells`` (pad endpoints) are dilated into the mask too."""
    grid = _make_grid()
    route = _straight_guide_route(grid)
    far_cell = grid.world_to_grid(11.5, 11.5)
    corridor = build_corridor_mask(grid, route, radius_cells=1, extra_cells=(far_cell,))
    assert far_cell in corridor


# ---------------------------------------------------------------------------
# CoupledPathfinder.route_coupled corridor kwarg
# ---------------------------------------------------------------------------


def _make_simple_pair_pads() -> tuple[Pad, Pad, Pad, Pad]:
    """Two-pad fixture the coupled pathfinder routes in well under 1 s."""
    p_start = Pad(x=2.0, y=5.0, width=0.2, height=0.2, net=1, net_name="DP+", layer=Layer.F_CU)
    p_end = Pad(x=10.0, y=5.0, width=0.2, height=0.2, net=1, net_name="DP+", layer=Layer.F_CU)
    n_start = Pad(x=2.0, y=5.4, width=0.2, height=0.2, net=2, net_name="DP-", layer=Layer.F_CU)
    n_end = Pad(x=10.0, y=5.4, width=0.2, height=0.2, net=2, net_name="DP-", layer=Layer.F_CU)
    return p_start, p_end, n_start, n_end


def test_route_coupled_signature_accepts_corridor():
    """The kwarg must be on the method with a None default (legacy)."""
    import inspect

    sig = inspect.signature(CoupledPathfinder.route_coupled)
    assert "corridor" in sig.parameters
    assert sig.parameters["corridor"].default is None


def test_route_coupled_succeeds_inside_corridor():
    """A corridor dilated around the P-side path admits the pair."""
    rules = DesignRules()
    grid = _make_grid()
    pf = CoupledPathfinder(grid=grid, rules=rules, target_spacing_cells=2, min_spacing_cells=2)
    p_start, p_end, n_start, n_end = _make_simple_pair_pads()

    corridor = build_corridor_mask(
        grid,
        _straight_guide_route(grid),
        radius_cells=8,
        extra_cells=(
            grid.world_to_grid(p_start.x, p_start.y),
            grid.world_to_grid(p_end.x, p_end.y),
            grid.world_to_grid(n_start.x, n_start.y),
            grid.world_to_grid(n_end.x, n_end.y),
        ),
    )
    result = pf.route_coupled(p_start, p_end, n_start, n_end, corridor=corridor)
    assert result is not None, "corridor-bounded search must route the simple pair"


def test_route_coupled_corridor_prunes_outside_states():
    """A corridor that covers only the start cells (no path to the
    goal) must make the search fail fast instead of exploring the open
    grid."""
    rules = DesignRules()
    grid = _make_grid()
    pf = CoupledPathfinder(grid=grid, rules=rules, target_spacing_cells=2, min_spacing_cells=2)
    p_start, p_end, n_start, n_end = _make_simple_pair_pads()

    # Corridor = just the start pad neighborhoods.  Goal cells are
    # exempt from the check but unreachable because every intermediate
    # cell is pruned.
    start_only: set[tuple[int, int]] = set()
    for pad in (p_start, n_start):
        cx, cy = grid.world_to_grid(pad.x, pad.y)
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                start_only.add((cx + dx, cy + dy))

    t0 = time.monotonic()
    result = pf.route_coupled(p_start, p_end, n_start, n_end, corridor=frozenset(start_only))
    elapsed = time.monotonic() - t0

    assert result is None, "search must fail when the corridor has no path to the goal"
    assert elapsed < 10.0, f"pruned search must exit fast; took {elapsed:.2f}s"


def test_route_coupled_corridor_none_preserves_legacy_behaviour():
    """``corridor=None`` (default) matches the unconstrained search."""
    rules = DesignRules()
    pf = CoupledPathfinder(
        grid=_make_grid(), rules=rules, target_spacing_cells=2, min_spacing_cells=2
    )
    p_start, p_end, n_start, n_end = _make_simple_pair_pads()

    result_default = pf.route_coupled(p_start, p_end, n_start, n_end)
    assert result_default is not None


# ---------------------------------------------------------------------------
# DiffPairRouter integration: corridor-guided attempt
# ---------------------------------------------------------------------------


def _opt_in_diffpair_class_map(net_names: list[str]) -> dict[str, NetClassRouting]:
    nc = NetClassRouting(name="HighSpeedOptIn", coupled_routing=True)
    return dict.fromkeys(net_names, nc)


def _two_pad_diffpair_router(diffpair_spacing: float = 0.8) -> Autorouter:
    """30x10mm board with one straight two-pad diff pair (see
    ``test_diffpair_routing_integration.py`` for the geometry rationale)."""
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(
        width=30.0,
        height=10.0,
        rules=rules,
        net_class_map=_opt_in_diffpair_class_map(["USB_D+", "USB_D-"]),
    )
    p_y = 5.0 - diffpair_spacing / 2
    n_y = 5.0 + diffpair_spacing / 2
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": p_y,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 5.0,
                "y": n_y,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
        ],
    )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 25.0,
                "y": p_y,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 25.0,
                "y": n_y,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
        ],
    )
    return router


def test_coupled_routing_uses_corridor_phase(caplog):
    """On a clean fixture the corridor attempt should succeed (the
    structured timing log reports ``phase=corridor``) and the pair
    should be fully routed."""
    router = _two_pad_diffpair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    with caplog.at_level(logging.INFO, logger="kicad_tools.router.diffpair_routing"):
        routes, warnings, routed_net_ids = router.route_diffpair_prepass(config)

    assert 1 in routed_net_ids and 2 in routed_net_ids, (
        f"Expected both diff-pair nets to route; got {routed_net_ids}"
    )
    timing_records = [r for r in caplog.records if "diffpair coupled timing" in r.getMessage()]
    assert timing_records, "per-pair timing log must be emitted (issue #3439)"
    assert any("phase=corridor" in r.getMessage() for r in timing_records), (
        "corridor-guided attempt should succeed on a clean straight pair; "
        f"got: {[r.getMessage() for r in timing_records]}"
    )


def test_guide_route_is_not_committed_to_route_list():
    """The single-ended guide route must never leak into
    ``autorouter.routes`` -- only the coupled P/N routes are committed."""
    router = _two_pad_diffpair_router()
    config = DifferentialPairConfig(enabled=True, spacing=0.8)

    routes, _warnings, routed_net_ids = router.route_diffpair_prepass(config)
    assert routed_net_ids == {1, 2}

    # Exactly the committed coupled routes appear on the autorouter;
    # an uncommitted guide route would add a third entry for net 1.
    net1_routes = [r for r in router.routes if r.net == 1]
    net2_routes = [r for r in router.routes if r.net == 2]
    assert len(net1_routes) == 1
    assert len(net2_routes) == 1

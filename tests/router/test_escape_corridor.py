"""Tests for congestion-aware escape-corridor reservation (Issue #4474).

Background
----------
Phase 4 of epic #4410.  ``EscapeCorridorPlanner``
(``kicad_tools.router.escape_corridor``) clusters a dense high-pin-count
part's pins by face/destination, sizes a corridor per cluster from
``CongestionEstimator`` demand, assigns clusters to distinct layers, and
SOFT-reserves the corridor cells on the routing grid BEFORE the general
multi-net negotiation.

Contract pinned here (all on a synthetic dense-QFN fixture, not board-05):

1. Clustering: pins group by (nearest bbox face, escape octant).
2. Sizing: a congested cluster reserves a strictly wider corridor than an
   equal-pin-count uncongested one (sized to CongestionEstimator demand).
3. Layer assignment: clusters spread across distinct routable layers on a
   4-layer stack; degrades gracefully on a 2-layer board.
4. Reservation happens as a standalone pre-pass -- cells owned by the
   cluster's net ids appear on the grid before any routing.
5. Post-Kelvin destinations (#4499): a sense net's cluster aims at its
   shunt/root pad, not the raw net centroid.
6. Default-OFF (the #4051 rollout convention): ``Autorouter`` defaults the
   flag False, never constructs the planner, and reserves nothing -- so
   routing output is byte-identical to pre-#4474 main.
7. Edge cases: an uncongested face is a no-op; a 2-layer board does not
   crash the layer-assignment step.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.congestion_estimator import CongestionEstimator, TileGrid
from kicad_tools.router.core import Autorouter
from kicad_tools.router.escape import PackageInfo, PackageType
from kicad_tools.router.escape_corridor import (
    EscapeCorridorPlanner,
    _octant_of,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# =============================================================================
# Fixtures
# =============================================================================

BOARD = 30.0
CENTER = 15.0


@pytest.fixture
def rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


@pytest.fixture
def grid_4layer(rules: DesignRules) -> RoutingGrid:
    stack = LayerStack.four_layer_all_signal()
    return RoutingGrid(BOARD, BOARD, rules, origin_x=0, origin_y=0, layer_stack=stack)


@pytest.fixture
def grid_2layer(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(BOARD, BOARD, rules, origin_x=0, origin_y=0)


def make_dense_qfn(
    *,
    pins_per_side: int = 4,
    pitch: float = 0.5,
    center: tuple[float, float] = (CENTER, CENTER),
    dest_dist: float = 8.0,
) -> tuple[list[Pad], dict[int, list[tuple[float, float]]]]:
    """A dense 4-sided QFN whose every pin escapes straight out its own face.

    Returns ``(pkg_pads, net_pad_positions)``.  Each pin gets a unique net
    whose only OTHER pad sits ``dest_dist`` mm directly outward from the
    package face, so the pin's escape octant is unambiguously N/S/E/W.
    Net ids are laid out by side so tests can address a whole face's nets.
    """
    cx, cy = center
    half = (pins_per_side - 1) * pitch / 2.0
    pkg_pads: list[Pad] = []
    net_pad_positions: dict[int, list[tuple[float, float]]] = {}

    # side -> (face offset direction). KiCad y grows downward: N=min y.
    sides = {
        "N": (0.0, -1.0),
        "S": (0.0, 1.0),
        "W": (-1.0, 0.0),
        "E": (1.0, 0.0),
    }
    nid = 1
    edge = half + 1.0  # pad-row offset from center to the package edge
    for side, (ox, oy) in sides.items():
        for i in range(pins_per_side):
            along = -half + i * pitch
            if side in ("N", "S"):
                px, py = cx + along, cy + oy * edge
            else:
                px, py = cx + ox * edge, cy + along
            pad = Pad(
                x=px,
                y=py,
                width=0.25,
                height=0.25,
                net=nid,
                net_name=f"{side}_NET_{i}",
                layer=Layer.F_CU,
                ref="U3",
            )
            pkg_pads.append(pad)
            # Destination pad: straight out the face.
            dx, dy = px + ox * dest_dist, py + oy * dest_dist
            net_pad_positions[nid] = [(px, py), (dx, dy)]
            nid += 1
    return pkg_pads, net_pad_positions


def make_package(pads: list[Pad], ref: str = "U3") -> PackageInfo:
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    center = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
    return PackageInfo(
        ref=ref,
        package_type=PackageType.QFN,
        center=center,
        pads=pads,
        pin_count=len(pads),
        pin_pitch=0.5,
        bounding_box=bbox,
        is_dense=True,
    )


def uniform_estimator(peak_south: float = 0.0, base: float = 0.0) -> CongestionEstimator:
    """A hand-built estimator: ``base`` demand everywhere, ``peak_south`` in
    the southern half of the board (rows below center)."""
    tgrid = TileGrid.from_board(0.0, 0.0, BOARD, BOARD, target_tiles=100)
    demand = [[base] * tgrid.cols for _ in range(tgrid.rows)]
    if peak_south:
        for r in range(tgrid.rows):
            # tile row center y
            y = tgrid.origin_y + (r + 0.5) * tgrid.tile_h
            if y > CENTER:
                for c in range(tgrid.cols):
                    demand[r][c] = peak_south
    est = CongestionEstimator(grid=tgrid)
    est.demand = demand
    return est


# =============================================================================
# 1. Clustering
# =============================================================================


def test_octant_helper_cardinal_directions() -> None:
    assert _octant_of(1.0, 0.0) == "E"
    assert _octant_of(0.0, 1.0) == "S"
    assert _octant_of(0.0, -1.0) == "N"
    assert _octant_of(-1.0, 0.0) == "W"


def test_clusters_by_face_and_destination(grid_4layer: RoutingGrid) -> None:
    pads, npp = make_dense_qfn()
    pkg = make_package(pads)
    planner = EscapeCorridorPlanner(grid_4layer, net_pad_positions=npp)
    plan = planner.plan(pkg)

    # Four faces -> four clusters, each pointing straight out its own octant.
    assert plan.cluster_count == 4
    by_key = {c.key: c for c in plan.clusters}
    assert set(by_key) == {("N", "N"), ("S", "S"), ("E", "E"), ("W", "W")}
    for cluster in plan.clusters:
        assert cluster.pin_count == 4
        # Every pin's net id is unique to the cluster.
        assert len(cluster.net_ids) == 4


def test_pin_assigned_to_nearest_face(grid_4layer: RoutingGrid) -> None:
    pads, npp = make_dense_qfn(pins_per_side=3)
    pkg = make_package(pads)
    planner = EscapeCorridorPlanner(grid_4layer, net_pad_positions=npp)
    plan = planner.plan(pkg)
    south = next(c for c in plan.clusters if c.face == "S")
    # All south-cluster pads sit on the max-y edge of the bbox.
    max_y = pkg.bounding_box[3]
    assert all(math.isclose(p.y, max_y) for p in south.pads)


# =============================================================================
# 2. Corridor sizing to CongestionEstimator demand
# =============================================================================


def test_corridor_sized_to_congestion_demand(grid_4layer: RoutingGrid) -> None:
    """A congested (south) cluster reserves strictly more cells than an
    equal-pin-count uncongested (north) cluster."""
    pads, npp = make_dense_qfn()
    pkg = make_package(pads)
    est = uniform_estimator(peak_south=50.0, base=1.0)
    planner = EscapeCorridorPlanner(grid_4layer, est, net_pad_positions=npp)
    plan = planner.plan_and_reserve(pkg)

    south = next(c for c in plan.clusters if c.face == "S")
    north = next(c for c in plan.clusters if c.face == "N")
    assert south.demand > north.demand
    # Same pin count, same destination distance -> the wider (higher-demand)
    # corridor is the only difference, so it reserves more cells.
    assert south.reserved_cell_count > north.reserved_cell_count


def test_zero_demand_estimator_is_noop_reservation(grid_4layer: RoutingGrid) -> None:
    """Edge case: every face uncongested (demand 0) -> planner reserves nothing."""
    pads, npp = make_dense_qfn()
    pkg = make_package(pads)
    est = uniform_estimator(peak_south=0.0, base=0.0)
    planner = EscapeCorridorPlanner(grid_4layer, est, net_pad_positions=npp)
    plan = planner.plan_and_reserve(pkg)
    assert plan.total_reserved_cells == 0
    assert not grid_4layer._reserved_for_nets


# =============================================================================
# 2b. Selectivity threshold (#4519): only genuinely congested clusters reserve
# =============================================================================


def _make_ns_pair() -> tuple[PackageInfo, dict[int, list[tuple[float, float]]]]:
    """A minimal 2-pin part: one pin escaping N, one escaping S, far apart so
    each lands in a distinct congestion tile (no face/tile overlap)."""
    n_pad = Pad(x=CENTER, y=5.0, width=0.25, height=0.25, net=1, net_name="N_NET", ref="U9")
    s_pad = Pad(x=CENTER, y=25.0, width=0.25, height=0.25, net=2, net_name="S_NET", ref="U9")
    npp = {
        1: [(CENTER, 5.0), (CENTER, 1.0)],  # off-package pad due north
        2: [(CENTER, 25.0), (CENTER, 29.0)],  # off-package pad due south
    }
    pkg = make_package([n_pad, s_pad], ref="U9")
    return pkg, npp


def _south_hot_estimator(pkg: PackageInfo, s_pad_xy: tuple[float, float]) -> CongestionEstimator:
    """Estimator with demand 100 in ONLY the tile holding ``s_pad_xy``."""
    tgrid = TileGrid.from_board(0.0, 0.0, BOARD, BOARD, target_tiles=100)
    demand = [[0.0] * tgrid.cols for _ in range(tgrid.rows)]
    col, row = tgrid.tile_at(*s_pad_xy)
    demand[row][col] = 100.0
    est = CongestionEstimator(grid=tgrid)
    est.demand = demand
    return est


def test_relative_threshold_reserves_only_congested_cluster(
    grid_4layer: RoutingGrid,
) -> None:
    """#4519: with a relative-to-peak floor, only the congested (south) cluster
    reserves a channel; the uncongested cluster is a no-op.

    Without the floor (default 0.0) every nonzero-demand cluster reserves --
    the PR #4509 over-reservation.  With ``min_reserve_demand_frac`` set, a
    cluster reserves only when ``cluster.demand / peak_tile_demand`` exceeds
    the fraction, so a diffuse low-demand face is skipped.
    """
    pkg, npp = _make_ns_pair()
    est = _south_hot_estimator(pkg, (CENTER, 25.0))

    # Baseline: no relative floor -> both clusters reserve (the over-reservation
    # -- even the zero-demand north cluster is not skipped, only exactly-zero is,
    # and the north tile IS exactly zero here so it is skipped; use a tiny base
    # to demonstrate the permissive path reserves a diffuse cluster too).
    est_base = _south_hot_estimator(pkg, (CENTER, 25.0))
    n_col, n_row = est_base.grid.tile_at(CENTER, 5.0)
    est_base.demand[n_row][n_col] = 1.0  # tiny nonzero north demand
    permissive = EscapeCorridorPlanner(grid_4layer, est_base, net_pad_positions=npp)
    permissive_plan = permissive.plan_and_reserve(pkg)
    assert {c.face for c in permissive_plan.clusters if c.reserved_cell_count > 0} == {"N", "S"}

    # Selective: a 0.5 * peak floor keeps only the south hotspot cluster.
    grid_2 = RoutingGrid(
        BOARD, BOARD, grid_4layer.rules, origin_x=0, origin_y=0, layer_stack=grid_4layer.layer_stack
    )
    selective = EscapeCorridorPlanner(
        grid_2, est, net_pad_positions=npp, min_reserve_demand_frac=0.5
    )
    selective_plan = selective.plan_and_reserve(pkg)
    reserved_faces = {c.face for c in selective_plan.clusters if c.reserved_cell_count > 0}
    assert reserved_faces == {"S"}
    assert selective_plan.total_reserved_cells > 0
    assert selective_plan.total_reserved_cells < permissive_plan.total_reserved_cells


def test_cluster_below_relative_threshold_is_skipped(grid_4layer: RoutingGrid) -> None:
    """A nonzero-but-below-threshold cluster reserves zero cells; one above it
    reserves a nonzero count (the selectivity contract, #4519)."""
    pads, npp = make_dense_qfn()
    pkg = make_package(pads)
    est = uniform_estimator(peak_south=100.0, base=2.0)
    planner = EscapeCorridorPlanner(
        grid_4layer, est, net_pad_positions=npp, min_reserve_demand_frac=0.5
    )
    plan = planner.plan_and_reserve(pkg)
    threshold = planner._reserve_threshold(plan.clusters)
    south = next(c for c in plan.clusters if c.face == "S")
    north = next(c for c in plan.clusters if c.face == "N")
    # North sits in base-demand tiles (well under 0.5 * the peak cluster) -> skipped.
    assert north.demand <= threshold
    assert north.reserved_cell_count == 0
    # South is the peak-congested cluster -> above threshold, reserves cells.
    assert south.demand > threshold
    assert south.reserved_cell_count > 0


def test_relative_threshold_ignored_without_estimator(grid_4layer: RoutingGrid) -> None:
    """Demand-agnostic mode (no estimator) reserves every cluster regardless of
    the relative floor -- the gate is only consulted when an estimator exists."""
    pads, npp = make_dense_qfn()
    pkg = make_package(pads)
    planner = EscapeCorridorPlanner(
        grid_4layer, None, net_pad_positions=npp, min_reserve_demand_frac=0.9
    )
    plan = planner.plan_and_reserve(pkg)
    assert plan.total_reserved_cells > 0
    reserved_faces = {c.face for c in plan.clusters if c.reserved_cell_count > 0}
    assert reserved_faces == {"N", "S", "E", "W"}


# =============================================================================
# 2c. Corridor-length cap (#4519): a corridor stays a local escape channel
# =============================================================================


def test_max_corridor_length_caps_reserved_extent(grid_4layer: RoutingGrid) -> None:
    """A corridor with ``max_corridor_length_mm`` set never extends past the cap
    from the cluster origin along the escape direction (#4519)."""
    # A far destination (dest_dist large) would otherwise span most of the board.
    pads, npp = make_dense_qfn(dest_dist=12.0)
    pkg = make_package(pads)
    est = uniform_estimator(base=5.0)

    cap_mm = 2.0
    capped = EscapeCorridorPlanner(
        grid_4layer, est, net_pad_positions=npp, max_corridor_length_mm=cap_mm
    )
    capped_plan = capped.plan(pkg)

    for cluster in capped_plan.clusters:
        dx, dy = cluster.escape_dir
        ox, oy = cluster.origin
        for gx, gy in cluster.corridor_cells:
            wx, wy = grid_4layer.grid_to_world(gx, gy)
            # Longitudinal distance (projection onto the escape direction).
            along = (wx - ox) * dx + (wy - oy) * dy
            # Allow one grid resolution of slack for cell-center quantization.
            assert along <= cap_mm + grid_4layer.resolution


def test_uncapped_corridor_reaches_farther_than_capped(grid_4layer: RoutingGrid) -> None:
    """The cap is load-bearing: an uncapped corridor reserves strictly more
    cells than a short-capped one for the same far destination (#4519)."""
    pads, npp = make_dense_qfn(dest_dist=12.0)
    pkg = make_package(pads)
    est = uniform_estimator(base=5.0)

    capped = EscapeCorridorPlanner(
        grid_4layer, est, net_pad_positions=npp, max_corridor_length_mm=2.0
    )
    capped_plan = capped.plan_and_reserve(pkg)

    grid_b = RoutingGrid(
        BOARD, BOARD, grid_4layer.rules, origin_x=0, origin_y=0, layer_stack=grid_4layer.layer_stack
    )
    uncapped = EscapeCorridorPlanner(grid_b, est, net_pad_positions=npp)
    uncapped_plan = uncapped.plan_and_reserve(pkg)

    assert capped_plan.total_reserved_cells < uncapped_plan.total_reserved_cells


# =============================================================================
# 3b. Inner-layer restriction (#4519, Scope item 3)
# =============================================================================


def test_inner_layers_only_confines_to_inner_layers(grid_4layer: RoutingGrid) -> None:
    """With ``inner_layers_only`` set, no cluster is assigned an outer
    (component) layer on a 4-layer stack (#4519)."""
    pads, npp = make_dense_qfn()
    pkg = make_package(pads)
    est = uniform_estimator(base=5.0)  # all clusters congested
    outer = set(grid_4layer.layer_stack.get_outer_layer_indices())

    planner = EscapeCorridorPlanner(grid_4layer, est, net_pad_positions=npp, inner_layers_only=True)
    plan = planner.plan_and_reserve(pkg)
    used = plan.layers_used()
    assert used  # something was assigned
    assert used.isdisjoint(outer)  # never an outer/component layer

    # Sanity: without the flag the same clusters DO spread onto outer layers.
    grid_b = RoutingGrid(
        BOARD, BOARD, grid_4layer.rules, origin_x=0, origin_y=0, layer_stack=grid_4layer.layer_stack
    )
    unrestricted = EscapeCorridorPlanner(grid_b, est, net_pad_positions=npp)
    unrestricted_plan = unrestricted.plan_and_reserve(pkg)
    assert unrestricted_plan.layers_used() & outer


def test_inner_layers_only_degrades_on_2layer(grid_2layer: RoutingGrid) -> None:
    """A 2-layer board has no inner layers; ``inner_layers_only`` must not
    crash or reserve nothing -- it falls back to the outer routable layers."""
    pads, npp = make_dense_qfn()
    pkg = make_package(pads)
    est = uniform_estimator(base=5.0)
    planner = EscapeCorridorPlanner(grid_2layer, est, net_pad_positions=npp, inner_layers_only=True)
    plan = planner.plan_and_reserve(pkg)  # must not raise
    used = plan.layers_used()
    assert used
    assert used.issubset(set(grid_2layer.get_routable_indices()))


# =============================================================================
# 3. Layer assignment
# =============================================================================


def test_layer_assignment_distributes_across_layers_4layer(
    grid_4layer: RoutingGrid,
) -> None:
    pads, npp = make_dense_qfn()
    pkg = make_package(pads)
    est = uniform_estimator(base=5.0)  # all clusters congested so all reserve
    planner = EscapeCorridorPlanner(grid_4layer, est, net_pad_positions=npp)
    plan = planner.plan_and_reserve(pkg)
    # Four clusters over four routable layers -> four distinct layers.
    assert len(plan.layers_used()) == 4


def test_layer_assignment_degrades_on_2layer(grid_2layer: RoutingGrid) -> None:
    pads, npp = make_dense_qfn()
    pkg = make_package(pads)
    est = uniform_estimator(base=5.0)
    planner = EscapeCorridorPlanner(grid_2layer, est, net_pad_positions=npp)
    plan = planner.plan_and_reserve(pkg)  # must not raise
    routable = set(grid_2layer.get_routable_indices())
    used = plan.layers_used()
    assert used  # something was assigned
    assert used.issubset(routable)


# =============================================================================
# 4. Reservation is a pre-pass (cells on grid, owned by cluster nets)
# =============================================================================


def test_reservation_populates_grid_before_negotiation(
    grid_4layer: RoutingGrid,
) -> None:
    pads, npp = make_dense_qfn()
    pkg = make_package(pads)
    est = uniform_estimator(base=5.0)
    planner = EscapeCorridorPlanner(grid_4layer, est, net_pad_positions=npp)

    assert not grid_4layer._reserved_for_nets  # nothing reserved yet
    plan = planner.plan_and_reserve(pkg)
    assert plan.total_reserved_cells > 0
    assert grid_4layer._reserved_for_nets  # reserved as a standalone pre-pass

    # Every reserved cell is owned by the planning nets only (never a
    # foreign net), matching the SOFT corridor owner-set contract.
    all_cluster_nets: set[int] = set()
    for c in plan.clusters:
        all_cluster_nets |= set(c.net_ids)
    for owners in grid_4layer._reserved_for_nets.values():
        assert set(owners).issubset(all_cluster_nets)


# =============================================================================
# 5. Post-Kelvin destination (#4499)
# =============================================================================


def test_kelvin_root_overrides_destination(grid_4layer: RoutingGrid) -> None:
    """A sense net whose Kelvin root is supplied aims its cluster at the
    shunt pad, not the raw off-package centroid."""
    # One south pin on a sense net; its net has an off-package pad to the
    # EAST, but the Kelvin root (shunt) is to the SOUTH.
    cx, cy = CENTER, CENTER
    sense_pad = Pad(x=cx, y=cy + 2.0, width=0.25, height=0.25, net=1, net_name="ISENSE_A", ref="U3")
    pkg = make_package([sense_pad])
    # net_pad_positions would push the destination east...
    npp = {1: [(cx, cy + 2.0), (cx + 15.0, cy + 2.0)]}
    # ...but the Kelvin root is due south.
    kelvin_roots = {1: (cx, cy + 15.0)}
    planner = EscapeCorridorPlanner(grid_4layer, net_pad_positions=npp, kelvin_roots=kelvin_roots)
    plan = planner.plan(pkg)
    assert plan.cluster_count == 1
    cluster = plan.clusters[0]
    # Escape direction is southward (dy dominant, positive), not eastward.
    assert cluster.escape_dir[1] > abs(cluster.escape_dir[0])
    assert cluster.dest_octant == "S"


# =============================================================================
# 6. Default-OFF byte-identity guard (the #4051 rollout convention)
# =============================================================================


def _make_router_with_dense_qfn(*, enable: bool) -> Autorouter:
    stack = LayerStack.four_layer_all_signal()
    router = Autorouter(width=BOARD, height=BOARD, layer_stack=stack)
    router.enable_escape_corridor_reservation = enable
    pads, npp = make_dense_qfn()
    comp_pins = [
        {
            "number": str(i + 1),
            "x": p.x,
            "y": p.y,
            "net": int(p.net),
            "net_name": p.net_name,
        }
        for i, p in enumerate(pads)
    ]
    router.add_component("U3", comp_pins)
    # Each net's far destination pad on a separate footprint, so nets have
    # real 2-pad geometry (nonzero RUDY demand at U3 -> the reservation
    # gate opens) and an off-package destination for the planner to aim at.
    dest_pins = []
    for i, p in enumerate(pads):
        far = npp[int(p.net)][1]
        dest_pins.append(
            {
                "number": str(i + 1),
                "x": far[0],
                "y": far[1],
                "net": int(p.net),
                "net_name": p.net_name,
            }
        )
    router.add_component("DST", dest_pins)
    return router


def test_autorouter_flag_defaults_off() -> None:
    router = Autorouter(width=BOARD, height=BOARD)
    assert router.enable_escape_corridor_reservation is False
    assert router._escape_corridor_plans == []
    # The lazily-built escape router mirrors the OFF state and carries no
    # reserved plans -> nothing to consume.
    assert router._escape.enable_escape_corridor_reservation is False
    assert router._escape.escape_corridor_plans == []


def _stub_heavy_escape(router: Autorouter, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the escape pre-phase's A*-heavy sub-steps so a test can exercise
    the (cheap) corridor-reservation gate without routing real escapes."""
    monkeypatch.setattr(router, "_run_subgrid_prepass", lambda: [])
    monkeypatch.setattr(router, "generate_escape_routes", lambda pkgs: [])
    monkeypatch.setattr(router, "_build_pad_channel_budgets", lambda pkgs: [])


def test_flag_off_reserves_nothing_flag_on_reserves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-OFF byte-identity: with the flag absent the escape pre-phase
    reserves zero corridor cells; with it on, corridors are reserved."""
    off = _make_router_with_dense_qfn(enable=False)
    _stub_heavy_escape(off, monkeypatch)
    off._run_escape_prephase()
    assert off._escape_corridor_plans == []
    assert not off.grid._reserved_for_nets

    on = _make_router_with_dense_qfn(enable=True)
    _stub_heavy_escape(on, monkeypatch)
    on._run_escape_prephase()
    # The planner ran and produced at least one plan; reservation is advisory
    # so cells are reserved when the dense QFN clusters resolve.
    assert on._escape_corridor_plans
    total = sum(p.total_reserved_cells for p in on._escape_corridor_plans)
    assert total > 0
    assert on.grid._reserved_for_nets


def test_escape_router_threads_flag_and_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = _make_router_with_dense_qfn(enable=True)
    _stub_heavy_escape(on, monkeypatch)
    on._run_escape_prephase()
    # The escape router built for the run sees the flag ON and the plans.
    assert on._escape.enable_escape_corridor_reservation is True
    assert on._escape.escape_corridor_plans is on._escape_corridor_plans

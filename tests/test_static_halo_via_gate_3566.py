"""Issue #3566: Python fallback router static-halo gate for vias/diagonals.

PR #3565 (Issue #3545) made statically blocked foreign cells (pad
clearance halos, keepouts) non-negotiable in the sharing A*: all five
C++ branches plus the Python ``Router._is_trace_blocked`` /
``RoutingGrid.compute_expanded_blocked`` paths were gated on the static
blockage snapshot.  Two Python fallback branches were missed and kept
the pre-#3545 ``usage_count > 0`` release for foreign static cells:

* ``Router._is_via_blocked``
* ``Router._is_diagonal_corner_blocked``

Post-#3545, a net's own escape route legitimately crosses its own pad
halo cells, leaving them at ``usage_count > 0`` while ``cell.net`` stays
the owner net.  On the Python backend a FOREIGN net could then place a
via whose envelope intrudes into that halo, or cut a diagonal corner
through it -- shipping sub-clearance copper caught only by exact KiCad
DRC (neither ``validate_via_clearance`` nor the #3545 segment-only
demotion backstop covers vias-vs-pads).

These tests are the Python-backend twin of
``tests/test_static_halo_ripup_3545.py::TestStaticHaloRipupRestore``:
they exercise the pure-Python :class:`kicad_tools.router.pathfinder.Router`
directly (instantiating ``Router`` IS the Python backend -- no
``router_cpp`` involvement), and pin:

* ``_is_via_blocked`` blocks a foreign via over a static halo cell with
  ``usage_count > 0`` (the #3566 gate),
* ``_is_diagonal_corner_blocked`` blocks the corner-cut equivalently,
* relief probes (#3438) keep their soft-crossing semantics,
* same-net checks remain passable,
* the finalization backstop's new via quadrant
  (``RoutingGrid.worst_via_pad_deficit`` consumed by
  ``Autorouter._demote_pad_clearance_violation_nets``) demotes nets
  whose committed vias violate foreign-pad clearance on BOTH backends.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router import DesignRules, load_pcb_for_routing
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Via

FIXTURE = Path(__file__).parent / "fixtures" / "routing-diagnostic.kicad_pcb"


@pytest.fixture
def rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.2,
    )


@pytest.fixture
def grid(rules: DesignRules) -> RoutingGrid:
    # 20mm x 20mm board at 0.2mm resolution.
    return RoutingGrid(100, 100, rules, origin_x=0.0, origin_y=0.0)


@pytest.fixture
def router(grid: RoutingGrid, rules: DesignRules) -> Router:
    """Pure-Python A* pathfinder (the Python fallback backend)."""
    return Router(grid, rules)


def _tht_pad(x: float, y: float, net: int, net_name: str, ref: str, pin: str) -> Pad:
    """1.7mm circular THT pad, mirroring J1 on the routing-diagnostic fixture."""
    return Pad(
        x=x,
        y=y,
        width=1.7,
        height=1.7,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
        through_hole=True,
        drill=1.0,
    )


def _make_static_halo_cell(grid: RoutingGrid, gx: int, gy: int, layer: int, net: int) -> None:
    """Single statically blocked non-obstacle cell (a pad clearance halo
    cell), captured into the static blockage snapshot.

    Mirrors the C++ parity test's ``Grid3D.mark_blocked(x, y, layer, net,
    blocked=True, is_obstacle=False)`` setup in
    ``test_static_halo_ripup_3545.py``.
    """
    cell = grid.grid[layer][gy][gx]
    cell.blocked = True
    cell.net = net
    # Snapshot is normally captured by the first ``mark_route``; trigger
    # the same hook directly so the test does not depend on route-copper
    # envelopes contaminating the via kernel.
    grid._ensure_static_blockage_snapshot()


class TestPythonViaStaticHaloGate:
    """``_is_via_blocked`` must not release foreign static halo cells at
    ``usage_count > 0`` (Issue #3566, C++ parity for #3545)."""

    def test_foreign_via_blocked_on_static_halo_with_usage(
        self, grid: RoutingGrid, router: Router
    ) -> None:
        """The exact #3566 defect: usage > 0 released the halo to vias."""
        gx, gy, layer = 35, 57, 0
        _make_static_halo_cell(grid, gx, gy, layer, net=1)

        # Sanity: at usage 0 the legacy clause already blocks.
        assert router._is_via_blocked(gx, gy, layer, net=3, allow_sharing=True) is True

        # NET1's own escape route crossed its own halo (legal): usage > 0,
        # net stays the owner.
        grid.grid[layer][gy][gx].usage_count = 1

        # Pre-#3566 the usage>0 clause released the cell and the foreign
        # via envelope was allowed to intrude into the pad's clearance
        # halo.  Post-fix the static gate blocks regardless of usage.
        assert router._is_via_blocked(gx, gy, layer, net=3, allow_sharing=True) is True, (
            "foreign via released a static pad-halo cell at usage_count > 0"
        )

    def test_own_net_via_remains_passable(self, grid: RoutingGrid, router: Router) -> None:
        """The owner net's own via over its own halo stays legal."""
        gx, gy, layer = 35, 57, 0
        _make_static_halo_cell(grid, gx, gy, layer, net=1)
        grid.grid[layer][gy][gx].usage_count = 1

        assert router._is_via_blocked(gx, gy, layer, net=1, allow_sharing=True) is False

    def test_relief_probe_keeps_soft_crossing(self, grid: RoutingGrid, router: Router) -> None:
        """#3438 semantics survive: relief probes stay static-visible
        (passable at a penalty) so conflict attribution works."""
        gx, gy, layer = 35, 57, 0
        _make_static_halo_cell(grid, gx, gy, layer, net=1)

        router.set_relief_mode(True)
        try:
            # usage 0: relief releases the foreign static cell (soft).
            assert router._is_via_blocked(gx, gy, layer, net=3, allow_sharing=True) is False
            # usage > 0: the new static gate must NOT re-block the probe.
            grid.grid[layer][gy][gx].usage_count = 1
            assert router._is_via_blocked(gx, gy, layer, net=3, allow_sharing=True) is False
        finally:
            router.set_relief_mode(False)

    def test_plane_pad_halo_cell_with_usage_blocks_foreign_via(
        self, grid: RoutingGrid, router: Router
    ) -> None:
        """Realistic geometry: a real plane-net pad's halo at usage > 0.

        Twin of ``TestStaticHaloRipupRestore.test_python_ripup_restores_
        static_halo`` but for via placement.  Signal-pad halos are fully
        ``is_obstacle`` since #2940, so the live remaining release path
        runs through NET-0 statics: plane-net pads (``pad.net == 0``,
        bonded by ``kct stitch``) keep ``is_obstacle = False`` on every
        halo/metal cell, and the pre-#3566 ``cell_net == 0`` branch
        released them to foreign vias as soon as ``usage_count > 0``.
        The via is centered so its envelope's only blocked cell is the
        halo edge cell.
        """
        pad = Pad(
            x=8.0,
            y=12.0,
            width=1.7,
            height=1.7,
            net=0,
            net_name="",
            layer=Layer.F_CU,
            ref="J1",
            pin="1",
            through_hole=True,
            drill=1.0,
        )
        grid.add_pad(pad)
        grid._ensure_static_blockage_snapshot()

        pad_gx, pad_gy = grid.world_to_grid(8.0, 12.0)
        layer = 0

        # Outermost blocked halo cell scanning west from the pad center.
        edge_gx = pad_gx
        while edge_gx > 0 and bool(grid._blocked[layer, pad_gy, edge_gx - 1]):
            edge_gx -= 1
        assert bool(grid._blocked[layer, pad_gy, edge_gx]) is True
        assert not bool(grid._is_obstacle[layer, pad_gy, edge_gx]), (
            "plane-pad halo cells must be non-obstacle (else this test "
            "no longer pins the #3566 release path)"
        )
        assert int(grid._net[layer, pad_gy, edge_gx]) == 0

        # Via centered so the envelope's rightmost cell IS the halo edge.
        r_v = router._via_half_cells
        via_gx, via_gy = edge_gx - r_v, pad_gy

        # Precondition: every blocked cell in the via envelope is a
        # non-obstacle net-0 halo cell -- give them all usage > 0 so the
        # pre-#3566 logic would release the entire envelope.
        blocked_in_envelope = 0
        for dx, dy in zip(router._via_offset_dx, router._via_offset_dy, strict=True):
            cx, cy = via_gx + int(dx), via_gy + int(dy)
            if bool(grid._blocked[layer, cy, cx]):
                assert not bool(grid._is_obstacle[layer, cy, cx])
                assert int(grid._net[layer, cy, cx]) == 0
                grid.grid[layer][cy][cx].usage_count = 1
                blocked_in_envelope += 1
        assert blocked_in_envelope >= 1, "via envelope must touch the halo"

        assert router._is_via_blocked(via_gx, via_gy, layer, net=3, allow_sharing=True) is True, (
            "foreign via intruded into a plane pad's clearance halo at usage > 0"
        )


class TestPythonDiagonalCornerStaticHaloGate:
    """``_is_diagonal_corner_blocked`` must not release foreign static
    halo cells at ``usage_count > 0`` (Issue #3566)."""

    def test_foreign_corner_cut_blocked_on_static_halo_with_usage(
        self, grid: RoutingGrid, router: Router
    ) -> None:
        gx, gy, layer = 35, 57, 0
        _make_static_halo_cell(grid, gx, gy, layer, net=1)

        # Diagonal move from (34, 57) with (dx=1, dy=1) checks adjacent
        # cells (34, 58) and (35, 57) -- the static halo cell.
        # Sanity at usage 0 (legacy clause):
        assert (
            router._is_diagonal_corner_blocked(34, 57, 1, 1, layer, net=3, allow_sharing=True)
            is True
        )

        grid.grid[layer][gy][gx].usage_count = 1
        assert (
            router._is_diagonal_corner_blocked(34, 57, 1, 1, layer, net=3, allow_sharing=True)
            is True
        ), "foreign diagonal corner-cut released a static halo cell at usage > 0"

    def test_own_net_corner_cut_remains_passable(self, grid: RoutingGrid, router: Router) -> None:
        gx, gy, layer = 35, 57, 0
        _make_static_halo_cell(grid, gx, gy, layer, net=1)
        grid.grid[layer][gy][gx].usage_count = 1

        assert (
            router._is_diagonal_corner_blocked(34, 57, 1, 1, layer, net=1, allow_sharing=True)
            is False
        )

    def test_relief_probe_keeps_soft_crossing(self, grid: RoutingGrid, router: Router) -> None:
        """The static gate is relief-exempt (matches the C++ gate); the
        legacy usage clause in this branch never had a relief carve-out
        and is unchanged by #3566."""
        gx, gy, layer = 35, 57, 0
        _make_static_halo_cell(grid, gx, gy, layer, net=1)
        grid.grid[layer][gy][gx].usage_count = 1

        router.set_relief_mode(True)
        try:
            assert (
                router._is_diagonal_corner_blocked(34, 57, 1, 1, layer, net=3, allow_sharing=True)
                is False
            ), "relief probe must not be hard-blocked by the #3566 static gate"
        finally:
            router.set_relief_mode(False)


class TestViaPadDeficitBackstop:
    """Finalization backstop via quadrant (Issue #3566): sub-clearance
    vias next to foreign pads are demoted, never committed."""

    def test_worst_via_pad_deficit_geometry(self, grid: RoutingGrid) -> None:
        """Exact geometry: via 1.0mm from a 1.7mm pad center."""
        grid.add_pad(_tht_pad(8.0, 12.0, net=1, net_name="NET1", ref="J1", pin="1"))
        via = Via(
            x=7.0,
            y=12.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=3,
            net_name="NET3",
        )
        # clearance = 1.0 - 0.3 (via radius) - 0.85 (pad radius) = -0.15mm
        # deficit = 0.2 - (-0.15) = 0.35mm
        deficit, loc = grid.worst_via_pad_deficit(via, exclude_net=3)
        assert deficit == pytest.approx(0.35, abs=0.01)
        assert loc == (8.0, 12.0)

    def test_clean_via_has_no_deficit(self, grid: RoutingGrid) -> None:
        grid.add_pad(_tht_pad(8.0, 12.0, net=1, net_name="NET1", ref="J1", pin="1"))
        via = Via(
            x=5.0,
            y=12.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=3,
            net_name="NET3",
        )
        # clearance = 3.0 - 0.3 - 0.85 = 1.85mm >> 0.2mm required.
        deficit, loc = grid.worst_via_pad_deficit(via, exclude_net=3)
        assert deficit == 0.0
        assert loc is None

    def test_same_net_pad_excluded(self, grid: RoutingGrid) -> None:
        """A via tight against its OWN net's pad never registers."""
        grid.add_pad(_tht_pad(8.0, 12.0, net=3, net_name="NET3", ref="J1", pin="1"))
        via = Via(
            x=8.0,
            y=12.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=3,
            net_name="NET3",
        )
        deficit, _loc = grid.worst_via_pad_deficit(via, exclude_net=3)
        assert deficit == 0.0

    def test_via_violation_is_demoted(self, rules: DesignRules) -> None:
        """A committed via inside a foreign pad's halo strips the net."""
        router, _ = load_pcb_for_routing(str(FIXTURE), rules=rules, validate_drc=False)
        # NET3 via 0.6mm from J1 pad 1 (NET1, 1.7mm circular at 8.0/12.0):
        # clearance = 0.6 - 0.3 - 0.85 = -0.55mm => deficit 0.75mm, far
        # beyond the 0.2mm grid-resolution nudge reach.
        bad = Route(net=3, net_name="NET3")
        bad.vias.append(
            Via(
                x=7.4,
                y=12.0,
                drill=0.3,
                diameter=0.6,
                layers=(Layer.F_CU, Layer.B_CU),
                net=3,
                net_name="NET3",
            )
        )
        router.routes.append(bad)
        net_routes = {3: [bad]}

        demoted = router._demote_pad_clearance_violation_nets(net_routes)

        assert demoted == [3]
        assert net_routes[3] == []
        assert bad not in router.routes

    def test_clean_via_route_is_not_demoted(self, rules: DesignRules) -> None:
        router, _ = load_pcb_for_routing(str(FIXTURE), rules=rules, validate_drc=False)
        good = Route(net=3, net_name="NET3")
        good.vias.append(
            Via(
                x=5.0,
                y=10.0,
                drill=0.3,
                diameter=0.6,
                layers=(Layer.F_CU, Layer.B_CU),
                net=3,
                net_name="NET3",
            )
        )
        router.routes.append(good)
        net_routes = {3: [good]}

        demoted = router._demote_pad_clearance_violation_nets(net_routes)

        assert demoted == []
        assert net_routes[3] == [good]
        assert good in router.routes

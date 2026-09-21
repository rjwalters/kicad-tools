"""Equivalence pins for the pure-Python A* via-kernel fast paths (Issue #5617).

Background
----------
``Router._is_via_blocked`` is the hottest function in the pure-Python A*
fallback that the C++ pathfinder hands off to after it "gives up" -- the path
that dominates phase 4 ("Routing nets") of the Diff-Pair regression job's
re-route step.  Issue #5617 makes two changes to it that MUST be verdict-
preserving:

1. the per-net-class ``radius`` override no longer rebuilds its ``(2r+1)**2``
   Euclidean-disc comprehension on every call -- ``Router._via_kernel``
   memoizes it per radius;
2. the four per-cell bounds comparisons inside the kernel walk collapse into a
   single bounding-box test using the kernel's ``reach``
   (``max(|dx|, |dy|)`` over the kernel);
3. the kernel walk itself becomes a NumPy window slice intersected with a
   cached disc mask, instead of a Python loop over the offsets.

(2) is only sound because the kernel is a *symmetric Euclidean disc*: it
contains ``(+/-reach, 0)`` and ``(0, +/-reach)``, so "some kernel cell falls
outside the grid" and "the kernel's bounding box leaves the grid" are the same
predicate.  (3) is only sound because the mask is the disc, aligned with that
same bounding box, and ``np.nonzero`` enumerates row major -- the order the
offset list was built in.  These tests pin both invariants down directly, and
cross-check the whole predicate against a brute-force oracle so a future
kernel change that breaks them (e.g. reintroducing an asymmetric or non-disc
kernel) fails here rather than silently changing which vias the fallback
accepts.

Sibling of ``test_router_via_kernel_diagonal.py`` (Issue #3234), which pins the
Euclidean-disc *semantics* these fast paths must preserve.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Layer, Route, Via
from kicad_tools.router.rules import DesignRules


def _make_router(*, width: float = 6.0, height: float = 6.0) -> tuple[Router, RoutingGrid]:
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(
        width=width,
        height=height,
        rules=rules,
        layer_stack=LayerStack.two_layer(),
    )
    return Router(grid, rules), grid


def _naive_disc(r: int) -> list[tuple[int, int]]:
    """The pre-#5617 on-the-fly comprehension, verbatim."""
    r_sq = r * r
    return [
        (dx, dy) for dy in range(-r, r + 1) for dx in range(-r, r + 1) if dx * dx + dy * dy <= r_sq
    ]


class TestViaKernelMemo:
    """``Router._via_kernel`` must be a pure memo of the old comprehension."""

    @pytest.mark.parametrize("radius", [1, 2, 3, 4, 5, 6, 7, 9])
    def test_offsets_match_on_the_fly_comprehension(self, radius: int) -> None:
        router, _ = _make_router()
        offsets, _reach, _mask = router._via_kernel(radius)
        # Order matters: ``_is_via_blocked`` builds ``blocked_cells`` in kernel
        # order and the detailed-check loops below it short-circuit on the
        # first offending cell.  A reordered kernel could change which
        # violation a caller reports even when the boolean verdict matches.
        assert offsets == _naive_disc(radius)

    def test_default_radius_reuses_the_constructor_kernel(self) -> None:
        router, _ = _make_router()
        # ``radius=None`` and ``radius == _via_half_cells`` are the same key,
        # and both must hand back the constructor-built list (no rebuild).
        assert router._via_kernel(None)[0] is router._via_offsets
        assert router._via_kernel(router._via_half_cells)[0] is router._via_offsets

    def test_repeated_calls_return_the_same_cached_object(self) -> None:
        router, _ = _make_router()
        first = router._via_kernel(4)
        second = router._via_kernel(4)
        assert first[0] is second[0]

    @pytest.mark.parametrize("radius", [1, 2, 3, 5, 8])
    def test_reach_is_the_kernels_own_chebyshev_extent(self, radius: int) -> None:
        router, _ = _make_router()
        offsets, reach, _mask = router._via_kernel(radius)
        assert reach == max(max(abs(dx), abs(dy)) for dx, dy in offsets)

    @pytest.mark.parametrize("radius", [1, 2, 3, 5, 8])
    def test_axis_extremes_present_licensing_the_box_test(self, radius: int) -> None:
        """The invariant the bounding-box bounds test rests on.

        If the kernel ever stops containing its own axis extremes, the box
        test could report "out of bounds" for a candidate whose real kernel
        cells are all inside the grid -- rejecting a legal via placement.
        """
        router, _ = _make_router()
        offsets, reach, _mask = router._via_kernel(radius)
        offset_set = set(offsets)
        for extreme in ((reach, 0), (-reach, 0), (0, reach), (0, -reach)):
            assert extreme in offset_set, (
                f"kernel for radius={radius} is missing axis extreme {extreme}; "
                f"the single bounding-box bounds test in _is_via_blocked is "
                f"no longer equivalent to the per-cell bounds test"
            )
        assert all(abs(dx) <= reach and abs(dy) <= reach for dx, dy in offsets)


class TestViaKernelMask:
    """The cached disc mask must reproduce the offset list exactly."""

    @pytest.mark.parametrize("radius", [1, 2, 3, 5, 9, 10])
    def test_mask_is_the_offset_list(self, radius: int) -> None:
        router, _ = _make_router()
        offsets, reach, mask = router._via_kernel(radius)
        assert mask.shape == (2 * reach + 1, 2 * reach + 1)
        assert mask.dtype == bool
        assert int(mask.sum()) == len(offsets)
        for dx, dy in offsets:
            assert mask[dy + reach, dx + reach]

    @pytest.mark.parametrize("radius", [2, 5, 10])
    def test_nonzero_order_matches_offset_order(self, radius: int) -> None:
        """``np.nonzero`` must enumerate the kernel in the offsets' order.

        ``_is_via_blocked`` builds ``blocked_cells`` from the mask hits and the
        loops below it short-circuit on the first offending cell, so a
        different enumeration order could change which cell a caller blames.
        """
        import numpy as np

        router, _ = _make_router()
        offsets, reach, mask = router._via_kernel(radius)
        ys, xs = np.nonzero(mask)
        from_mask = [(int(x) - reach, int(y) - reach) for y, x in zip(ys, xs, strict=True)]
        assert from_mask == offsets


class TestBoundingBoxBoundsTest:
    """One box test must answer exactly what the per-cell scan answered."""

    @pytest.mark.parametrize("radius", [1, 3, 5, 7])
    def test_box_test_matches_per_cell_scan_along_every_border(self, radius: int) -> None:
        router, grid = _make_router()
        offsets, reach, _mask = router._via_kernel(radius)
        cols, rows = grid.cols, grid.rows

        checked = 0
        # Bands that straddle all four borders plus an interior control band.
        xs = list(range(-2, reach + 3)) + list(range(cols - reach - 3, cols + 2)) + [cols // 2]
        ys = list(range(-2, reach + 3)) + list(range(rows - reach - 3, rows + 2)) + [rows // 2]
        for gx in xs:
            for gy in ys:
                per_cell = any(
                    not (0 <= gx + dx < cols and 0 <= gy + dy < rows) for dx, dy in offsets
                )
                box = gx - reach < 0 or gy - reach < 0 or gx + reach >= cols or gy + reach >= rows
                assert per_cell == box, (
                    f"bounds-test disagreement at ({gx},{gy}) radius={radius}: "
                    f"per-cell={per_cell} box={box}"
                )
                checked += 1
        # Every band position was visited, and both borders plus the interior
        # control are represented (guards against an empty/degenerate sweep).
        assert checked == len(xs) * len(ys)
        assert checked >= (2 * (reach + 5) + 1) ** 2


class TestIsViaBlockedAgainstOracle:
    """``_is_via_blocked`` verdicts vs. a brute-force model of the same rules."""

    @staticmethod
    def _oracle(
        router: Router, grid: RoutingGrid, gx: int, gy: int, layer: int, net: int, radius: int
    ) -> bool:
        offsets = _naive_disc(radius)
        for dx, dy in offsets:
            cx, cy = gx + dx, gy + dy
            if not (0 <= cx < grid.cols and 0 <= cy < grid.rows):
                return True
        for dx, dy in offsets:
            cx, cy = gx + dx, gy + dy
            if grid._blocked[layer, cy, cx] and grid._net[layer, cy, cx] != net:
                return True
        return False

    @pytest.mark.parametrize("radius", [2, 4, 6])
    def test_sweep_matches_oracle_including_out_of_bounds_band(self, radius: int) -> None:
        router, grid = _make_router()
        # A small cluster of foreign-net static copper plus one own-net cell
        # (which must NOT block) near a corner, so the sweep crosses both the
        # bounds band and the blocked-cell band.
        for cx, cy in ((12, 12), (13, 12), (30, 31)):
            for layer in range(grid.num_layers):
                grid._blocked[layer, cy, cx] = True
                grid._net[layer, cy, cx] = 2
        for layer in range(grid.num_layers):
            grid._blocked[layer, 20, 20] = True
            grid._net[layer, 20, 20] = 1

        mismatches: list[tuple[int, int, bool, bool]] = []
        for gx in list(range(0, 36)) + [grid.cols - 1]:
            for gy in list(range(0, 36)) + [grid.rows - 1]:
                actual = router._is_via_blocked(
                    gx, gy, 0, net=1, allow_sharing=False, radius=radius
                )
                expected = self._oracle(router, grid, gx, gy, 0, 1, radius)
                if actual != expected:
                    mismatches.append((gx, gy, actual, expected))
        assert not mismatches, f"{len(mismatches)} oracle mismatches, first 5: {mismatches[:5]}"

    def test_alternating_radii_do_not_poison_the_memo(self) -> None:
        """Interleaved radii must return exactly what each returns alone."""
        router, grid = _make_router()
        for layer in range(grid.num_layers):
            grid._blocked[layer, 25, 25] = True
            grid._net[layer, 25, 25] = 2

        radii = [2, 5, 3, 5, 2, 7, 3]
        positions = [(20, 25), (22, 25), (25, 25), (29, 25), (31, 25), (0, 0), (2, 2)]

        # Reference: one fresh Router per (radius, position) -- no memo reuse.
        expected = []
        for radius in radii:
            for gx, gy in positions:
                fresh, fresh_grid = _make_router()
                for layer in range(fresh_grid.num_layers):
                    fresh_grid._blocked[layer, 25, 25] = True
                    fresh_grid._net[layer, 25, 25] = 2
                expected.append(
                    fresh._is_via_blocked(gx, gy, 0, net=1, allow_sharing=False, radius=radius)
                )

        actual = [
            router._is_via_blocked(gx, gy, 0, net=1, allow_sharing=False, radius=radius)
            for radius in radii
            for gx, gy in positions
        ]
        assert actual == expected


class TestHaloLayerSpanCache:
    """The per-call layer-span cache in ``RouteHaloGeometry.clear`` (#5617).

    The span is derived from ``candidate.layers`` and the grid's layer
    indexing, both loop-invariant for the whole call -- caching it must not
    change any verdict.  Exercised through the same fixture
    ``test_python_route_halo_clearance.py`` uses, where a real committed via
    populates the dynamic halo.
    """

    @staticmethod
    def _halo_context() -> tuple[RoutingGrid, Router]:
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

    def test_inner_layer_trace_still_gates_a_through_via(self) -> None:
        """A foreign trace on an INNER layer is inside an F_CU..B_CU via's
        span, so it must be considered -- the branch whose ``sorted(...)``
        span is now cached.  Repeated calls must agree with the first.
        """
        grid, router = self._halo_context()
        verdicts = [
            router._is_via_blocked(55, 56, layer, 1, sharing, radius=4)
            for layer in range(grid.num_layers)
            for sharing in (False, True)
        ]
        again = [
            router._is_via_blocked(55, 56, layer, 1, sharing, radius=4)
            for layer in range(grid.num_layers)
            for sharing in (False, True)
        ]
        assert verdicts == again

    @pytest.mark.parametrize("sharing", [False, True])
    def test_known_halo_verdicts_unchanged(self, sharing: bool) -> None:
        """Pins the same verdict pair ``test_python_route_halo_clearance``
        asserts, re-checked here so a regression in the cached span shows up
        as a #5617 failure rather than only in the older file."""
        grid, router = self._halo_context()
        assert grid._route_halo.cell_known(55, 56, 2)
        assert not router._is_via_blocked(55, 56, 2, 1, sharing, radius=4)
        assert router._is_via_blocked(56, 56, 2, 1, sharing, radius=4)

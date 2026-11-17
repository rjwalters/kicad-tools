"""HARD bundle-plan lane reservation on the C++ grid (Issue #4256, A3).

Acceptance criterion 2: a bundle-plan lane must be a HARD keep-out that
foreign nets are PROVABLY excluded from on the production C++ grid — not
merely biased (as #4053's SOFT attractor was, which the greedy A* "paid
through").  The proof uses the same instrument the #4079 parity gate uses:
``CppPathfinder.is_trace_blocked`` returns ``True`` for a foreign net on a
HARD-reserved cell and ``False`` for the owning net, and the two backends
agree.  A SOFT reservation of the same cell does NOT block the foreign net —
that contrast is what makes the HARD flip load-bearing.

These tests require the C++ backend (``kct build-native``); they skip when
it is unavailable so Python-only environments stay green.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not built (run: kct build-native)",
)


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


def _grid_4layer() -> RoutingGrid:
    stack = LayerStack.four_layer_all_signal()
    return RoutingGrid(50, 50, _rules(), origin_x=0, origin_y=0, layer_stack=stack)


def _inner_layer_idx(grid: RoutingGrid) -> int:
    for idx in range(grid.num_layers):
        layer_enum = grid.index_to_layer(idx)
        if layer_enum not in (Layer.F_CU.value, Layer.B_CU.value):
            return idx
    raise AssertionError("4-layer all-signal stack should expose an inner layer")


class TestHardReservationExcludesForeignNetOnCpp:
    """A HARD, C++-mirrored bundle-plan lane fences a foreign net out."""

    def test_foreign_net_provably_excluded(self) -> None:
        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
        from kicad_tools.router.pathfinder import Router

        grid = _grid_4layer()
        rules = grid.rules
        inner = _inner_layer_idx(grid)
        gx, gy = 25, 25
        owner = 21  # TMDS_D0_P-style member net id

        # HARD (soft=False) + C++-mirrored (mirror_to_cpp=True): exactly the
        # args the A3 bundle-plan lanes use.
        n = grid.reserve_corridor_cells(
            inner, [(gx, gy)], frozenset({owner}), soft=False, mirror_to_cpp=True
        )
        assert n == 1
        # AC2 part 1: reservation actually lands.
        assert grid.reserved_cell_count() > 0

        cpp_grid = CppGrid.from_routing_grid(grid)
        assert cpp_grid._impl.reserved_cell_count() > 0, (
            "HARD bundle-plan reservation must marshal onto the C++ grid."
        )

        cpp_pf = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        cpp_pf.set_routable_layers(cpp_grid.get_routable_indices())
        py_router = Router(grid, rules, diagonal_routing=True)

        # AC2 part 2: a foreign net is PROVABLY excluded (blocked), the owner
        # passes, and the two backends agree.
        for probe, expect_blocked in ((owner, False), (9999, True)):
            cpp_blocked = cpp_pf._impl.is_trace_blocked(gx, gy, inner, probe, False)
            py_blocked = py_router._is_trace_blocked(gx, gy, inner, probe, False)
            assert cpp_blocked == py_blocked, (
                f"net {probe}: backends diverge (cpp={cpp_blocked}, py={py_blocked})"
            )
            assert cpp_blocked is expect_blocked, (
                f"net {probe}: expected C++ blocked={expect_blocked}, got {cpp_blocked}"
            )

    def test_foreign_exclusion_is_hard_even_in_relief_mode(self) -> None:
        """The HARD keep-out is not soft-negotiable (allow_sharing=True)."""
        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder

        grid = _grid_4layer()
        rules = grid.rules
        inner = _inner_layer_idx(grid)
        gx, gy = 25, 25

        grid.reserve_corridor_cells(
            inner, [(gx, gy)], frozenset({21}), soft=False, mirror_to_cpp=True
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        cpp_pf = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        cpp_pf.set_routable_layers(cpp_grid.get_routable_indices())

        # allow_sharing=True (relief/negotiated mode) must still block.
        assert cpp_pf._impl.is_trace_blocked(gx, gy, inner, 9999, True) is True


class TestSoftReservationDoesNotExclude:
    """Contrast: a SOFT reservation does NOT fence a foreign net out.

    This is the #4053 failure mode the A3 HARD flip fixes — a SOFT lane is a
    mere attractor bias the greedy A* pays through, so foreign traffic is not
    excluded.  Proving the SOFT case does NOT block confirms the HARD flip is
    load-bearing (not incidentally passing).
    """

    def test_soft_reservation_leaves_foreign_passable(self) -> None:
        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder

        grid = _grid_4layer()
        rules = grid.rules
        inner = _inner_layer_idx(grid)
        gx, gy = 25, 25

        # SOFT + mirrored: reserved, but attractor-only.
        grid.reserve_corridor_cells(
            inner, [(gx, gy)], frozenset({21}), soft=True, mirror_to_cpp=True
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        cpp_pf = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        cpp_pf.set_routable_layers(cpp_grid.get_routable_indices())

        # Foreign net is NOT blocked by a SOFT reservation.
        assert cpp_pf._impl.is_trace_blocked(gx, gy, inner, 9999, False) is False

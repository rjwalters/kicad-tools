"""Regression tests for the pad-exit clearance guard (Issue #3226).

Background
----------
The A* pad-exit relaxation (``pathfinder.cpp:680`` / ``:1175`` and the
Python sibling at ``pathfinder.py:2632``) lets a same-net trace step into
a FOREIGN pad's clearance halo cell while exiting its source pad's
metal.  Without further constraint, this admits a step that places the
trace centerline at the *inner* edge of the foreign pad's halo -- so
close to the foreign pad metal that the resulting trace edge violates
the per-net required clearance.

On board 05 (BLDC controller) at ``--backend cpp``, this surfaced as 9
residual ``clearance_pad_segment`` violations (8 sub-127um positive +
1 severe -0.265mm at U10-17 / PWM_AH) plus 1 ``clearance_pad_via`` at
the same U10-17 location.  PR #3225 closed the foreign-pad-metal
traversal class but left the foreign-pad-halo class open.

The fix (this PR) tightens the relaxation: a pad-exit step into a
foreign clearance-halo cell is still allowed, but only when the
candidate cell does not place any foreign-pad-metal cell within the
per-net trace radius.  This preserves the legitimate pad-exit step
into the OUTER edge of a neighbour halo while rejecting the unsafe
inner-edge steps.

Test strategy
-------------
The C++ and Python A* loops both expose the same surface:

1.  ``Pathfinder::is_foreign_pad_metal_within_radius`` (C++) and
    ``Pathfinder._is_foreign_pad_metal_within_radius`` (Python) take a
    candidate cell + per-net radius and return True iff any cell within
    Chebyshev radius has ``pad_blocked = true && cell.net != net``.

2.  The hot-path A* call site invokes the helper inside the pad-exit
    relaxation branch.  When the helper returns True the candidate is
    rejected with ``reason=pad_exit_clearance_too_tight``.

This file exercises the helper directly so the regression is anchored
to a deterministic fixture rather than to board-05's full route which
depends on the negotiated rip-up loop and a long list of unrelated
heuristics.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from kicad_tools.router.cpp_backend import is_cpp_available, router_cpp
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router as Pathfinder
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available; run `kct build-native`",
)


def _make_grid(
    *,
    width: float = 10.0,
    height: float = 10.0,
    resolution: float = 0.1,
) -> tuple[RoutingGrid, DesignRules]:
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=resolution,
    )
    layer_stack = LayerStack.two_layer()
    grid = RoutingGrid(
        width=width,
        height=height,
        rules=rules,
        layer_stack=layer_stack,
    )
    return grid, rules


# ---------------------------------------------------------------------------
# Python helper: direct unit tests
# ---------------------------------------------------------------------------


class TestPythonForeignPadMetalGuard:
    """``Pathfinder._is_foreign_pad_metal_within_radius`` semantics."""

    def test_no_foreign_pad_returns_false(self) -> None:
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        # Empty grid -- no foreign pad metal anywhere.
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=3) is False

    def test_zero_radius_returns_false(self) -> None:
        """Radius 0 is a no-op (the relaxation always passes a positive radius)."""
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        # Mark a cell as foreign pad metal directly.
        py_grid._pad_blocked[0, 50, 50] = True
        py_grid._net[0, 50, 50] = 2
        py_grid._blocked[0, 50, 50] = True
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=0) is False

    def test_foreign_pad_metal_at_center_rejects(self) -> None:
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        py_grid._pad_blocked[0, 50, 50] = True
        py_grid._net[0, 50, 50] = 2  # foreign
        py_grid._blocked[0, 50, 50] = True
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=3) is True

    def test_foreign_pad_metal_just_outside_radius_passes(self) -> None:
        """Cells at Chebyshev distance > radius are ignored."""
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        # Mark foreign pad at (50, 60) -- Chebyshev distance 10 from candidate (50, 50).
        py_grid._pad_blocked[0, 60, 50] = True
        py_grid._net[0, 60, 50] = 2
        py_grid._blocked[0, 60, 50] = True
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=3) is False

    def test_foreign_pad_metal_at_chebyshev_radius_rejects(self) -> None:
        """Cell at Chebyshev distance == radius is treated as in-range (inclusive)."""
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        # Foreign pad at exactly radius=3 cells away.
        py_grid._pad_blocked[0, 50, 53] = True
        py_grid._net[0, 50, 53] = 2
        py_grid._blocked[0, 50, 53] = True
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=3) is True

    def test_own_net_pad_metal_does_not_reject(self) -> None:
        """Only FOREIGN-net pad metal triggers the guard."""
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        py_grid._pad_blocked[0, 50, 50] = True
        py_grid._net[0, 50, 50] = 1  # same net as routing net
        py_grid._blocked[0, 50, 50] = True
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=3) is False

    def test_clearance_halo_does_not_reject(self) -> None:
        """Pure clearance halo (``pad_blocked == False``) is the cell the
        relaxation is supposed to admit -- the guard must skip it."""
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        # ``blocked=True`` but ``pad_blocked=False`` (halo, not pad copper).
        py_grid._blocked[0, 50, 50] = True
        py_grid._net[0, 50, 50] = 2  # foreign halo
        py_grid._pad_blocked[0, 50, 50] = False
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=3) is False

    def test_out_of_bounds_clamping(self) -> None:
        """Candidate cell at the grid edge must not raise on the clamp."""
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        # No foreign metal anywhere, candidate at corner cell (0, 0).
        assert pf._is_foreign_pad_metal_within_radius(0, 0, 0, net=1, radius=3) is False
        assert (
            pf._is_foreign_pad_metal_within_radius(
                py_grid.cols - 1, py_grid.rows - 1, 0, net=1, radius=3
            )
            is False
        )

    def test_integration_pad_geometry(self) -> None:
        """End-to-end: a real pad added via ``grid.add_pad`` populates
        ``_pad_blocked``/``_net``/``_blocked`` consistently, and the
        helper detects it within radius."""
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        foreign_pad = Pad(
            x=5.0,
            y=5.0,
            width=0.3,
            height=1.5,
            net=2,
            net_name="FOREIGN",
            layer=Layer.F_CU,
            ref="U1",
            pin="17",
        )
        py_grid.add_pad(foreign_pad)
        # Pad center grid cell.
        gx, gy = py_grid.world_to_grid(foreign_pad.x, foreign_pad.y)
        # Candidate at the pad center: foreign pad metal at distance 0.
        assert pf._is_foreign_pad_metal_within_radius(gx, gy, 0, net=1, radius=3) is True
        # Candidate far enough that no metal cell falls within radius=3.
        # Pad is 0.3 wide x 1.5 tall => metal extends ~7 cells in y.
        # 30 cells away on x is well outside any halo.
        assert (
            pf._is_foreign_pad_metal_within_radius(gx + 30, gy, 0, net=1, radius=3) is False
        )


# ---------------------------------------------------------------------------
# C++ helper: direct binding tests
# ---------------------------------------------------------------------------


@requires_cpp
class TestCppForeignPadMetalGuard:
    """``Pathfinder::is_foreign_pad_metal_within_radius`` semantics."""

    def _make_cpp_grid(
        self, cols: int = 100, rows: int = 100, layers: int = 2, resolution: float = 0.1
    ) -> tuple[router_cpp.Grid3D, router_cpp.Pathfinder]:
        grid = router_cpp.Grid3D(cols, rows, layers, resolution, 0.0, 0.0)
        rules = router_cpp.DesignRules()
        rules.trace_width = 0.2
        rules.trace_clearance = 0.15
        rules.via_diameter = 0.6
        rules.via_drill = 0.3
        rules.via_clearance = 0.15
        rules.grid_resolution = resolution
        rules.cost_straight = 1.0
        rules.cost_turn = 1.5
        rules.cost_via = 10.0
        pf = router_cpp.Pathfinder(grid, rules, True)
        return grid, pf

    def test_no_foreign_pad_returns_false(self) -> None:
        grid, pf = self._make_cpp_grid()
        assert pf.is_foreign_pad_metal_within_radius(50, 50, 0, 1, 3) is False

    def test_zero_radius_returns_false(self) -> None:
        grid, pf = self._make_cpp_grid()
        # Mark a cell as foreign pad metal via ``mark_blocked`` with
        # ``pad_blocked=True``.
        grid.mark_blocked(50, 50, 0, 2, False, True)
        assert pf.is_foreign_pad_metal_within_radius(50, 50, 0, 1, 0) is False

    def test_foreign_pad_metal_at_center_rejects(self) -> None:
        grid, pf = self._make_cpp_grid()
        grid.mark_blocked(50, 50, 0, 2, False, True)
        assert pf.is_foreign_pad_metal_within_radius(50, 50, 0, 1, 3) is True

    def test_own_net_pad_metal_does_not_reject(self) -> None:
        grid, pf = self._make_cpp_grid()
        grid.mark_blocked(50, 50, 0, 1, False, True)  # same net = 1
        assert pf.is_foreign_pad_metal_within_radius(50, 50, 0, 1, 3) is False

    def test_clearance_halo_does_not_reject(self) -> None:
        grid, pf = self._make_cpp_grid()
        # ``pad_blocked=False`` (pure halo cell, foreign net).
        grid.mark_blocked(50, 50, 0, 2, False, False)
        assert pf.is_foreign_pad_metal_within_radius(50, 50, 0, 1, 3) is False

    def test_foreign_pad_at_chebyshev_radius_rejects(self) -> None:
        grid, pf = self._make_cpp_grid()
        grid.mark_blocked(53, 50, 0, 2, False, True)
        assert pf.is_foreign_pad_metal_within_radius(50, 50, 0, 1, 3) is True

    def test_foreign_pad_just_outside_radius_passes(self) -> None:
        grid, pf = self._make_cpp_grid()
        # 4 cells away (Chebyshev > 3) -- guard must not fire.
        grid.mark_blocked(54, 50, 0, 2, False, True)
        assert pf.is_foreign_pad_metal_within_radius(50, 50, 0, 1, 3) is False

    def test_out_of_bounds_does_not_crash(self) -> None:
        grid, pf = self._make_cpp_grid()
        # Candidate at the corner -- the helper must clamp without UB.
        assert pf.is_foreign_pad_metal_within_radius(0, 0, 0, 1, 3) is False


# ---------------------------------------------------------------------------
# Symmetry: Python and C++ guards return identical answers
# ---------------------------------------------------------------------------


@requires_cpp
class TestPythonCppGuardSymmetry:
    """The Python and C++ A* loops must agree on the guard verdict so the
    cpp -> python fallback and any cross-backend regression-bisect runs see
    the same accept/reject decisions on identical grids."""

    def test_dense_lqfp_like_geometry(self) -> None:
        """Build a tiny LQFP-like row of three pads (own, foreign, foreign)
        at 0.8 mm pitch and confirm both backends agree on the guard
        verdict at every cell in a 5-cell horizontal window around the
        OWN pad's exit cell.

        Geometry: own pad at x=2.0, foreign pads at x=2.8 and x=3.6.
        All on layer 0, sized 0.3 x 1.5 (LQFP-32 perimeter pad).
        """
        py_grid, rules = _make_grid(width=10.0, height=10.0)
        pf = Pathfinder(py_grid, rules)

        # Add the three pads via the public API so both halo and metal
        # are populated correctly on the Python side.
        for x_offset, net_id in [(2.0, 1), (2.8, 2), (3.6, 3)]:
            pad = Pad(
                x=x_offset,
                y=5.0,
                width=0.3,
                height=1.5,
                net=net_id,
                net_name=f"NET{net_id}",
                layer=Layer.F_CU,
                ref="U1",
                pin=str(net_id),
            )
            py_grid.add_pad(pad)

        # Build C++ grid by bulk sync (mirrors the production
        # ``CppGrid.from_routing_grid`` path) -- because we want the
        # guards to be compared on identical inputs.
        from kicad_tools.router.cpp_backend import CppGrid

        cpp_grid = CppGrid.from_routing_grid(py_grid)
        cpp_rules = router_cpp.DesignRules()
        cpp_rules.trace_width = 0.2
        cpp_rules.trace_clearance = 0.15
        cpp_rules.via_diameter = 0.6
        cpp_rules.via_drill = 0.3
        cpp_rules.via_clearance = 0.15
        cpp_rules.grid_resolution = py_grid.resolution
        cpp_rules.cost_straight = 1.0
        cpp_rules.cost_turn = 1.5
        cpp_rules.cost_via = 10.0
        cpp_pf = router_cpp.Pathfinder(cpp_grid._impl, cpp_rules, True)

        # Sweep candidate cells in a 5x1 window covering the OWN pad and
        # both foreign neighbours.  At each cell, ask both backends
        # whether routing net=1 would be safe at that centerline.
        radius = math.ceil((0.2 / 2 + 0.15) / py_grid.resolution)
        own_gx, own_gy = py_grid.world_to_grid(2.0, 5.0)
        agreements = 0
        for dx in range(-2, 30):  # span past both foreign pads
            gx = own_gx + dx
            if gx < 0 or gx >= py_grid.cols:
                continue
            py_verdict = pf._is_foreign_pad_metal_within_radius(
                gx, own_gy, 0, net=1, radius=radius
            )
            cpp_verdict = cpp_pf.is_foreign_pad_metal_within_radius(
                gx, own_gy, 0, 1, radius
            )
            assert py_verdict == cpp_verdict, (
                f"Python/C++ guard disagreement at (gx={gx}, gy={own_gy}): "
                f"py={py_verdict} cpp={cpp_verdict}"
            )
            agreements += 1
        assert agreements > 5, "Expected the sweep to cover at least a few cells"

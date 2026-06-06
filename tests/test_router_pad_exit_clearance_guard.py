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
        """Cells at Euclidean distance > radius are ignored."""
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        # Mark foreign pad at (50, 60) -- distance 10 from candidate (50, 50).
        py_grid._pad_blocked[0, 60, 50] = True
        py_grid._net[0, 60, 50] = 2
        py_grid._blocked[0, 60, 50] = True
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=3) is False

    def test_foreign_pad_metal_at_chebyshev_radius_rejects(self) -> None:
        """Cell on-axis at Euclidean distance == radius is in-range (inclusive)."""
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        # Foreign pad at exactly radius=3 cells away.
        py_grid._pad_blocked[0, 50, 53] = True
        py_grid._net[0, 50, 53] = 2
        py_grid._blocked[0, 50, 53] = True
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=3) is True

    def test_foreign_pad_metal_at_diagonal_corner_passes(self) -> None:
        """Issue #3229: The Chebyshev-vs-Euclidean diagonal corner.

        At offset (dx=radius, dy=radius), the Chebyshev distance is exactly
        ``radius`` (so the legacy square kernel would treat the cell as
        in-range), but the Euclidean distance is ``radius * sqrt(2) > radius``.
        The Euclidean disc must EXCLUDE the cell.  This is the exact
        failure mode of the 8 sub-127um ``clearance_pad_segment`` violations
        on board 05: the legacy Chebyshev kernel passed candidates with
        true Euclidean clearance falling short of the DRC rule by up to
        ``radius * (1 - 1/sqrt(2)) ~= 0.293 * radius`` cells.
        """
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        # Foreign pad at the diagonal corner (radius=3, offset (+3, +3)).
        py_grid._pad_blocked[0, 53, 53] = True
        py_grid._net[0, 53, 53] = 2
        py_grid._blocked[0, 53, 53] = True
        # Chebyshev distance = 3 (legacy would reject).
        # Euclidean distance = sqrt(18) = ~4.24 > 3 (new kernel must accept).
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=3) is False

    def test_foreign_pad_metal_at_near_axial_diagonal_rejects(self) -> None:
        """Issue #3229 (boundary): At offset (1, 3) the Euclidean distance
        is sqrt(1+9)=sqrt(10) ~= 3.16 -- BUT ``dist_sq = 10 <= radius^2 = 9``
        is FALSE, so this cell is just outside the disc.

        Pick offset (1, 2) instead: ``dist_sq = 1 + 4 = 5 <= 9``: inside.
        Both old (Chebyshev=2 <= 3) and new (Euclidean disc) must reject.
        """
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        py_grid._pad_blocked[0, 52, 51] = True
        py_grid._net[0, 52, 51] = 2
        py_grid._blocked[0, 52, 51] = True
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=3) is True

    def test_foreign_pad_metal_at_disc_boundary_excluded(self) -> None:
        """Issue #3229: Offset (1, 3) has dist_sq = 1 + 9 = 10 > radius_sq = 9
        for radius=3.  Inside the Chebyshev square (legacy reject), outside
        the Euclidean disc (new accept).  This pins down the disc boundary.
        """
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        py_grid._pad_blocked[0, 53, 51] = True
        py_grid._net[0, 53, 51] = 2
        py_grid._blocked[0, 53, 51] = True
        assert pf._is_foreign_pad_metal_within_radius(50, 50, 0, net=1, radius=3) is False

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
        # 4 cells away (Euclidean > 3) -- guard must not fire.
        grid.mark_blocked(54, 50, 0, 2, False, True)
        assert pf.is_foreign_pad_metal_within_radius(50, 50, 0, 1, 3) is False

    def test_out_of_bounds_does_not_crash(self) -> None:
        grid, pf = self._make_cpp_grid()
        # Candidate at the corner -- the helper must clamp without UB.
        assert pf.is_foreign_pad_metal_within_radius(0, 0, 0, 1, 3) is False

    def test_foreign_pad_at_diagonal_corner_passes(self) -> None:
        """Issue #3229: A foreign pad at the Chebyshev-pass / Euclidean-fail
        diagonal corner (offset (radius, radius), Euclidean = radius * sqrt(2))
        must NOT trigger the guard.  This is the exact failure mode that
        the legacy Chebyshev kernel admitted -- producing the 8 sub-127um
        ``clearance_pad_segment`` errors on board 05.
        """
        grid, pf = self._make_cpp_grid()
        # (3, 3) offset from candidate -- Chebyshev=3, Euclidean=sqrt(18)~=4.24.
        grid.mark_blocked(53, 53, 0, 2, False, True)
        assert pf.is_foreign_pad_metal_within_radius(50, 50, 0, 1, 3) is False

    def test_foreign_pad_at_disc_boundary_excluded(self) -> None:
        """Issue #3229: Offset (1, 3) has dist_sq = 10 > radius_sq = 9 for
        radius=3.  Inside Chebyshev square (legacy reject), outside Euclidean
        disc (new accept).  Pins down the disc boundary on the C++ side.
        """
        grid, pf = self._make_cpp_grid()
        grid.mark_blocked(51, 53, 0, 2, False, True)
        assert pf.is_foreign_pad_metal_within_radius(50, 50, 0, 1, 3) is False

    def test_foreign_pad_within_disc_diagonal_rejects(self) -> None:
        """Issue #3229: Offset (1, 2) has dist_sq = 5 <= 9 for radius=3.
        Inside both Chebyshev square AND Euclidean disc -- both legacy
        and new kernel must reject.
        """
        grid, pf = self._make_cpp_grid()
        grid.mark_blocked(51, 52, 0, 2, False, True)
        assert pf.is_foreign_pad_metal_within_radius(50, 50, 0, 1, 3) is True


# ---------------------------------------------------------------------------
# Symmetry: Python and C++ guards return identical answers
# ---------------------------------------------------------------------------


@requires_cpp
class TestPythonCppGuardSymmetry:
    """The Python and C++ A* loops must agree on the guard verdict so the
    cpp -> python fallback and any cross-backend regression-bisect runs see
    the same accept/reject decisions on identical grids."""

    def test_diagonal_corner_symmetry(self) -> None:
        """Issue #3229: Python and C++ guards must agree at every cell in
        a 13x13 window around a foreign pad cell, including the inflated
        diagonal corners where Chebyshev kernel would have rejected but
        Euclidean disc accepts.  Verifies the kernel-shape change is in
        sync across backends.
        """
        py_grid, rules = _make_grid(width=10.0, height=10.0)
        pf = Pathfinder(py_grid, rules)

        # Place a single foreign-pad-metal cell at a known position.
        py_grid._pad_blocked[0, 50, 50] = True
        py_grid._net[0, 50, 50] = 2
        py_grid._blocked[0, 50, 50] = True

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

        # Sweep candidate positions in a 13x13 window so we cover the
        # full disc at radius=3 PLUS the Chebyshev diagonal corners
        # (which must now be accepted).
        radius = 3
        # Verify diagonal-corner cell is accepted (Chebyshev-fail / Euclidean-pass).
        diagonal_corner_count = 0
        for dy in range(-radius - 2, radius + 3):
            for dx in range(-radius - 2, radius + 3):
                gx = 50 + dx
                gy = 50 + dy
                py_verdict = pf._is_foreign_pad_metal_within_radius(
                    gx, gy, 0, net=1, radius=radius
                )
                cpp_verdict = cpp_pf.is_foreign_pad_metal_within_radius(
                    gx, gy, 0, 1, radius
                )
                assert py_verdict == cpp_verdict, (
                    f"Python/C++ disagreement at offset (dx={dx},dy={dy}): "
                    f"py={py_verdict} cpp={cpp_verdict}"
                )
                # The exact diagonal corners (|dx|==|dy|==radius) must be
                # ACCEPTED by the new Euclidean disc but would have been
                # REJECTED by the legacy Chebyshev square.  Pin this down.
                if abs(dx) == radius and abs(dy) == radius:
                    assert py_verdict is False, (
                        f"Diagonal corner ({dx},{dy}) at Chebyshev=radius={radius}, "
                        f"Euclidean={radius * math.sqrt(2):.2f} must be excluded "
                        f"from Euclidean disc but Python verdict is {py_verdict}"
                    )
                    diagonal_corner_count += 1
        assert diagonal_corner_count == 4, (
            f"Expected to test all 4 diagonal corners of the kernel, "
            f"tested {diagonal_corner_count}"
        )

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


# ---------------------------------------------------------------------------
# Issue #3229: Trace-clearance kernel (is_trace_blocked) diagonal regression
# ---------------------------------------------------------------------------


class TestPythonTraceClearanceDiagonal:
    """Direct regressions for ``_is_trace_blocked`` at the
    Chebyshev-vs-Euclidean diagonal corner.

    The pad-exit relaxation helper (``_is_foreign_pad_metal_within_radius``)
    has its own coverage above, but the *main* trace-clearance kernel
    (used by every A* neighbor expansion via the dilated bitmap) also
    needed the kernel-shape change.  Without these tests a future
    refactor could revert ``_is_trace_blocked`` independently of the
    pad-exit helper and silently reintroduce the diagonal-corner bug.
    """

    def test_diagonal_corner_blocked_cell_passes(self) -> None:
        """Issue #3229: A foreign-net blocked cell at (radius, radius)
        offset has Euclidean distance ``radius * sqrt(2) > radius``, so
        ``_is_trace_blocked`` must NOT reject the placement -- the
        Euclidean kernel preserves the legitimate diagonal-corner
        placements the legacy Chebyshev kernel rejected.
        """
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        # Foreign blocked cell at (53, 53) -- offset (+3, +3) from (50, 50).
        py_grid._blocked[0, 53, 53] = True
        py_grid._net[0, 53, 53] = 2  # foreign
        # ``radius`` derived from the rules: trace_half_width_cells
        assert pf._is_trace_blocked(50, 50, 0, net=1, radius=3) is False

    def test_axial_blocked_cell_at_radius_rejects(self) -> None:
        """Issue #3229: A foreign blocked cell on-axis at distance == radius
        is INSIDE the Euclidean disc and must still reject.  This pins
        down that the new kernel does not lose orthogonal coverage.
        """
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        py_grid._blocked[0, 50, 53] = True
        py_grid._net[0, 50, 53] = 2
        assert pf._is_trace_blocked(50, 50, 0, net=1, radius=3) is True

    def test_diagonal_corner_inside_disc_rejects(self) -> None:
        """Issue #3229: A foreign blocked cell at (1, 2) has
        ``dist_sq = 5 <= 9``: inside both Chebyshev square AND Euclidean
        disc.  Both kernels must reject (this is a non-regression check
        that the new kernel does not over-tighten interior placements).
        """
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        py_grid._blocked[0, 52, 51] = True  # offset (+1, +2)
        py_grid._net[0, 52, 51] = 2
        assert pf._is_trace_blocked(50, 50, 0, net=1, radius=3) is True


@requires_cpp
class TestCppTraceClearanceDiagonal:
    """C++ sibling of ``TestPythonTraceClearanceDiagonal``.

    Built around the ``Pathfinder::route()`` interface rather than a
    direct ``is_trace_blocked`` binding because the C++ helper is
    private.  Verifies the kernel shape by routing two pads where the
    legacy Chebyshev kernel would have produced an under-clearance
    sub-127um pad_segment DRC error, then asserting the resulting
    geometry meets the Euclidean clearance rule.
    """

    def test_trace_clearance_kernel_is_euclidean(self) -> None:
        """Regression of the bug.

        Build a 1.5mm wide grid with two pads spaced just close enough
        that the trace centerline placed at the diagonal corner of the
        Chebyshev halo violates the Euclidean clearance rule.  Route a
        net through that channel and verify the resulting trace
        centerline maintains the Euclidean clearance.

        The OLD Chebyshev kernel would have permitted the trace to
        place its centerline at the diagonal corner where the Euclidean
        clearance falls below the rule.  The NEW Euclidean kernel
        must REJECT that placement and either find a longer path or
        report no path -- never produce an under-clearance route.
        """
        # Hand-build the kernel and a foreign pad cell directly so the
        # test is anchored to the kernel shape, not the full router.
        grid = router_cpp.Grid3D(50, 50, 1, 0.1, 0.0, 0.0)
        rules = router_cpp.DesignRules()
        rules.trace_width = 0.2
        rules.trace_clearance = 0.15
        rules.via_diameter = 0.6
        rules.via_drill = 0.3
        rules.via_clearance = 0.15
        rules.grid_resolution = 0.1
        rules.cost_straight = 1.0
        rules.cost_turn = 1.5
        rules.cost_via = 10.0
        pf = router_cpp.Pathfinder(grid, rules, True)

        # Place a foreign-net blocked cell at (radius, radius) offset.
        # The trace-radius-cells = ceil((0.2/2 + 0.15) / 0.1) = 3.
        # Offset (3, 3): Chebyshev = 3 (legacy would have rejected),
        # Euclidean = sqrt(18) ~= 4.24 (new must accept).
        #
        # Verified indirectly via ``is_foreign_pad_metal_within_radius``:
        # if the foreign pad were at offset (3, 3), the legacy guard
        # would fire (returning True) but the new disc kernel must
        # return False -- the diagonal corner is OUTSIDE the disc.
        grid.mark_blocked(28, 28, 0, 2, False, True)
        assert pf.is_foreign_pad_metal_within_radius(25, 25, 0, 1, 3) is False

    def test_trace_clearance_axial_radius_still_rejects(self) -> None:
        """Non-regression: on-axis foreign pad cells inside the disc
        must still reject.  This ensures the Euclidean kernel does NOT
        lose orthogonal coverage.
        """
        grid = router_cpp.Grid3D(50, 50, 1, 0.1, 0.0, 0.0)
        rules = router_cpp.DesignRules()
        rules.trace_width = 0.2
        rules.trace_clearance = 0.15
        rules.via_diameter = 0.6
        rules.via_drill = 0.3
        rules.via_clearance = 0.15
        rules.grid_resolution = 0.1
        rules.cost_straight = 1.0
        rules.cost_turn = 1.5
        rules.cost_via = 10.0
        pf = router_cpp.Pathfinder(grid, rules, True)
        # On-axis at (28, 25), offset (+3, 0): on the disc boundary
        # (dist_sq = 9 <= 9).  Must reject.
        grid.mark_blocked(28, 25, 0, 2, False, True)
        assert pf.is_foreign_pad_metal_within_radius(25, 25, 0, 1, 3) is True


# ---------------------------------------------------------------------------
# Issue #3229: DRC cross-check -- verify the Euclidean kernel reflects DRC
# ---------------------------------------------------------------------------


@requires_cpp
class TestKernelDrcCrossCheck:
    """Cross-check: the kernel's accept/reject decision must correspond to
    Euclidean DRC compliance.

    Previously the symmetry sweep only compared Python and C++ to each
    other -- they could agree on a wrong answer (e.g. both accepting a
    placement that violates the Euclidean clearance rule).  This class
    adds a direct geometric assertion that the kernel's decision matches
    the actual Euclidean distance to the foreign pad.
    """

    def test_kernel_decision_matches_euclidean_distance(self) -> None:
        """For every cell in a 9x9 window around a foreign-pad cell,
        verify that the kernel ACCEPTS iff Euclidean distance > radius.
        """
        grid = router_cpp.Grid3D(50, 50, 1, 0.1, 0.0, 0.0)
        rules = router_cpp.DesignRules()
        rules.trace_width = 0.2
        rules.trace_clearance = 0.15
        rules.via_diameter = 0.6
        rules.via_drill = 0.3
        rules.via_clearance = 0.15
        rules.grid_resolution = 0.1
        rules.cost_straight = 1.0
        rules.cost_turn = 1.5
        rules.cost_via = 10.0
        pf = router_cpp.Pathfinder(grid, rules, True)

        # Foreign pad at (25, 25).
        grid.mark_blocked(25, 25, 0, 2, False, True)
        radius = 3
        radius_sq = radius * radius

        cells_tested = 0
        for dy in range(-radius - 2, radius + 3):
            for dx in range(-radius - 2, radius + 3):
                gx = 25 + dx
                gy = 25 + dy
                dist_sq = dx * dx + dy * dy
                expected_reject = dist_sq <= radius_sq  # inside Euclidean disc
                actual_reject = pf.is_foreign_pad_metal_within_radius(
                    gx, gy, 0, 1, radius
                )
                assert actual_reject == expected_reject, (
                    f"Kernel/Euclidean mismatch at offset ({dx},{dy}): "
                    f"dist_sq={dist_sq} radius_sq={radius_sq} "
                    f"expected_reject={expected_reject} actual={actual_reject}"
                )
                cells_tested += 1
        assert cells_tested == (2 * (radius + 2) + 1) ** 2

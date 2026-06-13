"""Regression tests for the via-clearance kernel (Issue #3234).

Background
----------
PR #3232 (Issue #3229) switched the *trace* clearance kernel from a
Chebyshev (square) scan to a Euclidean (circular disc) scan in both the
C++ ``Pathfinder::is_trace_blocked`` / ``is_foreign_pad_metal_within_radius``
and the Python ``Router._is_trace_blocked`` /
``Router._is_foreign_pad_metal_within_radius``.  The same Chebyshev/
Euclidean mismatch remained on the *via* side in
``Pathfinder::is_via_blocked_diag`` (C++) and ``Router._is_via_blocked``
(Python): the legacy nested ``dx/dy`` loops over a ``[-r, r]^2`` square
admitted candidate via centers at the diagonal corners whose true
Euclidean clearance to the blocking cell fell up to
``via_half_cells * (1 - 1/sqrt(2)) ~= 0.293 * via_half_cells`` cells
short of the rule.  This is the exact failure mode of residual
``clearance_segment_via`` / ``clearance_pad_via`` violations on dense
layouts.

This file pins down the Euclidean-disc semantics that Issue #3234
introduces.  Mirrors the structure of ``test_router_pad_exit_clearance_guard.py``
(PR #3232's regression file for the trace side).
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available, router_cpp
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router as Pathfinder
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


def _make_cpp_pf_with_blocker(
    blocker_xy: tuple[int, int],
    layers: int = 2,
    blocker_net: int = 2,
) -> tuple[object, object]:
    """Build a standalone C++ Pathfinder with a single blocked cell.

    Returns ``(cpp_grid_impl, cpp_pathfinder)`` -- both unwrapped C++
    objects so the test can probe ``is_via_blocked`` directly without
    funneling through ``CppGrid``.
    """
    grid = router_cpp.Grid3D(101, 101, layers, 0.1, 0.0, 0.0)
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
    grid.mark_blocked(blocker_xy[0], blocker_xy[1], 0, blocker_net, False, False)
    return grid, pf


# ---------------------------------------------------------------------------
# Python kernel: direct unit tests on the precomputed offset list
# ---------------------------------------------------------------------------


class TestPythonViaKernelIsEuclidean:
    """The precomputed ``Router._via_offset_dx`` / ``_via_offset_dy``
    arrays must form a Euclidean disc, not a Chebyshev square."""

    def test_kernel_offsets_form_euclidean_disc(self) -> None:
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        r = pf._via_half_cells
        r_sq = r * r

        offsets = sorted(
            (int(dx), int(dy)) for dx, dy in zip(pf._via_offset_dx, pf._via_offset_dy, strict=True)
        )

        # Every offset must satisfy the Euclidean filter.
        for dx, dy in offsets:
            assert dx * dx + dy * dy <= r_sq, (
                f"Offset ({dx},{dy}) violates disc: dist_sq={dx * dx + dy * dy} > r_sq={r_sq}"
            )

        # The kernel must be COMPLETE: every Chebyshev-square cell that
        # satisfies the filter is present.  This catches accidental
        # over-pruning (e.g. an off-by-one strict-vs-non-strict inequality).
        expected = sorted(
            (dx, dy)
            for dy in range(-r, r + 1)
            for dx in range(-r, r + 1)
            if dx * dx + dy * dy <= r_sq
        )
        assert offsets == expected

    def test_diagonal_corners_excluded(self) -> None:
        """The four diagonal corners of the Chebyshev square --
        ``(+/-r, +/-r)`` -- have Euclidean distance ``r * sqrt(2) > r``
        and must NOT be in the kernel.  This is the exact failure mode
        the legacy Chebyshev kernel admitted.
        """
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        r = pf._via_half_cells

        offsets_set = {
            (int(dx), int(dy)) for dx, dy in zip(pf._via_offset_dx, pf._via_offset_dy, strict=True)
        }
        for sx in (-1, 1):
            for sy in (-1, 1):
                corner = (sx * r, sy * r)
                assert corner not in offsets_set, (
                    f"Diagonal corner {corner} present in via kernel: "
                    f"dist_sq={r * r + r * r} > r_sq={r * r} -- the kernel is "
                    f"still Chebyshev (legacy bug)"
                )


class TestPythonViaBlockedDiagonal:
    """``Router._is_via_blocked`` must use the Euclidean disc.

    Direct functional regressions on the helper.  Mirrors
    ``test_router_pad_exit_clearance_guard.TestPythonForeignPadMetalGuard``.
    """

    def test_diagonal_corner_blocker_passes(self) -> None:
        """Issue #3234: A foreign-net blocked cell at offset
        ``(+via_half_cells, +via_half_cells)`` has Chebyshev distance
        ``via_half_cells`` (legacy reject) but Euclidean distance
        ``via_half_cells * sqrt(2)`` (Euclidean accept).
        """
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        r = pf._via_half_cells
        # Foreign blocked cell at (50 + r, 50 + r) on each layer.
        for layer in range(py_grid.num_layers):
            py_grid._blocked[layer, 50 + r, 50 + r] = True
            py_grid._net[layer, 50 + r, 50 + r] = 2
        # The candidate at (50, 50) puts the blocker at the diagonal corner
        # of the kernel.  The Euclidean disc must EXCLUDE it -> accept.
        for layer in range(py_grid.num_layers):
            assert pf._is_via_blocked(50, 50, layer, net=1, allow_sharing=False) is False, (
                f"Diagonal corner blocker triggered rejection on layer {layer}"
            )

    def test_axial_blocker_at_radius_rejects(self) -> None:
        """A foreign blocked cell on-axis at distance exactly
        ``via_half_cells`` is INSIDE the Euclidean disc (boundary cell,
        ``dist_sq = r^2``) and must still reject.  Pins down that the
        new kernel does not lose orthogonal coverage.
        """
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        r = pf._via_half_cells
        for layer in range(py_grid.num_layers):
            py_grid._blocked[layer, 50, 50 + r] = True
            py_grid._net[layer, 50, 50 + r] = 2
        # Layer 0 only -- candidate at (50, 50) sees the blocker on the
        # disc boundary.
        assert pf._is_via_blocked(50, 50, 0, net=1, allow_sharing=False) is True

    def test_inside_disc_diagonal_rejects(self) -> None:
        """A foreign blocked cell at offset ``(1, 2)`` has
        ``dist_sq = 5``.  For any ``via_half_cells >= 3`` it is inside
        both the Chebyshev square AND the Euclidean disc.  Both kernels
        must reject -- non-regression check that the new kernel does
        not over-tighten interior placements.
        """
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        r = pf._via_half_cells
        assert r >= 3, (
            f"Test fixture assumes via_half_cells >= 3 (got {r}); "
            f"adjust _make_grid() if the rule changes"
        )
        py_grid._blocked[0, 52, 51] = True  # offset (+1, +2) from (50, 50)
        py_grid._net[0, 52, 51] = 2
        assert pf._is_via_blocked(50, 50, 0, net=1, allow_sharing=False) is True


# ---------------------------------------------------------------------------
# C++ kernel: direct unit tests via ``Pathfinder.is_via_blocked``
# ---------------------------------------------------------------------------


@requires_cpp
class TestCppViaBlockedDiagonal:
    """``Pathfinder::is_via_blocked_diag`` must use the Euclidean disc.

    Sibling of ``TestPythonViaBlockedDiagonal``.  C++ exposes
    ``is_via_blocked`` which dispatches to ``is_via_blocked_diag`` with
    dummy out-params.
    """

    def test_diagonal_corner_blocker_passes(self) -> None:
        # Compute via_half_cells the same way the C++ ctor does:
        # ceil((via_diameter/2 + via_clearance) / resolution).
        r = math.ceil((0.6 / 2 + 0.15) / 0.1)
        # Place the blocker at (50 + r, 50 + r); candidate at (50, 50)
        # sees it at the diagonal corner -> Euclidean accept.
        _, pf = _make_cpp_pf_with_blocker((50 + r, 50 + r))
        assert pf.is_via_blocked(50, 50, 1, False, 0) is False

    def test_axial_blocker_at_radius_rejects(self) -> None:
        r = math.ceil((0.6 / 2 + 0.15) / 0.1)
        # Blocker on-axis at distance r.
        _, pf = _make_cpp_pf_with_blocker((50 + r, 50))
        assert pf.is_via_blocked(50, 50, 1, False, 0) is True

    def test_inside_disc_diagonal_rejects(self) -> None:
        r = math.ceil((0.6 / 2 + 0.15) / 0.1)
        assert r >= 3, f"Test assumes r >= 3 (got {r})"
        # Blocker at offset (+1, +2) -- inside both square and disc.
        _, pf = _make_cpp_pf_with_blocker((51, 52))
        assert pf.is_via_blocked(50, 50, 1, False, 0) is True

    def test_just_outside_disc_passes(self) -> None:
        """Offset where dist_sq is just above r_sq must accept.

        For r=5 (the via_half_cells under the fixture), pick offset
        ``(5, 1)`` -- ``dist_sq = 26 > 25 = r_sq``.  This is inside the
        Chebyshev square (legacy reject) but outside the Euclidean disc
        (new accept).
        """
        r = math.ceil((0.6 / 2 + 0.15) / 0.1)
        # Need r >= 5 so the offset (r, 1) has dist_sq = r^2 + 1 > r^2
        # while still being inside the Chebyshev square.
        assert r == 5, f"Test assumes r=5 with the fixture's via_diameter/clearance (got {r})"
        _, pf = _make_cpp_pf_with_blocker((50 + r, 51))
        assert pf.is_via_blocked(50, 50, 1, False, 0) is False


# ---------------------------------------------------------------------------
# Symmetry: Python and C++ via kernels return identical answers
# ---------------------------------------------------------------------------


@requires_cpp
class TestPythonCppViaKernelSymmetry:
    """Mirror of ``TestPythonCppGuardSymmetry`` (PR #3232) for the via
    kernel.  Python and C++ ``is_via_blocked`` must agree at every cell
    in a sweep, including the 4 diagonal corners that the Euclidean
    kernel must accept (and the legacy Chebyshev kernel rejected)."""

    def test_diagonal_corner_symmetry(self) -> None:
        """Sweep a ``(2r+5) x (2r+5)`` window of candidate positions
        around a single blocked cell.  Python and C++ verdicts must agree
        on every cell, AND the 4 diagonal corners must all be ACCEPTED.
        """
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        r = pf._via_half_cells

        # Place the blocked cell on EVERY layer so the per-layer Python
        # check and the all-layer C++ check return the same boolean
        # without us having to OR across layers in the symmetry loop.
        for layer in range(py_grid.num_layers):
            py_grid._blocked[layer, 50, 50] = True
            py_grid._net[layer, 50, 50] = 2

        cpp_grid = CppGrid.from_routing_grid(py_grid)
        cpp_rules = router_cpp.DesignRules()
        cpp_rules.trace_width = 0.2
        cpp_rules.trace_clearance = 0.15
        cpp_rules.via_diameter = 0.6
        cpp_rules.via_drill = 0.3
        cpp_rules.via_clearance = 0.15
        cpp_rules.grid_resolution = 0.1
        cpp_rules.cost_straight = 1.0
        cpp_rules.cost_turn = 1.5
        cpp_rules.cost_via = 10.0
        cpp_pf = router_cpp.Pathfinder(cpp_grid._impl, cpp_rules, True)

        diag_accepts = 0
        for dy in range(-r - 2, r + 3):
            for dx in range(-r - 2, r + 3):
                gx, gy = 50 + dx, 50 + dy
                py_blocked = any(
                    pf._is_via_blocked(gx, gy, layer, net=1, allow_sharing=False)
                    for layer in range(py_grid.num_layers)
                )
                cpp_blocked = cpp_pf.is_via_blocked(gx, gy, 1, False, 0)
                assert py_blocked == cpp_blocked, (
                    f"Python/C++ via-kernel disagreement at offset "
                    f"(dx={dx},dy={dy}): py={py_blocked} cpp={cpp_blocked}"
                )
                if abs(dx) == r and abs(dy) == r:
                    assert py_blocked is False, (
                        f"Diagonal corner ({dx},{dy}) at Chebyshev=r={r}, "
                        f"Euclidean={r * math.sqrt(2):.2f} must be "
                        f"ACCEPTED by the disc kernel"
                    )
                    diag_accepts += 1
        assert diag_accepts == 4, f"Expected to test all 4 diagonal corners, tested {diag_accepts}"

    def test_kernel_decision_matches_euclidean_distance(self) -> None:
        """Cross-check: the C++ kernel's accept/reject decision must
        correspond to actual Euclidean disc membership.  Closes the
        "both backends agree on a wrong answer" loophole that a pure
        Python<->C++ symmetry sweep alone would miss.
        """
        r = math.ceil((0.6 / 2 + 0.15) / 0.1)
        r_sq = r * r
        _, pf = _make_cpp_pf_with_blocker((25, 25))

        cells_tested = 0
        for dy in range(-r - 2, r + 3):
            for dx in range(-r - 2, r + 3):
                gx, gy = 25 + dx, 25 + dy
                dist_sq = dx * dx + dy * dy
                expected_reject = dist_sq <= r_sq
                actual_reject = pf.is_via_blocked(gx, gy, 1, False, 0)
                assert actual_reject == expected_reject, (
                    f"Via-kernel/Euclidean mismatch at offset ({dx},{dy}): "
                    f"dist_sq={dist_sq} r_sq={r_sq} "
                    f"expected_reject={expected_reject} "
                    f"actual_reject={actual_reject}"
                )
                cells_tested += 1
        assert cells_tested == (2 * (r + 2) + 1) ** 2


# ---------------------------------------------------------------------------
# Per-net radius override: the slow path must also use Euclidean filter
# ---------------------------------------------------------------------------


class TestPythonViaRadiusOverrideIsEuclidean:
    """Issue #3234: the per-net ``radius`` override branch in
    ``Router._is_via_blocked`` (line ~1621) must apply the Euclidean
    disc filter too -- without it, custom-via-diameter nets would
    silently revert to the legacy Chebyshev kernel."""

    def test_override_diagonal_corner_blocker_passes(self) -> None:
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        # Pick a radius that differs from ``_via_half_cells`` so the
        # override branch is exercised.
        override_r = pf._via_half_cells + 2
        # Blocker at the diagonal corner of the OVERRIDE radius.
        bx, by = 50 + override_r, 50 + override_r
        for layer in range(py_grid.num_layers):
            py_grid._blocked[layer, by, bx] = True
            py_grid._net[layer, by, bx] = 2
        # With radius=override_r, blocker at offset (+r,+r) is outside
        # the Euclidean disc (dist_sq = 2*r^2 > r^2) -> accept.
        assert pf._is_via_blocked(50, 50, 0, net=1, allow_sharing=False, radius=override_r) is False

    def test_override_axial_blocker_at_radius_rejects(self) -> None:
        py_grid, rules = _make_grid()
        pf = Pathfinder(py_grid, rules)
        override_r = pf._via_half_cells + 2
        # On-axis at distance == override_r: inside disc (boundary).
        py_grid._blocked[0, 50, 50 + override_r] = True
        py_grid._net[0, 50, 50 + override_r] = 2
        assert pf._is_via_blocked(50, 50, 0, net=1, allow_sharing=False, radius=override_r) is True


@requires_cpp
class TestCppViaRadiusOverrideIsEuclidean:
    """Sibling for the C++ ``radius_override > 0`` slow path."""

    def test_override_diagonal_corner_blocker_passes(self) -> None:
        override_r = 7  # arbitrary; > default via_half_cells_=5
        _, pf = _make_cpp_pf_with_blocker((50 + override_r, 50 + override_r))
        # is_via_blocked(x, y, net, allow_sharing, radius_override)
        assert pf.is_via_blocked(50, 50, 1, False, override_r) is False

    def test_override_axial_blocker_at_radius_rejects(self) -> None:
        override_r = 7
        _, pf = _make_cpp_pf_with_blocker((50 + override_r, 50))
        assert pf.is_via_blocked(50, 50, 1, False, override_r) is True

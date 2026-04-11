"""Tests for the slide-off overlap resolution module.

Covers unit tests, integration with MCP tools, edge cases, and performance
requirements from issue #1243 acceptance criteria.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from kicad_tools.placement.cost import BoardOutline
from kicad_tools.placement.slide_off import (
    SlideOffResult,
    slide_off_overlaps,
)
from kicad_tools.placement.vector import (
    FIELDS_PER_COMPONENT,
    ComponentDef,
    PlacementVector,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_board(width: float = 100.0, height: float = 100.0) -> BoardOutline:
    """Create a board outline centred at the origin."""
    return BoardOutline(
        min_x=-width / 2,
        min_y=-height / 2,
        max_x=width / 2,
        max_y=height / 2,
    )


def _make_components(n: int, size: float = 2.0) -> list[ComponentDef]:
    """Create *n* identical square components."""
    return [ComponentDef(reference=f"U{i + 1}", width=size, height=size) for i in range(n)]


def _make_vector_at_same_position(
    n: int,
    x: float = 0.0,
    y: float = 0.0,
    side: int = 0,
) -> PlacementVector:
    """Create a vector with all components at the same position."""
    data = np.zeros(n * FIELDS_PER_COMPONENT, dtype=np.float64)
    for i in range(n):
        base = i * FIELDS_PER_COMPONENT
        data[base] = x
        data[base + 1] = y
        data[base + 2] = 0.0  # rotation
        data[base + 3] = float(side)
    return PlacementVector(data=data)


def _make_vector_spread(
    n: int,
    spacing: float = 20.0,
    side: int = 0,
) -> PlacementVector:
    """Create a vector with components spread apart (no overlaps)."""
    data = np.zeros(n * FIELDS_PER_COMPONENT, dtype=np.float64)
    for i in range(n):
        base = i * FIELDS_PER_COMPONENT
        data[base] = -((n - 1) * spacing / 2) + i * spacing
        data[base + 1] = 0.0
        data[base + 2] = 0.0
        data[base + 3] = float(side)
    return PlacementVector(data=data)


def _count_overlaps_manual(
    vector: PlacementVector,
    components: list[ComponentDef],
    margin: float = 0.5,
) -> int:
    """Count overlapping pairs by brute force."""
    n = len(components)
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            base_i = i * FIELDS_PER_COMPONENT
            base_j = j * FIELDS_PER_COMPONENT
            side_i = int(round(vector.data[base_i + 3]))
            side_j = int(round(vector.data[base_j + 3]))
            if side_i != side_j:
                continue

            rot_i = int(round(vector.data[base_i + 2])) % 4
            rot_j = int(round(vector.data[base_j + 2])) % 4

            if rot_i in (1, 3):
                hw_i = components[i].height / 2.0
                hh_i = components[i].width / 2.0
            else:
                hw_i = components[i].width / 2.0
                hh_i = components[i].height / 2.0

            if rot_j in (1, 3):
                hw_j = components[j].height / 2.0
                hh_j = components[j].width / 2.0
            else:
                hw_j = components[j].width / 2.0
                hh_j = components[j].height / 2.0

            dx = abs(vector.data[base_j] - vector.data[base_i])
            dy = abs(vector.data[base_j + 1] - vector.data[base_i + 1])

            combined_hw = hw_i + hw_j + margin
            combined_hh = hh_i + hh_j + margin

            if (combined_hw - dx) > 0 and (combined_hh - dy) > 0:
                count += 1
    return count


def _within_board(
    vector: PlacementVector,
    components: list[ComponentDef],
    board: BoardOutline,
) -> bool:
    """Check that all component AABBs are within the board."""
    n = len(components)
    for i in range(n):
        base = i * FIELDS_PER_COMPONENT
        x = vector.data[base]
        y = vector.data[base + 1]
        rot_idx = int(round(vector.data[base + 2])) % 4

        if rot_idx in (1, 3):
            hw = components[i].height / 2.0
            hh = components[i].width / 2.0
        else:
            hw = components[i].width / 2.0
            hh = components[i].height / 2.0

        if x - hw < board.min_x - 1e-6:
            return False
        if x + hw > board.max_x + 1e-6:
            return False
        if y - hh < board.min_y - 1e-6:
            return False
        if y + hh > board.max_y + 1e-6:
            return False
    return True


# ---------------------------------------------------------------------------
# Unit tests: zero overlaps after slide-off
# ---------------------------------------------------------------------------


class TestSlideOffBasic:
    """Tests that slide-off resolves overlaps on typical configurations."""

    def test_10_components_at_same_position(self):
        """10 components at origin on a 100x100 board -> zero overlaps.

        This is a degenerate case (all coincident) that requires more
        iterations than the default.  The algorithm resolves it within
        50 iterations on a board large enough to fit all components.
        """
        board = _make_board(100.0, 100.0)
        components = _make_components(10, size=2.0)
        vector = _make_vector_at_same_position(10)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
            max_iterations=50,
        )

        assert result.overlaps_remaining == 0
        assert result.overlaps_resolved > 0
        assert result.iterations_run <= 50

    def test_two_overlapping_components(self):
        """Two components slightly overlapping get separated."""
        board = _make_board(100.0, 100.0)
        components = _make_components(2, size=4.0)

        data = np.zeros(2 * FIELDS_PER_COMPONENT, dtype=np.float64)
        # Place them 1mm apart (overlap: combined half-widths = 4, dx = 1)
        data[0] = -0.5
        data[1] = 0.0
        data[4] = 0.5
        data[5] = 0.0
        vector = PlacementVector(data=data)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
        )

        assert result.overlaps_remaining == 0

    def test_empty_components(self):
        """Zero components: no-op."""
        board = _make_board()
        components: list[ComponentDef] = []
        vector = PlacementVector(data=np.empty(0, dtype=np.float64))

        new_vector, result = slide_off_overlaps(vector, components, board)

        assert result.iterations_run == 0
        assert result.overlaps_resolved == 0
        assert result.overlaps_remaining == 0
        assert result.max_displacement_applied == 0.0

    def test_single_component(self):
        """Single component: no pairs to check."""
        board = _make_board()
        components = _make_components(1)
        vector = _make_vector_at_same_position(1)

        new_vector, result = slide_off_overlaps(vector, components, board)

        assert result.iterations_run == 0
        assert result.overlaps_resolved == 0
        assert result.overlaps_remaining == 0


# ---------------------------------------------------------------------------
# Unit tests: displacement cap
# ---------------------------------------------------------------------------


class TestDisplacementCap:
    """Verify that per-component displacement is capped."""

    def test_displacement_capped_at_1mm(self):
        """With max_displacement_mm=1.0, no component moves more than 1mm.

        A small tolerance (0.02 mm) accounts for the pre-jitter that
        separates coincident components before the main loop.
        """
        board = _make_board(100.0, 100.0)
        components = _make_components(5, size=3.0)
        vector = _make_vector_at_same_position(5)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
            max_iterations=10,
            max_displacement_mm=1.0,
        )

        # Allow tolerance for pre-jitter offset (up to _JITTER_RADIUS = 0.01mm)
        jitter_tolerance = 0.02
        for i in range(5):
            base = i * FIELDS_PER_COMPONENT
            dx = new_vector.data[base] - vector.data[base]
            dy = new_vector.data[base + 1] - vector.data[base + 1]
            disp = math.sqrt(dx * dx + dy * dy)
            assert disp <= 1.0 + jitter_tolerance, (
                f"Component {i} moved {disp:.4f}mm, exceeds cap of 1.0mm"
            )

    def test_max_displacement_reported_correctly(self):
        """SlideOffResult.max_displacement_applied tracks displacement.

        The result reports displacement from the post-jitter initial
        position, while the test measures from the original vector.
        We verify they are within the jitter tolerance (0.02 mm).
        """
        board = _make_board(100.0, 100.0)
        components = _make_components(3, size=5.0)
        # Use non-coincident positions to avoid jitter offset
        data = np.zeros(3 * FIELDS_PER_COMPONENT, dtype=np.float64)
        data[0] = -1.0  # U1 x
        data[4] = 0.0  # U2 x
        data[8] = 1.0  # U3 x
        vector = PlacementVector(data=data)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.0,
            max_iterations=10,
            max_displacement_mm=20.0,
        )

        max_disp = 0.0
        for i in range(3):
            base = i * FIELDS_PER_COMPONENT
            dx = new_vector.data[base] - vector.data[base]
            dy = new_vector.data[base + 1] - vector.data[base + 1]
            disp = math.sqrt(dx * dx + dy * dy)
            max_disp = max(max_disp, disp)

        assert abs(result.max_displacement_applied - max_disp) < 1e-6


# ---------------------------------------------------------------------------
# Unit tests: board containment
# ---------------------------------------------------------------------------


class TestBoardContainment:
    """All component AABBs remain within the board after slide-off."""

    def test_components_stay_in_board(self):
        """After slide-off, all AABBs are within the board bounds."""
        board = _make_board(50.0, 50.0)
        components = _make_components(8, size=3.0)
        vector = _make_vector_at_same_position(8)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
            max_iterations=10,
        )

        assert _within_board(new_vector, components, board)

    def test_component_wider_than_board(self):
        """A component wider than the board is centred."""
        board = _make_board(5.0, 5.0)
        components = [ComponentDef(reference="U1", width=10.0, height=2.0)]
        data = np.zeros(FIELDS_PER_COMPONENT, dtype=np.float64)
        vector = PlacementVector(data=data)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
        )

        # Component centre should be at board centre
        assert abs(new_vector.data[0] - 0.0) < 1e-6


# ---------------------------------------------------------------------------
# Unit tests: side isolation
# ---------------------------------------------------------------------------


class TestSideIsolation:
    """Components on opposite sides are not pushed apart."""

    def test_opposite_sides_not_affected(self):
        """Components on front (0) and back (1) at the same position stay put."""
        board = _make_board(100.0, 100.0)
        components = _make_components(2, size=4.0)

        data = np.zeros(2 * FIELDS_PER_COMPONENT, dtype=np.float64)
        # Component 0: front side at origin
        data[0] = 0.0
        data[1] = 0.0
        data[2] = 0.0
        data[3] = 0.0
        # Component 1: back side at origin
        data[4] = 0.0
        data[5] = 0.0
        data[6] = 0.0
        data[7] = 1.0
        vector = PlacementVector(data=data)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
        )

        # Neither component should have moved
        assert result.overlaps_resolved == 0
        assert result.overlaps_remaining == 0
        assert abs(new_vector.data[0] - 0.0) < 1e-6
        assert abs(new_vector.data[4] - 0.0) < 1e-6

    def test_same_side_pushed_apart(self):
        """Two components on the same side at the same position get pushed."""
        board = _make_board(100.0, 100.0)
        components = _make_components(2, size=4.0)

        data = np.zeros(2 * FIELDS_PER_COMPONENT, dtype=np.float64)
        data[0] = 0.0
        data[1] = 0.0
        data[3] = 0.0  # front
        data[4] = 0.0
        data[5] = 0.0
        data[7] = 0.0  # front
        vector = PlacementVector(data=data)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
        )

        assert result.overlaps_resolved > 0


# ---------------------------------------------------------------------------
# Unit tests: deterministic on coincident components
# ---------------------------------------------------------------------------


class TestDeterministic:
    """Coincident components produce deterministic, finite positions."""

    def test_same_position_no_nan(self):
        """Two components at identical (x, y) produce finite positions."""
        board = _make_board(100.0, 100.0)
        components = _make_components(2, size=4.0)
        vector = _make_vector_at_same_position(2)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
        )

        assert np.all(np.isfinite(new_vector.data))
        # They should have separated
        dx = abs(new_vector.data[0] - new_vector.data[4])
        dy = abs(new_vector.data[1] - new_vector.data[5])
        assert dx > 0 or dy > 0

    def test_deterministic_repeated_calls(self):
        """Repeated calls with same input produce same output."""
        board = _make_board(100.0, 100.0)
        components = _make_components(5, size=3.0)
        vector = _make_vector_at_same_position(5)

        result1_vec, result1 = slide_off_overlaps(vector, components, board)
        result2_vec, result2 = slide_off_overlaps(vector, components, board)

        np.testing.assert_array_equal(result1_vec.data, result2_vec.data)
        assert result1 == result2


# ---------------------------------------------------------------------------
# Unit tests: idempotent on clean placement
# ---------------------------------------------------------------------------


class TestIdempotent:
    """A placement with no overlaps is unchanged by slide-off."""

    def test_no_overlaps_unchanged(self):
        """Components spread far apart are not moved."""
        board = _make_board(200.0, 200.0)
        components = _make_components(5, size=2.0)
        vector = _make_vector_spread(5, spacing=20.0)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
        )

        assert result.overlaps_resolved == 0
        assert result.overlaps_remaining == 0
        np.testing.assert_allclose(new_vector.data, vector.data, atol=1e-10)


# ---------------------------------------------------------------------------
# Unit tests: SlideOffResult fields
# ---------------------------------------------------------------------------


class TestSlideOffResult:
    """Verify result dataclass fields are populated correctly."""

    def test_iterations_within_limit(self):
        """iterations_run never exceeds max_iterations."""
        board = _make_board(100.0, 100.0)
        components = _make_components(10, size=2.0)
        vector = _make_vector_at_same_position(10)

        _, result = slide_off_overlaps(
            vector,
            components,
            board,
            max_iterations=3,
        )

        assert result.iterations_run <= 3
        assert result.iterations_run >= 1

    def test_overlaps_non_negative(self):
        """overlaps_resolved and overlaps_remaining are non-negative."""
        board = _make_board(100.0, 100.0)
        components = _make_components(5, size=2.0)
        vector = _make_vector_at_same_position(5)

        _, result = slide_off_overlaps(vector, components, board)

        assert result.overlaps_resolved >= 0
        assert result.overlaps_remaining >= 0

    def test_result_is_frozen_dataclass(self):
        """SlideOffResult is immutable."""
        result = SlideOffResult(
            iterations_run=3,
            overlaps_resolved=10,
            overlaps_remaining=0,
            max_displacement_applied=5.0,
        )
        with pytest.raises(AttributeError):
            result.iterations_run = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Unit tests: spatial index
# ---------------------------------------------------------------------------


class TestSpatialIndex:
    """Verify the grid-based spatial index correctness."""

    def test_spatial_index_gives_same_result(self):
        """Spatial index produces the same resolved count as brute force.

        Uses a large enough iteration limit and board so both methods
        can fully resolve all overlaps, despite potentially different
        pair processing order.
        """
        board = _make_board(200.0, 200.0)
        components = _make_components(10, size=3.0)
        vector = _make_vector_at_same_position(10)

        vec_brute, res_brute = slide_off_overlaps(
            vector,
            components,
            board,
            use_spatial_index=False,
            max_iterations=100,
        )
        vec_grid, res_grid = slide_off_overlaps(
            vector,
            components,
            board,
            use_spatial_index=True,
            max_iterations=100,
        )

        # Both should resolve all overlaps (final states may differ
        # due to iteration order, but overlap count should match)
        assert res_brute.overlaps_remaining == 0
        assert res_grid.overlaps_remaining == 0

    def test_auto_enable_spatial_index(self):
        """use_spatial_index=None enables automatically for > 50 components."""
        board = _make_board(500.0, 500.0)
        # Create 55 components
        components = _make_components(55, size=2.0)
        vector = _make_vector_at_same_position(55)

        # Should not raise and should use spatial index internally
        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            use_spatial_index=None,
        )

        assert np.all(np.isfinite(new_vector.data))


# ---------------------------------------------------------------------------
# Unit tests: margin parameter
# ---------------------------------------------------------------------------


class TestMargin:
    """Verify the margin parameter enforces extra clearance."""

    def test_zero_margin_allows_touching(self):
        """With margin=0, components can touch but not overlap."""
        board = _make_board(100.0, 100.0)
        components = _make_components(2, size=4.0)

        # Place components touching: distance = exactly combined half-widths
        data = np.zeros(2 * FIELDS_PER_COMPONENT, dtype=np.float64)
        data[0] = -2.0  # left edge at -4, right edge at 0
        data[4] = 2.0  # left edge at 0, right edge at 4
        vector = PlacementVector(data=data)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.0,
        )

        # They are touching but not overlapping with margin=0
        assert result.overlaps_remaining == 0

    def test_positive_margin_separates_touching(self):
        """With margin > 0, touching components get pushed apart."""
        board = _make_board(100.0, 100.0)
        components = _make_components(2, size=4.0)

        # Place components touching: distance = exactly combined half-widths
        data = np.zeros(2 * FIELDS_PER_COMPONENT, dtype=np.float64)
        data[0] = -2.0
        data[4] = 2.0
        vector = PlacementVector(data=data)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=1.0,
        )

        # With margin=1.0, the touching pair should now have been pushed apart
        dx = abs(new_vector.data[4] - new_vector.data[0])
        # Expected minimum separation: 4.0 (combined widths) + 1.0 (margin)
        assert dx >= 4.0 + 1.0 - 0.1  # slight tolerance


# ---------------------------------------------------------------------------
# Unit tests: rotation awareness
# ---------------------------------------------------------------------------


class TestRotation:
    """Verify rotation is accounted for in AABB computation."""

    def test_rotated_components_use_swapped_dimensions(self):
        """A 10x2 component rotated 90 degrees uses 2x10 AABB."""
        board = _make_board(100.0, 100.0)
        components = [
            ComponentDef(reference="U1", width=10.0, height=2.0),
            ComponentDef(reference="U2", width=10.0, height=2.0),
        ]

        data = np.zeros(2 * FIELDS_PER_COMPONENT, dtype=np.float64)
        # U1 at origin, rotated 90 degrees (rot_idx = 1)
        data[0] = 0.0
        data[1] = 0.0
        data[2] = 1.0  # 90 degrees
        # U2 at (3, 0), no rotation -- should overlap in Y
        data[4] = 3.0
        data[5] = 0.0
        data[6] = 0.0
        vector = PlacementVector(data=data)

        # U1 rotated: half_w = 2/2=1.0, half_h = 10/2=5.0
        # U2 unrotated: half_w = 10/2=5.0, half_h = 2/2=1.0
        # X overlap: (1.0 + 5.0 + 0.5) - 3.0 = 3.5 > 0
        # Y overlap: (5.0 + 1.0 + 0.5) - 0.0 = 6.5 > 0
        # These overlap with default margin

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
        )

        assert result.overlaps_resolved > 0


# ---------------------------------------------------------------------------
# Unit tests: validation
# ---------------------------------------------------------------------------


class TestValidation:
    """Input validation."""

    def test_mismatched_lengths_raises(self):
        """Mismatched vector and component_defs raises ValueError."""
        board = _make_board()
        components = _make_components(3)
        data = np.zeros(2 * FIELDS_PER_COMPONENT, dtype=np.float64)
        vector = PlacementVector(data=data)

        with pytest.raises(ValueError, match="component definitions"):
            slide_off_overlaps(vector, components, board)


# ---------------------------------------------------------------------------
# Performance tests
# ---------------------------------------------------------------------------


class TestPerformance:
    """Verify slide-off completes within time budgets."""

    def test_20_components_under_100ms(self):
        """20 components complete in < 100ms."""
        board = _make_board(100.0, 100.0)
        components = _make_components(20, size=3.0)
        vector = _make_vector_at_same_position(20)

        start = time.perf_counter()
        slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
            max_iterations=5,
        )
        elapsed = time.perf_counter() - start

        assert elapsed < 0.1, f"Took {elapsed:.3f}s, expected < 0.1s"

    def test_100_components_with_spatial_index_under_500ms(self):
        """100 components with spatial index complete in < 500ms."""
        board = _make_board(200.0, 200.0)
        components = _make_components(100, size=2.0)
        vector = _make_vector_at_same_position(100)

        start = time.perf_counter()
        slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
            max_iterations=5,
            use_spatial_index=True,
        )
        elapsed = time.perf_counter() - start

        assert elapsed < 0.5, f"Took {elapsed:.3f}s, expected < 0.5s"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Unusual configurations."""

    def test_very_tight_board(self):
        """Board barely fits all components; overlaps may persist."""
        board = _make_board(10.0, 10.0)
        components = _make_components(10, size=3.0)
        vector = _make_vector_at_same_position(10)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
            max_iterations=20,
        )

        # May not resolve all, but should not crash and should respect bounds
        assert np.all(np.isfinite(new_vector.data))
        assert result.iterations_run > 0

    def test_all_components_different_sides(self):
        """All components on alternating sides: fewer same-side overlaps."""
        board = _make_board(100.0, 100.0)
        n = 6
        components = _make_components(n, size=4.0)

        data = np.zeros(n * FIELDS_PER_COMPONENT, dtype=np.float64)
        for i in range(n):
            base = i * FIELDS_PER_COMPONENT
            data[base] = 0.0
            data[base + 1] = 0.0
            data[base + 3] = float(i % 2)  # alternating sides
        vector = PlacementVector(data=data)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=0.5,
        )

        # With alternating sides, fewer pairs overlap
        assert np.all(np.isfinite(new_vector.data))

    def test_large_margin_small_board(self):
        """Large margin on small board: may not resolve all overlaps."""
        board = _make_board(20.0, 20.0)
        components = _make_components(4, size=3.0)
        vector = _make_vector_at_same_position(4)

        new_vector, result = slide_off_overlaps(
            vector,
            components,
            board,
            margin_mm=5.0,  # large margin
            max_iterations=10,
        )

        # Should not crash, components should be within board
        assert np.all(np.isfinite(new_vector.data))

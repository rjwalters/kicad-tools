"""Exact O(N) disc dilation for the HV pairwise masks (issue #8414).

``Router._dilate_zero_padded`` used to OR the whole grid once per disc offset,
so one call cost O(N * pi * r**2): 1.3-3.4 s per widening mask and 15.7 s for
the r=97 gradient band on a 15.9 M-cell board, 125 s of a 128 s fallback call.
The replacement, ``_disc_dilate_exact``, uses a separable distance transform
with a threshold. These tests pin it **byte-identical** to the original
shift-and-OR loop (kept as ``_disc_dilate_shift_or``, the fallback for a
structuring element the separable form cannot express) across radii, densities
and the border cases the zero-padding convention exists for.
"""

from __future__ import annotations

import numpy as np
import pytest

from kicad_tools.router.grid import RoutingGrid, halo_mask
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import (
    Router,
    _disc_dilate_exact,
    _disc_dilate_shift_or,
    _separable_disc_heights,
)
from kicad_tools.router.rules import DesignRules


def _assert_identical(mask: np.ndarray, radius: int) -> None:
    disc = halo_mask(radius)
    expected = _disc_dilate_shift_or(mask, disc)
    actual = _disc_dilate_exact(mask, disc)
    assert actual.dtype == expected.dtype == np.bool_
    assert actual.shape == expected.shape
    # Byte-identical, not merely equal-valued.
    assert actual.tobytes() == expected.tobytes(), (
        f"r={radius}: {int(np.count_nonzero(actual != expected))} cells differ"
    )


@pytest.mark.parametrize("radius", [2, 3, 4, 5, 7, 8, 11, 16, 23, 28, 37, 46])
@pytest.mark.parametrize("density", [0.0005, 0.01, 0.2])
def test_exact_matches_shift_or_on_random_masks(radius: int, density: float) -> None:
    rng = np.random.default_rng(8414 + radius)
    mask = rng.random((3, 61, 97)) < density
    _assert_identical(mask, radius)


@pytest.mark.parametrize("radius", [60, 97])
def test_exact_matches_shift_or_at_the_gradient_band_radius(radius: int) -> None:
    """The profiled band radius (97) is wider than this whole grid."""
    rng = np.random.default_rng(radius)
    mask = rng.random((2, 140, 90)) < 0.002
    _assert_identical(mask, radius)


@pytest.mark.parametrize("radius", [2, 5, 13, 30])
def test_exact_matches_shift_or_for_copper_touching_every_border(radius: int) -> None:
    """Out-of-bounds stays EMPTY: border copper must not seal a border band."""
    mask = np.zeros((2, 40, 55), dtype=np.bool_)
    mask[0, 0, :] = True  # whole top row
    mask[0, :, -1] = True  # whole right column
    mask[1, -1, 7] = True  # bottom edge
    mask[1, 13, 0] = True  # left edge
    mask[1, 0, 0] = mask[1, 0, -1] = mask[1, -1, 0] = mask[1, -1, -1] = True  # corners
    _assert_identical(mask, radius)


@pytest.mark.parametrize("radius", [2, 6, 19])
def test_exact_matches_shift_or_on_degenerate_masks(radius: int) -> None:
    shape = (2, 25, 31)
    _assert_identical(np.zeros(shape, dtype=np.bool_), radius)
    _assert_identical(np.ones(shape, dtype=np.bool_), radius)
    centre = np.zeros(shape, dtype=np.bool_)
    centre[1, 12, 15] = True  # a single cell reproduces the disc itself
    _assert_identical(centre, radius)
    # One-row and one-column grids (radius far beyond the extent).
    _assert_identical(np.random.default_rng(1).random((1, 1, 40)) < 0.1, radius)
    _assert_identical(np.random.default_rng(2).random((1, 40, 1)) < 0.1, radius)


def test_exact_matches_shift_or_on_dense_blocks_and_stripes() -> None:
    mask = np.zeros((1, 80, 80), dtype=np.bool_)
    mask[0, 10:30, 10:50] = True  # solid pad block
    mask[0, 60, :] = True  # full-width trace
    mask[0, :, 70] = True  # full-height trace
    mask[0, 40:70:3, 20:40:5] = True  # via field
    for radius in (2, 9, 17, 33):
        _assert_identical(mask, radius)


def test_separable_heights_describe_the_halo_disc() -> None:
    for radius in (2, 5, 28, 97):
        disc = halo_mask(radius)
        heights = _separable_disc_heights(disc)
        assert heights is not None
        rebuilt = np.zeros_like(disc)
        for a, h in enumerate(heights):
            if h >= 0:
                rebuilt[radius - h : radius + h + 1, radius + a] = True
                rebuilt[radius - h : radius + h + 1, radius - a] = True
        assert rebuilt.tobytes() == disc.tobytes()


def test_non_separable_structure_falls_back_to_shift_or() -> None:
    """A ring is not column-convex: the exact path must decline, not guess."""
    ring = halo_mask(6).copy()
    ring[4:9, 4:9] = False
    assert _separable_disc_heights(ring) is None
    mask = np.random.default_rng(3).random((1, 30, 30)) < 0.05
    expected = _disc_dilate_shift_or(mask, ring)
    assert _disc_dilate_exact(mask, ring).tobytes() == expected.tobytes()


@pytest.mark.parametrize("radius", [0, 1, 2, 12, 40])
def test_router_method_is_unchanged_on_a_real_grid(radius: int) -> None:
    rules = DesignRules(grid_resolution=0.1)
    grid = RoutingGrid(width=12.0, height=9.0, rules=rules, layer_stack=LayerStack.two_layer())
    router = Router(grid, rules)
    rng = np.random.default_rng(radius)
    mask = rng.random((grid.num_layers, grid.rows, grid.cols)) < 0.003
    mask[:, 0, 0] = True
    result = router._dilate_zero_padded(mask, radius)
    if radius <= 1:
        assert result is mask  # the historical radius<=1 no-op is preserved
    else:
        expected = _disc_dilate_shift_or(mask, grid._get_disc_kernel(radius))
        assert result.tobytes() == expected.tobytes()

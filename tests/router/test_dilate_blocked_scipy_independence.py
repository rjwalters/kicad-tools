"""``RoutingGrid._dilate_blocked`` must not depend on whether scipy is importable.

Issue #8657.  The dilation used to call ``scipy.ndimage.binary_dilation``
whenever scipy could be imported and fall back to a NumPy shift-and-OR loop
otherwise.  The two disagreed within ``radius`` cells of the grid edge: scipy
treats out-of-bounds as empty, the loop pads with ones (out-of-bounds is
blocked, so a wide trace cannot hang off the board).  The same board could
therefore route differently depending on an undeclared transitive dependency.

CI installs no scipy, which is how the scipy branch went untested.  These
tests do not need the real package: the scipy-present leg installs a *fake*
``scipy`` / ``scipy.ndimage`` in ``sys.modules`` whose ``binary_dilation``
reproduces the old zero-padded answer and records that it was called.  With
the old branch in place that leg fails twice over (wrong cells at the edge,
and the fake was consulted); it passes only when the branch is gone.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import DesignRules

RADII = (2, 3, 8, 16, 27)


def _grid() -> RoutingGrid:
    return RoutingGrid(
        8.0,
        6.0,
        DesignRules(),
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=LayerStack.four_layer_all_signal(),
    )


def _shifted_or(mask: np.ndarray, radius: int, *, pad_value: bool) -> np.ndarray:
    """Independent disc dilation: OR of every in-disc shift of a padded copy.

    Written from the Euclidean inequality directly rather than from
    ``halo_mask`` / ``_get_disc_kernel``, so it does not share the kernel
    builder with the code under test.
    """
    _, rows, cols = mask.shape
    padded = np.pad(
        mask,
        ((0, 0), (radius, radius), (radius, radius)),
        mode="constant",
        constant_values=pad_value,
    )
    out = np.zeros_like(mask)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy <= radius * radius:
                out |= padded[
                    :,
                    radius + dy : radius + dy + rows,
                    radius + dx : radius + dx + cols,
                ]
    return out


def _reference(mask: np.ndarray, radius: int) -> np.ndarray:
    """The documented answer: out-of-bounds counts as blocked."""
    return _shifted_or(mask, radius, pad_value=True)


def _masks(grid: RoutingGrid) -> dict[str, np.ndarray]:
    shape = (grid.num_layers, grid.rows, grid.cols)
    rows, cols = grid.rows, grid.cols

    empty = np.zeros(shape, dtype=np.bool_)

    full = np.ones(shape, dtype=np.bool_)

    interior = empty.copy()
    interior[0, rows // 2, cols // 2] = True
    interior[1, rows // 2 - 3 : rows // 2 + 3, cols // 2 + 5] = True

    edges = empty.copy()
    edges[0, 0, cols // 3] = True  # first row
    edges[0, rows - 1, 2 * cols // 3] = True  # last row
    edges[1, rows // 3, 0] = True  # first column
    edges[1, 2 * rows // 3, cols - 1] = True  # last column

    corners = empty.copy()
    corners[0, 0, 0] = True
    corners[1, 0, cols - 1] = True
    corners[2, rows - 1, 0] = True
    corners[3, rows - 1, cols - 1] = True

    mixed = interior | edges
    mixed[2, 1, 1] = True  # one cell in from a corner
    mixed[3, rows // 2, cols // 2] = True

    rng = np.random.default_rng(8657)
    scattered = rng.random(shape) < 0.002

    return {
        "empty": empty,
        "full": full,
        "interior": interior,
        "edges": edges,
        "corners": corners,
        "mixed": mixed,
        "scattered": scattered,
    }


class _FakeScipy:
    """A stand-in ``scipy`` whose ``binary_dilation`` gives the OLD answer."""

    def __init__(self) -> None:
        self.calls = 0
        self.scipy = types.ModuleType("scipy")
        self.ndimage = types.ModuleType("scipy.ndimage")
        self.scipy.ndimage = self.ndimage  # type: ignore[attr-defined]
        self.ndimage.binary_dilation = self._binary_dilation  # type: ignore[attr-defined]

    def _binary_dilation(self, input, structure=None, **_kwargs):  # noqa: A002
        # scipy's default border handling: out-of-bounds is empty.
        self.calls += 1
        assert structure is not None and structure.shape[0] == 1
        radius = (structure.shape[1] - 1) // 2
        return _shifted_or(np.asarray(input, dtype=np.bool_), radius, pad_value=False)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "scipy", self.scipy)
        monkeypatch.setitem(sys.modules, "scipy.ndimage", self.ndimage)


def _block_scipy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both names: blocking only ``scipy`` does not stop
    # ``from scipy.ndimage import ...`` if the submodule is already imported.
    monkeypatch.setitem(sys.modules, "scipy", None)
    monkeypatch.setitem(sys.modules, "scipy.ndimage", None)


def test_grid_is_large_enough_for_the_largest_radius() -> None:
    """The fixtures only mean something if interior cells exist at radius 27."""
    grid = _grid()
    assert grid.num_layers == 4
    assert min(grid.rows, grid.cols) > 2 * max(RADII) + 1


def test_fake_scipy_reproduces_the_edge_disagreement() -> None:
    """The simulated old path really differs from the reference, at the edge only.

    Guards the regression test below against going vacuous: if the two
    padding conventions agreed on these fixtures, "equal with and without
    scipy" would prove nothing.
    """
    grid = _grid()
    mask = _masks(grid)["interior"]
    for radius in RADII:
        ones = _shifted_or(mask, radius, pad_value=True)
        zeros = _shifted_or(mask, radius, pad_value=False)
        differ = ones != zeros
        assert differ.any()
        inner = differ[:, radius:-radius, radius:-radius]
        assert not inner.any()


@pytest.mark.parametrize("radius", RADII)
@pytest.mark.parametrize(
    "mask_name", ["empty", "full", "interior", "edges", "corners", "mixed", "scattered"]
)
def test_dilate_blocked_is_identical_with_and_without_scipy(
    monkeypatch: pytest.MonkeyPatch, mask_name: str, radius: int
) -> None:
    grid = _grid()
    mask = _masks(grid)[mask_name]
    expected = _reference(mask, radius)

    with monkeypatch.context() as absent:
        _block_scipy(absent)
        without_scipy = grid._dilate_blocked(mask.copy(), radius)

    fake = _FakeScipy()
    with monkeypatch.context() as present:
        fake.install(present)
        # The fake is what an ``import`` inside the function would resolve to.
        from scipy.ndimage import binary_dilation  # type: ignore[import-not-found]

        assert binary_dilation.__self__ is fake
        with_scipy = grid._dilate_blocked(mask.copy(), radius)

    assert without_scipy.dtype == np.bool_
    assert with_scipy.dtype == np.bool_
    assert without_scipy.tobytes() == expected.tobytes()
    assert with_scipy.tobytes() == expected.tobytes()
    assert with_scipy.tobytes() == without_scipy.tobytes()
    assert fake.calls == 0, "_dilate_blocked consulted scipy"


def test_out_of_bounds_is_blocked_on_an_empty_mask() -> None:
    """An empty mask dilates to a ``radius``-cell frame around the grid.

    Every cell within ``radius`` of the grid edge can reach an out-of-bounds
    cell inside the disc, so it is blocked; everything further in is free.
    """
    grid = _grid()
    empty = _masks(grid)["empty"]
    radius = 8
    out = grid._dilate_blocked(empty, radius)
    assert out[:, :radius, :].all() and out[:, -radius:, :].all()
    assert out[:, :, :radius].all() and out[:, :, -radius:].all()
    assert not out[:, radius:-radius, radius:-radius].any()


@pytest.mark.parametrize("radius", [0, 1])
def test_trivial_radii_do_not_expand(monkeypatch: pytest.MonkeyPatch, radius: int) -> None:
    grid = _grid()
    mask = _masks(grid)["mixed"]
    fake = _FakeScipy()
    fake.install(monkeypatch)
    out = grid._dilate_blocked(mask, radius)
    assert out.tobytes() == mask.tobytes()
    assert fake.calls == 0

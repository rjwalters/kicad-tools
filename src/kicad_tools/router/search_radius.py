"""One A* search-radius helper (Epic #5509 Phase 4e slice 2, Issue #6960).

The A* *search radius* is the number of grid cells a search scans around a
candidate trace or via centre for blocked foreign cells::

    x = (half_extent_mm + clearance_mm) / resolution

rounded up to a cell count.  Before Issue #6960 that expression was
hand-derived at 21 sites in ``pathfinder.py``, ``cpp_backend.py`` and
``diffpair_routing.py``, with three different rounding rules.  This module
puts them behind one function **without changing any radius**: each call
site names its rounding rule explicitly via the keyword-only ``rounding``
argument, so the remaining disagreement is visible and tested
(``tests/router/test_search_radius_cells.py``).  Unifying the variants is a
separate, behaviour-changing follow-up under Epic #5509.

This is a leaf module: it imports only :mod:`math` and :mod:`typing`, so
``cpp_backend.py`` and ``diffpair_routing.py`` can import it without pulling
in ``pathfinder.py`` or ``grid.py``.
"""

from __future__ import annotations

import math
from typing import Literal

__all__ = ["SearchRounding", "search_radius_cells"]

SearchRounding = Literal["guarded", "ceil", "nearest"]


def search_radius_cells(
    half_extent_mm: float,
    clearance_mm: float,
    resolution: float,
    *,
    rounding: SearchRounding,
) -> int:
    """A* search radius in grid cells around a candidate centre (Epic #5509).

    ``x = (half_extent_mm + clearance_mm) / resolution``, then

    * ``"guarded"``: ``max(1, math.ceil(round(x, 6)))``
    * ``"ceil"``:    ``max(1, math.ceil(x))``
    * ``"nearest"``: ``max(1, int(x + 0.5))``

    The result is always at least 1 cell.

    **Float order is the behaviour contract.**  Callers pass the half extent
    exactly as their pre-refactor expression computed it (``width / 2`` or a
    precomputed ``half_mm``), and this function performs the identical IEEE
    operations ``(half + clearance) / resolution``.  Do not pre-sum, reorder
    or rescale the operands: e.g. ``(0.1 + 0.2) / 0.1`` is
    ``3.0000000000000004``, which ``"ceil"`` turns into 4 cells.

    Rounding rationale, by variant:

    * ``"guarded"`` -- Issue #864: ``round(x, 6)`` before ``ceil`` so
      floating-point noise (``0.30000000000000004 / 0.1``) cannot add a
      phantom extra cell of clearance (4 cells instead of 3).  Used by the
      Python ``Router`` global radii (Issue #553 trace half-width plus
      clearance; via diameter/2 plus via clearance), the per-(width,
      clearance) cache (Issue #1674 per-net-class trace widths), the per-net
      trace / via radii (Issues #1674, #1692), the intra-pair partner radius
      (Issue #2559 / Epic #2556 Phase 1C), Python pairwise widening
      (Issue #4431) and ``CppPathfinder.find_blocking_nets_relaxed``.
    * ``"ceil"`` -- bare ``ceil`` with no #864 guard.  Used by
      ``CppPathfinder._route_impl`` (Issue #1702 per-net trace / via radii,
      Issue #2587 intra-pair radius, Issue #3229), i.e. the radius the
      native A* actually searches with, and by ``CoupledPathfinder``.
    * ``"nearest"`` -- round half up.  Used by
      ``CppPathfinder.find_blocking_nets`` (rip-up victim selection).

    Args:
        half_extent_mm: Half the copper extent (trace half-width or via
            radius) in mm.
        clearance_mm: Clearance (or pairwise widening) in mm added to the
            half extent.
        resolution: Grid resolution in mm per cell.
        rounding: Rounding rule; keyword-only with no default so every call
            site states its variant.

    Returns:
        Radius in grid cells (at least 1).

    Raises:
        ValueError: If ``rounding`` is not one of the three variants.
    """
    x = (half_extent_mm + clearance_mm) / resolution
    if rounding == "guarded":
        return max(1, math.ceil(round(x, 6)))
    if rounding == "ceil":
        return max(1, math.ceil(x))
    if rounding == "nearest":
        return max(1, int(x + 0.5))
    raise ValueError(
        f"unknown search-radius rounding {rounding!r}; expected 'guarded', 'ceil' or 'nearest'"
    )

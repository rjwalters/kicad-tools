"""One A* search-radius helper (Epic #5509 Phase 4e, Issues #6960 / #6994).

The A* *search radius* is the number of grid cells a search scans around a
candidate trace or via centre for blocked foreign cells::

    x = (half_extent_mm + clearance_mm) / resolution

rounded up to a whole cell count.  Issue #6960 put the 21 hand-derived sites
in ``pathfinder.py``, ``cpp_backend.py`` and ``diffpair_routing.py`` behind
this function with their three historical rounding rules spelled out.  Issue
#6994 collapsed them onto the single correct rule, the Issue #864
*guarded* ceiling, and the native A* uses the same rule through
``router_cpp.search_radius_cells`` (``cpp/include/types.hpp``).

This is a leaf module: it imports only :mod:`math`, so ``cpp_backend.py`` and
``diffpair_routing.py`` can import it without pulling in ``pathfinder.py`` or
``grid.py``.
"""

from __future__ import annotations

import math

__all__ = ["search_radius_cells"]


def search_radius_cells(
    half_extent_mm: float,
    clearance_mm: float,
    resolution: float,
) -> int:
    """A* search radius in grid cells around a candidate centre (Epic #5509).

    Returns ``max(1, math.ceil(round(x, 6)))`` with
    ``x = (half_extent_mm + clearance_mm) / resolution``.

    **Why the exact ceiling.**  The search must refuse every cell where
    committed copper would violate clearance, and only those.  DRC passes at
    exactly the clearance (``distance >= clearance``), so the radius is the
    exact ceiling of the real-valued ``x``:

    * rounding to nearest under-blocks (admits a ring inside the clearance
      whenever ``frac(x) < 0.5``), which the rip-up scan used to do before
      Issue #6994;
    * a bare ``ceil`` over-blocks arbitrarily: float noise such as
      ``(0.1 + 0.2) / 0.1 == 3.0000000000000004`` adds a phantom ring exactly
      when that ring is legal (Issue #864).

    ``round(x, 6)`` strips that noise.  The native mirror in ``types.hpp``
    uses a 1e-4-cell snap instead, because its float32 inputs carry ~1e-7
    relative error; ``tests/router/test_search_radius_cells.py`` pins the two
    to the same answer over the supported inputs.

    **Float order is the behaviour contract.**  Callers pass the half extent
    exactly as their expression computes it (``width / 2`` or a precomputed
    ``half_mm``); this function performs ``(half + clearance) / resolution``.

    Args:
        half_extent_mm: Half the copper extent (trace half-width or via
            radius) in mm.
        clearance_mm: Clearance (or pairwise widening) in mm added to the
            half extent.
        resolution: Grid resolution in mm per cell.

    Returns:
        Radius in grid cells (at least 1).
    """
    x = (half_extent_mm + clearance_mm) / resolution
    return max(1, math.ceil(round(x, 6)))

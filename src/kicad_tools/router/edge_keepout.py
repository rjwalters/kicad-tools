"""One source of truth for the routing grid's board-edge keepout (Issue #6043).

``Autorouter._edge_clearance`` is the *copper-to-edge* clearance the board's
rules (or the manufacturer profile) ask for.  ``validate_routes`` and
``drc_nudge`` read it with exactly that meaning, so it is never rewritten
here.

Before #6043 the grid only received a keepout when that clearance was
positive.  With no manufacturer (``None``) or an explicit ``0`` nothing at
all was blocked, so the router could lay copper straight across an interior
``Edge.Cuts`` window, cutout or round hole.  Copper across a routed cut is
never valid output, so those cases now get a *physical floor*: the radius
the grid needs so the full copper shape -- not just its centreline -- stays
off the cut.

``add_edge_keepout(segments, r)`` blocks centreline cells within
``int(r / resolution) + 1`` cells of a Bresenham-rasterised edge.  The
rasterised edge can sit about half a cell diagonal off the true line, so the
floor is::

    r_floor = max_copper_half_extent + resolution * sqrt(2) / 2

where ``max_copper_half_extent`` is the largest ``trace_width / 2`` or
``via_diameter / 2`` over the board ``DesignRules`` and every net class
applied to a net on the board.

The floor is stored on the router as ``_edge_keepout_floor``, separately from
``_edge_clearance``.  Every keepout (re)install site -- the initial load, the
trial reset, the parallel-worker rebuild and both fine-grid retries -- goes
through :func:`install_edge_keepout`, so none of them can drop it.

A positive clearance is unchanged: :func:`edge_keepout_radius` returns it
as is, and the grid is given exactly that value.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "compute_edge_keepout_floor",
    "edge_copper_half_extent",
    "edge_keepout_radius",
    "install_edge_keepout",
    "refresh_edge_keepout_floor",
]


def _positive(value: Any) -> float:
    """``value`` as a finite positive float, else ``0.0``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number <= 0:
        return 0.0
    return number


def edge_copper_half_extent(router: Any) -> float:
    """The widest copper half-extent any net on ``router`` can route with (mm).

    The maximum of ``trace_width / 2`` and ``via_diameter / 2`` over the
    board rules and the net class of every net the router knows about.
    Net classes in the map that no board net uses are ignored: they cannot
    put copper on this board.
    """
    rules = getattr(router, "rules", None)
    widest = max(
        _positive(getattr(rules, "trace_width", 0.0)),
        _positive(getattr(rules, "via_diameter", 0.0)),
    )
    class_map = getattr(router, "net_class_map", None) or {}
    net_names = getattr(router, "net_names", None) or {}
    for name in net_names.values():
        net_class = class_map.get(name) if name else None
        if net_class is None:
            continue
        widest = max(
            widest,
            _positive(getattr(net_class, "trace_width", 0.0)),
            _positive(getattr(net_class, "via_size", 0.0)),
        )
    return widest / 2.0


def compute_edge_keepout_floor(router: Any, resolution: float | None = None) -> float:
    """The physical keepout radius for ``router`` when no clearance is set (mm).

    ``resolution`` defaults to the router's own grid resolution.  A finer
    grid (the fine-grid retries) needs a smaller quantisation margin, so the
    floor computed for the main grid is conservative there.
    """
    if resolution is None:
        grid = getattr(router, "grid", None)
        resolution = getattr(grid, "resolution", None)
        if resolution is None:
            resolution = getattr(getattr(router, "rules", None), "grid_resolution", 0.0)
    return edge_copper_half_extent(router) + _positive(resolution) * math.sqrt(2.0) / 2.0


def edge_keepout_radius(router: Any) -> float | None:
    """The radius every grid keepout install site must use, or ``None``.

    A positive ``_edge_clearance`` is returned unchanged.  Otherwise the
    stored ``_edge_keepout_floor`` is returned (``None`` when the router was
    never given one, e.g. a hand-built ``Autorouter`` with no outline).
    """
    clearance = getattr(router, "_edge_clearance", None)
    if clearance is not None and clearance > 0:
        return float(clearance)
    floor = getattr(router, "_edge_keepout_floor", None)
    if floor is not None and floor > 0:
        return float(floor)
    return None


def install_edge_keepout(router: Any, grid: Any) -> int:
    """Block ``router``'s board-edge keepout on ``grid``; return cells blocked.

    A no-op (returns 0) when the router has no outline segments or no
    keepout radius.
    """
    segments = getattr(router, "_edge_segments", None)
    if not segments:
        return 0
    radius = edge_keepout_radius(router)
    if radius is None:
        return 0
    return int(grid.add_edge_keepout(segments, radius))


def refresh_edge_keepout_floor(router: Any) -> int:
    """Grow the stored floor if net classes widened since load; return cells blocked.

    Callers (the CLI's net-class sidecar, impedance sizing, diff-pair
    wiring) can replace entries in ``router.net_class_map`` after
    ``load_pcb_for_routing`` installed the floor.  This recomputes it from
    the current map and, only when it grew, stores the larger value and
    stamps the wider keepout onto ``router.grid`` (additive, so nothing
    already blocked is reopened).  A no-op when a positive clearance is set
    or the loader never assigned a floor.
    """
    floor = getattr(router, "_edge_keepout_floor", None)
    if floor is None or not getattr(router, "_edge_segments", None):
        return 0
    clearance = getattr(router, "_edge_clearance", None)
    if clearance is not None and clearance > 0:
        return 0
    current = compute_edge_keepout_floor(router)
    if current <= floor + 1e-9:
        return 0
    router._edge_keepout_floor = current
    return install_edge_keepout(router, router.grid)

"""Grid-engine enforcement of board-file keepout rule areas (Issue #6008).

KiCad rule areas -- ``(zone ... (keepout (tracks not_allowed) (vias
not_allowed) ...))`` -- used to constrain only the lattice engine (#4605).
The default grid engine rasterised nothing, so grid-routed copper could
cross a declared keepout -- and headless ``kicad-cli pcb drc`` 10.0.1 did not
catch it either, since it does not enforce rule areas on its own (kct now
writes explicit per-area ``intersectsArea`` rules into the ``.kicad_dru``,
Issue #6039).

This module projects the SAME resolved areas the lattice engine consumes
(:meth:`Autorouter._lattice_keepout_projection` -> ``KeepoutArea``: the shared
#5575 parse plus the ``spatial_keepouts`` per-class filter) onto a
:class:`~kicad_tools.router.grid.RoutingGrid`, so the two engines read one
definition and cannot drift apart.

How each area lands on the grid:

* **Track-blocking, applies to every net** (KiCad's default, by far the
  common case): its cells are stamped as static net-0 obstacles on the area's
  layers via :meth:`RoutingGrid.mark_static_keepout_cells`.  Every consumer of
  the occupancy planes -- the Python and C++ A*, the diff-pair router, the
  validators and the unrouted-cause classifier (#5944) -- then sees them with
  no further plumbing.  A blocked cell is also a hard obstacle for a via whose
  disc reaches it, which is conservative for a tracks-only area (KiCad would
  allow the via) but never produces a violation.
* **Track-blocking with a ``spatial_keepouts`` filter**: a static cell cannot
  say "blocked for these nets only", so the area is registered as a net-aware
  mask the trace predicates consult (``RoutingGrid.rule_area_trace_blocked``
  and the C++ ``Grid3D::rule_area_trace_blocked``).
* **Via-blocking** (filtered or not): registered as a net-aware mask the via
  predicates consult (``rule_area_via_blocked``).  A via-only area therefore
  blocks vias but leaves tracks free, matching KiCad.

A cell belongs to an area when its grid node lies inside the polygon or
within half a cell of its boundary.  The predicates measure blocked cells
inside the same Euclidean disc they use for any other obstacle, so copper
keeps the usual trace/via clearance from the area edge -- slightly stricter
than KiCad's zero-clearance keepout test, never looser.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .grid import RoutingGrid

__all__ = [
    "GridRuleArea",
    "install_rule_area_keepouts",
    "rasterise_polygon",
]


@dataclass
class GridRuleArea:
    """One keepout rule area projected onto a routing grid (Issue #6008).

    Attributes:
        name: The zone's name ("" when unnamed).
        polygon: Sheet-absolute polygon (mm), for reporting.
        layers: Grid layer indices the area covers.
        blocks_tracks: The area forbids tracks.
        blocks_vias: The area forbids vias.
        only: Net ids the area applies to (``None`` = every net).
        exempt: Net ids the area never applies to.
        gx0, gy0: Grid cell of ``mask[0, 0]``.
        mask: ``(h, w)`` boolean raster of the area's cells.
        static_tracks: ``True`` when the track rule was stamped as static
            net-0 cells (so the net-aware trace check skips it).
    """

    name: str
    polygon: tuple[tuple[float, float], ...]
    layers: frozenset[int]
    blocks_tracks: bool
    blocks_vias: bool
    only: frozenset[int] | None
    exempt: frozenset[int]
    gx0: int
    gy0: int
    mask: np.ndarray
    static_tracks: bool = False
    _cell_count: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self._cell_count = int(self.mask.sum())

    @property
    def filtered(self) -> bool:
        """``True`` when a ``spatial_keepouts`` filter narrows the nets."""
        return self.only is not None or bool(self.exempt)

    @property
    def cell_count(self) -> int:
        return self._cell_count

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.polygon]
        ys = [p[1] for p in self.polygon]
        return (min(xs), min(ys), max(xs), max(ys))

    def applies_to(self, net: int) -> bool:
        """Same rule as ``lattice.obstacles.KeepoutArea.applies_to``."""
        if self.only is not None and net not in self.only:
            return False
        return net not in self.exempt

    def contains(self, gx: int, gy: int) -> bool:
        """``True`` when grid cell ``(gx, gy)`` is one of the area's cells."""
        h, w = self.mask.shape
        x, y = gx - self.gx0, gy - self.gy0
        return 0 <= x < w and 0 <= y < h and bool(self.mask[y, x])

    def hits_disc(self, gx: int, gy: int, radius: int) -> bool:
        """``True`` when an area cell lies in the Euclidean disc of ``radius``
        cells around ``(gx, gy)`` -- the same disc the trace/via kernels use."""
        h, w = self.mask.shape
        r = max(0, int(radius))
        x0 = max(gx - r, self.gx0)
        x1 = min(gx + r, self.gx0 + w - 1)
        y0 = max(gy - r, self.gy0)
        y1 = min(gy + r, self.gy0 + h - 1)
        if x0 > x1 or y0 > y1:
            return False
        window = self.mask[y0 - self.gy0 : y1 - self.gy0 + 1, x0 - self.gx0 : x1 - self.gx0 + 1]
        if not window.any():
            return False
        dy = np.arange(y0, y1 + 1)[:, None] - gy
        dx = np.arange(x0, x1 + 1)[None, :] - gx
        return bool((window & (dx * dx + dy * dy <= r * r)).any())

    def cells(self) -> Iterable[tuple[int, int]]:
        """Every ``(gx, gy)`` cell of the area."""
        ys, xs = np.nonzero(self.mask)
        for y, x in zip(ys.tolist(), xs.tolist(), strict=True):
            yield (self.gx0 + x, self.gy0 + y)


def _point_in_polygon(
    px: np.ndarray, py: np.ndarray, polygon: Sequence[tuple[float, float]]
) -> np.ndarray:
    """Vectorised even-odd point-in-polygon test."""
    inside = np.zeros(px.shape, dtype=bool)
    n = len(polygon)
    for i in range(n):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % n]
        if ay == by:
            continue
        crosses = (ay > py) != (by > py)
        x_at = ax + (py - ay) * (bx - ax) / (by - ay)
        inside ^= crosses & (px < x_at)
    return inside


def _distance_to_boundary(
    px: np.ndarray, py: np.ndarray, polygon: Sequence[tuple[float, float]]
) -> np.ndarray:
    """Vectorised distance from each point to the polygon's boundary."""
    best = np.full(px.shape, np.inf)
    n = len(polygon)
    for i in range(n):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % n]
        vx, vy = bx - ax, by - ay
        seg2 = vx * vx + vy * vy
        if seg2 == 0.0:
            d = np.hypot(px - ax, py - ay)
        else:
            t = np.clip(((px - ax) * vx + (py - ay) * vy) / seg2, 0.0, 1.0)
            d = np.hypot(px - (ax + t * vx), py - (ay + t * vy))
        best = np.minimum(best, d)
    return best


def rasterise_polygon(
    grid: RoutingGrid, polygon: Sequence[tuple[float, float]]
) -> tuple[int, int, np.ndarray]:
    """Cells of ``grid`` covered by ``polygon`` as ``(gx0, gy0, mask)``.

    A cell is covered when its node lies inside the polygon or within half a
    cell of its boundary, so a sliver narrower than one cell still blocks.
    Cells outside the grid are dropped; an area entirely off-grid yields an
    empty ``(0, 0)`` mask.
    """
    res = float(grid.resolution)
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    gx0 = max(0, int(math.floor((min(xs) - grid.origin_x) / res)) - 1)
    gy0 = max(0, int(math.floor((min(ys) - grid.origin_y) / res)) - 1)
    gx1 = min(grid.cols - 1, int(math.ceil((max(xs) - grid.origin_x) / res)) + 1)
    gy1 = min(grid.rows - 1, int(math.ceil((max(ys) - grid.origin_y) / res)) + 1)
    if gx0 > gx1 or gy0 > gy1:
        return (0, 0, np.zeros((0, 0), dtype=bool))
    wx = grid.origin_x + np.arange(gx0, gx1 + 1, dtype=float) * res
    wy = grid.origin_y + np.arange(gy0, gy1 + 1, dtype=float) * res
    px, py = np.meshgrid(wx, wy)
    mask = _point_in_polygon(px, py, polygon)
    mask |= _distance_to_boundary(px, py, polygon) <= res / 2.0 + 1e-9
    return (gx0, gy0, mask)


def install_rule_area_keepouts(grid: RoutingGrid, areas: Iterable[Any]) -> list[GridRuleArea]:
    """Project resolved keepout rule areas onto ``grid`` (Issue #6008).

    Args:
        grid: The routing grid (its C++ mirror, when attached, is updated
            too).
        areas: ``lattice.obstacles.KeepoutArea``-shaped objects (``polygon``,
            ``layers``, ``blocks_tracks``, ``blocks_vias``, ``only``,
            ``exempt``, ``name``) -- in production the output of
            ``Autorouter._lattice_keepout_projection``.

    Returns:
        The areas registered on the grid (also stored on
        ``grid._rule_area_keepouts``).  Idempotent per grid: a second call is
        a no-op returning the existing registration.
    """
    existing = getattr(grid, "_rule_area_keepouts", None)
    if existing:
        return list(existing)
    num_layers = int(grid.num_layers)
    registered: list[GridRuleArea] = []
    for area in areas:
        polygon = tuple((float(x), float(y)) for x, y in area.polygon)
        if len(polygon) < 3 or not (area.blocks_tracks or area.blocks_vias):
            continue
        layers = frozenset(int(i) for i in area.layers if 0 <= int(i) < num_layers)
        if not layers:
            continue
        gx0, gy0, mask = rasterise_polygon(grid, polygon)
        if not mask.any():
            continue
        entry = GridRuleArea(
            name=str(getattr(area, "name", "") or ""),
            polygon=polygon,
            layers=layers,
            blocks_tracks=bool(area.blocks_tracks),
            blocks_vias=bool(area.blocks_vias),
            only=None if area.only is None else frozenset(int(n) for n in area.only),
            exempt=frozenset(int(n) for n in (area.exempt or ())),
            gx0=gx0,
            gy0=gy0,
            mask=mask,
        )
        if entry.blocks_tracks and not entry.filtered:
            cells = list(entry.cells())
            grid.mark_static_keepout_cells(
                (layer, gx, gy) for layer in sorted(layers) for gx, gy in cells
            )
            _claim_owned_halo_cells(grid, sorted(layers), cells)
            # #5662: a rule area has no geometry registry the exact clearance
            # kernel could re-measure -- record its cells as registry-less so
            # no halo refinement can open them and the unrouted-cause
            # attribution names a keepout.
            grid._mark_raster_only_cells(sorted(layers), set(cells))
            entry.static_tracks = True
        registered.append(entry)
    grid._rule_area_keepouts = registered
    cpp_grid = getattr(grid, "_cpp_grid", None)
    if cpp_grid is not None and registered:
        mirror_rule_areas_to_cpp(grid, cpp_grid)
    return registered


def _claim_owned_halo_cells(
    grid: RoutingGrid, layers: list[int], cells: list[tuple[int, int]]
) -> None:
    """Hand area cells a net already owns (pad halos) to net 0.

    ``mark_static_keepout_cells`` leaves already-blocked cells alone, so a pad
    clearance halo reaching into the area would stay passable for the pad's
    own net -- a route of that net could then run inside the keepout.  Pad
    METAL keeps its owner (the pad must stay reachable); every other owned
    cell inside the area becomes a net-0 static obstacle.
    """
    if not cells:
        return
    xs = np.fromiter((c[0] for c in cells), dtype=np.intp, count=len(cells))
    ys = np.fromiter((c[1] for c in cells), dtype=np.intp, count=len(cells))
    cpp_impl = getattr(getattr(grid, "_cpp_grid", None), "_impl", None)
    blocked = grid._blocked
    net = grid._net
    pad_blocked = grid._pad_blocked
    for layer in layers:
        hit = (
            np.asarray(blocked[layer, ys, xs], dtype=bool)
            & (np.asarray(net[layer, ys, xs]) != 0)
            & ~np.asarray(pad_blocked[layer, ys, xs], dtype=bool)
        )
        if not hit.any():
            continue
        hx, hy = xs[hit], ys[hit]
        net[layer, hy, hx] = 0
        grid._original_net[layer, hy, hx] = 0
        if grid._static_blocked is not None:
            grid._static_blocked[layer, hy, hx] = True
        if cpp_impl is not None:
            obstacle = grid._is_obstacle
            for x, y in zip(hx.tolist(), hy.tolist(), strict=True):
                cpp_impl.mark_blocked(
                    int(x), int(y), int(layer), 0, bool(obstacle[layer, y, x]), False
                )
    grid.bump_occupancy_generation()


def mirror_rule_areas_to_cpp(grid: RoutingGrid, cpp_grid: Any) -> None:
    """Install ``grid``'s net-aware rule areas on a C++ grid mirror.

    Only the parts static cells cannot express travel here: filtered track
    rules and every via rule.  Raises ``RuntimeError`` on a native module
    that predates the binding, rather than silently routing through the
    areas.
    """
    impl = getattr(cpp_grid, "_impl", None)
    if impl is None:
        return
    areas = getattr(grid, "_rule_area_keepouts", None) or []
    net_aware = [a for a in areas if a.blocks_vias or (a.blocks_tracks and not a.static_tracks)]
    if not net_aware:
        return
    if not hasattr(impl, "add_rule_area_keepout"):
        raise RuntimeError(
            "Rebuild native router (kct build-native): keepout rule-area support required"
        )
    impl.clear_rule_area_keepouts()
    for area in net_aware:
        h, w = area.mask.shape
        flat = np.flatnonzero(area.mask).astype(np.int64).tolist()
        impl.add_rule_area_keepout(
            bool(area.blocks_tracks and not area.static_tracks),
            bool(area.blocks_vias),
            sorted(area.layers),
            int(area.gx0),
            int(area.gy0),
            int(w),
            int(h),
            flat,
            area.only is not None,
            sorted(area.only or ()),
            sorted(area.exempt),
        )

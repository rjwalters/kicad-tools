"""Physical geometry and marking provenance for dynamic route-halo refinement."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

import numpy as np
from shapely.geometry import LineString, Point  # type: ignore[import-untyped]
from shapely.ops import nearest_points  # type: ignore[import-untyped]

from kicad_tools.acceleration import to_numpy

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .primitives import Segment, Via


#: Reject nearby copper by an axis-aligned bounding-box gap before paying for
#: the exact GEOS distance (issue #5240). Kept as a module-level switch so the
#: exhaustive pre-#5240 scan stays reachable as a test oracle; flipping it off
#: must never change a verdict, only the amount of work done to reach it.
_PRUNE_BY_BOUNDS = True


class RouteHaloGeometry:
    """A conservative mark is refinable only when its actual copper is known.

    Marks are counted independently of the grid's single owner field. Missing
    geometry disables refinement in its footprint; clearing/ripping up geometry
    cannot leave a stale authorization. Two-mm bins limit exact comparisons to
    nearby copper.
    """

    def __init__(self, grid: RoutingGrid):
        self.grid = grid
        self.marks: Counter[tuple] = Counter()
        self._generation = -1
        self._complete = False
        self._cells = np.zeros((0, 0, 0), dtype=np.int32)
        self._objects: list[tuple] = []
        # Axis-aligned (minx, miny, maxx, maxy) of each object's centreline,
        # parallel to ``_objects`` -- the prune's lower-bound input.
        self._bounds: list[tuple[float, float, float, float]] = []
        self._bins: dict[tuple[int, int], set[int]] = defaultdict(set)

    def segment_key(self, seg: Segment) -> tuple:
        a = self.grid.world_to_grid(seg.x1, seg.y1)
        b = self.grid.world_to_grid(seg.x2, seg.y2)
        a, b = sorted((a, b))
        return (0, seg.net, self.grid.layer_to_index(seg.layer.value), *a, *b, seg.width)

    def via_key(self, via: Via) -> tuple:
        x, y = self.grid.world_to_grid(via.x, via.y)
        return (1, via.net, -1, x, y, x, y, via.diameter, via.drill)

    def record(self, key: tuple, radius: int, add: bool) -> None:
        mark = (key, radius)
        if add:
            self.marks[mark] += 1
        elif self.marks[mark] > 1:
            self.marks[mark] -= 1
        else:
            self.marks.pop(mark, None)
        self._generation = -1

    @staticmethod
    def _bins_in(bounds):
        x1, y1, x2, y2 = bounds
        for y in range(math.floor(y1 / 2), math.floor(y2 / 2) + 1):
            for x in range(math.floor(x1 / 2), math.floor(x2 / 2) + 1):
                yield x, y

    def _refresh(self) -> None:
        if self._generation == self.grid.occupancy_generation:
            return
        self._generation = self.grid.occupancy_generation
        self._objects = []
        self._bounds = []
        self._bins.clear()
        registered = set()
        for route in self.grid.routes:
            for seg in route.segments:
                registered.add(self.segment_key(seg))
                self._objects.append(
                    (seg, LineString(((seg.x1, seg.y1), (seg.x2, seg.y2))), seg.width / 2)
                )
            for via in route.vias:
                registered.add(self.via_key(via))
                self._objects.append((via, Point(via.x, via.y), max(via.diameter, via.drill) / 2))
        self._complete = bool(self.marks) and all(key in registered for key, _ in self.marks)
        self._cells = np.zeros(
            (self.grid.num_layers, self.grid.rows, self.grid.cols), dtype=np.int32
        )
        for index, (_, shape, half) in enumerate(self._objects):
            x1, y1, x2, y2 = shape.bounds
            self._bounds.append((x1, y1, x2, y2))
            for bucket in self._bins_in((x1 - half, y1 - half, x2 + half, y2 + half)):
                self._bins[bucket].add(index)
        net_plane = to_numpy(self.grid._net)
        for key, radius in self.marks:
            known = key in registered
            kind, net, layer, x1, y1, x2, y2, *_ = key
            x, y = x1, y1
            dx, dy = abs(x2 - x1), abs(y2 - y1)
            sx, sy = (1 if x1 < x2 else -1), (1 if y1 < y2 else -1)
            err = dx - dy
            while True:
                layers = range(self.grid.num_layers) if kind else (layer,)
                for plane in layers:
                    ax, bx = max(0, x - radius), min(self.grid.cols, x + radius + 1)
                    ay, by = max(0, y - radius), min(self.grid.rows, y + radius + 1)
                    if ax >= bx or ay >= by:
                        continue
                    region = self._cells[plane, ay:by, ax:bx]
                    if not known:
                        # Do not let a later known mark hide an unknown overlap.
                        region[:] = -1
                    else:
                        region[(region != -1) & (net_plane[plane, ay:by, ax:bx] == net)] = net
                if (x, y) == (x2, y2):
                    break
                twice = 2 * err
                if twice > -dy:
                    err -= dy
                    x += sx
                if twice < dx:
                    err += dx
                    y += sy

    @property
    def complete(self) -> bool:
        self._refresh()
        return self._complete

    def cell_known(self, x: int, y: int, layer: int) -> bool:
        grid = self.grid
        if not (0 <= x < grid.cols and 0 <= y < grid.rows and 0 <= layer < grid.num_layers):
            return False
        cell = grid.cell_at(layer, y, x)
        if not cell.blocked or cell.net <= 0 or cell.is_obstacle or cell.pad_blocked:
            return False
        if grid._static_blocked is not None and grid._static_blocked[layer, y, x]:
            return False
        if (layer, y, x) in grid._reserved_for_nets:
            return False
        self._refresh()
        return bool(self._cells[layer, y, x] == cell.net)

    def clear(
        self, candidate, router, *, partner_net=None, partner_clearance=None, require_geometry=True
    ) -> bool:
        """Check known copper and drills using the effective routing rules.

        This physical check does not authorize bypassing unknown occupancy.
        Callers must check cell_known for every blocked cell they refine.
        With require_geometry=False, absent geometry is clear; use that mode
        only to reject known conflicts, never to authorize raster relaxation.
        """
        from .pairwise_clearance import _attach_zone_exempts
        from .primitives import Segment

        self._refresh()
        if require_geometry and (not self.marks or not self._objects):
            return False
        is_trace = isinstance(candidate, Segment)
        if is_trace:
            cax, cay = candidate.x1, candidate.y1
            cbx, cby = candidate.x2, candidate.y2
            half = candidate.width / 2
            layer = candidate.layer
        else:
            cax = cbx = candidate.x
            cay = cby = candidate.y
            half, layer = candidate.diameter / 2, None
        # Built on first use: a call whose neighbours are all pruned below
        # never needs a shapely geometry at all.
        shape = None
        names = router._route_halo_names
        own_name = names.get(candidate.net, "")
        nc = router._halo_net_class(candidate.net)
        scalar = (
            (nc.clearance if nc else router.rules.trace_clearance)
            if is_trace
            else router.rules.via_clearance
        )
        table = router.rules.pairwise_clearance
        widen = table.max_required_clearance() if table is not None else 0
        radius = max(half, getattr(candidate, "drill", 0) / 2)
        margin = radius + max(
            scalar,
            router.rules.via_clearance,
            partner_clearance or 0,
            widen,
            router.rules.min_hole_to_hole,
            router.rules.min_drill_clearance,
        )
        x1, x2 = (cax, cbx) if cax <= cbx else (cbx, cax)
        y1, y2 = (cay, cby) if cay <= cby else (cby, cay)
        # Issue #5617: the through-via layer-span test below rebuilt
        # ``sorted(self.grid.layer_to_index(l.value) for l in candidate.layers)``
        # once per surviving halo OBJECT, even though ``candidate.layers`` and
        # ``self.grid`` are both loop-invariant for the whole call.  A py-spy
        # profile of the pure-Python A* fallback (board 06, the ``USB3_RX1-``
        # resume-exhaustion net) attributed ~11% of the fallback's total wall
        # time to that one generator+``sorted`` rebuild and its per-layer enum
        # ``.value`` lookups.  Cache it per call, LAZILY, so a call that never
        # reaches the branch still pays nothing -- same values, same
        # comparison, same verdict, byte-identical routing output.
        candidate_layer_span: tuple[int, int] | None = None
        layer_to_index = self.grid.layer_to_index
        indices: set[int] = set()
        for bucket in self._bins_in((x1 - margin, y1 - margin, x2 + margin, y2 + margin)):
            indices.update(self._bins.get(bucket, ()))
        for index in sorted(indices):
            other, other_shape, other_radius = self._objects[index]
            other_trace = isinstance(other, Segment)
            same_net = other.net == candidate.net
            if is_trace and same_net:
                continue
            if other_trace and (same_net or (is_trace and other.layer != layer)):
                continue
            if not is_trace and other_trace and other.layer not in candidate.layers:
                # Through vias list outer endpoints, but span every inner layer.
                if candidate_layer_span is None:
                    indices_span = sorted(layer_to_index(l.value) for l in candidate.layers)
                    candidate_layer_span = (indices_span[0], indices_span[-1])
                if (
                    not candidate_layer_span[0]
                    <= layer_to_index(other.layer.value)
                    <= candidate_layer_span[1]
                ):
                    continue
            if _PRUNE_BY_BOUNDS:
                # Conservative axis-aligned reject. Both ``return False``
                # branches below need ``distance`` under some threshold, and
                # every one of those thresholds is bounded by
                # ``margin + other_radius``:
                #   * ``half``/``candidate.drill / 2`` <= ``radius`` and every
                #     ``required``/``floor`` term (scalar, partner, pairwise
                #     ``widen``, via, hole-to-hole, drill) is inside the
                #     ``max(...)`` ``margin`` was built from, so
                #     ``half + required <= margin``;
                #   * ``other.width / 2`` / ``other.diameter / 2`` /
                #     ``other.drill / 2`` are all <= ``other_radius``.
                # The gap between two bounding boxes is a lower bound on the
                # distance between the geometries inside them, so a box gap at
                # or above that limit proves neither branch can fire -- without
                # measuring the exact distance.
                obx1, oby1, obx2, oby2 = self._bounds[index]
                gapx = obx1 - x2 if obx1 > x2 else (x1 - obx2 if x1 > obx2 else 0.0)
                gapy = oby1 - y2 if oby1 > y2 else (y1 - oby2 if y1 > oby2 else 0.0)
                limit = margin + other_radius
                if gapx * gapx + gapy * gapy >= limit * limit:
                    continue
            if shape is None:
                shape = LineString(((cax, cay), (cbx, cby))) if is_trace else Point(cax, cay)
            distance = shape.distance(other_shape)
            if not is_trace and not other_trace:
                if (
                    same_net
                    and abs(candidate.x - other.x) < 1e-6
                    and abs(candidate.y - other.y) < 1e-6
                ):
                    continue
                floor = (
                    router.rules.min_drill_clearance if same_net else router.rules.min_hole_to_hole
                )
                if distance - (candidate.drill + other.drill) / 2 < floor - 1e-4:
                    return False
                if same_net:
                    continue
            required = scalar
            if is_trace and other.net == partner_net and partner_clearance is not None:
                required = partner_clearance
            elif table is not None:
                pair = table.required_clearance(own_name, names.get(other.net, ""))
                if pair > required:
                    a, b = nearest_points(shape, other_shape)
                    shared_layer = layer if is_trace else (other.layer if other_trace else None)
                    if not _attach_zone_exempts(
                        router._attach_zones,
                        (a.x + b.x) / 2,
                        (a.y + b.y) / 2,
                        own_name,
                        names.get(other.net, ""),
                        shared_layer,
                    ):
                        required = pair
            if is_trace and not other_trace:
                required = max(required, router.rules.via_clearance)
            other_half = other.width / 2 if other_trace else other.diameter / 2
            if distance - half - other_half < required - 1e-4:
                return False
        return True

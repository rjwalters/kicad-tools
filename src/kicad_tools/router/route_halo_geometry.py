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
        #: Monotone token bumped on every actual rebuild in :meth:`_refresh`
        #: (issue #5617).  See :attr:`state_version`.
        self._version = 0
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
        self._version += 1
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

    @property
    def state_version(self) -> int:
        """Monotone token identifying the geometry :meth:`clear` will read.

        Issue #5617.  :meth:`clear`'s verdict is a pure function of its
        arguments, the router's (per-route constant) rule configuration, and
        the snapshot ``_refresh`` rebuilds -- ``_objects`` / ``_bounds`` /
        ``_bins`` / ``_cells`` / ``marks``.  That snapshot is rebuilt exactly
        when ``_refresh`` observes a new ``grid.occupancy_generation`` or the
        ``-1`` sentinel :meth:`record` writes, so a caller that memoises a
        ``clear`` verdict can key on this token and be certain a stale entry
        is never served: any ``record`` or grid-occupancy change between two
        reads forces a rebuild, and every rebuild bumps the counter.

        Reading it refreshes first, so the token a caller sees is the one the
        immediately-following ``clear`` call will itself use.
        """
        self._refresh()
        return self._version

    def cell_known(self, x: int, y: int, layer: int) -> bool:
        """Is ``(x, y, layer)``'s conservative mark backed by known copper?

        Issue #5617.  Reads the four occupancy planes DIRECTLY rather than
        through ``grid.cell_at(...)`` + four ``_CellView`` properties.  The
        predicate, its operand order and its short-circuit points are
        unchanged -- ``_CellView.blocked`` / ``.net`` / ``.is_obstacle`` /
        ``.pad_blocked`` are each defined as exactly the indexed read spelled
        out below (``grid.py``) -- but the per-cell ``_CellView`` allocation
        and the four bound-property calls are gone.

        This is a hot leaf of the pure-Python A* fallback: a py-spy profile of
        the Diff-Pair regression job's re-route step (board 06, ``--seed 42``)
        attributed **15.9 s of phase 4's 524.2 s (3.0 %)** to it, reached from
        two call sites -- ``Router._trace_halo_clear``'s
        ``all(cell_known(...) for ...)`` guard (8.4 s) and
        ``Router._is_via_blocked``'s known-cell filter (7.5 s) -- with 8.2 s of
        that inside ``cell_at`` + the four property getters.
        """
        grid = self.grid
        if not (0 <= x < grid.cols and 0 <= y < grid.rows and 0 <= layer < grid.num_layers):
            return False
        if not grid._blocked[layer, y, x]:
            return False
        net = int(grid._net[layer, y, x])
        if net <= 0 or grid._is_obstacle[layer, y, x] or grid._pad_blocked[layer, y, x]:
            return False
        static_blocked = grid._static_blocked
        if static_blocked is not None and static_blocked[layer, y, x]:
            return False
        if (layer, y, x) in grid._reserved_for_nets:
            return False
        self._refresh()
        return bool(self._cells[layer, y, x] == net)

    def window_known(self, x0: int, y0: int, x1: int, y1: int) -> np.ndarray | None:
        """:meth:`cell_known` for the whole grid column ``[x0, x1) x [y0, y1)``.

        Issue #5720.  Returns a ``(num_layers, y1 - y0, x1 - x0)`` boolean
        array whose ``[layer, y - y0, x - x0]`` entry equals
        ``cell_known(x, y, layer)``, or ``None`` when the window is not wholly
        inside the grid (``cell_known`` answers ``False`` for every cell of a
        window that is even partly out of bounds, and the callers that reach
        for this method already reject on the out-of-bounds case, so ``None``
        never has to be interpreted as a verdict).

        Same predicate, same operand set, evaluated with one array op per
        occupancy plane instead of one Python call plus five scalar indexed
        reads per cell.  The per-cell short-circuit is gone by construction --
        every plane is read for every cell in the window -- which is
        unobservable: the reads have no side effects, and ``_refresh`` bumps
        :attr:`state_version` at most once per ``grid.occupancy_generation``
        however many times it is called (so an extra call cannot perturb a
        caller's memo token).  It is skipped anyway when nothing survives the
        hard-blockage planes.

        ``CoupledPathfinder._is_via_blocked`` sweeps a
        ``(2 * via_extra_cells + 1)^2`` envelope on every layer and then walked
        its blocked cells a second time through ``cell_known``; on the #5410
        fixture that second walk cost 37.3 us of the predicate's 131.2
        us/call.
        """
        grid = self.grid
        num_layers = grid.num_layers
        if num_layers <= 0 or x1 <= x0 or y1 <= y0:
            return None
        if x0 < 0 or y0 < 0 or x1 > grid.cols or y1 > grid.rows:
            return None
        window = (slice(0, num_layers), slice(y0, y1), slice(x0, x1))
        nets = to_numpy(grid._net[window])
        # ``cell_known``'s conjunction, plane by plane: conservatively blocked,
        # owned by a real signal net, and not hard-blocked by pad metal, a true
        # obstacle or a static halo.
        known = to_numpy(grid._blocked[window]) & (nets > 0)
        known &= ~to_numpy(grid._is_obstacle[window])
        known &= ~to_numpy(grid._pad_blocked[window])
        static_blocked = grid._static_blocked
        if static_blocked is not None:
            known &= ~static_blocked[window]
        reserved = grid._reserved_for_nets
        if reserved:
            # Bounded by the window volume, and only for the cells that are
            # still candidates -- the same dict lookups ``cell_known`` would
            # have done, no more.
            for zz, yy, xx in zip(*known.nonzero(), strict=True):
                if (int(zz), y0 + int(yy), x0 + int(xx)) in reserved:
                    known[zz, yy, xx] = False
        if not known.any():
            return known
        self._refresh()
        known &= to_numpy(self._cells[window]) == nets
        return known

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


class RouteHaloRefiner:
    """Adapter that lets a non-:class:`~.pathfinder.Router` search refine halos.

    :meth:`RouteHaloGeometry.clear` reads a small, duck-typed surface off its
    ``router`` argument -- ``rules``, ``_route_halo_names``,
    ``_halo_net_class`` and ``_attach_zones``.  The per-net A* satisfies that
    surface directly (issue #5410 / PR #5425), but the coupled differential-
    pair A* in :mod:`.diffpair_routing` is a separate class whose blocked
    predicates still reject every foreign-net cell of a *dynamic route halo*
    without ever measuring the copper underneath it.  This adapter supplies
    the surface so that search can apply the identical refinement.

    **Fail-closed by construction.**  Every entry point answers "not clear"
    unless a net-name map has been installed (:meth:`set_net_name_to_id`) and
    the halo reports the cell's owner as verified copper.  A caller that never
    wires the map keeps its pre-existing, conservative raster verdict, and a
    pairwise (HV-isolation) table is never consulted with unknown net names --
    the direction that would *under*-block.
    """

    def __init__(self, grid, rules, net_class_map=None):
        self.grid = grid
        self.rules = rules
        self.net_class_map = net_class_map if net_class_map is not None else {}
        self._net_name_to_id: dict[str, int] = {}
        self._route_halo_names: dict[int, str] = {}
        self._attach_zones: tuple = ()
        #: Issue #5696: ``(gx, gy, net) -> bool`` memo for the geometric half of
        #: :meth:`via_clear`, dropped wholesale whenever :meth:`_memo_token`
        #: changes.  Unlike ``Router._via_halo_cache`` this adapter has no
        #: per-route reset hook, so its token carries the rule configuration
        #: itself rather than relying on a caller to drop the memo.
        self._via_memo: dict[tuple[int, int, int], bool] = {}
        self._via_memo_token: tuple | None = None

    # -- duck-typed ``router`` surface consumed by ``RouteHaloGeometry`` ----

    def _get_net_class(self, net_name: str):
        return self.net_class_map.get(net_name)

    def _halo_net_class(self, net: int):
        return self._get_net_class(self._route_halo_names.get(net, ""))

    # -- configuration ------------------------------------------------------

    def set_net_name_to_id(self, mapping: dict[str, int]) -> None:
        """Install the net-name -> net-id map that arms the refinement."""
        self._net_name_to_id = dict(mapping)
        self._route_halo_names = {net: name for name, net in mapping.items()}

    def set_attach_zones(self, zones) -> None:
        self._attach_zones = tuple(zones)

    @property
    def armed(self) -> bool:
        """True once a net-name map is installed (see the fail-closed note)."""
        return bool(self._route_halo_names)

    # -- refinement ---------------------------------------------------------

    @property
    def _halo(self):
        return getattr(self.grid, "_route_halo", None)

    def cells_known(self, cells, layer: int) -> bool:
        """True when every cell in *cells* is verified dynamic route copper."""
        halo = self._halo
        if halo is None or not self.armed:
            return False
        return all(halo.cell_known(x, y, layer) for x, y in cells)

    def cells_known_mask(self, mask, x0: int, y0: int) -> bool:
        """:meth:`cells_known` for a per-layer mask stack (issue #5720).

        *mask* is a ``(num_layers, height, width)`` boolean array selecting the
        cells to verify inside the grid column whose top-left cell is
        ``(x0, y0)``.  Equivalent to::

            all(
                self.cells_known(
                    [(x0 + x, y0 + y) for y, x in zip(*mask[layer].nonzero())], layer
                )
                for layer in range(grid.num_layers)
            )

        with one array op per occupancy plane for the whole column instead of a
        Python call per selected cell.

        Fail-closed exactly like :meth:`cells_known`: a dormant refiner (no
        halo, or no net-name map installed) answers ``False`` regardless of
        *mask* -- including an all-``False`` one, which is the one case where
        this differs from ``all(...)`` over an empty cell list.  Callers guard
        on ``mask.any()`` before asking.
        """
        halo = self._halo
        if halo is None or not self.armed:
            return False
        known = halo.window_known(x0, y0, x0 + mask.shape[2], y0 + mask.shape[1])
        if known is None:
            # Out-of-bounds window: ``cell_known`` answers False for every cell
            # in it, so only an empty selection is (vacuously) all-known.
            return not bool(mask.any())
        return not bool((mask & ~known).any())

    def _layer_object(self, layer: int):
        from .primitives import Layer

        enum_value = self.grid._index_to_layer.get(layer)
        if enum_value is None:
            return None
        try:
            return Layer(enum_value)
        except ValueError:
            return None

    def _resolve_partner_net_id(self, net_name: str) -> int | None:
        net_class = self._get_net_class(net_name)
        if net_class is None or net_class.diffpair_partner is None:
            return None
        return self._net_name_to_id.get(net_class.diffpair_partner)

    def trace_clear(self, cells, layer: int, gx: int, gy: int, net: int, from_cell=None) -> bool:
        """Physical clearance of the swept trace step ending at ``(gx, gy)``."""
        halo = self._halo
        if halo is None or not self.armed:
            return False
        from .primitives import Segment

        if not self.cells_known(cells, layer):
            return False
        copper_layer = self._layer_object(layer)
        if copper_layer is None:
            return False
        name = self._route_halo_names.get(net, "")
        nc = self._halo_net_class(net)
        x1, y1 = self.grid.grid_to_world(*(from_cell or (gx, gy)))
        x2, y2 = self.grid.grid_to_world(gx, gy)
        segment = Segment(
            x1,
            y1,
            x2,
            y2,
            nc.trace_width if nc else self.rules.trace_width,
            copper_layer,
            net,
            name,
        )
        partner = self._resolve_partner_net_id(name)
        gap = nc.effective_intra_pair_clearance() if nc and partner is not None else None
        return bool(halo.clear(segment, self, partner_net=partner, partner_clearance=gap))

    #: Entry cap for :attr:`_via_memo` (issue #5696).  Purely a memory
    #: backstop: dropping memo entries can only cost work, never change a
    #: verdict, because every miss recomputes the same pure function.
    _VIA_MEMO_MAX = 500_000

    def _memo_token(self, halo) -> tuple:
        """Everything :meth:`_via_geometry_clear` reads that is not in its key.

        Issue #5696.  ``Router._via_halo_clear_cached`` (``pathfinder.py``)
        keys an identical memo on ``(gx, gy, net)`` and tokens it on
        ``halo.state_version`` alone.  That is sound **there** only because
        ``Router.clear_via_cache()`` drops the memo at the start of every route
        / A* search, pinning the rule configuration for the memo's lifetime.
        This adapter has no such per-route reset hook -- its ``rules`` object
        is mutated in place between probes of the same cell by callers and by
        ``tests/test_coupled_route_halo_clearance.py`` -- so copying that token
        as-is would serve a *stale clearance verdict*, which in the
        "was blocked, now cached clear" direction is a silent DRC violation.

        The token therefore carries the rule configuration too.  Its members
        are exactly the inputs of ``RouteHaloGeometry.clear(via, self)`` that
        the key does not already pin, read off that method's source for the
        via branch (``is_trace`` false, ``partner_net``/``partner_clearance``
        ``None``, ``require_geometry`` true):

        * ``halo`` itself and :attr:`RouteHaloGeometry.state_version` -- the
          rebuilt snapshot (``_objects`` / ``_bounds`` / ``_bins`` / ``_cells``
          / ``marks``) every verdict is measured against.
        * ``rules.via_clearance`` -- the via branch's ``scalar``, and a term of
          ``margin``.
        * ``rules.via_drill`` / ``rules.via_diameter`` -- the candidate ``Via``
          :meth:`via_clear` builds (``via_diameter`` only when the net has no
          class; the class itself is covered by ``net_class_map`` below).
        * ``rules.min_hole_to_hole`` / ``rules.min_drill_clearance`` -- the
          drill-to-drill ``floor`` and two more ``margin`` terms.
        * ``rules.pairwise_clearance`` -- ``widen`` and the per-pair
          ``required_clearance`` lookup.
        * ``_route_halo_names`` (the candidate's and every neighbour's net
          name), ``net_class_map`` (``nc.via_size``) and ``_attach_zones``
          (the HV widening's exemption test).
        * ``rules.trace_clearance`` is not read on the via branch; it is
          included so the token stays correct if the memo is ever extended to
          :meth:`trace_clear`.  An over-wide token can only cost recomputation.

        Container members are compared by value, but Python's tuple comparison
        short-circuits on identity, so the common case (nothing replaced) costs
        a pointer compare per member rather than a dict walk.  The token also
        holds a strong reference to each member, which is what makes the
        comparison meaningful -- no member can be freed and its address reused
        by a different object while the token is live.

        Residual assumption, stated rather than hidden: a mutation that neither
        changes one of the scalars above nor replaces a container object -- for
        example mutating a ``NetClassRouting`` *in place* inside the same
        ``net_class_map`` dict -- is not observed.  That is the same assumption
        the un-memoised code already makes everywhere else the net-class
        dimensions are cached (``_install_cpp_halo_net_dimensions``), and
        neither production wiring nor the test-suite does it: classes are built
        once and the map is replaced wholesale.
        """
        rules = self.rules
        return (
            halo,
            halo.state_version,
            rules.via_clearance,
            rules.via_drill,
            rules.via_diameter,
            rules.trace_clearance,
            rules.min_hole_to_hole,
            rules.min_drill_clearance,
            rules.pairwise_clearance,
            self._route_halo_names,
            self.net_class_map,
            self._attach_zones,
        )

    def _via_geometry_clear(self, halo, gx: int, gy: int, net: int) -> bool:
        """Memoised geometric half of :meth:`via_clear` (issue #5696).

        The split is what makes the memo sound.  :meth:`via_clear`'s
        ``cells_known`` guard reads the grid's occupancy planes *directly*
        (``_blocked`` / ``_net`` / ``_is_obstacle`` / ``_pad_blocked`` /
        ``_static_blocked`` / ``_reserved_for_nets``), several of which can be
        written without bumping ``grid.occupancy_generation`` -- exactly what
        ``test_refinement_preserves_hard_or_unverifiable_blockage`` does.  That
        guard stays **outside** the memo and is re-evaluated on every call; it
        is also the cheap half (direct array reads), while the expensive half
        is the shapely distance sweep inside ``RouteHaloGeometry.clear``.

        What is left once the guard has passed depends only on ``(gx, gy,
        net)`` -- the key -- plus :meth:`_memo_token`.  Note in particular that
        it does **not** depend on ``cells`` or ``layer``: the candidate ``Via``
        always spans layer 0 to layer ``num_layers - 1`` and is centred on
        ``grid_to_world(gx, gy)``, which is why ``_is_via_blocked`` already
        hoists one verdict across its per-layer loop.
        """
        token = self._memo_token(halo)
        if self._via_memo_token != token:
            self._via_memo.clear()
            self._via_memo_token = token
        key = (gx, gy, net)
        hit = self._via_memo.get(key)
        if hit is None:
            hit = self._via_geometry_clear_uncached(halo, gx, gy, net)
            if len(self._via_memo) >= self._VIA_MEMO_MAX:
                self._via_memo.clear()
            self._via_memo[key] = hit
        return hit

    def via_clear(self, cells, layer: int, gx: int, gy: int, net: int) -> bool:
        """Physical clearance of a through via centred on ``(gx, gy)``."""
        if not self.cells_known(cells, layer):
            return False
        return self.via_clear_verified(gx, gy, net)

    def via_clear_verified(self, gx: int, gy: int, net: int) -> bool:
        """:meth:`via_clear`'s geometric half, for an already-verified caller.

        Issue #5720.  **Precondition**: the caller must have already
        established, with :meth:`cells_known` or :meth:`cells_known_mask` and
        for the layer it is refining, that every conservatively-blocked cell it
        wants relaxed is verified dynamic route copper.  Calling this without
        that check authorises raster relaxation over pad metal, static halos,
        keepouts, reservations and unknown owners -- a silent DRC violation
        with no error signal.  Use :meth:`via_clear`, which performs the check
        itself, unless you have just performed it.

        Splitting it out lets ``CoupledPathfinder._is_via_blocked`` reuse the
        one hoisted geometry verdict (#5410) without re-running the guard a
        third time over the same envelope: the guard is genuinely per-layer,
        the geometry is not -- a through via always spans layer 0 to layer
        ``num_layers - 1`` and is centred on ``grid_to_world(gx, gy)``, which is
        why :meth:`_via_geometry_clear` keys on ``(gx, gy, net)`` alone.
        """
        halo = self._halo
        if halo is None or not self.armed:
            return False
        return self._via_geometry_clear(halo, gx, gy, net)

    def _via_geometry_clear_uncached(self, halo, gx: int, gy: int, net: int) -> bool:
        """``via_clear``'s post-``cells_known`` body, with no memo (issue #5696).

        Kept as a separate entry point so the equivalence tests can pin the
        memoised path against the computation it replaces.
        """
        from .primitives import Via

        first_layer = self._layer_object(0)
        last_layer = self._layer_object(self.grid.num_layers - 1)
        if first_layer is None or last_layer is None:
            return False
        name = self._route_halo_names.get(net, "")
        nc = self._halo_net_class(net)
        x, y = self.grid.grid_to_world(gx, gy)
        via = Via(
            x,
            y,
            self.rules.via_drill,
            nc.via_size if nc else self.rules.via_diameter,
            (first_layer, last_layer),
            net,
            name,
        )
        return bool(halo.clear(via, self))

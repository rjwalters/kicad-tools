"""Congestion-aware escape-corridor reservation for dense high-pin-count parts.

Issue #4474 (Phase 4 of epic #4410).  Dense high-pin-count parts (the
DRV8301 HTSSOP-56 on board-05, any congested QFN/BGA/HTSSOP) escape their
pins per-pin with no congestion-aware channel planning: the escape router
places in-pad micro-vias greedily and the pins that share a congested face
lose the general multi-net negotiation because *nothing reserves a channel
for them*.  This module closes that gap by planning, sizing, and reserving
escape corridors from :class:`~kicad_tools.router.congestion_estimator.CongestionEstimator`
demand **before** the general negotiation runs.

Pipeline (all driven by :class:`EscapeCorridorPlanner`):

1. **Cluster** a dense part's routable pins by *face* (the bbox edge each
   pin sits nearest) and by *destination* (the compass octant of the vector
   from the pin to where its net has to go).  The destination geometry is
   the *post-Kelvin* topology (#4499): an ISENSE sense tap terminates AT its
   shunt/sense-resistor pad, not at the raw RSMT centroid, so ``kelvin_roots``
   overrides the off-package centroid for those nets.
2. **Size** each cluster's corridor to demand: the corridor's lateral
   half-width grows with the pin count *and* with the local
   ``CongestionEstimator`` demand, so a congested cluster (U3's south sense
   band) gets a wider reserved channel than an uncongested one.
3. **Assign layers** per cluster, distributing clusters across the routable
   layers so different clusters escape on different layers where the stack
   allows -- reducing same-layer contention.  Degrades gracefully on a
   2-layer board (round-robins over whatever routable layers exist).
4. **Reserve** the corridor cells on the routing grid via
   :meth:`RoutingGrid.reserve_corridor_cells` (SOFT by default -- an
   attractor bonus for the owning nets, not a hard fence, so unrelated nets
   are never starved of the long channel).

**Rollout convention (Issue #4051 precedent):** this planner is *never
constructed* unless the caller opts in via the router's
``enable_escape_corridor_reservation`` flag.  With the flag absent, routing
output is byte-identical to pre-change main -- there is no import-time or
run-time side effect.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .congestion_estimator import CongestionEstimator
    from .escape import PackageInfo
    from .grid import RoutingGrid
    from .primitives import Pad


# Compass octant names indexed by ``int((angle + 22.5) / 45) % 8`` where the
# angle is measured with ``atan2(dy, dx)`` in degrees.  These are grid-space
# directions (KiCad y grows downward), so "N" is decreasing y and "S" is
# increasing y -- consistent bucketing is all that matters here.
_OCTANTS = ("E", "SE", "S", "SW", "W", "NW", "N", "NE")


def _octant_of(dx: float, dy: float) -> str:
    """Return the compass octant name for a direction vector."""
    if dx == 0.0 and dy == 0.0:
        return "E"
    angle = math.degrees(math.atan2(dy, dx))
    idx = int((angle + 22.5) // 45) % 8
    return _OCTANTS[idx]


@dataclass
class EscapeCluster:
    """A group of a dense part's pins that escape toward a common neighbor.

    A cluster is keyed by ``(face, dest_octant)`` -- the bbox edge the pins
    sit nearest and the compass direction of their escape.  The reserved
    corridor is a rectangle launched from :attr:`origin` (the cluster's pin
    centroid) along :attr:`escape_dir` toward the cluster's shared
    destination.

    Attributes:
        face: Nearest bbox edge for the cluster's pins (``"N"``/``"S"``/
            ``"E"``/``"W"``).
        dest_octant: Compass octant of the mean escape direction.
        pads: The pins in this cluster.
        net_ids: The distinct routable net ids the pins belong to (the
            corridor owner set for :meth:`RoutingGrid.reserve_corridor_cells`).
        escape_dir: Unit mean escape vector (grid space).
        origin: Cluster pin centroid (mm), the corridor launch point.
        destination: Mean destination point (mm) the corridor extends toward.
        demand: ``CongestionEstimator`` demand aggregated over the cluster's
            pin tiles.  Drives corridor sizing (higher demand -> wider).
        layer_idx: Assigned escape layer (grid index), or ``None`` before
            :meth:`EscapeCorridorPlanner._assign_layers` runs.
        corridor_cells: Grid cells the reserved corridor covers.
        reserved_cell_count: Cells actually reserved on the grid.
    """

    face: str
    dest_octant: str
    pads: list[Pad]
    net_ids: frozenset[int]
    escape_dir: tuple[float, float]
    origin: tuple[float, float]
    destination: tuple[float, float]
    demand: float = 0.0
    layer_idx: int | None = None
    corridor_cells: frozenset[tuple[int, int]] = field(default_factory=frozenset)
    reserved_cell_count: int = 0

    @property
    def key(self) -> tuple[str, str]:
        """The clustering key ``(face, dest_octant)``."""
        return (self.face, self.dest_octant)

    @property
    def pin_count(self) -> int:
        """Number of pins in the cluster."""
        return len(self.pads)


@dataclass
class EscapeCorridorPlan:
    """The planned (and optionally reserved) corridors for one dense part.

    Attributes:
        ref: The dense part's component reference (e.g. ``"U3"``).
        clusters: The per-``(face, dest_octant)`` clusters, deterministically
            ordered by key.
        total_reserved_cells: Sum of ``reserved_cell_count`` across clusters
            after :meth:`EscapeCorridorPlanner.reserve` runs.
    """

    ref: str
    clusters: list[EscapeCluster] = field(default_factory=list)
    total_reserved_cells: int = 0

    @property
    def cluster_count(self) -> int:
        """Number of clusters in the plan."""
        return len(self.clusters)

    def layers_used(self) -> set[int]:
        """The set of distinct layer indices the plan assigned."""
        return {c.layer_idx for c in self.clusters if c.layer_idx is not None}


class EscapeCorridorPlanner:
    """Plans and reserves demand-sized escape corridors for a dense part.

    The planner is *generic*: it takes any :class:`PackageInfo`-shaped
    object (only ``.ref``, ``.pads``, ``.center``, ``.bounding_box`` are
    read) and a :class:`CongestionEstimator`, and produces an
    :class:`EscapeCorridorPlan`.  It has no board-05-specific logic; the
    board-05 sense band is simply the motivating congested cluster.

    Args:
        grid: The routing grid the corridors are reserved on.
        congestion_estimator: RUDY demand source used to size corridors.
            When ``None`` every cluster is treated as zero-demand (corridors
            are sized by pin count alone).
        net_pad_positions: Board-wide ``net_id -> [(x, y), ...]`` map of ALL
            pad positions, used to resolve each net's off-package
            destination.  When absent the package center is used as a
            fallback destination.
        kelvin_roots: ``net_id -> (x, y)`` map of Kelvin/sense-star root
            (shunt-pad) positions (#4499).  For a net in this map the
            destination is the root pad, so an ISENSE cluster's corridor
            aims at the shunt where the star terminates rather than at the
            raw net centroid.
        cells_per_pin: Base corridor lateral cells contributed per pin
            (before the demand multiplier).
        max_corridor_length_mm: Optional cap on how far a corridor extends
            from the launch point (defaults to the destination distance).
    """

    def __init__(
        self,
        grid: RoutingGrid,
        congestion_estimator: CongestionEstimator | None = None,
        *,
        net_pad_positions: dict[int, list[tuple[float, float]]] | None = None,
        kelvin_roots: dict[int, tuple[float, float]] | None = None,
        cells_per_pin: float = 1.0,
        max_corridor_length_mm: float | None = None,
        min_reserve_demand: float = 0.0,
    ) -> None:
        self.grid = grid
        self.congestion = congestion_estimator
        self.net_pad_positions = net_pad_positions or {}
        self.kelvin_roots = kelvin_roots or {}
        self.cells_per_pin = float(cells_per_pin)
        self.max_corridor_length_mm = max_corridor_length_mm
        self.min_reserve_demand = float(min_reserve_demand)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(self, package: PackageInfo) -> EscapeCorridorPlan:
        """Cluster the part's pins, size corridors, and assign layers.

        Does NOT touch the grid -- call :meth:`reserve` (or
        :meth:`plan_and_reserve`) to commit the reservation.
        """
        clusters = self._cluster_pins(package)
        for cluster in clusters:
            self._size_corridor(package, cluster)
        self._assign_layers(clusters)
        return EscapeCorridorPlan(ref=package.ref, clusters=clusters)

    def reserve(self, plan: EscapeCorridorPlan, *, soft: bool = True) -> int:
        """Reserve every cluster's corridor cells on the grid.

        Returns the total number of cells reserved.  A SOFT reservation
        (default) applies only the A* attractor bonus for the owning nets
        and fences nothing out -- so a long escape channel never starves
        unrelated nets (the #4087 hard-fence hazard).  Reservation is
        advisory: a cluster with no owner nets or no cells is skipped.
        """
        total = 0
        for cluster in plan.clusters:
            if cluster.layer_idx is None or not cluster.corridor_cells or not cluster.net_ids:
                continue
            # Edge case (#4474 test plan): a cluster on a face the
            # CongestionEstimator reports as uncongested does not warrant a
            # reserved channel -- reserving one would only fence copper for
            # no benefit.  Skip it (no-op) when demand is at/under the
            # threshold.  When no estimator was supplied the planner runs in
            # demand-agnostic mode and reserves every cluster.
            if self.congestion is not None and cluster.demand <= self.min_reserve_demand:
                continue
            count = self.grid.reserve_corridor_cells(
                layer_idx=cluster.layer_idx,
                cells=set(cluster.corridor_cells),
                net_ids=cluster.net_ids,
                soft=soft,
            )
            cluster.reserved_cell_count = count
            total += count
        plan.total_reserved_cells = total
        return total

    def plan_and_reserve(self, package: PackageInfo, *, soft: bool = True) -> EscapeCorridorPlan:
        """Convenience: :meth:`plan` then :meth:`reserve`."""
        plan = self.plan(package)
        self.reserve(plan, soft=soft)
        return plan

    # ------------------------------------------------------------------
    # Step 1: clustering
    # ------------------------------------------------------------------

    def _cluster_pins(self, package: PackageInfo) -> list[EscapeCluster]:
        """Group routable pins by (nearest bbox face, escape octant)."""
        min_x, min_y, max_x, max_y = package.bounding_box
        pkg_pad_xy = {(round(p.x, 4), round(p.y, 4)) for p in package.pads}

        buckets: dict[tuple[str, str], list[tuple[Pad, tuple[float, float]]]] = {}
        for pad in package.pads:
            net_id = int(pad.net)
            if net_id == 0:
                # Unassigned / plane pads escape via zone fill, not corridors.
                continue
            dest = self._destination_for(net_id, pad, package, pkg_pad_xy)
            if dest is None:
                continue
            dx = dest[0] - pad.x
            dy = dest[1] - pad.y
            face = self._face_of(pad, min_x, min_y, max_x, max_y)
            octant = _octant_of(dx, dy)
            buckets.setdefault((face, octant), []).append((pad, dest))

        clusters: list[EscapeCluster] = []
        for (face, octant), members in sorted(buckets.items()):
            pads = [m[0] for m in members]
            dests = [m[1] for m in members]
            n = len(pads)
            ox = sum(p.x for p in pads) / n
            oy = sum(p.y for p in pads) / n
            dcx = sum(d[0] for d in dests) / n
            dcy = sum(d[1] for d in dests) / n
            ddx, ddy = dcx - ox, dcy - oy
            norm = math.hypot(ddx, ddy)
            if norm == 0.0:
                edir = (1.0, 0.0)
            else:
                edir = (ddx / norm, ddy / norm)
            net_ids = frozenset(int(p.net) for p in pads)
            clusters.append(
                EscapeCluster(
                    face=face,
                    dest_octant=octant,
                    pads=pads,
                    net_ids=net_ids,
                    escape_dir=edir,
                    origin=(ox, oy),
                    destination=(dcx, dcy),
                )
            )
        return clusters

    def _destination_for(
        self,
        net_id: int,
        pad: Pad,
        package: PackageInfo,
        pkg_pad_xy: set[tuple[float, float]],
    ) -> tuple[float, float] | None:
        """Resolve where ``net_id`` needs to escape toward from ``pad``.

        Priority:
          1. Kelvin/sense-star root (#4499) -- the shunt pad the sense tap
             terminates at.
          2. Centroid of the net's OFF-package pads (from
             ``net_pad_positions``).
          3. The package center (fallback), so a net that lives entirely on
             this package still yields a well-defined outward direction.
        """
        root = self.kelvin_roots.get(net_id)
        if root is not None:
            return root

        positions = self.net_pad_positions.get(net_id)
        if positions:
            off = [(x, y) for (x, y) in positions if (round(x, 4), round(y, 4)) not in pkg_pad_xy]
            if off:
                return (sum(p[0] for p in off) / len(off), sum(p[1] for p in off) / len(off))

        # Fallback: aim away from the package center through the pad.
        cx, cy = package.center
        if pad.x == cx and pad.y == cy:
            return None
        return (cx + 2.0 * (pad.x - cx), cy + 2.0 * (pad.y - cy))

    @staticmethod
    def _face_of(pad: Pad, min_x: float, min_y: float, max_x: float, max_y: float) -> str:
        """Return the bbox edge (``N``/``S``/``E``/``W``) the pad sits nearest."""
        dist_w = abs(pad.x - min_x)
        dist_e = abs(max_x - pad.x)
        dist_n = abs(pad.y - min_y)
        dist_s = abs(max_y - pad.y)
        nearest = min(dist_w, dist_e, dist_n, dist_s)
        if nearest == dist_n:
            return "N"
        if nearest == dist_s:
            return "S"
        if nearest == dist_w:
            return "W"
        return "E"

    # ------------------------------------------------------------------
    # Step 2: corridor sizing (demand-driven)
    # ------------------------------------------------------------------

    def _size_corridor(self, package: PackageInfo, cluster: EscapeCluster) -> None:
        """Compute ``cluster.demand`` and the corridor cell footprint.

        The lateral half-width scales with the pin count AND the local
        congestion demand::

            lat_half_cells = cells_per_pin * pin_count * (1 + demand_factor)

        so a congested cluster reserves a strictly wider channel than an
        equal-pin-count uncongested one -- the "size corridors to demand"
        acceptance criterion.
        """
        res = self.grid.resolution
        cluster.demand = self._cluster_demand(cluster)

        # Normalise demand into a [0, 1] widening factor relative to the
        # estimator's peak tile demand (so sizing is scale-free across boards).
        demand_factor = 0.0
        peak = self._peak_demand()
        if peak > 0.0:
            demand_factor = min(1.0, cluster.demand / peak)

        lat_half_cells = self.cells_per_pin * cluster.pin_count * (1.0 + demand_factor)
        lat_half = max(res, lat_half_cells * res * 0.5)

        cx, cy = cluster.origin
        dx, dy = cluster.escape_dir
        # Lateral (perpendicular) unit vector.
        lat_dx, lat_dy = -dy, dx

        # Corridor length: reach toward the destination, capped.
        dest_dist = math.hypot(cluster.destination[0] - cx, cluster.destination[1] - cy)
        length = dest_dist if dest_dist > 0.0 else max(res * 4.0, lat_half * 2.0)
        if self.max_corridor_length_mm is not None:
            length = min(length, self.max_corridor_length_mm)
        length = max(length, res * 2.0)

        step = res * 0.5
        cells: set[tuple[int, int]] = set()
        t = 0.0
        while t <= length:
            u = -lat_half
            while u <= lat_half:
                wx = cx + dx * t + lat_dx * u
                wy = cy + dy * t + lat_dy * u
                cells.add(self.grid.world_to_grid(wx, wy))
                u += step
            t += step
        cluster.corridor_cells = frozenset(cells)

    def _cluster_demand(self, cluster: EscapeCluster) -> float:
        """Aggregate ``CongestionEstimator`` demand over the cluster's tiles."""
        if self.congestion is None:
            return 0.0
        tgrid = self.congestion.grid
        seen: set[tuple[int, int]] = set()
        total = 0.0
        for pad in cluster.pads:
            col, row = tgrid.tile_at(pad.x, pad.y)
            if (row, col) in seen:
                continue
            seen.add((row, col))
            total += self.congestion.get_tile_demand(row, col)
        return total

    def _peak_demand(self) -> float:
        """Peak per-tile demand across the estimator (0 when unavailable)."""
        if self.congestion is None:
            return 0.0
        peak = 0.0
        for row in self.congestion.get_demand_grid():
            for val in row:
                if val > peak:
                    peak = val
        return peak

    # ------------------------------------------------------------------
    # Step 3: layer assignment
    # ------------------------------------------------------------------

    def _assign_layers(self, clusters: list[EscapeCluster]) -> None:
        """Distribute clusters across routable layers to cut same-layer contention.

        Highest-demand clusters are assigned first (they most need an
        uncontested layer).  Assignment round-robins over the routable
        layers, so on a 4-layer stack four clusters land on four distinct
        layers; on a 2-layer board it degrades to round-robin over the two
        outer layers.
        """
        routable = self._routable_layers()
        if not routable:
            return
        order = sorted(
            range(len(clusters)),
            key=lambda i: (-clusters[i].demand, clusters[i].key),
        )
        for rank, idx in enumerate(order):
            clusters[idx].layer_idx = routable[rank % len(routable)]

    def _routable_layers(self) -> list[int]:
        """Routable grid-layer indices, preferring inner layers first.

        Inner (non-outer) signal layers are listed before outer layers so
        that -- on a 4-layer stack -- clusters escape on the inner routing
        layers where a dense part's in-pad vias drop to, keeping the outer
        (component) layers freer.  Falls back to whatever routable layers
        exist (2-layer boards have only outer layers).
        """
        stack = getattr(self.grid, "layer_stack", None)
        get_routable = getattr(self.grid, "get_routable_indices", None)
        if get_routable is not None:
            routable = list(get_routable())
        elif stack is not None:
            routable = list(stack.get_routable_indices())
        else:
            return list(range(getattr(self.grid, "num_layers", 1)))

        if stack is not None:
            outer = set(stack.get_outer_layer_indices())
            inner = [i for i in routable if i not in outer]
            outers = [i for i in routable if i in outer]
            return inner + outers
        return routable

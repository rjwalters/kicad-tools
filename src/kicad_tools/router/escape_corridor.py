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
from typing import TYPE_CHECKING, Any

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
    # Issue #6352 (experimental): which assignment strategy produced the
    # clusters ("greedy" -- the #4474 face/octant + round-robin layers path --
    # or "flow", the joint min-cost-flow pin -> exit assignment), and, when
    # "flow" was requested but could not be used, why the plan fell back to
    # greedy.  ``flow_stats`` carries solver instrumentation (pins, exits,
    # capacity, assigned/unassigned, cost) so an inert treatment cannot pass
    # as a null result.  The greedy default leaves all three at their
    # defaults, so a default plan is unchanged.
    assignment: str = "greedy"
    fallback_reason: str | None = None
    flow_stats: dict[str, Any] = field(default_factory=dict)

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
        min_reserve_demand: Absolute demand floor -- a cluster whose
            aggregated ``CongestionEstimator`` demand is at or below this
            value is skipped (no reservation).  The permissive ``0.0``
            default only skips exactly-zero-demand clusters; a real board
            wants a positive floor (or ``min_reserve_demand_frac``) so only
            genuinely congested clusters reserve a channel (#4519).
        min_reserve_demand_frac: Selectivity floor expressed as a fraction of
            the *most-congested cluster's* demand in the plan (#4519).  A
            cluster is skipped unless its demand exceeds
            ``min_reserve_demand_frac * max(cluster.demand for the plan)`` --
            i.e. only clusters near the peak-congested one reserve a channel,
            which is the "only the congested cluster gets a channel" intent.
            This is scale-free (self-calibrating per package) and, unlike a
            fraction of the estimator's *per-tile* peak, actually discriminates
            on a real board: ``cluster.demand`` aggregates several pin tiles,
            so on a dense part every face's cluster typically sums above the
            single-tile peak and a per-tile fraction fails to trim anything
            (measured on board-05's U3/U10, #4519).  The effective threshold
            is ``max(min_reserve_demand, min_reserve_demand_frac *
            max_cluster_demand)``.  Defaults to ``0.0`` (no relative gating)
            so the library default reserves every nonzero-demand cluster; a
            production caller (``router/core.py``) supplies a positive
            fraction to make the reservation selective.
        inner_layers_only: When True, restrict corridor layer assignment to
            the stack's INNER routable layers (#4519, Scope item 3), keeping
            the outer component layers free of escape attractors.  Degrades
            gracefully to all routable layers when the stack has no inner
            layers (e.g. a 2-layer board).  Defaults to False (assign over
            all routable layers, inner-preferred) so existing callers are
            unchanged.
        assignment: Pin -> corridor assignment strategy (Issue #6352,
            EXPERIMENTAL).  ``"greedy"`` (default) is the #4474 path: nearest
            face, ``(face, octant)`` clusters, round-robin layers.  ``"flow"``
            solves the pin -> ``(face, layer)`` exit assignment jointly with
            min-cost max-flow (``escape_flow.py``) under per-exit capacities
            derived from free grid cells, then builds the same
            :class:`EscapeCluster` objects so :meth:`reserve` is shared.  When
            the flow assigns no pin, or leaves any pin unassigned, the plan
            falls back to greedy and records ``fallback_reason``.
        flow_cost_model: Cost-model overrides for ``assignment="flow"``
            (defaults to :class:`~kicad_tools.router.escape_flow.EscapeFlowCostModel`).
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
        min_reserve_demand_frac: float = 0.0,
        inner_layers_only: bool = False,
        assignment: str = "greedy",
        flow_cost_model: Any = None,
    ) -> None:
        if assignment not in ("greedy", "flow"):
            raise ValueError(f"unknown escape-corridor assignment {assignment!r}")
        self.assignment = assignment
        self.flow_cost_model = flow_cost_model
        self.grid = grid
        self.congestion = congestion_estimator
        self.net_pad_positions = net_pad_positions or {}
        self.kelvin_roots = kelvin_roots or {}
        self.cells_per_pin = float(cells_per_pin)
        self.max_corridor_length_mm = max_corridor_length_mm
        self.min_reserve_demand = float(min_reserve_demand)
        self.min_reserve_demand_frac = float(min_reserve_demand_frac)
        self.inner_layers_only = bool(inner_layers_only)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(self, package: PackageInfo) -> EscapeCorridorPlan:
        """Cluster the part's pins, size corridors, and assign layers.

        Does NOT touch the grid -- call :meth:`reserve` (or
        :meth:`plan_and_reserve`) to commit the reservation.

        With ``assignment="flow"`` (Issue #6352) the joint min-cost-flow
        assignment is tried first; an empty or infeasible flow falls back to
        the greedy plan below with ``fallback_reason`` recorded.
        """
        if self.assignment == "flow":
            flow_plan, reason, stats = self._plan_flow(package)
            if flow_plan is not None:
                return flow_plan
            greedy = self._plan_greedy(package)
            greedy.fallback_reason = reason
            greedy.flow_stats = stats
            return greedy
        return self._plan_greedy(package)

    def _plan_greedy(self, package: PackageInfo) -> EscapeCorridorPlan:
        """The #4474 greedy plan: nearest face, octant clusters, round-robin layers."""
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
        # Selectivity threshold (#4519).  Combine the absolute floor with the
        # relative floor (a fraction of the most-congested cluster's demand) so
        # a reservation is a *selective* signal -- only genuinely congested
        # clusters reserve a channel, not every face of a dense part.
        threshold = self._reserve_threshold(plan.clusters)
        total = 0
        for cluster in plan.clusters:
            if cluster.layer_idx is None or not cluster.corridor_cells or not cluster.net_ids:
                continue
            # Edge case (#4474 test plan): a cluster on a face the
            # CongestionEstimator reports as uncongested does not warrant a
            # reserved channel -- reserving one would only fence copper for
            # no benefit.  Skip it (no-op) when demand is at/under the
            # threshold.  When no estimator was supplied the planner runs in
            # demand-agnostic mode and reserves every cluster (the threshold
            # is only consulted when a congestion estimator is present).
            if self.congestion is not None and cluster.demand <= threshold:
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
    # Experimental: joint min-cost-flow assignment (Issue #6352)
    # ------------------------------------------------------------------

    _FLOW_FACES = ("N", "E", "S", "W")

    def _plan_flow(
        self, package: PackageInfo
    ) -> tuple[EscapeCorridorPlan | None, str | None, dict[str, Any]]:
        """Build a plan from the joint pin -> (face, layer) flow assignment.

        Returns ``(plan, None, stats)`` on success, or ``(None, reason,
        stats)`` when the caller must fall back to greedy.  Never touches the
        grid's reservation state -- capacity probing only reads cells.
        """
        from .escape_flow import (
            EscapeFlowCostModel,
            ExitSlot,
            assign_pins_to_exits,
            exit_point,
            pin_exit_cost_mm,
        )

        model = self.flow_cost_model or EscapeFlowCostModel()
        bbox = package.bounding_box
        min_x, min_y, max_x, max_y = bbox
        pkg_pad_xy = {(round(p.x, 4), round(p.y, 4)) for p in package.pads}

        # Canonical pin order: (ref, pin, net, x, y).  Ties in the flow resolve
        # toward lower indices, so this order IS the tie-break.
        pins: list[tuple[Pad, tuple[float, float]]] = []
        for pad in sorted(
            package.pads,
            key=lambda p: (str(p.ref), str(p.pin), int(p.net), p.x, p.y),
        ):
            net_id = int(pad.net)
            if net_id == 0:
                continue
            dest = self._destination_for(net_id, pad, package, pkg_pad_xy)
            if dest is None:
                continue
            pins.append((pad, dest))

        layers = self._routable_layers()
        stats: dict[str, Any] = {"pins": len(pins), "layers": list(layers)}
        if not pins:
            return None, "no routable pins", stats
        if not layers:
            return None, "no routable layers", stats

        package_nets = frozenset(int(p.net) for p in package.pads if int(p.net) != 0)
        offset = self._flow_probe_offset(package)
        peak = self._peak_demand()
        exits: list[ExitSlot] = []
        congestion: list[float] = []
        load_step = model.to_units(model.load_step_mm)
        for face in self._FLOW_FACES:
            face_demand = self._face_demand(face, bbox, offset)
            norm = min(1.0, face_demand / peak) if peak > 0.0 else 0.0
            for layer_idx in layers:
                cap = self._exit_capacity(face, bbox, offset, layer_idx, package_nets)
                exits.append(
                    ExitSlot(
                        face=face,
                        layer_idx=layer_idx,
                        capacity=cap,
                        load_step_cost=load_step,
                    )
                )
                congestion.append(norm)
        stats["exits"] = len(exits)
        stats["exit_capacity"] = {f"{e.face}/L{e.layer_idx}": e.capacity for e in exits}

        costs: dict[tuple[int, int], int] = {}
        for pi, (pad, dest) in enumerate(pins):
            pin_face = self._face_of(pad, min_x, min_y, max_x, max_y)
            pad_layer = self._pad_layer_index(pad)
            for ei, slot in enumerate(exits):
                needs_via = (not pad.through_hole) and pad_layer != slot.layer_idx
                mm = pin_exit_cost_mm(
                    pin_xy=(pad.x, pad.y),
                    pin_face=pin_face,
                    dest_xy=dest,
                    exit_face=slot.face,
                    bbox=bbox,
                    needs_via=needs_via,
                    exit_congestion=congestion[ei],
                    model=model,
                )
                costs[(pi, ei)] = model.to_units(mm)

        result = assign_pins_to_exits(len(pins), exits, costs)
        stats.update(
            {
                "total_capacity": result.total_capacity,
                "assigned": result.assigned_count,
                "unassigned": len(result.unassigned),
                "cost": result.total_cost,
                "augmentations": result.augmentations,
            }
        )
        if result.assigned_count == 0:
            return None, "empty: flow assigned no pin", stats
        if not result.feasible:
            return (
                None,
                f"infeasible: {len(result.unassigned)} of {len(pins)} pins unassigned",
                stats,
            )

        # Count pins whose flow exit differs from the greedy nearest face --
        # the only place the joint assignment can change corridor geometry
        # beyond layer choice.
        moved = 0
        buckets: dict[tuple[str, int, str], list[tuple[Pad, tuple[float, float], tuple]]] = {}
        for pi, ei in sorted(result.assignment.items()):
            pad, dest = pins[pi]
            slot = exits[ei]
            if slot.face != self._face_of(pad, min_x, min_y, max_x, max_y):
                moved += 1
            ep = exit_point(slot.face, pad.x, pad.y, bbox)
            octant = _octant_of(dest[0] - ep[0], dest[1] - ep[1])
            buckets.setdefault((slot.face, slot.layer_idx, octant), []).append((pad, dest, ep))
        stats["face_moved"] = moved

        clusters: list[EscapeCluster] = []
        for (face, layer_idx, octant), members in sorted(buckets.items()):
            n = len(members)
            ox = sum(m[2][0] for m in members) / n
            oy = sum(m[2][1] for m in members) / n
            dcx = sum(m[1][0] for m in members) / n
            dcy = sum(m[1][1] for m in members) / n
            ddx, ddy = dcx - ox, dcy - oy
            norm = math.hypot(ddx, ddy)
            edir = (1.0, 0.0) if norm == 0.0 else (ddx / norm, ddy / norm)
            cluster = EscapeCluster(
                face=face,
                dest_octant=octant,
                pads=[m[0] for m in members],
                net_ids=frozenset(int(m[0].net) for m in members),
                escape_dir=edir,
                origin=(ox, oy),
                destination=(dcx, dcy),
                layer_idx=layer_idx,
            )
            self._size_corridor(package, cluster)
            clusters.append(cluster)
        stats["clusters"] = len(clusters)
        plan = EscapeCorridorPlan(
            ref=package.ref, clusters=clusters, assignment="flow", flow_stats=stats
        )
        return plan, None, stats

    def _pad_layer_index(self, pad: Pad) -> int | None:
        """Grid layer index of the pad's copper (``None`` if not in the stack)."""
        to_index = getattr(self.grid, "layer_to_index", None)
        if to_index is None:
            return None
        try:
            return int(to_index(pad.layer.value))
        except Exception:
            return None

    def _flow_probe_offset(self, package: PackageInfo) -> float:
        """Distance outside the pad-centre bbox where exit capacity is probed.

        Clears the largest pad half-extent plus one track pitch, so the probe
        line sits in the channel a fanned-out track actually uses rather than
        on the pads' own clearance halo.
        """
        half = 0.0
        for p in package.pads:
            half = max(half, 0.5 * max(float(p.width), float(p.height)))
        return half + self._track_pitch()

    def _track_pitch(self) -> float:
        rules = getattr(self.grid, "rules", None)
        width = float(getattr(rules, "trace_width", 0.2) or 0.2)
        clearance = float(getattr(rules, "trace_clearance", 0.15) or 0.15)
        return width + clearance

    @staticmethod
    def _face_probe_line(
        face: str, bbox: tuple[float, float, float, float], offset: float
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Endpoints of the probe segment running along ``face`` at ``offset``."""
        min_x, min_y, max_x, max_y = bbox
        if face == "N":
            return (min_x, min_y - offset), (max_x, min_y - offset)
        if face == "S":
            return (min_x, max_y + offset), (max_x, max_y + offset)
        if face == "W":
            return (min_x - offset, min_y), (min_x - offset, max_y)
        return (max_x + offset, min_y), (max_x + offset, max_y)

    def _exit_capacity(
        self,
        face: str,
        bbox: tuple[float, float, float, float],
        offset: float,
        layer_idx: int,
        package_nets: frozenset[int],
    ) -> int:
        """Tracks that fit through ``face`` on ``layer_idx`` (hypothesis).

        Walks the probe line one grid cell at a time and counts cells that
        are free, or blocked only by one of the package's own nets (its pad
        halos are passable for the net that owns them).  Capacity is the free
        length divided by one track pitch.  A face of zero span (a single-row
        package's short side) still probes one cell.
        """
        res = float(self.grid.resolution)
        (x0, y0), (x1, y1) = self._face_probe_line(face, bbox, offset)
        length = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(length / res) + 1)
        cols = getattr(self.grid, "cols", None)
        rows = getattr(self.grid, "rows", None)
        cell_at = getattr(self.grid, "cell_at", None)
        seen: set[tuple[int, int]] = set()
        free = 0
        for i in range(steps):
            t = 0.0 if steps == 1 else i / (steps - 1)
            wx = x0 + (x1 - x0) * t
            wy = y0 + (y1 - y0) * t
            # Off-board probe points are not free (world_to_grid clamps).
            ox = getattr(self.grid, "origin_x", 0.0)
            oy = getattr(self.grid, "origin_y", 0.0)
            rgx = round((wx - ox) / res)
            rgy = round((wy - oy) / res)
            if cols is not None and rows is not None and not (0 <= rgx < cols and 0 <= rgy < rows):
                continue
            gx, gy = self.grid.world_to_grid(wx, wy)
            if (gx, gy) in seen:
                continue
            seen.add((gx, gy))
            if cell_at is None:
                free += 1
                continue
            cell = cell_at(layer_idx, gy, gx)
            if not cell.blocked or (cell.net != 0 and int(cell.net) in package_nets):
                free += 1
        return int((free * res) // self._track_pitch()) if free else 0

    def _face_demand(
        self, face: str, bbox: tuple[float, float, float, float], offset: float
    ) -> float:
        """Mean ``CongestionEstimator`` tile demand along a face's probe line."""
        if self.congestion is None:
            return 0.0
        tgrid = self.congestion.grid
        (x0, y0), (x1, y1) = self._face_probe_line(face, bbox, offset)
        samples = 8
        tiles: set[tuple[int, int]] = set()
        for i in range(samples):
            t = i / (samples - 1)
            col, row = tgrid.tile_at(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            tiles.add((row, col))
        if not tiles:
            return 0.0
        return sum(self.congestion.get_tile_demand(r, c) for r, c in sorted(tiles)) / len(tiles)

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

    def _reserve_threshold(self, clusters: list[EscapeCluster]) -> float:
        """Effective per-cluster demand floor for the :meth:`reserve` gate.

        Combines the absolute :attr:`min_reserve_demand` floor with the
        relative :attr:`min_reserve_demand_frac` floor (a fraction of the
        most-congested cluster's demand in this plan).  A cluster reserves a
        channel only when its aggregated demand strictly exceeds this value
        (#4519).

        The relative term self-calibrates per package: it ranks each cluster
        against the plan's peak cluster demand, so the gate keeps only the
        clusters nearest the congestion peak regardless of the board's
        absolute demand magnitude -- no global magic number.  A per-tile peak
        does not work here because ``cluster.demand`` aggregates several tiles
        and typically sums above the single-tile peak on a dense part (#4519).
        """
        threshold = self.min_reserve_demand
        if self.min_reserve_demand_frac > 0.0 and clusters:
            max_demand = max((c.demand for c in clusters), default=0.0)
            if max_demand > 0.0:
                threshold = max(threshold, self.min_reserve_demand_frac * max_demand)
        return threshold

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

        When :attr:`inner_layers_only` is set (#4519, Scope item 3), the
        outer layers are dropped entirely so escape attractors never land on
        the component layers -- unless the stack has no inner layers (a
        2-layer board), in which case the outer layers are retained so
        assignment does not degrade to an empty set.
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
            if self.inner_layers_only and inner:
                return inner
            return inner + outers
        return routable

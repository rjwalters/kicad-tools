"""RoutingPlan sidecar: report-only serialization of the tile-based
two-phase global routing pass (Issue #5519, Phase 1 of Epic #5510).

The tile-based :class:`~kicad_tools.router.global_router.GlobalRouter`
already runs on the default ``kct route`` path for dense-package boards
(``Autorouter.route_with_escape`` -> ``route_all_two_phase`` ->
``TwoPhaseRouter.route_all``).  Its result -- corridor assignments, edge
capacity/demand/overflow on the coarse :class:`~kicad_tools.router.
region_graph.RegionGraph` -- was previously discarded (only a corridor
count was printed).  :class:`RoutingPlan` serializes that already-computed
state into a JSON sidecar for diagnostics/tooling.  This is a report-only
artifact: building it never mutates the ``RegionGraph`` or changes which
copper is routed (Phase 1 of the epic is explicitly byte-identical).

.. note::
   This module is **unrelated** to ``boards/03-usb-joystick/routing_plan.py``
   / ``boards/03-usb-joystick/routing-plan.json`` (a board-03 copper replay
   recipe predating this epic).  Do not confuse the two when grepping.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .global_router import GlobalRoutingResult
    from .layers import LayerStack
    from .region_graph import RegionGraph
    from .rules import NetClassRouting

#: Sidecar schema version.  Bump when the on-disk shape changes in a way
#: that is not purely additive.
SCHEMA_VERSION = 1

#: Valid values for :attr:`NetPlanEntry.status`.
NET_STATUS_VALUES = frozenset({"assigned", "failed", "pour_skipped", "single_pad", "no_endpoints"})


def signal_layer_indices(layer_stack: LayerStack) -> list[int]:
    """Indices of the layers that may carry signal capacity (Issue #5575).

    **Not** ``LayerStack.signal_layers``: that property filters on
    ``LayerDefinition.is_routable``, which returns ``True`` unconditionally
    -- PLANE layers included, by design (#5014).  Counting a ``sig/gnd/
    pwr/sig`` stack's two planes as routing capacity makes the plan
    advertise 2x the capacity the board has, so the plan's capacity model
    uses this stricter "routable AND not a plane" set instead.

    This is a *planning* restriction only.  It does not change what the
    detailed router is allowed to do (``--reserve-plane-layers`` owns
    that); it changes what the report-only plan claims is available.

    Args:
        layer_stack: The grid's layer stack.

    Returns:
        Ascending list of non-plane layer indices.
    """
    return [i for i in range(layer_stack.num_layers) if not layer_stack.is_plane_layer(i)]


@dataclass
class RegionInfo:
    """Spatial bounds of one coarse-grid region (mirrors ``Region``)."""

    row: int
    col: int
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "col": self.col,
            "min_x": self.min_x,
            "min_y": self.min_y,
            "max_x": self.max_x,
            "max_y": self.max_y,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegionInfo:
        return cls(
            row=data["row"],
            col=data["col"],
            min_x=data["min_x"],
            min_y=data["min_y"],
            max_x=data["max_x"],
            max_y=data["max_y"],
        )


@dataclass
class NetPlanEntry:
    """Per-net global-routing outcome.

    Attributes:
        name: Net name.
        net_class: Resolved net-class name (``None`` when the net has no
            entry in the net-class map).
        pitch_mm: Trace width + clearance used to estimate capacity for
            this net's class (or the design-rule default).
        layer_set: Layers this net was assigned to.  Phase 1 always emits
            a single-element list (the round-robin layer); a list from
            day one so later phases (multi-layer assignment) are additive.
        region_path: Sequence of region IDs the net's corridor traverses
            (empty when the net was not assigned a corridor).
        status: One of :data:`NET_STATUS_VALUES`.
    """

    name: str
    net_class: str | None
    pitch_mm: float
    layer_set: list[int]
    region_path: list[int]
    status: str

    def __post_init__(self) -> None:
        if self.status not in NET_STATUS_VALUES:
            raise ValueError(
                f"Invalid RoutingPlan net status {self.status!r}; "
                f"expected one of {sorted(NET_STATUS_VALUES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "class": self.net_class,
            "pitch_mm": self.pitch_mm,
            "layer_set": list(self.layer_set),
            "region_path": list(self.region_path),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NetPlanEntry:
        return cls(
            name=data["name"],
            net_class=data.get("class"),
            pitch_mm=data["pitch_mm"],
            layer_set=list(data.get("layer_set", [])),
            region_path=list(data.get("region_path", [])),
            status=data["status"],
        )


@dataclass
class EdgePlanEntry:
    """Per-undirected-edge demand/capacity/overflow.

    One row per undirected region-graph edge (never per directed edge).
    The region graph backs each pair with two directed ``RegionEdge``
    objects; ``demand`` / ``overflow`` sum **both** directions so they agree
    with ``RegionGraph.get_total_overflow()`` regardless of which direction
    carried the traffic (Issue #5544), while ``capacity`` / ``blockage_mm``
    are read from the ascending ``(min(a, b), max(a, b))`` edge alone
    because they are symmetric across the pair by construction.  See
    :meth:`RoutingPlan.from_global_result` for the full rationale.

    Attributes:
        a: Smaller region ID of the pair.
        b: Larger region ID of the pair.
        capacity: Scalar per-direction capacity (summed across the graph's
            SIGNAL layers -- plane layers carry none since Issue #5575).
        demand: Scalar utilization in **base-pitch units**, not net count
            (Issue #5575): a net whose class pitch is ``k`` times the base
            pitch contributes ``k``.  Summed across layers AND across both
            traversal directions of the pair.
        overflow: Sum of ``ceil(max(0, utilization - capacity))`` over both
            directed edges of the pair -- NOT ``max(0, demand - capacity)``,
            since each direction is measured against the same capacity
            independently (matching ``RegionGraph.get_total_overflow()``).
        blockage_mm: Pad blockage length along the boundary (mm).  Rule-area
            and preserved-copper blockage is tracked per layer on the graph
            (``RegionEdge.layer_blockage``) and shows up here as reduced
            ``capacity``.
        nets: Net IDs whose corridor crosses this edge.
        layers: Per-layer ``{"capacity": int, "demand": float}`` rows, keyed
            by the string layer index; ``demand`` likewise sums both
            directions.  Empty when the graph has no per-layer data
            (``num_layers <= 1``).  A PLANE layer has no row at all.
    """

    a: int
    b: int
    capacity: int
    demand: float
    overflow: int
    blockage_mm: float
    nets: list[int]
    layers: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "a": self.a,
            "b": self.b,
            "capacity": self.capacity,
            "demand": self.demand,
            "overflow": self.overflow,
            "blockage_mm": self.blockage_mm,
            "nets": list(self.nets),
            "layers": {k: dict(v) for k, v in self.layers.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EdgePlanEntry:
        return cls(
            a=data["a"],
            b=data["b"],
            capacity=data["capacity"],
            demand=data["demand"],
            overflow=data["overflow"],
            blockage_mm=data["blockage_mm"],
            nets=list(data.get("nets", [])),
            layers={k: dict(v) for k, v in data.get("layers", {}).items()},
        )


@dataclass
class OverflowReport:
    """Congestion summary for the global-routing pass.

    Deliberately named ``overflow_report`` -- never "certificate" -- to
    avoid confusion with the planarity certificate owned by
    ``monotone_certificate.py``.  Phase 1 is a report only; it does not
    prove infeasibility or certify anything.
    """

    iterations: int
    total_overflow: int
    overflowed_edges: int
    failed_nets: list[int]
    feasible: bool
    elapsed_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "iterations": self.iterations,
            "total_overflow": self.total_overflow,
            "overflowed_edges": self.overflowed_edges,
            "failed_nets": list(self.failed_nets),
            "feasible": self.feasible,
            "elapsed_s": self.elapsed_s,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OverflowReport:
        return cls(
            iterations=data["iterations"],
            total_overflow=data["total_overflow"],
            overflowed_edges=data["overflowed_edges"],
            failed_nets=list(data.get("failed_nets", [])),
            feasible=data["feasible"],
            elapsed_s=data["elapsed_s"],
        )


@dataclass
class RoutingPlan:
    """Report-only sidecar of a tile-based global-routing pass.

    Built from state the existing ``GlobalRouter`` / ``RegionGraph`` pass
    already computes (:meth:`from_global_result`); never mutates the
    graph.  See the module docstring for the full context.
    """

    schema_version: int = SCHEMA_VERSION
    source: dict[str, Any] = field(default_factory=dict)
    layers: dict[str, list[int]] = field(default_factory=dict)
    regions: dict[int, RegionInfo] = field(default_factory=dict)
    nets: dict[int, NetPlanEntry] = field(default_factory=dict)
    edges: list[EdgePlanEntry] = field(default_factory=list)
    overflow_report: OverflowReport | None = None
    relief: list[Any] = field(default_factory=list)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_global_result(
        cls,
        result: GlobalRoutingResult,
        graph: RegionGraph,
        *,
        net_order: Iterable[int],
        net_names: dict[int, str],
        net_class_map: dict[str, NetClassRouting] | None,
        layer_stack: LayerStack,
        tile_mm: float,
        elapsed_s: float,
        default_trace_width: float,
        default_trace_clearance: float,
        pour_skipped: Iterable[int] = (),
        single_pad: Iterable[int] = (),
    ) -> RoutingPlan:
        """Build a :class:`RoutingPlan` from an already-completed global pass.

        Read-only with respect to *result* / *graph*: this never calls
        ``update_utilization`` / ``release_utilization`` /
        ``update_history_costs`` / ``reset_utilization`` and only reads
        ``graph.edges`` / ``graph.regions`` (plus the public
        ``get_total_overflow`` / ``get_overflowed_edges`` queries), so
        building the plan cannot perturb subsequent routing.

        Args:
            result: The completed ``GlobalRoutingResult``.
            graph: The ``RegionGraph`` the result was computed against.
            net_order: The exact net-ID iterable passed as
                ``GlobalRouter.route_all(..., net_order=net_order)`` --
                i.e. every net that was a *candidate* for global routing
                (pour nets and single-pad nets already filtered out).
                Used to detect ``"no_endpoints"`` nets (Issue #5519: nets
                ``GlobalRouter.route_all`` silently drops because fewer
                than two of their pads resolved to a position).
            net_names: ``net_id -> net_name``.
            net_class_map: ``{net_name: NetClassRouting}`` (or ``None``).
            layer_stack: The grid's layer stack (for ``layers.signal`` /
                ``layers.plane`` reporting).
            tile_mm: The coarse-grid tile size used to build *graph*.
            elapsed_s: Wall-clock time spent in ``GlobalRouter.route_all``.
            default_trace_width: ``DesignRules.trace_width`` fallback for
                nets with no net-class-map entry.
            default_trace_clearance: ``DesignRules.trace_clearance``
                fallback (paired with ``default_trace_width`` above).
            pour_skipped: Net IDs skipped as pour nets before the global
                pass (``two_phase.py``'s local ``pour_nets``).
            single_pad: Net IDs filtered as trivially-connected single-pad
                nets before the global pass (``two_phase.py``'s local
                ``single_pad_nets``).

        Returns:
            A populated :class:`RoutingPlan`.
        """
        net_class_map = net_class_map or {}
        pour_skipped_set = set(pour_skipped)
        single_pad_set = set(single_pad)
        net_order_list = list(net_order)

        # Full net universe this plan reports on: every net considered as
        # a global-routing candidate, plus the ones filtered out before
        # the global pass so their disposition is not silently invisible.
        net_universe: list[int] = []
        seen_nets: set[int] = set()
        for net_id in (*net_order_list, *pour_skipped_set, *single_pad_set):
            if net_id not in seen_nets:
                seen_nets.add(net_id)
                net_universe.append(net_id)

        used_region_ids: set[int] = set()
        edge_nets: dict[tuple[int, int], list[int]] = {}
        nets: dict[int, NetPlanEntry] = {}

        for net_id in net_universe:
            name = net_names.get(net_id, f"Net {net_id}")
            ncr = net_class_map.get(name)
            if ncr is not None:
                pitch_mm = ncr.trace_width + ncr.clearance
                net_class = ncr.name
            else:
                pitch_mm = default_trace_width + default_trace_clearance
                net_class = None

            assignment = result.assignments.get(net_id)
            if assignment is not None:
                status = "assigned"
                region_path = list(assignment.region_path)
                layer_set = [assignment.layer]
                used_region_ids.update(region_path)
                for i in range(len(region_path) - 1):
                    a, b = region_path[i], region_path[i + 1]
                    key = (a, b) if a < b else (b, a)
                    edge_nets.setdefault(key, [])
                    if net_id not in edge_nets[key]:
                        edge_nets[key].append(net_id)
            elif net_id in result.failed_nets:
                status = "failed"
                region_path = []
                layer_set = []
            elif net_id in pour_skipped_set:
                status = "pour_skipped"
                region_path = []
                layer_set = []
            elif net_id in single_pad_set:
                status = "single_pad"
                region_path = []
                layer_set = []
            else:
                # GlobalRouter.route_all silently drops nets with fewer
                # than two resolvable pad positions (global_router.py).
                status = "no_endpoints"
                region_path = []
                layer_set = []

            nets[net_id] = NetPlanEntry(
                name=name,
                net_class=net_class,
                pitch_mm=pitch_mm,
                layer_set=layer_set,
                region_path=region_path,
                status=status,
            )

        edges: list[EdgePlanEntry] = []
        edge_lookup = getattr(graph, "_edge_lookup", {})
        for (a, b), net_ids in sorted(edge_nets.items()):
            # Every adjacent region pair is backed by TWO directed
            # ``RegionEdge`` objects (``source=a, target=b`` and
            # ``source=b, target=a``), and ``update_utilization()`` only
            # bumps the one matching a path's actual traversal direction.
            # ``RegionGraph.get_total_overflow()`` sums both directions per
            # undirected pair (Issue #5529 / PR #5540), so the traffic
            # counters here must be summed the same way -- reading only the
            # ascending (a, b) edge reported ``overflow=0`` for a pair whose
            # traffic ran descending-only (Issue #5544).
            edge = edge_lookup.get((a, b))
            if edge is None:
                continue
            reverse = edge_lookup.get((b, a))
            # ``capacity`` / ``layer_capacity`` / ``blockage`` are symmetric
            # across the pair by construction: ``RegionGraph._build_edges``
            # builds both directed edges from the same ``edge_capacity`` /
            # ``layer_cap``, and ``register_obstacles`` (via
            # ``_accumulate_edge_blockage``) recomputes both from the same
            # (order-independent) region-blockage average.  Only the
            # utilization-derived fields differ per direction, so only those
            # are combined.
            reverse_utilization = reverse.utilization if reverse is not None else 0
            reverse_overflow = reverse.overflow if reverse is not None else 0
            reverse_layer_utilization = reverse.layer_utilization if reverse is not None else {}
            layers_block: dict[str, dict[str, float]] = {}
            layer_indices = (
                set(edge.layer_capacity)
                | set(edge.layer_utilization)
                | set(reverse_layer_utilization)
            )
            for layer_idx in sorted(layer_indices):
                layers_block[str(layer_idx)] = {
                    "capacity": edge.layer_capacity.get(layer_idx, 0),
                    "demand": (
                        edge.layer_utilization.get(layer_idx, 0.0)
                        + reverse_layer_utilization.get(layer_idx, 0.0)
                    ),
                }
            edges.append(
                EdgePlanEntry(
                    a=a,
                    b=b,
                    capacity=edge.capacity,
                    demand=edge.utilization + reverse_utilization,
                    overflow=edge.overflow + reverse_overflow,
                    blockage_mm=edge.blockage,
                    nets=sorted(net_ids),
                    layers=layers_block,
                )
            )

        regions: dict[int, RegionInfo] = {}
        for region_id in sorted(used_region_ids):
            region = graph.regions.get(region_id)
            if region is None:
                continue
            regions[region_id] = RegionInfo(
                row=region.row,
                col=region.col,
                min_x=region.min_x,
                min_y=region.min_y,
                max_x=region.max_x,
                max_y=region.max_y,
            )

        total_overflow = graph.get_total_overflow()
        overflowed_edges = len(graph.get_overflowed_edges())
        overflow_report = OverflowReport(
            iterations=result.iterations,
            total_overflow=total_overflow,
            overflowed_edges=overflowed_edges,
            failed_nets=sorted(result.failed_nets),
            feasible=(total_overflow == 0 and not result.failed_nets),
            elapsed_s=elapsed_s,
        )

        # Issue #5575: ``LayerStack.signal_layers`` filters on
        # ``is_routable``, which is unconditionally True -- PLANE layers
        # included (#5014).  Reporting those as signal capacity is exactly
        # the dishonesty this phase removes, so derive the signal set as
        # "routable AND not a plane" and keep it in lockstep with the
        # ``signal_layer_indices`` the RegionGraph allocates capacity on.
        signal_layers = signal_layer_indices(layer_stack)
        plane_layers = [layer.index for layer in layer_stack.plane_layers]

        from kicad_tools import __version__ as kct_version

        source = {
            "pcb": None,
            "kct_version": kct_version,
            "layer_stack": layer_stack.name,
            "tile_mm": tile_mm,
            "cols": graph.num_cols,
            "rows": graph.num_rows,
        }

        return cls(
            schema_version=SCHEMA_VERSION,
            source=source,
            layers={"signal": signal_layers, "plane": plane_layers},
            regions=regions,
            nets=nets,
            edges=edges,
            overflow_report=overflow_report,
            relief=[],
        )

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": dict(self.source),
            "layers": {k: list(v) for k, v in self.layers.items()},
            "regions": {str(k): v.to_dict() for k, v in self.regions.items()},
            "nets": {str(k): v.to_dict() for k, v in self.nets.items()},
            "edges": [e.to_dict() for e in self.edges],
            "overflow_report": self.overflow_report.to_dict() if self.overflow_report else None,
            "relief": list(self.relief),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RoutingPlan:
        overflow_data = data.get("overflow_report")
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            source=dict(data.get("source", {})),
            layers={k: list(v) for k, v in data.get("layers", {}).items()},
            regions={int(k): RegionInfo.from_dict(v) for k, v in data.get("regions", {}).items()},
            nets={int(k): NetPlanEntry.from_dict(v) for k, v in data.get("nets", {}).items()},
            edges=[EdgePlanEntry.from_dict(e) for e in data.get("edges", [])],
            overflow_report=OverflowReport.from_dict(overflow_data) if overflow_data else None,
            relief=list(data.get("relief", [])),
        )

    def summary_line(self) -> str:
        """One-line text-mode summary: ``Routing plan: N nets, E edges,
        overflow O on K edges (T s)``."""
        if self.overflow_report is not None:
            total_overflow = self.overflow_report.total_overflow
            overflowed_edges = self.overflow_report.overflowed_edges
            elapsed_s = self.overflow_report.elapsed_s
        else:
            total_overflow = 0
            overflowed_edges = 0
            elapsed_s = 0.0
        return (
            f"Routing plan: {len(self.nets)} nets, {len(self.edges)} edges, "
            f"overflow {total_overflow} on {overflowed_edges} edges "
            f"({elapsed_s:.1f}s)"
        )

    def write_sidecar(self, path: Path | str, *, quiet: bool = False) -> bool:
        """Serialize this plan to *path* as JSON.

        Mirrors the non-fatal sidecar-write contract used elsewhere in the
        CLI (``_write_net_class_map_sidecar``): a blocked write (e.g. a
        read-only output directory, or an unexpectedly non-serializable
        value) is a warning, never a raised exception -- the route step
        must not fail just because a diagnostic sidecar could not be
        written.

        Args:
            path: Destination path for the JSON sidecar.
            quiet: When ``True``, suppress the warning line on failure.

        Returns:
            ``True`` if the sidecar was written, ``False`` if the write
            was blocked (a warning was printed unless ``quiet``).
        """
        target = Path(path)
        try:
            payload = self.to_dict()
            target.write_text(json.dumps(payload, indent=2))
        except (OSError, TypeError, ValueError) as e:
            if not quiet:
                print(f"  Warning: could not write routing-plan sidecar: {e}")
            return False
        return True


# =============================================================================
# Plan stage (Issue #5520, Epic #5510 Phase 1b)
#
# The tile-graph build + global pass below was previously inlined in
# ``TwoPhaseRouter.route_all`` (``algorithms/two_phase.py``), so only boards
# that reach the two-phase path (dense packages, or an explicit
# ``--two-phase``) produced a plan.  Factoring it here lets
# ``Autorouter.plan_routing`` run the SAME stage ahead of
# ``route_all_negotiated`` -- i.e. on every default ``kct route`` -- without
# switching non-dense boards onto the two-phase router (which would be a
# behaviour change).  ``TwoPhaseRouter`` calls these helpers too, so dense
# and non-dense boards share one implementation and cannot drift.
# =============================================================================

#: Tile size heuristic: ~10x the routing pitch per tile, minimum 1 mm.
#: Unchanged from the pre-#5520 inline two-phase code.
TILE_PITCH_FACTOR = 10.0

#: Minimum coarse-grid dimension (3x3), matching the inline two-phase code.
MIN_TILE_DIM = 3

#: Negotiated-iteration budget for the global pass (pre-#5520 constant).
GLOBAL_MAX_ITERATIONS = 15

#: History-cost increment per overflowed edge per iteration (pre-#5520).
GLOBAL_HISTORY_INCREMENT = 1.0


@dataclass
class PlanNetSelection:
    """The net universe a plan is computed over.

    Attributes:
        net_order: Nets handed to ``GlobalRouter.route_all`` (priority
            order, pour / single-pad nets already removed).
        pour_nets: Nets filtered out as pour nets (zone-filled).
        single_pad_nets: Nets filtered out as trivially connected.
    """

    net_order: list[int]
    pour_nets: list[int]
    single_pad_nets: list[int]


@dataclass
class PlanBuildResult:
    """Everything one plan stage produced.

    ``plan`` is ``None`` when the caller passed ``emit=False``; the graph
    and the global-routing result are always returned so the two-phase
    router can keep using them for corridor extraction.
    """

    plan: RoutingPlan | None
    region_graph: RegionGraph
    global_result: GlobalRoutingResult
    tile_mm: float
    elapsed_s: float


def select_plan_nets(
    router: Any,
    *,
    report: Callable[[str], None] | None = None,
) -> PlanNetSelection:
    """Reproduce the two-phase global-pass net filter.

    Args:
        router: Anything exposing ``nets`` / ``net_names`` /
            ``net_class_map`` / ``_get_net_priority`` and (optionally)
            ``_pour_nets_without_zones`` / ``_interleave_match_groups`` /
            ``_apply_byte_lane_inner_priority``.  Both
            :class:`~kicad_tools.router.algorithms.two_phase.TwoPhaseRouter`
            and :class:`~kicad_tools.router.core.Autorouter` qualify --
            deliberately, so a dense board's plan and the same board's
            plan built from the ``Autorouter`` agree on ``nets``.
        report: Optional sink for the two "Skipping N ..." diagnostic
            lines.  ``None`` (the default) keeps the selection SILENT,
            which is what the ``route_all_negotiated`` hook needs: the
            plan stage must not add stdout to the many unit tests that
            call it directly.

    Returns:
        A :class:`PlanNetSelection`.
    """
    nets = router.nets
    net_names = router.net_names
    net_class_map = getattr(router, "net_class_map", None) or {}
    pour_without_zones = getattr(router, "_pour_nets_without_zones", None) or set()

    def _emit(message: str) -> None:
        if report is not None:
            report(message)

    net_order = sorted(nets.keys(), key=lambda n: router._get_net_priority(n))
    net_order = [n for n in net_order if n != 0]

    # Issue #1295: pour nets are connected via zone fills.
    # Issue #1841: pour nets WITHOUT zones route as ordinary signals.
    pour_nets: list[int] = []
    signal_nets: list[int] = []
    for n in net_order:
        net_name = net_names.get(n, "")
        if net_name in pour_without_zones:
            signal_nets.append(n)
            continue
        net_class = net_class_map.get(net_name)
        if net_class and net_class.is_pour_net:
            pour_nets.append(n)
        else:
            signal_nets.append(n)
    if pour_nets:
        pour_names = [net_names.get(n, f"Net {n}") for n in pour_nets]
        _emit(f"  Skipping {len(pour_nets)} pour net(s) (use zone fill instead): {pour_names}")
    net_order = signal_nets

    # Single-pad nets are trivially connected (mirrors core.py's filter).
    single_pad_nets: list[int] = []
    multi_pad_nets: list[int] = []
    for n in net_order:
        if len(nets.get(n, [])) < 2:
            single_pad_nets.append(n)
        else:
            multi_pad_nets.append(n)
    if single_pad_nets:
        _emit(f"  Skipping {len(single_pad_nets)} single-pad net(s) (trivially connected)")
    net_order = multi_pad_nets

    # Issue #2914 / #2962 / #2983 / #4051: fairness + byte-lane ordering
    # passes, when the caller exposes them.
    interleave = getattr(router, "_interleave_match_groups", None)
    if interleave is not None:
        net_order = interleave(net_order)
    byte_lane = getattr(router, "_apply_byte_lane_inner_priority", None)
    if byte_lane is not None:
        net_order = byte_lane(net_order)

    return PlanNetSelection(
        net_order=net_order,
        pour_nets=pour_nets,
        single_pad_nets=single_pad_nets,
    )


#: Type of one blockage rectangle: ``(min_x, min_y, max_x, max_y)`` in mm.
BlockageRect = tuple[float, float, float, float]


def _segment_layer_index(grid: Any, segment: Any) -> int | None:
    """Resolve a ``Segment``'s layer to a grid layer index.

    Router segments carry a ``Layer`` enum (whose ``.value`` is a KiCad
    layer id, NOT a stack index), but some library callers build them with
    a bare integer index.  Both are accepted; an unresolvable layer returns
    ``None`` so the caller can fall back to "every signal layer".
    """
    layer = getattr(segment, "layer", None)
    if layer is None:
        return None
    value = getattr(layer, "value", layer)
    try:
        return int(grid.layer_to_index(int(value)))
    except Exception:
        if isinstance(value, int) and 0 <= value < int(getattr(grid, "num_layers", 0)):
            return int(value)
        return None


def collect_blockage_rects(router: Any) -> list[tuple[BlockageRect, list[int] | None]]:
    """Blockage beyond pads, as ``(rect, layers)`` pairs (Issue #5575).

    Two sources, both of which the detailed router already honours but
    which the plan's capacity model was blind to:

    1. **Keepout rule areas** -- ``(zone ... (keepout ...))`` areas that
       block tracks and/or vias, via the router's
       ``_keepout_rule_area_polygons()`` hook.  Each polygon contributes
       its *bounding box* (conservative: a convex hull would be tighter,
       but over-blocking a boundary only under-reports capacity).  The
       ``spatial_keepouts`` per-class read/exempt filters are deliberately
       IGNORED here: a per-class exemption cannot be expressed as a scalar
       edge blockage, so the plan treats such an area as blocking for
       everyone (the conservative direction).
    2. **Preserved copper** -- ``router.existing_routes``, populated under
       ``--preserve-existing`` / ``--nets`` / ``--complete``.  Each
       ``Segment`` becomes its axis-aligned bbox padded by
       ``width / 2 + trace_clearance``; each ``Via`` a square of
       ``diameter + clearance`` on every signal layer.

    Copper **pours are deliberately excluded**: zones are never loaded into
    the routing grid on the ``kct route`` path (pours are filled *after*
    routing and flow around traces), so counting them would make the plan
    stricter than the router it describes and report overflow the router
    never experiences.

    Args:
        router: ``Autorouter`` or ``TwoPhaseRouter``.  Both hooks are read
            with ``getattr``, so a caller exposing neither (a bare unit-test
            router) simply contributes no blockage.

    Returns:
        List of ``(rect, layers)``; ``layers is None`` means "every signal
        layer".
    """
    out: list[tuple[BlockageRect, list[int] | None]] = []

    # --- 1. Keepout rule areas -------------------------------------------
    provider = getattr(router, "_keepout_rule_area_polygons", None)
    if callable(provider):
        try:
            areas = provider() or []
        except Exception:  # defensive: a bad board must not fail the plan
            areas = []
        for area in areas:
            polygon = getattr(area, "polygon", None)
            if not polygon:
                continue
            xs = [float(p[0]) for p in polygon]
            ys = [float(p[1]) for p in polygon]
            layers = getattr(area, "layers", None)
            out.append(
                (
                    (min(xs), min(ys), max(xs), max(ys)),
                    sorted(layers) if layers else None,
                )
            )

    # --- 2. Preserved copper ---------------------------------------------
    routes = getattr(router, "existing_routes", None) or []
    if routes:
        grid = router.grid
        clearance = float(router.rules.trace_clearance)
        for route in routes:
            for segment in getattr(route, "segments", ()):
                pad = float(segment.width) / 2.0 + clearance
                x_lo, x_hi = sorted((float(segment.x1), float(segment.x2)))
                y_lo, y_hi = sorted((float(segment.y1), float(segment.y2)))
                index = _segment_layer_index(grid, segment)
                out.append(
                    (
                        (x_lo - pad, y_lo - pad, x_hi + pad, y_hi + pad),
                        None if index is None else [index],
                    )
                )
            for via in getattr(route, "vias", ()):
                half = (float(via.diameter) + clearance) / 2.0
                out.append(
                    (
                        (
                            float(via.x) - half,
                            float(via.y) - half,
                            float(via.x) + half,
                            float(via.y) + half,
                        ),
                        None,  # a via pierces the stack: block every signal layer
                    )
                )

    return out


def _register_blockage(region_graph: RegionGraph, router: Any) -> None:
    """Feed :func:`collect_blockage_rects` into the graph, batched by layer set."""
    grouped: dict[tuple[int, ...] | None, list[BlockageRect]] = {}
    for rect, layers in collect_blockage_rects(router):
        key = None if layers is None else tuple(layers)
        grouped.setdefault(key, []).append(rect)
    for key, rects in grouped.items():
        region_graph.register_blockage_rects(rects, layers=None if key is None else list(key))


def build_plan(
    router: Any,
    *,
    net_order: Iterable[int],
    pour_nets: Iterable[int] = (),
    single_pad_nets: Iterable[int] = (),
    corridor_width_factor: float = 2.0,
    emit: bool = True,
    report: Callable[[str], None] | None = None,
) -> PlanBuildResult:
    """Run one tile-based global-routing pass and (optionally) plan it.

    This is the stage factored out of ``TwoPhaseRouter.route_all``: build
    the coarse :class:`~kicad_tools.router.region_graph.RegionGraph` with
    geometry-based capacity, register pads as blockage, run
    :class:`~kicad_tools.router.global_router.GlobalRouter` with negotiated
    iteration, and serialize the outcome into a :class:`RoutingPlan`.

    **Report-only.**  Nothing here touches ``router.grid``: no
    ``set_corridor_preference``, no obstacle marking, no RNG draw.  The
    only mutated object is the freshly-constructed ``RegionGraph``.  That
    is what lets ``Autorouter.route_all_negotiated`` run the stage on
    boards that never reach the two-phase router while keeping the routed
    copper byte-identical (Epic #5510 Phase 1 is report-only).

    Args:
        router: ``TwoPhaseRouter`` or ``Autorouter`` (needs ``grid``,
            ``rules``, ``nets``, ``net_names``, ``net_class_map``,
            ``pads``).
        net_order: Pre-filtered net order (see :func:`select_plan_nets`).
        pour_nets: Nets filtered out as pour nets, for plan reporting.
        single_pad_nets: Nets filtered out as single-pad, for reporting.
        corridor_width_factor: Corridor half-width as a multiple of the
            design-rule clearance (two-phase default 2.0).
        emit: When ``False``, run the pass but skip building the
            ``RoutingPlan`` (``PlanBuildResult.plan is None``).  The
            two-phase router still needs the corridors, so the pass
            itself always runs there.
        report: Optional sink for the one-line tile-grid diagnostic.
            ``None`` keeps the stage silent.

    Returns:
        A :class:`PlanBuildResult`.
    """
    from .global_router import GlobalRouter
    from .region_graph import RegionGraph

    grid = router.grid
    rules = router.rules

    # Routing pitch and tile sizing -- identical to the pre-#5520 inline code.
    trace_pitch = rules.trace_width + rules.trace_clearance
    corridor_width = corridor_width_factor * rules.trace_clearance
    tile_size = max(trace_pitch * TILE_PITCH_FACTOR, 1.0)
    num_cols = max(MIN_TILE_DIM, int(grid.width / tile_size))
    num_rows = max(MIN_TILE_DIM, int(grid.height / tile_size))

    # Issue #5575: only non-PLANE layers advertise capacity.  ``grid.
    # num_layers`` is still passed so the graph knows the stack depth, but
    # capacity (and the GlobalRouter's round-robin layer choice) is
    # confined to these indices.
    plan_signal_layers = signal_layer_indices(grid.layer_stack)

    # Issue #5575: per-class pitch.  ``Autorouter.net_class_map`` is keyed
    # by net NAME, so the lambda resolves id -> name -> class; nets with no
    # class entry fall back to the design-rule pitch (weight 1.0).
    net_class_map = getattr(router, "net_class_map", None) or {}
    net_names = router.net_names

    def _pitch_for_net(net_id: int) -> float:
        ncr = net_class_map.get(net_names.get(net_id, ""))
        if ncr is None:
            return float(trace_pitch)
        return float(ncr.trace_width) + float(ncr.clearance)

    region_graph = RegionGraph(
        board_width=grid.width,
        board_height=grid.height,
        origin_x=grid.origin_x,
        origin_y=grid.origin_y,
        num_cols=num_cols,
        num_rows=num_rows,
        trace_pitch=trace_pitch,
        num_layers=grid.num_layers,
        signal_layer_indices=plan_signal_layers,
        pitch_for_net=_pitch_for_net,
    )

    # Register pads as obstacles for blockage-aware capacity.
    region_graph.register_obstacles(list(router.pads.values()))
    # Issue #5575: blockage beyond pads -- keepout rule areas and preserved
    # copper.  Read-only with respect to the router (see
    # ``collect_blockage_rects``); it only mutates the fresh RegionGraph.
    _register_blockage(region_graph, router)

    if report is not None:
        stats = region_graph.get_statistics()
        report(
            f"  Tile grid: {num_cols}x{num_rows} "
            f"({stats['num_regions']} regions, {stats['num_edges']} edges, "
            f"pitch={trace_pitch:.3f}mm, signal layers={len(plan_signal_layers)}"
            f"/{grid.num_layers})"
        )

    global_router = GlobalRouter(
        region_graph=region_graph,
        corridor_width=corridor_width,
        default_layer=0,
        negotiated=True,
        max_iterations=GLOBAL_MAX_ITERATIONS,
        history_increment=GLOBAL_HISTORY_INCREMENT,
    )

    net_order_list = list(net_order)
    started = time.time()
    global_result = global_router.route_all(
        nets=router.nets,
        pad_dict=router.pads,
        net_order=net_order_list,
    )
    elapsed_s = time.time() - started

    plan: RoutingPlan | None = None
    if emit:
        plan = RoutingPlan.from_global_result(
            global_result,
            region_graph,
            net_order=net_order_list,
            net_names=router.net_names,
            net_class_map=getattr(router, "net_class_map", None),
            layer_stack=grid.layer_stack,
            tile_mm=tile_size,
            elapsed_s=elapsed_s,
            default_trace_width=rules.trace_width,
            default_trace_clearance=rules.trace_clearance,
            pour_skipped=pour_nets,
            single_pad=single_pad_nets,
        )

    return PlanBuildResult(
        plan=plan,
        region_graph=region_graph,
        global_result=global_result,
        tile_mm=tile_size,
        elapsed_s=elapsed_s,
    )

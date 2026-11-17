"""Joint min-cost-flow assignment of dense-package pins to escape exits.

Issue #6352 (candidate 1 of epic #5947).  EXPERIMENTAL, opt-in only.

The existing :class:`~kicad_tools.router.escape_corridor.EscapeCorridorPlanner`
assigns escape corridors *greedily*: every pin escapes through the bbox face
it sits nearest, pins are bucketed by ``(face, destination octant)``, and the
resulting clusters are round-robined over the routable layers.  No step ever
asks whether a face/layer channel is *oversubscribed* -- if twenty pins all
want the east face on layer 1, twenty pins get a corridor there.

This module poses the same decision as a single joint problem:

    source --(1)--> pin --(cost)--> exit slot (face, layer) --(capacity)--> sink

and solves it with min-cost max-flow, so every pin is assigned to exactly one
exit, no exit carries more pins than its physical capacity, and the total
detour + via + congestion cost is minimal.  The joint formulation is the idea
TraceMaker describes for escape routing; that project is GPL-3.0-or-later and
``copyleft-ideas-only`` in the ecosystem registry, so **nothing here is
derived from its source** -- the model and the solver below are written from
the textbook successive-shortest-path algorithm (Edmonds-Karp augmentation
with Johnson potentials and Dijkstra), and the cost/capacity mapping is this
repository's own hypothesis, documented in
``docs/research/kct-route-escape-flow-experiment.md``.

Two layers, both pure (no grid, no router):

* :func:`min_cost_flow` -- a small general-purpose min-cost-flow solver over
  integer capacities and non-negative integer costs.  Graphs are per-package
  (a few hundred arcs), so a hand-rolled solver is cheaper than a new
  dependency (``scipy``/``networkx`` are not core dependencies).
* :func:`assign_pins_to_exits` -- the bipartite pin -> exit assignment built
  on top of it, returning an :class:`EscapeFlowAssignment` that records which
  pins could not be assigned (``feasible=False``) instead of raising.

**Determinism.**  Every tie is broken by insertion order: pins are numbered
in the caller's (sorted) order, exits likewise, arcs are relaxed in insertion
order, and Dijkstra pops ``(distance, node_index)`` tuples, so an equal-cost
alternative always resolves to the lowest-index pin/exit.  The same inputs
therefore always yield the same assignment -- a requirement for
``kct route --seed`` reproducibility.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# A large-but-finite sentinel distance.  Integer costs keep the solver exact
# (no float tie wobble), so "infinity" is just bigger than any real path.
_INF = 1 << 62


# =============================================================================
# General min-cost flow (successive shortest paths)
# =============================================================================


@dataclass
class _Arc:
    to: int
    rev: int  # index of the reverse arc in ``graph[to]``
    cap: int
    cost: int


@dataclass
class FlowResult:
    """Result of :func:`min_cost_flow`.

    Attributes:
        flow: Total units pushed from ``source`` to ``sink``.
        cost: Total cost of that flow.
        arc_flow: Flow on each *forward* arc, indexed in the order the arcs
            were supplied to :func:`min_cost_flow`.
        augmentations: Number of shortest-path augmentations performed
            (instrumentation; bounded by ``flow``).
    """

    flow: int
    cost: int
    arc_flow: list[int]
    augmentations: int


def min_cost_flow(
    num_nodes: int,
    arcs: Sequence[tuple[int, int, int, int]],
    source: int,
    sink: int,
    *,
    max_flow: int | None = None,
) -> FlowResult:
    """Min-cost (max-)flow by successive shortest paths.

    Args:
        num_nodes: Node count; nodes are ``0 .. num_nodes - 1``.
        arcs: ``(tail, head, capacity, cost)`` tuples.  Capacities and costs
            must be non-negative integers (non-negative costs let the first
            Dijkstra run with zero potentials).
        source: Source node.
        sink: Sink node.
        max_flow: Optional cap on the total flow; ``None`` pushes the maximum.

    Returns:
        A :class:`FlowResult`.  Among all maximum flows (or all flows of
        value ``max_flow``) the returned one has minimum total cost.

    Raises:
        ValueError: On a negative capacity/cost or an out-of-range node.
    """
    if not (0 <= source < num_nodes and 0 <= sink < num_nodes):
        raise ValueError("source/sink out of range")
    graph: list[list[_Arc]] = [[] for _ in range(num_nodes)]
    handles: list[tuple[int, int]] = []
    for tail, head, cap, cost in arcs:
        if not (0 <= tail < num_nodes and 0 <= head < num_nodes):
            raise ValueError(f"arc ({tail}, {head}) references an unknown node")
        if cap < 0 or cost < 0:
            raise ValueError("min_cost_flow requires non-negative capacity and cost")
        graph[tail].append(_Arc(head, len(graph[head]), int(cap), int(cost)))
        graph[head].append(_Arc(tail, len(graph[tail]) - 1, 0, -int(cost)))
        handles.append((tail, len(graph[tail]) - 1))

    limit = _INF if max_flow is None else int(max_flow)
    potential = [0] * num_nodes
    total_flow = 0
    total_cost = 0
    augmentations = 0

    while total_flow < limit:
        # Dijkstra on reduced costs.  Heap entries are (dist, node) so equal
        # distances pop in node-index order -- the determinism contract.
        dist = [_INF] * num_nodes
        prev: list[tuple[int, int] | None] = [None] * num_nodes
        dist[source] = 0
        heap: list[tuple[int, int]] = [(0, source)]
        while heap:
            d, u = heapq.heappop(heap)
            if d != dist[u]:
                continue
            pu = potential[u]
            for idx, arc in enumerate(graph[u]):
                if arc.cap <= 0:
                    continue
                nd = d + arc.cost + pu - potential[arc.to]
                if nd < dist[arc.to]:
                    dist[arc.to] = nd
                    prev[arc.to] = (u, idx)
                    heapq.heappush(heap, (nd, arc.to))
        if dist[sink] >= _INF:
            break
        for v in range(num_nodes):
            if dist[v] < _INF:
                potential[v] += dist[v]

        # Bottleneck along the path, then augment.
        push = limit - total_flow
        v = sink
        while v != source:
            step = prev[v]
            assert step is not None
            u, idx = step
            push = min(push, graph[u][idx].cap)
            v = u
        v = sink
        while v != source:
            step = prev[v]
            assert step is not None
            u, idx = step
            arc = graph[u][idx]
            arc.cap -= push
            graph[v][arc.rev].cap += push
            total_cost += push * arc.cost
            v = u
        total_flow += push
        augmentations += 1

    arc_flow = []
    for (tail, idx), (_t, _h, cap, _c) in zip(handles, arcs, strict=True):
        arc_flow.append(int(cap) - graph[tail][idx].cap)
    return FlowResult(
        flow=total_flow, cost=total_cost, arc_flow=arc_flow, augmentations=augmentations
    )


# =============================================================================
# Pin -> exit assignment
# =============================================================================


@dataclass(frozen=True)
class ExitSlot:
    """One escape exit a pin may be assigned to.

    Attributes:
        face: Package bbox face (``"N"``/``"S"``/``"E"``/``"W"``).
        layer_idx: Routing-grid layer index of the exit.
        capacity: Maximum number of pins this exit can carry (tracks that
            physically fit through it).  ``0`` makes the exit unusable.
        load_step_cost: Convex congestion term: the ``k``-th pin (0-based)
            assigned to this exit pays an extra ``k * load_step_cost``.  This
            spreads load across equally-good exits (e.g. inner layers)
            instead of filling the lowest-index one to capacity first.
    """

    face: str
    layer_idx: int
    capacity: int
    load_step_cost: int = 0

    @property
    def key(self) -> tuple[str, int]:
        return (self.face, self.layer_idx)


@dataclass
class EscapeFlowAssignment:
    """Outcome of :func:`assign_pins_to_exits`.

    Attributes:
        assignment: ``pin index -> exit index`` for every assigned pin.
        unassigned: Pin indices the flow could not place (no arc, or every
            reachable exit at capacity), ascending.
        total_cost: Sum of assigned arc costs plus convex load costs.
        total_capacity: Sum of exit capacities (instrumentation: lets a
            report distinguish "oversubscribed" from "disconnected").
        augmentations: Solver augmentations (instrumentation).
    """

    assignment: dict[int, int] = field(default_factory=dict)
    unassigned: list[int] = field(default_factory=list)
    total_cost: int = 0
    total_capacity: int = 0
    augmentations: int = 0

    @property
    def feasible(self) -> bool:
        """True when every pin received an exit."""
        return not self.unassigned

    @property
    def assigned_count(self) -> int:
        return len(self.assignment)

    def exit_loads(self, num_exits: int) -> list[int]:
        """Pins assigned to each exit, indexed by exit."""
        loads = [0] * num_exits
        for e in self.assignment.values():
            loads[e] += 1
        return loads


def assign_pins_to_exits(
    num_pins: int,
    exits: Sequence[ExitSlot],
    costs: Mapping[tuple[int, int], int],
) -> EscapeFlowAssignment:
    """Jointly assign pins to exits at minimum total cost.

    Args:
        num_pins: Pin count; pins are ``0 .. num_pins - 1`` in the caller's
            canonical (sorted) order.  Ties resolve toward lower indices.
        exits: Candidate exits; ties resolve toward lower indices.
        costs: ``(pin, exit) -> non-negative integer cost`` for every LEGAL
            pairing.  A missing pair means "this pin may not use this exit"
            (e.g. a pad that is disconnected from it).

    Returns:
        An :class:`EscapeFlowAssignment`.  The flow is maximum first (as many
        pins as capacity allows) and minimum-cost among maximum flows.  Pins
        the flow could not place are listed in ``unassigned`` -- an
        infeasible problem is a *result*, never an exception, so callers can
        fall back cleanly.
    """
    num_exits = len(exits)
    source = 0
    pin_base = 1
    exit_base = pin_base + num_pins
    sink = exit_base + num_exits
    arcs: list[tuple[int, int, int, int]] = []

    for p in range(num_pins):
        arcs.append((source, pin_base + p, 1, 0))
    pair_arc: list[tuple[int, int, int]] = []  # (arc index, pin, exit)
    for p in range(num_pins):
        for e in range(num_exits):
            c = costs.get((p, e))
            if c is None or exits[e].capacity <= 0:
                continue
            if c < 0:
                raise ValueError(f"negative cost for pin {p} exit {e}")
            pair_arc.append((len(arcs), p, e))
            arcs.append((pin_base + p, exit_base + e, 1, int(c)))
    total_capacity = 0
    for e, slot in enumerate(exits):
        cap = max(0, int(slot.capacity))
        total_capacity += cap
        if cap == 0:
            continue
        if slot.load_step_cost <= 0:
            arcs.append((exit_base + e, sink, cap, 0))
        else:
            # Convex cost as unit-capacity tiers k = 0..cap-1, but never more
            # tiers than there are pins (a tier no pin can fill is dead weight).
            for k in range(min(cap, num_pins)):
                arcs.append((exit_base + e, sink, 1, k * int(slot.load_step_cost)))

    result = min_cost_flow(sink + 1, arcs, source, sink)
    assignment: dict[int, int] = {}
    for arc_idx, p, e in pair_arc:
        if result.arc_flow[arc_idx] > 0:
            assignment[p] = e
    unassigned = [p for p in range(num_pins) if p not in assignment]
    return EscapeFlowAssignment(
        assignment=assignment,
        unassigned=unassigned,
        total_cost=result.cost,
        total_capacity=total_capacity,
        augmentations=result.augmentations,
    )


# =============================================================================
# Geometric cost model (repository-owned hypothesis)
# =============================================================================


@dataclass(frozen=True)
class EscapeFlowCostModel:
    """Maps pin/exit geometry to the integer arc costs the solver consumes.

    Every term is a *hypothesis* under test in issue #6352, not a measured
    constant; the research note records the values used.

    Attributes:
        under_body_factor: Multiplier on the in-package leg when a pin is
            sent to a face other than its own (it must travel under the
            package body, typically on an inner layer after an in-pad via).
        via_cost_mm: Equivalent length charged when the exit layer differs
            from the pad's own copper layer (one escape via).
        congestion_cost_mm: Equivalent length charged per unit of normalised
            congestion demand (``[0, 1]``) at an exit face.
        load_step_mm: Convex load cost per already-assigned pin on an exit;
            see :attr:`ExitSlot.load_step_cost`.
        scale: Cost units per mm (integer costs keep ties exact).
    """

    under_body_factor: float = 2.0
    via_cost_mm: float = 1.0
    congestion_cost_mm: float = 2.0
    load_step_mm: float = 0.05
    scale: int = 1000

    def to_units(self, mm: float) -> int:
        return max(0, int(round(mm * self.scale)))


def exit_point(
    face: str, x: float, y: float, bbox: tuple[float, float, float, float]
) -> tuple[float, float]:
    """Project ``(x, y)`` onto the given bbox face (clamped to the face span)."""
    min_x, min_y, max_x, max_y = bbox
    cx = min(max(x, min_x), max_x)
    cy = min(max(y, min_y), max_y)
    if face == "N":
        return (cx, min_y)
    if face == "S":
        return (cx, max_y)
    if face == "W":
        return (min_x, cy)
    return (max_x, cy)


def pin_exit_cost_mm(
    *,
    pin_xy: tuple[float, float],
    pin_face: str,
    dest_xy: tuple[float, float],
    exit_face: str,
    bbox: tuple[float, float, float, float],
    needs_via: bool,
    exit_congestion: float,
    model: EscapeFlowCostModel,
) -> float:
    """Equivalent-length cost (mm) of escaping ``pin`` through ``exit_face``.

    ``in-package leg * (under_body_factor if foreign face) + exit-to-dest leg
    + via + congestion``.  Pure geometry -- the planner supplies the inputs.
    """
    ex, ey = exit_point(exit_face, pin_xy[0], pin_xy[1], bbox)
    leg_in = math.hypot(ex - pin_xy[0], ey - pin_xy[1])
    if exit_face != pin_face:
        leg_in *= model.under_body_factor
    leg_out = math.hypot(dest_xy[0] - ex, dest_xy[1] - ey)
    cost = leg_in + leg_out
    if needs_via:
        cost += model.via_cost_mm
    cost += model.congestion_cost_mm * max(0.0, min(1.0, exit_congestion))
    return cost

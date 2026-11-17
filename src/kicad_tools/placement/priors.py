"""Netlist graph analysis for physics-informed placement priors.

Analyzes net connectivity to extract placement priors -- domain knowledge
that seeds the optimizer with a reasonable starting region.  This is the PCB
equivalent of physics-informed priors in EM simulation optimization.

Four analysis capabilities:

1. **Affinity graph**: edge weight = number of shared nets between two
   components.  Used to place high-affinity components close together.
2. **Connected clusters**: groups of tightly-connected components identified
   via greedy modularity maximisation on the affinity graph.
3. **Power domain detection**: groups components by shared power/ground nets.
4. **Signal flow ordering**: topological ordering from source connectors
   through processing to sink connectors.

Two placement prior functions:

- :func:`schematic_proximity_prior`: places high-affinity components close
  together using the weighted-centroid rule.
- :func:`power_domain_clustering`: groups components by power domain.

Usage::

    from kicad_tools.placement.priors import (
        build_affinity_graph,
        find_clusters,
        detect_power_domains,
        detect_signal_flow,
        schematic_proximity_prior,
        power_domain_clustering,
    )
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from numpy.typing import NDArray

from .cost import BoardOutline, Net
from .vector import (
    FIELDS_PER_COMPONENT,
    ComponentDef,
    PlacementVector,
)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AffinityGraph:
    """Weighted undirected graph of component connectivity.

    Attributes:
        references: Ordered list of component reference designators.
        weights: NxN symmetric matrix where ``weights[i][j]`` is the number
            of nets shared between components *i* and *j*.
    """

    references: tuple[str, ...]
    weights: NDArray[np.float64]

    @property
    def num_components(self) -> int:
        """Number of components in the graph."""
        return len(self.references)

    def weight(self, ref_a: str, ref_b: str) -> float:
        """Return the affinity weight between two components.

        Returns 0.0 if either reference is not in the graph.
        """
        ref_to_idx = {r: i for i, r in enumerate(self.references)}
        idx_a = ref_to_idx.get(ref_a)
        idx_b = ref_to_idx.get(ref_b)
        if idx_a is None or idx_b is None:
            return 0.0
        return float(self.weights[idx_a, idx_b])


@dataclass(frozen=True)
class ComponentGroup:
    """A group of component references identified by analysis.

    Attributes:
        name: Human-readable group label (e.g. "cluster-0", "VCC domain").
        references: Component reference designators in this group.
    """

    name: str
    references: tuple[str, ...]


@dataclass(frozen=True)
class SignalFlowResult:
    """Result of signal flow analysis.

    Attributes:
        ordering: Topological ordering of component references from sources
            to sinks.  Components not reachable from any source appear at the
            end in their original order.
        sources: References identified as signal sources (input connectors).
        sinks: References identified as signal sinks (output connectors).
    """

    ordering: tuple[str, ...]
    sources: tuple[str, ...]
    sinks: tuple[str, ...]


# ---------------------------------------------------------------------------
# Affinity graph construction
# ---------------------------------------------------------------------------

# Common power/ground net name patterns (case-insensitive prefix match)
_POWER_NET_PREFIXES = (
    "vcc",
    "vdd",
    "v3.3",
    "v3v3",
    "v5",
    "v1.8",
    "v1v8",
    "v2.5",
    "v12",
    "+3v3",
    "+5v",
    "+12v",
    "+3.3v",
    "+1.8v",
    "avcc",
    "avdd",
    "dvcc",
    "dvdd",
    "vin",
    "vout",
    "vbus",
    "vsys",
    "vbat",
)

_GROUND_NET_PREFIXES = (
    "gnd",
    "agnd",
    "dgnd",
    "pgnd",
    "vss",
    "avss",
    "dvss",
    "ground",
)


def _is_power_or_ground_net(name: str) -> bool:
    """Check whether a net name looks like a power or ground rail."""
    lower = name.lower().strip()
    for prefix in _POWER_NET_PREFIXES + _GROUND_NET_PREFIXES:
        if lower == prefix or lower.startswith(prefix + "_") or lower.startswith(prefix + "/"):
            return True
    # Exact match patterns
    if lower in {"gnd", "vcc", "vdd", "vss", "vee"}:
        return True
    return False


def _is_power_net(name: str) -> bool:
    """Check whether a net name looks like a power rail (not ground)."""
    lower = name.lower().strip()
    for prefix in _POWER_NET_PREFIXES:
        if lower == prefix or lower.startswith(prefix + "_") or lower.startswith(prefix + "/"):
            return True
    return False


def _is_ground_net(name: str) -> bool:
    """Check whether a net name looks like a ground rail."""
    lower = name.lower().strip()
    for prefix in _GROUND_NET_PREFIXES:
        if lower == prefix or lower.startswith(prefix + "_") or lower.startswith(prefix + "/"):
            return True
    return False


def build_affinity_graph(
    components: Sequence[ComponentDef],
    nets: Sequence[Net],
    *,
    exclude_power_nets: bool = False,
) -> AffinityGraph:
    """Build a component affinity graph from netlist connectivity.

    Each edge weight equals the number of nets shared between a pair of
    components.  Power and ground nets can optionally be excluded to focus
    on signal connectivity only.

    Args:
        components: Component definitions.
        nets: Net connectivity information.
        exclude_power_nets: If True, skip nets whose names match common
            power/ground patterns when computing affinity weights.

    Returns:
        An :class:`AffinityGraph` with the weighted adjacency matrix.
    """
    n = len(components)
    references = tuple(c.reference for c in components)
    ref_to_idx: dict[str, int] = {r: i for i, r in enumerate(references)}
    weights = np.zeros((n, n), dtype=np.float64)

    for net in nets:
        if exclude_power_nets and _is_power_or_ground_net(net.name):
            continue

        # Collect unique component indices connected by this net
        indices: set[int] = set()
        for ref, _ in net.pins:
            idx = ref_to_idx.get(ref)
            if idx is not None:
                indices.add(idx)

        # Add edge weight for every pair
        idx_list = sorted(indices)
        for a_pos in range(len(idx_list)):
            for b_pos in range(a_pos + 1, len(idx_list)):
                weights[idx_list[a_pos], idx_list[b_pos]] += 1.0
                weights[idx_list[b_pos], idx_list[a_pos]] += 1.0

    return AffinityGraph(references=references, weights=weights)


# ---------------------------------------------------------------------------
# Cluster detection (greedy modularity)
# ---------------------------------------------------------------------------


def find_clusters(
    graph: AffinityGraph,
    *,
    min_affinity: float = 1.0,
) -> list[ComponentGroup]:
    """Find connected clusters of tightly-connected components.

    Uses a simple connected-components approach on the affinity graph,
    keeping only edges with weight >= *min_affinity*.  Each connected
    component becomes a cluster.

    Args:
        graph: Component affinity graph.
        min_affinity: Minimum edge weight to consider two components as
            connected.  Lower values produce larger, looser clusters.

    Returns:
        List of :class:`ComponentGroup` instances, one per cluster.
        Isolated components (no edges above threshold) each form their
        own singleton cluster.
    """
    n = graph.num_components
    if n == 0:
        return []

    # Build adjacency list from weight matrix
    adj: dict[int, set[int]] = defaultdict(set)
    for i in range(n):
        for j in range(i + 1, n):
            if graph.weights[i, j] >= min_affinity:
                adj[i].add(j)
                adj[j].add(i)

    # BFS to find connected components
    visited: set[int] = set()
    clusters: list[ComponentGroup] = []
    cluster_idx = 0

    for start in range(n):
        if start in visited:
            continue
        # BFS from start
        component_indices: list[int] = []
        queue = [start]
        visited.add(start)
        while queue:
            node = queue.pop(0)
            component_indices.append(node)
            for neighbour in sorted(adj.get(node, set())):
                if neighbour not in visited:
                    visited.add(neighbour)
                    queue.append(neighbour)

        refs = tuple(graph.references[i] for i in sorted(component_indices))
        clusters.append(ComponentGroup(name=f"cluster-{cluster_idx}", references=refs))
        cluster_idx += 1

    return clusters


# ---------------------------------------------------------------------------
# Power domain detection
# ---------------------------------------------------------------------------


def detect_power_domains(
    components: Sequence[ComponentDef],
    nets: Sequence[Net],
) -> list[ComponentGroup]:
    """Identify power domains by grouping components on shared power/ground nets.

    Each distinct power or ground net that connects two or more components
    defines a domain.  Components connected to the same power net are
    grouped together.

    Args:
        components: Component definitions.
        nets: Net connectivity information.

    Returns:
        List of :class:`ComponentGroup` instances, one per power domain.
        The group name is the power/ground net name.
    """
    ref_set: set[str] = {c.reference for c in components}
    domains: list[ComponentGroup] = []

    for net in nets:
        if not _is_power_or_ground_net(net.name):
            continue

        # Collect unique component references on this net
        refs_on_net: list[str] = []
        seen: set[str] = set()
        for ref, _ in net.pins:
            if ref in ref_set and ref not in seen:
                refs_on_net.append(ref)
                seen.add(ref)

        if len(refs_on_net) >= 2:
            domains.append(
                ComponentGroup(
                    name=net.name,
                    references=tuple(sorted(refs_on_net)),
                )
            )

    return domains


# ---------------------------------------------------------------------------
# Signal flow detection
# ---------------------------------------------------------------------------

# Connector reference designator prefixes (J for connectors, P for plugs)
_CONNECTOR_PREFIXES = ("J", "P", "CN", "CONN", "USB", "HDR")


def _is_connector(reference: str) -> bool:
    """Heuristic: check if a reference designator looks like a connector."""
    upper = reference.upper()
    for prefix in _CONNECTOR_PREFIXES:
        if upper.startswith(prefix) and (
            len(upper) == len(prefix) or upper[len(prefix) :].lstrip("0123456789") == ""
        ):
            return True
    return False


def detect_signal_flow(
    components: Sequence[ComponentDef],
    nets: Sequence[Net],
) -> SignalFlowResult:
    """Detect signal flow ordering through the design.

    Identifies connectors as sources/sinks using reference designator
    heuristics, then performs a BFS-based topological ordering from
    sources through the component graph.

    Source connectors are those with lower numeric suffixes or fewer net
    connections; sink connectors are those with higher suffixes or more
    connections.  When the heuristic is ambiguous, all connectors are
    treated as sources and a simple BFS from them determines the ordering.

    Args:
        components: Component definitions.
        nets: Net connectivity information.

    Returns:
        A :class:`SignalFlowResult` with topological ordering, sources,
        and sinks.
    """
    ref_list = [c.reference for c in components]

    # Build adjacency (signal nets only, exclude power/ground)
    ref_to_idx: dict[str, int] = {r: i for i, r in enumerate(ref_list)}
    n = len(ref_list)
    adj: dict[int, set[int]] = defaultdict(set)

    for net in nets:
        if _is_power_or_ground_net(net.name):
            continue
        indices: set[int] = set()
        for ref, _ in net.pins:
            idx = ref_to_idx.get(ref)
            if idx is not None:
                indices.add(idx)
        idx_list = sorted(indices)
        for a_pos in range(len(idx_list)):
            for b_pos in range(a_pos + 1, len(idx_list)):
                adj[idx_list[a_pos]].add(idx_list[b_pos])
                adj[idx_list[b_pos]].add(idx_list[a_pos])

    # Identify connectors
    connector_indices: list[int] = [i for i, r in enumerate(ref_list) if _is_connector(r)]

    if not connector_indices:
        # No connectors found -- return original order
        return SignalFlowResult(
            ordering=tuple(ref_list),
            sources=(),
            sinks=(),
        )

    # Count signal net connections per connector
    conn_degrees: dict[int, int] = {}
    for idx in connector_indices:
        conn_degrees[idx] = len(adj.get(idx, set()))

    # Heuristic: connectors with fewer connections are more likely inputs,
    # connectors with more connections are more likely outputs.
    # Split at median degree.  In case of tie, use lower reference number
    # as source.
    if len(connector_indices) == 1:
        # Single connector is both source and sink
        sources_idx = set(connector_indices)
        sinks_idx = set(connector_indices)
    else:
        degrees = sorted(conn_degrees[i] for i in connector_indices)
        median_deg = degrees[len(degrees) // 2]

        sources_idx: set[int] = set()
        sinks_idx: set[int] = set()
        for idx in connector_indices:
            if conn_degrees[idx] <= median_deg:
                sources_idx.add(idx)
            else:
                sinks_idx.add(idx)

        # Ensure at least one source and one sink
        if not sources_idx:
            sources_idx = {connector_indices[0]}
        if not sinks_idx:
            sinks_idx = {connector_indices[-1]}

    # BFS from sources to determine ordering
    visited: set[int] = set()
    ordering: list[int] = []
    queue = sorted(sources_idx)  # deterministic start order
    for s in queue:
        visited.add(s)

    while queue:
        node = queue.pop(0)
        ordering.append(node)
        for neighbour in sorted(adj.get(node, set())):
            if neighbour not in visited:
                visited.add(neighbour)
                queue.append(neighbour)

    # Append any unreachable components at the end (in original order)
    for i in range(n):
        if i not in visited:
            ordering.append(i)

    return SignalFlowResult(
        ordering=tuple(ref_list[i] for i in ordering),
        sources=tuple(ref_list[i] for i in sorted(sources_idx)),
        sinks=tuple(ref_list[i] for i in sorted(sinks_idx)),
    )


# ---------------------------------------------------------------------------
# Placement priors
# ---------------------------------------------------------------------------


def schematic_proximity_prior(
    components: Sequence[ComponentDef],
    nets: Sequence[Net],
    board: BoardOutline,
) -> PlacementVector:
    """Generate a placement prior based on schematic proximity.

    Places each component at the weighted centroid of its connected
    neighbours (weighted by shared-net count).  Components with no
    connections are placed at the board centre.

    The algorithm:
    1. Build the affinity graph.
    2. Initialise all components at the board centre.
    3. Iteratively update each component's position to the weighted
       centroid of its neighbours (repeat until convergence or max
       iterations).
    4. Clamp positions to board bounds.

    This produces a placement where high-affinity components are close
    together, suitable as a GP prior mean function for Bayesian
    optimisation.

    All components are placed on the front side (side=0) with rotation=0.

    Args:
        components: Component definitions to place.
        nets: Net connectivity information.
        board: Board outline defining placement boundaries.

    Returns:
        A :class:`PlacementVector` encoding the prior placement.
    """
    n = len(components)
    if n == 0:
        return PlacementVector(data=np.empty(0, dtype=np.float64))

    graph = build_affinity_graph(components, nets)

    # Board centre
    cx = (board.min_x + board.max_x) / 2.0
    cy = (board.min_y + board.max_y) / 2.0

    # Initialise positions at board centre with slight jitter for symmetry
    # breaking
    rng = np.random.default_rng(42)
    positions = np.empty((n, 2), dtype=np.float64)
    jitter_scale = min(board.width, board.height) * 0.01
    for i in range(n):
        positions[i, 0] = cx + rng.uniform(-jitter_scale, jitter_scale)
        positions[i, 1] = cy + rng.uniform(-jitter_scale, jitter_scale)

    # Component half-sizes for bound clamping
    half_sizes = np.array(
        [(c.width / 2.0, c.height / 2.0) for c in components],
        dtype=np.float64,
    )

    # Iterative weighted-centroid update
    max_iterations = 200
    convergence_threshold = 1e-4

    for _iteration in range(max_iterations):
        new_positions = np.copy(positions)
        max_delta = 0.0

        for i in range(n):
            total_weight = 0.0
            wx = 0.0
            wy = 0.0
            for j in range(n):
                if i == j:
                    continue
                w = graph.weights[i, j]
                if w > 0:
                    total_weight += w
                    wx += w * positions[j, 0]
                    wy += w * positions[j, 1]

            if total_weight > 0:
                target_x = wx / total_weight
                target_y = wy / total_weight
                # Blend: move 70% toward centroid, keep 30% of current
                # position for stability
                alpha = 0.7
                new_x = (1.0 - alpha) * positions[i, 0] + alpha * target_x
                new_y = (1.0 - alpha) * positions[i, 1] + alpha * target_y
            else:
                # Unconnected component stays at current position
                new_x = positions[i, 0]
                new_y = positions[i, 1]

            # Clamp to board bounds
            x_lo = board.min_x + half_sizes[i, 0]
            x_hi = board.max_x - half_sizes[i, 0]
            y_lo = board.min_y + half_sizes[i, 1]
            y_hi = board.max_y - half_sizes[i, 1]

            if x_lo <= x_hi:
                new_x = max(x_lo, min(x_hi, new_x))
            else:
                new_x = cx
            if y_lo <= y_hi:
                new_y = max(y_lo, min(y_hi, new_y))
            else:
                new_y = cy

            delta = math.sqrt((new_x - positions[i, 0]) ** 2 + (new_y - positions[i, 1]) ** 2)
            max_delta = max(max_delta, delta)

            new_positions[i, 0] = new_x
            new_positions[i, 1] = new_y

        positions = new_positions

        if max_delta < convergence_threshold:
            break

    # Encode as PlacementVector
    data = np.zeros(n * FIELDS_PER_COMPONENT, dtype=np.float64)
    for i in range(n):
        base = i * FIELDS_PER_COMPONENT
        data[base] = positions[i, 0]  # x
        data[base + 1] = positions[i, 1]  # y
        data[base + 2] = 0.0  # rotation index (0 = 0 degrees)
        data[base + 3] = 0.0  # side (0 = front)

    return PlacementVector(data=data)


def power_domain_clustering(
    components: Sequence[ComponentDef],
    nets: Sequence[Net],
) -> list[ComponentGroup]:
    """Group components by power domain.

    Convenience wrapper around :func:`detect_power_domains`.

    Args:
        components: Component definitions.
        nets: Net connectivity information.

    Returns:
        List of :class:`ComponentGroup` instances, one per power domain.
    """
    return detect_power_domains(components, nets)


def prior_mean_position(
    component_index: int,
    positions: NDArray[np.float64],
    graph: AffinityGraph,
) -> tuple[float, float]:
    """Compute the prior mean position for one component.

    The prior mean is the weighted centroid of connected neighbours.
    This can be used as the GP prior mean function in Bayesian
    optimisation: the GP learns the residual between this prior and
    the actual optimal placement.

    Args:
        component_index: Index of the target component.
        positions: Current positions array of shape (N, 2).
        graph: Component affinity graph.

    Returns:
        Tuple (x, y) of the prior mean position.  If the component has
        no neighbours, returns its current position.
    """
    n = graph.num_components
    i = component_index

    total_weight = 0.0
    wx = 0.0
    wy = 0.0

    for j in range(n):
        if i == j:
            continue
        w = graph.weights[i, j]
        if w > 0:
            total_weight += w
            wx += w * positions[j, 0]
            wy += w * positions[j, 1]

    if total_weight > 0:
        return (wx / total_weight, wy / total_weight)
    else:
        return (float(positions[i, 0]), float(positions[i, 1]))

"""Trace length analysis for PCB designs.

Analyzes trace lengths for timing-critical nets, differential pairs,
and length matching. Supports automatic identification of critical nets
and skew calculation for differential pairs.

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.analysis import TraceLengthAnalyzer
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> analyzer = TraceLengthAnalyzer()
    >>> reports = analyzer.analyze_all_critical(pcb)
    >>> for report in reports:
    ...     print(f"{report.net_name}: {report.total_length_mm:.2f}mm")
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from heapq import heappop, heappush
from typing import TYPE_CHECKING, Any, Iterator, NamedTuple

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


# Pattern for identifying timing-critical nets
CRITICAL_NET_PATTERNS = [
    # Clock signals
    r"(?i)^CLK",
    r"(?i)CLK$",
    r"(?i)CLOCK",
    r"(?i)_CLK_",
    # USB differential pairs
    r"(?i)USB.*[DP][\+\-]?$",
    r"(?i)USB.*DATA",
    r"(?i)^D[\+\-]$",
    # High-speed serial
    r"(?i)LVDS",
    r"(?i)MIPI",
    r"(?i)HDMI",
    r"(?i)DP_",  # DisplayPort
    r"(?i)PCIE",
    r"(?i)SATA",
    # DDR memory
    r"(?i)DDR",
    r"(?i)^DQ\d",
    r"(?i)^DQS",
    r"(?i)^DM\d",
    r"(?i)^A\d+$",  # Address lines (context dependent)
    # Ethernet
    r"(?i)ETH.*[TP][\+\-]?",
    r"(?i)RGMII",
    r"(?i)RMII",
    # CAN bus
    r"(?i)CAN.*[HL]$",
]

# Patterns for identifying differential pair partners
DIFF_PAIR_PATTERNS = [
    # USB: D+/D-, USB_D+/USB_D-
    (r"(?i)^(.*)[\+P]$", r"\1-", r"\1N"),
    (r"(?i)^(.*)[_]?P$", r"\1_N", r"\1N"),
    # LVDS/MIPI: _P/_N suffixes
    (r"(?i)^(.*)_P$", r"\1_N"),
    (r"(?i)^(.*)_N$", r"\1_P"),
    # TX+/TX-, RX+/RX-
    (r"(?i)^(.*)\+$", r"\1-"),
    (r"(?i)^(.*)-$", r"\1+"),
]


@dataclass
class TraceLengthReport:
    """Report on trace length for a net.

    Attributes:
        net_name: Name of the net.
        net_class: Net class if defined (e.g., "USB", "Clock").
        total_length_mm: Total trace length in millimeters.
        segment_count: Number of straight trace segments.
        arc_count: Number of curved copper tracks.
        arc_lengths: Analytic swept lengths of curved tracks.
        segment_lengths: Per-segment length breakdown.
        via_count: Number of vias in the net.
        layer_changes: Description of layer transitions (e.g., ["F.Cu → B.Cu"]).
        layers_used: Set of copper layers used by this net.
        target_length_mm: Target length if specified.
        tolerance_mm: Tolerance if specified.
        length_delta_mm: Difference from target (actual - target).
        within_tolerance: Whether length is within tolerance.
        pair_net: Name of differential pair partner if applicable.
        pair_length_mm: Length of differential pair partner.
        skew_mm: Length difference between pair members.
    """

    net_name: str
    net_class: str | None = None

    # Length measurements
    total_length_mm: float = 0.0
    segment_count: int = 0
    segment_lengths: list[float] = field(default_factory=list)
    via_count: int = 0
    layer_changes: list[str] = field(default_factory=list)
    layers_used: set[str] = field(default_factory=set)

    # Comparison to requirements
    target_length_mm: float | None = None
    tolerance_mm: float | None = None
    length_delta_mm: float | None = None
    within_tolerance: bool | None = None

    # For diff pairs
    pair_net: str | None = None
    pair_length_mm: float | None = None
    skew_mm: float | None = None

    # Curved tracks are separate from the straight-segment population.
    arc_count: int = 0
    arc_lengths: list[float] = field(default_factory=list)

    # Physical pad-to-pad path (Issue #9117).  ``path_length_mm`` is the
    # shortest copper path between the net's pads through the shared
    # copper-contact graph (longest pairwise shortest path for nets with
    # more than two pads); ``off_path_length_mm`` is the copper on no such
    # path -- dead-end loops, overlapping duplicates, stubs.  ``None``
    # until measured (:meth:`TraceLengthAnalyzer.measure_net_path`); the
    # summed ``total_length_mm`` keeps its meaning and its callers.
    path_length_mm: float | None = None
    off_path_length_mm: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "net_name": self.net_name,
            "total_length_mm": round(self.total_length_mm, 3),
            "segment_count": self.segment_count,
            "arc_count": self.arc_count,
            "via_count": self.via_count,
            "layers_used": sorted(self.layers_used),
        }

        if self.net_class:
            result["net_class"] = self.net_class

        if self.layer_changes:
            result["layer_changes"] = self.layer_changes

        if self.target_length_mm is not None:
            result["target_length_mm"] = self.target_length_mm
            result["length_delta_mm"] = round(self.length_delta_mm or 0, 3)
            result["within_tolerance"] = self.within_tolerance

        if self.tolerance_mm is not None:
            result["tolerance_mm"] = self.tolerance_mm

        if self.pair_net:
            result["differential_pair"] = {
                "pair_net": self.pair_net,
                "pair_length_mm": round(self.pair_length_mm or 0, 3),
                "skew_mm": round(self.skew_mm or 0, 3),
            }

        if self.path_length_mm is not None:
            result["path_length_mm"] = round(self.path_length_mm, 3)
        if self.off_path_length_mm is not None:
            result["off_path_length_mm"] = round(self.off_path_length_mm, 3)

        return result


@dataclass
class NetPathMeasurement:
    """Physical pad-to-pad copper path for one net (Issue #9117).

    Attributes:
        path_length_mm: Shortest copper path between the net's pads through
            the shared copper-contact graph (for nets with more than two
            pads: the longest pairwise shortest path between pads).  ``None``
            when the net has fewer than two pads, no shapely backend, or no
            connected pad-to-pad path (an open net).
        off_path_length_mm: Copper on no pad-to-pad path -- dead-end loops,
            overlapping duplicates, stubs -- i.e. summed track length minus
            the path.  ``None`` whenever the path is ``None``.
        pad_count: Number of pads of this net found in the contact graph.
        path_via_indices: Indices into ``board.vias`` of the vias the
            measured path runs through (for via-travel accounting).
        defect_track_mm: Track copper classified as defect -- dead-end
            branches, lollipops rooted at one pad/via terminal -- and
            exceeding ``defect_epsilon_mm`` per connected structure
            (Issue #9304).  Rejoined cycle copper (length-matching bumps
            and other parallel detours) and the sub-epsilon pad/via
            escape residual are never counted here.  ``None`` unless the
            measurement was requested with ``defect_epsilon_mm`` (or the
            classifier could not run); ``0.0`` on defect-free copper.
        defect_structures: Number of distinct off-path structures whose
            copper exceeded the epsilon and was classified as defect.
    """

    path_length_mm: float | None = None
    off_path_length_mm: float | None = None
    pad_count: int = 0
    path_via_indices: tuple[int, ...] = ()
    defect_track_mm: float | None = None
    defect_structures: int = 0


# Point-coincidence radius (mm) for path-graph nodes; mirrors the contact
# model's ``POSITION_TOLERANCE`` so a node is exactly "where copper meets".
_NODE_MERGE_MM = 0.01


class _PadToPadGraph(NamedTuple):
    """Weighted point graph over one net's copper-contact graph."""

    points: list[tuple[float, float]]
    adjacency: dict[int, list[tuple[int, float]]]
    pad_nodes: dict[int, int]
    via_nodes: dict[int, int]
    total_track_length_mm: float
    #: Per-track centrelines split at their graph stops, as
    #: ``{track index: [(node_a, node_b, piece_length_mm), ...]}``
    #: (Issue #9304: the defect-copper classifier sheds track copper at
    #: piece granularity, so the builder records which adjacency edges
    #: belong to which track's centreline and how long each piece is).
    track_pieces: dict[int, list[tuple[int, int, float]]]


def _build_pad_to_pad_graph(
    items: list[Any],
    bonds: list[tuple[int, int]],
) -> _PadToPadGraph:
    """Weighted point graph over one net's copper-contact graph.

    Nodes are physical meeting points: pad centres, via centres, track
    endpoints, and the projection of any bonded pad/via/track endpoint onto
    a track centreline (so a junction in the middle of a segment splits it
    instead of charging its full length).  Edges carry the centreline
    length between consecutive meeting points.  Track-to-track contact
    links are charged the geometric distance between their meeting points,
    so a chain of sub-width segment hops (bonded through capsule contact
    without sharing endpoints) pays what it physically spans instead of
    crossing copper at zero cost; a true junction merges into one node and
    pays nothing.  Pad, via and fill nodes are zero-cost joints: in-pad /
    in-annulus copper is a joint (it keeps in-pad escape copper on the
    path; vertical via travel is the caller's accounting), and a fill is a
    plane, so a path across a fill-bearing net measures the track hops
    between contact points, not a physical copper length.  Contact
    decisions are the shared model's (``bonds``), never re-derived.
    """
    from shapely.geometry import Point as _Point  # type: ignore[import-untyped]
    from shapely.ops import nearest_points as _nearest_points  # type: ignore[import-untyped]

    from kicad_tools.validate.copper_contact import FILL, PAD, TRACK, VIA

    points: list[tuple[float, float]] = []

    def node_for(point: Any) -> int:
        px, py = float(point[0]), float(point[1])
        for index, (nx, ny) in enumerate(points):
            if (px - nx) ** 2 + (py - ny) ** 2 < _NODE_MERGE_MM**2:
                return index
        points.append((px, py))
        return len(points) - 1

    adjacency: dict[int, list[tuple[int, float]]] = {}

    def link(a: int, b: int, weight: float) -> None:
        if a != b:
            adjacency.setdefault(a, []).append((b, weight))
            adjacency.setdefault(b, []).append((a, weight))

    def link_geometric(a: int, b: int) -> None:
        """Track-to-track contact link charged the distance it spans.

        Two bonded capsules meet somewhere between their centrelines;
        charging that distance keeps a chain of sub-width segment hops
        from crossing real copper at zero cost (Issue #9117), while a
        genuine end-to-end or mid-track junction merges into one node
        and pays nothing.
        """
        link(a, b, math.dist(points[a], points[b]))

    pad_nodes: dict[int, int] = {}
    via_nodes: dict[int, int] = {}
    fill_nodes: dict[int, int] = {}
    track_length: dict[int, float] = {}
    line_length: dict[int, float] = {}
    end_nodes: dict[int, tuple[int, int]] = {}
    attachments: dict[int, list[tuple[float, int]]] = {}

    for index, item in enumerate(items):
        if item.kind == PAD and item.center is not None:
            pad_nodes[index] = node_for(item.center)
        elif item.kind == VIA and item.center is not None:
            via_nodes[index] = node_for(item.center)
        elif item.kind == FILL:
            point = item.geom.representative_point()
            fill_nodes[index] = node_for((point.x, point.y))
        elif item.kind == TRACK:
            start, end = item.endpoints[0], item.endpoints[-1]
            track_length[index] = (
                math.dist(start, end) if item.straight else max(float(item.line.length), 0.0)
            )
            line_length[index] = max(float(item.line.length), 1e-9)
            end_nodes[index] = (node_for(start), node_for(end))

    def attach(track_index: int, point: Any) -> int:
        """Node at ``point``'s contact position on a track's centreline."""
        item = items[track_index]
        first, last = end_nodes[track_index]
        total = line_length[track_index]
        if total <= 2 * _NODE_MERGE_MM:
            return first
        geometry = point if hasattr(point, "project") else _Point(point)
        distance = item.line.project(geometry)
        if distance <= _NODE_MERGE_MM:
            return first
        if total - distance <= _NODE_MERGE_MM:
            return last
        split = item.line.interpolate(distance)
        node = node_for((split.x, split.y))
        attachments.setdefault(track_index, []).append((distance, node))
        return node

    for a, b in bonds:
        ia, ib = items[a], items[b]
        if ia.kind == TRACK and ib.kind == TRACK:
            for t_index, o_index in ((a, b), (b, a)):
                t_item, o_item = items[t_index], items[o_index]
                linked = False
                for ep_node, endpoint in zip(end_nodes[o_index], o_item.endpoints, strict=True):
                    if t_item.line.distance(_Point(endpoint)) <= (
                        t_item.half_width + o_item.half_width + 1e-9
                    ):
                        link_geometric(ep_node, attach(t_index, endpoint))
                        linked = True
                if not linked:
                    # Capsule contact with no endpoint involvement (an
                    # X crossing): bond at the closest centreline points.
                    p_t, p_o = _nearest_points(t_item.line, o_item.line)
                    link_geometric(attach(t_index, (p_t.x, p_t.y)), attach(o_index, (p_o.x, p_o.y)))
        elif ia.kind == TRACK or ib.kind == TRACK:
            t_index = a if ia.kind == TRACK else b
            o_index = b if ia.kind == TRACK else a
            o_item = items[o_index]
            if o_item.kind == FILL:
                p_t, _ = _nearest_points(items[t_index].line, o_item.geom)
                link(fill_nodes[o_index], attach(t_index, (p_t.x, p_t.y)), 0.0)
            elif o_item.center is not None:
                joint = pad_nodes.get(o_index, via_nodes.get(o_index))
                if joint is not None:
                    # Deliberately zero-cost: in-pad / in-annulus copper is
                    # a joint, which keeps in-pad escape copper on the path.
                    link(joint, attach(t_index, o_item.center), 0.0)
        elif FILL in (ia.kind, ib.kind):
            fill_index = a if ia.kind == FILL else b
            other_index = b if ia.kind == FILL else a
            if items[other_index].kind == FILL:
                link(fill_nodes[a], fill_nodes[b], 0.0)
            else:
                joint = pad_nodes.get(other_index, via_nodes.get(other_index))
                if joint is not None:
                    link(joint, fill_nodes[fill_index], 0.0)
        else:
            joint_a = pad_nodes.get(a, via_nodes.get(a))
            joint_b = pad_nodes.get(b, via_nodes.get(b))
            if joint_a is not None and joint_b is not None:
                link(joint_a, joint_b, 0.0)

    track_pieces: dict[int, list[tuple[int, int, float]]] = {}
    for track_index in track_length:
        first, last = end_nodes[track_index]
        stops = [(0.0, first), (line_length[track_index], last)]
        stops.extend(attachments.get(track_index, ()))
        stops.sort()
        total = track_length[track_index]
        scale = total / line_length[track_index]
        for position in range(len(stops) - 1):
            d0, n0 = stops[position]
            d1, n1 = stops[position + 1]
            piece_length = (d1 - d0) * scale
            link(n0, n1, piece_length)
            if n0 != n1:
                track_pieces.setdefault(track_index, []).append((n0, n1, piece_length))

    return _PadToPadGraph(
        points=points,
        adjacency=adjacency,
        pad_nodes=pad_nodes,
        via_nodes=via_nodes,
        total_track_length_mm=sum(track_length.values()),
        track_pieces=track_pieces,
    )


def _shortest_path_from(
    adjacency: dict[int, list[tuple[int, float]]], source: int
) -> tuple[dict[int, float], dict[int, int]]:
    """Dijkstra distances and predecessor map from ``source``."""
    dist = {source: 0.0}
    prev: dict[int, int] = {}
    heap: list[tuple[float, int]] = [(0.0, source)]
    while heap:
        d, node = heappop(heap)
        if d > dist.get(node, math.inf):
            continue
        for neighbour, weight in adjacency.get(node, ()):
            candidate = d + weight
            if candidate < dist.get(neighbour, math.inf) - 1e-12:
                dist[neighbour] = candidate
                prev[neighbour] = node
                heappush(heap, (candidate, neighbour))
    return dist, prev


def _edge_pair(a: int, b: int) -> tuple[int, int]:
    """Canonical undirected node-pair key for a graph edge."""
    return (a, b) if a < b else (b, a)


def _simple_graph_bridges(
    pairs_adjacency: dict[int, set[int]],
) -> set[tuple[int, int]]:
    """Bridges of the simple graph obtained by collapsing parallel edges.

    Iterative Tarjan low-link DFS.  Two vertices joined by more than one
    edge never form a bridge, which collapsing to a simple graph encodes
    exactly; the defect-copper classifier (Issue #9304) reasons about
    node pairs, so simple-graph bridges are the granularity it needs.
    """
    disc: dict[int, int] = {}
    low: dict[int, int] = {}
    bridges: set[tuple[int, int]] = set()
    timer = 0
    for root in pairs_adjacency:
        if root in disc:
            continue
        disc[root] = low[root] = timer
        timer += 1
        stack: list[tuple[int, int, Iterator[int]]] = [
            (root, -1, iter(sorted(pairs_adjacency[root])))
        ]
        while stack:
            node, parent, neighbours = stack[-1]
            pushed = False
            for neighbour in neighbours:
                if neighbour not in disc:
                    disc[neighbour] = low[neighbour] = timer
                    timer += 1
                    stack.append((neighbour, node, iter(sorted(pairs_adjacency[neighbour]))))
                    pushed = True
                    break
                if neighbour != parent and disc[neighbour] < low[node]:
                    low[node] = disc[neighbour]
            if pushed:
                continue
            stack.pop()
            if stack:
                parent_node = stack[-1][0]
                if low[node] < low[parent_node]:
                    low[parent_node] = low[node]
                if low[node] > disc[parent_node]:
                    bridges.add(_edge_pair(parent_node, node))
    return bridges


def _two_edge_connected_labels(
    pairs_adjacency: dict[int, set[int]],
    bridges: set[tuple[int, int]],
) -> dict[tuple[int, int], int]:
    """Label each non-bridge edge with a 2-edge-connected-component id."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != x:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b

    for a, neighbours in pairs_adjacency.items():
        for b in neighbours:
            if _edge_pair(a, b) not in bridges:
                union(a, b)
    labels: dict[tuple[int, int], int] = {}
    for a, neighbours in pairs_adjacency.items():
        for b in neighbours:
            pair = _edge_pair(a, b)
            if pair not in bridges and pair not in labels:
                labels[pair] = find(a)
    return labels


def _classify_defect_track_copper(
    path_graph: _PadToPadGraph,
    dijkstra_runs: dict[int, tuple[dict[int, float], dict[int, int]]],
    defect_epsilon_mm: float,
) -> tuple[float, int]:
    """Measure one net's defect track copper (Issue #9304).

    One uniform rule, applied to every member of a match group alike:
    copper is defect when it is neither on a pad-to-pad path, nor part of
    a cycle that rejoins one, nor sub-epsilon escape residual at a
    pad/via terminal.  Concretely, per track piece:

    * pieces on any pairwise pad-to-pad shortest path are signal copper;
    * pieces of a 2-edge-connected component that some shortest path
      enters are rejoined cycle copper (the tuner's length-matching
      bumps, parallel detours, duplicate overlaps) -- kept;
    * every other piece is a defect *candidate*: dead-end branches
      (bridges off every path), lollipops rooted at a single pad/via
      joint cluster, and detached cycles.  Within a rejoined component,
      off-path copper whose only boundary to signal copper lies inside
      ONE zero-cost joint cluster (one physical pad/via terminal) is a
      cluster-rooted appendage and also a candidate.

    Candidates form connected structures; a structure is shed only when
    its track copper exceeds ``defect_epsilon_mm``.  The epsilon is the
    caller's (the match-group producer passes
    ``OFF_PATH_REPORT_EPSILON_MM``), so the figure changes only where
    the off-path warning already fires: measured on this repo's boards,
    benign pad/via escape residual stays below ~0.4 mm per terminal and
    the genuine same-net loops sit at 6.2-10.2 mm, leaving 4.0 mm wide
    clearance on both sides.

    Returns ``(defect_track_mm, shed_structures)``.
    """
    adjacency = path_graph.adjacency
    pad_nodes = path_graph.pad_nodes

    def pair(a: int, b: int) -> tuple[int, int]:
        return _edge_pair(a, b)

    # Union of the edges of EVERY pairwise pad-to-pad shortest path
    # (2-pad nets: exactly the selected path; multi-pad nets keep every
    # branch that serves another pad, so a third-pad branch is never
    # misclassified as a dead end).
    path_pairs: set[tuple[int, int]] = set()
    pads = sorted(pad_nodes)
    for src in pads:
        _dist, prev = dijkstra_runs[src]
        for dst in pads:
            if dst <= src:
                continue
            node = pad_nodes[dst]
            if node not in _dist:
                continue
            while node != pad_nodes[src] and node in prev:
                path_pairs.add(pair(node, prev[node]))
                node = prev[node]
    if not path_pairs:
        return 0.0, 0

    pairs_adjacency: dict[int, set[int]] = defaultdict(set)
    zero_edge_nodes: set[int] = set()
    for a, neighbours in adjacency.items():
        for b, weight in neighbours:
            pairs_adjacency[a].add(b)
            if weight <= 0.0:
                zero_edge_nodes.add(a)
                zero_edge_nodes.add(b)
    bridges = _simple_graph_bridges(pairs_adjacency)
    labels = _two_edge_connected_labels(pairs_adjacency, bridges)
    rejoined = {labels[p] for p in path_pairs if p in labels}

    # Zero-cost clusters: nodes joined by weight-0 edges are one physical
    # pad/via/fill terminal.
    zparent: dict[int, int] = {}

    def zfind(x: int) -> int:
        while zparent.get(x, x) != x:
            zparent[x] = zparent.get(zparent[x], zparent[x])
            x = zparent[x]
        return x

    def zunion(a: int, b: int) -> None:
        root_a, root_b = zfind(a), zfind(b)
        if root_a != root_b:
            zparent[root_a] = root_b

    for a, neighbours in adjacency.items():
        for b, weight in neighbours:
            if weight <= 0.0:
                zunion(a, b)
    cluster_roots = {zfind(n) for n in zero_edge_nodes}

    # Piece keys ``(track_index, n0, n1)``: node-pair granularity cannot
    # tell a coincident duplicate overlay apart from the copy that stays
    # counted, so candidates carry piece identity.
    candidates: set[tuple[int, int, int]] = set()
    offpath_pieces: set[tuple[int, int, int]] = set()
    by_pair: dict[tuple[int, int], list[tuple[float, tuple[int, int, int]]]] = defaultdict(list)
    for track_index, pieces in path_graph.track_pieces.items():
        for n0, n1, weight in pieces:
            key = (track_index, n0, n1)
            by_pair[pair(n0, n1)].append((weight, key))
            p = pair(n0, n1)
            if p in path_pairs:
                continue
            label = labels.get(p)
            if label is None or label not in rejoined:
                candidates.add(key)
            else:
                offpath_pieces.add(key)
    # Coincident duplicates: parallel track pieces spanning the same node
    # pair with the same length are overlays of one physical connection
    # (the #9138 review's collinear stubs, an identical re-draw of a run);
    # each extra copy is a defect candidate while one copy stays counted.
    # A parallel piece with a DIFFERENT length is a detour, not an
    # overlay, and stays kept.  The scan covers on-path node pairs too --
    # an overlay copies a span the path also uses.
    for copies in by_pair.values():
        if len(copies) < 2:
            continue
        copies.sort()
        for weight, key in copies[1:]:
            if weight == copies[0][0]:
                candidates.add(key)

    # Cluster-rooted appendages inside rejoined components: groups are
    # connected through positive piece edges only -- a zero-cost cluster
    # is a separator, so copper hanging off one terminal forms its own
    # group even when other off-path copper touches the same terminal.
    if offpath_pieces:
        appendage_adjacency: dict[int, set[int]] = defaultdict(set)
        for _track, n0, n1 in offpath_pieces:
            appendage_adjacency[n0].add(n1)
            appendage_adjacency[n1].add(n0)
        visited: set[int] = set()
        for start in list(appendage_adjacency):
            if start in visited:
                continue
            group: set[int] = set()
            stack = [start]
            while stack:
                node = stack.pop()
                if node in group:
                    continue
                group.add(node)
                if zfind(node) in cluster_roots:
                    continue
                for neighbour in appendage_adjacency[node]:
                    if neighbour not in group:
                        stack.append(neighbour)
            visited |= group
            boundary = {
                node
                for node in group
                if zfind(node) in cluster_roots
                or any(
                    pair(node, neighbour) in path_pairs
                    for neighbour, _weight in adjacency.get(node, ())
                )
            }
            if boundary and len({zfind(node) for node in boundary}) == 1:
                for key in offpath_pieces:
                    if key[1] in group or key[2] in group:
                        candidates.add(key)

    # Group candidates into connected structures (zero-cost clusters
    # chain defects that share a terminal) and shed per-epsilon.
    cparent: dict[int, int] = {}

    def cfind(x: int) -> int:
        while cparent.get(x, x) != x:
            cparent[x] = cparent.get(cparent[x], cparent[x])
            x = cparent[x]
        return x

    def cunion(a: int, b: int) -> None:
        root_a, root_b = cfind(a), cfind(b)
        if root_a != root_b:
            cparent[root_a] = root_b

    for _track, n0, n1 in candidates:
        cunion(n0, n1)
    by_cluster: dict[int, list[int]] = defaultdict(list)
    for node in {x for _track, n0, n1 in candidates for x in (n0, n1)}:
        by_cluster[zfind(node)].append(node)
    for nodes in by_cluster.values():
        for node in nodes[1:]:
            cunion(nodes[0], node)

    structure_length: dict[int, float] = defaultdict(float)
    for track_index, pieces in path_graph.track_pieces.items():
        for n0, n1, weight in pieces:
            if (track_index, n0, n1) in candidates:
                structure_length[cfind(n0)] += weight

    defect_mm = 0.0
    shed = 0
    for total in structure_length.values():
        if total > defect_epsilon_mm:
            defect_mm += total
            shed += 1
    return defect_mm, shed


@dataclass
class DifferentialPairReport:
    """Report on a differential pair.

    Attributes:
        net_p: Positive net name.
        net_n: Negative net name.
        report_p: TraceLengthReport for positive net.
        report_n: TraceLengthReport for negative net.
        skew_mm: Length difference (absolute value).
        target_skew_mm: Maximum allowed skew.
        skew_within_tolerance: Whether skew is acceptable.
    """

    net_p: str
    net_n: str
    report_p: TraceLengthReport
    report_n: TraceLengthReport
    skew_mm: float
    target_skew_mm: float | None = None
    skew_within_tolerance: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "net_positive": self.net_p,
            "net_negative": self.net_n,
            "length_positive_mm": round(self.report_p.total_length_mm, 3),
            "length_negative_mm": round(self.report_n.total_length_mm, 3),
            "skew_mm": round(self.skew_mm, 3),
        }

        if self.target_skew_mm is not None:
            result["target_skew_mm"] = self.target_skew_mm
            result["skew_within_tolerance"] = self.skew_within_tolerance

        return result


class TraceLengthAnalyzer:
    """Analyze trace lengths on a PCB.

    Calculates trace lengths for individual nets, identifies timing-critical
    nets automatically, and analyzes differential pairs for skew.

    Args:
        critical_patterns: Additional regex patterns for critical net detection.
    """

    def __init__(self, critical_patterns: list[str] | None = None):
        """Initialize the analyzer.

        Args:
            critical_patterns: Additional regex patterns for identifying
                timing-critical nets. Added to the default patterns.
        """
        self._patterns = list(CRITICAL_NET_PATTERNS)
        if critical_patterns:
            self._patterns.extend(critical_patterns)
        self._compiled_patterns = [re.compile(p) for p in self._patterns]

    def analyze_net(
        self, board: PCB, net_name: str, *, with_path_length: bool = False
    ) -> TraceLengthReport:
        """Calculate trace length for a specific net.

        Args:
            board: PCB object to analyze.
            net_name: Name of the net to analyze.
            with_path_length: Also measure the physical pad-to-pad path
                (Issue #9117) and fill ``path_length_mm`` /
                ``off_path_length_mm``.  Off by default: the measurement
                builds a contact graph per net and existing callers keep
                the summed-length behaviour unchanged.

        Returns:
            TraceLengthReport with length measurements.
        """
        # Find net number
        net = board.get_net_by_name(net_name)
        if net is None:
            return TraceLengthReport(net_name=net_name)

        net_number = net.number

        # Calculate segment lengths
        segment_lengths: list[float] = []
        layers_used: set[str] = set()
        ordered_layers: list[str] = []

        for segment in board.segments_in_net(net_number):
            dx = segment.end[0] - segment.start[0]
            dy = segment.end[1] - segment.start[1]
            length = math.sqrt(dx * dx + dy * dy)
            segment_lengths.append(length)
            layers_used.add(segment.layer)

            # Track layer order for transitions
            if not ordered_layers or ordered_layers[-1] != segment.layer:
                ordered_layers.append(segment.layer)

        arc_lengths: list[float] = []
        for arc in board.arcs_in_net(net_number):
            arc_lengths.append(arc.length)
            layers_used.add(arc.layer)
            if not ordered_layers or ordered_layers[-1] != arc.layer:
                ordered_layers.append(arc.layer)

        # Count vias and identify layer changes
        via_count = sum(1 for _ in board.vias_in_net(net_number))

        # Build layer change descriptions
        layer_changes: list[str] = []
        for i in range(len(ordered_layers) - 1):
            layer_changes.append(f"{ordered_layers[i]} → {ordered_layers[i + 1]}")

        total_length = sum(segment_lengths) + sum(arc_lengths)

        report = TraceLengthReport(
            net_name=net_name,
            total_length_mm=total_length,
            segment_count=len(segment_lengths),
            segment_lengths=segment_lengths,
            arc_count=len(arc_lengths),
            arc_lengths=arc_lengths,
            via_count=via_count,
            layer_changes=layer_changes,
            layers_used=layers_used,
        )
        if with_path_length:
            measurement = self.measure_net_path(board, net_name)
            report.path_length_mm = measurement.path_length_mm
            report.off_path_length_mm = measurement.off_path_length_mm
        return report

    def measure_net_path(
        self,
        board: PCB,
        net_name: str,
        defect_epsilon_mm: float | None = None,
    ) -> NetPathMeasurement:
        """Measure the physical pad-to-pad copper path of one net.

        The path runs through the shared copper-contact graph
        (:mod:`kicad_tools.validate.copper_contact`): track copper bonded
        where it physically meets pads, other tracks or same-net fills,
        with vias joining layers.  For a two-pin net this is the shortest
        pad-to-pad path; for more pads it is the longest pairwise shortest
        path (the binding length-matching measurement).  Copper on no such
        path -- dead-end loops, overlapping duplicates, stubs -- is
        reported through :attr:`NetPathMeasurement.off_path_length_mm`
        instead of silently counting toward matched length (Issue #9117).

        Args:
            board: PCB object to analyze.
            net_name: Name of the net to analyze.
            defect_epsilon_mm: When given (Issue #9304), additionally
                classify defect track copper -- dead-end branches and
                lollipops rooted at one pad/via terminal whose connected
                structure exceeds this epsilon -- into
                :attr:`NetPathMeasurement.defect_track_mm`.  Rejoined
                cycle copper (length-matching bumps, parallel detours)
                and sub-epsilon escape residual are never classified as
                defect; ``None`` (the default) skips the classification.

        Returns:
            NetPathMeasurement; ``path_length_mm`` is ``None`` when the net
            is unknown, has fewer than two pads, no shapely backend, or no
            connected pad-to-pad path (an open net).
        """
        net = board.get_net_by_name(net_name)
        if net is None:
            return NetPathMeasurement()

        from kicad_tools._shapely import has_shapely

        if not has_shapely():
            return NetPathMeasurement()

        from kicad_tools.validate.connectivity import ConnectivityValidator
        from kicad_tools.validate.copper_contact import build_copper_contact_graph

        graph = build_copper_contact_graph(
            ConnectivityValidator(board), net.number, with_bonds=True
        )
        path_graph = _build_pad_to_pad_graph(graph.items, graph.bonds)
        adjacency, pad_nodes, via_nodes = (
            path_graph.adjacency,
            path_graph.pad_nodes,
            path_graph.via_nodes,
        )
        total = path_graph.total_track_length_mm
        pads = sorted(pad_nodes)
        if len(pads) < 2:
            return NetPathMeasurement(pad_count=len(pad_nodes))

        # Longest pairwise shortest path between pads (two-pin: the single
        # pair).  Documented multi-pin behaviour: other branches count as
        # off-path copper, never silently into the measured length.
        dijkstra_runs: dict[int, tuple[dict[int, float], dict[int, int]]] = {}
        best: tuple[float, int, int, dict[int, int]] | None = None
        for src in pads:
            dist, prev = _shortest_path_from(adjacency, pad_nodes[src])
            dijkstra_runs[src] = (dist, prev)
            for dst in pads:
                if dst <= src:
                    continue
                length = dist.get(pad_nodes[dst], math.inf)
                if length == math.inf:
                    continue
                if best is None or length > best[0]:
                    best = (length, src, dst, prev)
        if best is None:
            # No connected pad-to-pad path (an open net).
            return NetPathMeasurement(pad_count=len(pad_nodes))

        path_length, src, dst, prev = best
        node_to_via = {node: graph.items[index].key for index, node in via_nodes.items()}
        path_via_indices: set[int] = set()
        node: int | None = pad_nodes[dst]
        while node is not None and node != pad_nodes[src]:
            via_index = node_to_via.get(node)
            if via_index is not None:
                path_via_indices.add(via_index)
            node = prev.get(node)
        off_path = max(total - path_length, 0.0)
        defect_mm: float | None = None
        defect_structures = 0
        if defect_epsilon_mm is not None:
            defect_mm, defect_structures = _classify_defect_track_copper(
                path_graph, dijkstra_runs, defect_epsilon_mm
            )
        return NetPathMeasurement(
            path_length_mm=path_length,
            off_path_length_mm=off_path,
            pad_count=len(pad_nodes),
            path_via_indices=tuple(sorted(path_via_indices)),
            defect_track_mm=defect_mm,
            defect_structures=defect_structures,
        )

    def analyze_diff_pair(
        self,
        board: PCB,
        net_p: str,
        net_n: str,
        target_skew_mm: float | None = None,
    ) -> DifferentialPairReport:
        """Analyze a differential pair with skew calculation.

        Args:
            board: PCB object to analyze.
            net_p: Name of the positive (or first) net.
            net_n: Name of the negative (or second) net.
            target_skew_mm: Maximum allowed skew in mm.

        Returns:
            DifferentialPairReport with both net reports and skew.
        """
        report_p = self.analyze_net(board, net_p)
        report_n = self.analyze_net(board, net_n)

        skew = abs(report_p.total_length_mm - report_n.total_length_mm)

        # Update individual reports with pair info
        report_p.pair_net = net_n
        report_p.pair_length_mm = report_n.total_length_mm
        report_p.skew_mm = skew

        report_n.pair_net = net_p
        report_n.pair_length_mm = report_p.total_length_mm
        report_n.skew_mm = skew

        # Check skew tolerance
        skew_ok = None
        if target_skew_mm is not None:
            skew_ok = skew <= target_skew_mm

        return DifferentialPairReport(
            net_p=net_p,
            net_n=net_n,
            report_p=report_p,
            report_n=report_n,
            skew_mm=skew,
            target_skew_mm=target_skew_mm,
            skew_within_tolerance=skew_ok,
        )

    def analyze_all_critical(
        self,
        board: PCB,
        include_diff_pairs: bool = True,
    ) -> list[TraceLengthReport]:
        """Analyze all timing-critical nets.

        Automatically identifies critical nets by name pattern matching
        (CLK, USB, DDR, etc.) and calculates their trace lengths.

        Args:
            board: PCB object to analyze.
            include_diff_pairs: If True, include differential pair analysis.

        Returns:
            List of TraceLengthReport objects for critical nets.
        """
        critical_nets = self._identify_critical_nets(board)
        reports: list[TraceLengthReport] = []
        analyzed: set[str] = set()

        for net_name in critical_nets:
            if net_name in analyzed:
                continue

            report = self.analyze_net(board, net_name)
            analyzed.add(net_name)

            # Check for differential pair partner
            if include_diff_pairs:
                pair_name = self._find_diff_pair_partner(net_name, board)
                if pair_name and pair_name not in analyzed:
                    pair_report = self.analyze_net(board, pair_name)
                    analyzed.add(pair_name)

                    # Calculate skew
                    skew = abs(report.total_length_mm - pair_report.total_length_mm)

                    # Update both reports with pair info
                    report.pair_net = pair_name
                    report.pair_length_mm = pair_report.total_length_mm
                    report.skew_mm = skew

                    pair_report.pair_net = net_name
                    pair_report.pair_length_mm = report.total_length_mm
                    pair_report.skew_mm = skew

                    reports.append(pair_report)

            reports.append(report)

        # Sort by net name for consistent output
        reports.sort(key=lambda r: r.net_name)

        return reports

    def find_differential_pairs(self, board: PCB) -> list[tuple[str, str]]:
        """Find all differential pairs in the board.

        Identifies differential pairs by naming convention (e.g., _P/_N,
        +/-, D+/D-).

        Args:
            board: PCB object to analyze.

        Returns:
            List of (positive_net, negative_net) tuples.
        """
        net_names = {net.name for net in board.nets.values() if net.name}
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()

        for net_name in sorted(net_names):
            if net_name in seen:
                continue

            pair_name = self._find_diff_pair_partner(net_name, board)
            if pair_name and pair_name in net_names and pair_name not in seen:
                # Determine which is P and which is N
                if self._is_positive_net(net_name):
                    pairs.append((net_name, pair_name))
                else:
                    pairs.append((pair_name, net_name))
                seen.add(net_name)
                seen.add(pair_name)

        return pairs

    def _identify_critical_nets(self, board: PCB) -> list[str]:
        """Identify timing-critical nets by name pattern.

        Args:
            board: PCB object to analyze.

        Returns:
            List of net names matching critical patterns.
        """
        critical = []

        for net in board.nets.values():
            if not net.name:
                continue

            for pattern in self._compiled_patterns:
                if pattern.search(net.name):
                    critical.append(net.name)
                    break

        return sorted(critical)

    def _find_diff_pair_partner(self, net_name: str, board: PCB) -> str | None:
        """Find the differential pair partner for a net.

        Args:
            net_name: Name of the net to find partner for.
            board: PCB object (to verify partner exists).

        Returns:
            Partner net name if found, None otherwise.
        """
        net_names = {net.name for net in board.nets.values()}

        for pattern_tuple in DIFF_PAIR_PATTERNS:
            pattern = pattern_tuple[0]
            replacements = pattern_tuple[1:]

            match = re.match(pattern, net_name)
            if match:
                base = match.group(1)
                for replacement in replacements:
                    # Handle replacement patterns
                    if replacement.startswith(r"\1"):
                        partner = base + replacement[2:]
                    else:
                        partner = replacement.replace(r"\1", base)

                    if partner in net_names and partner != net_name:
                        return partner

        return None

    def _is_positive_net(self, net_name: str) -> bool:
        """Check if a net name indicates positive polarity.

        Args:
            net_name: Net name to check.

        Returns:
            True if net appears to be positive/P side.
        """
        return bool(re.search(r"(?i)[\+P]$", net_name) or re.search(r"(?i)_P$", net_name))

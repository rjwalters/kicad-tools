"""Branch-specific current-path intent declarations (Issue #4980).

A net's copper is not always electrically homogeneous. A single net can
carry a high-current force/trunk path (e.g. a 15 A mains conductor or a
3 A output rail) *and* one or more low-current sense/measurement/feedback
taps that share the same electrical net but must never be treated as
carrying the trunk's current, and must never be silently bridged or
reinforced together with it (a Kelvin four-terminal shunt is the sharpest
example: the force pads and the sense pads sit on the *same* net, but a
copper bridge or a shared reinforcement path between them defeats the
measurement).

Today's whole-net model -- :attr:`kicad_tools.router.rules.NetClassRouting
.target_ampacity` / ``trace_width`` / ``neck_trace_width`` -- applies
uniformly to *every* segment of a net. This module adds an orthogonal,
declarative model scoped to a physical copper branch rather than a whole
net: a :class:`CurrentPathSpec` names two stable ``RefDes.pad`` endpoints,
a continuous (and optionally pulsed) current, and whether the branch is
eligible for buttress-wire reinforcement.

Three things this module deliberately does NOT do, by design:

* **Infer branch currents from net topology.** A path is only ever what a
  human declared -- there is no heuristic that looks at connectivity and
  guesses "this must be the high-current branch". The issue is explicit
  that inferring unique branch currents from net connectivity alone is
  unsafe when loops, parallel routes, or multiple operating modes make the
  split ambiguous.
* **Silently fall back to whole-net ampacity when a declared path fails to
  resolve.** :func:`resolve_current_path` reports an explicit
  ``"unresolved"`` (endpoint moved/removed/off-net) or ``"ambiguous"``
  (the net's copper contains a loop reachable from the declared endpoints,
  so a single linear path cannot represent how current actually splits)
  status. Consumers (the ampacity DRC rule, the reinforcement pass) must
  treat both as "do not trust a single-path result here" -- never as
  "skip the check".
* **Silently waive copper that no declared path covers.** :func:`audit_
  current_paths` reports every net segment not covered by any resolved
  path as *uncovered*, so an audit can show unmodeled branches rather than
  hiding them.

Endpoint attachment is by pad **extent**, not by an exact pad-center hit
(:func:`_pad_covers`): a router may terminate a trace
anywhere inside a pad's copper, and a wide power pad is routinely entered
by several stubs at once, all shorted by the pad itself. Binding on an
exact center match instead reported such boards ``unresolved`` -- a *false*
fail-closed, which is not conservative at all: it teaches users to delete
declarations, and a deleted declaration is exactly the silent pass this
module exists to prevent. Copper outside the pad extent still never
attaches, so the fail-closed direction is preserved where it is real.

The declared-path graph is layer-aware. Only real same-net via spans or
plated multilayer pad copper establish layer changes. Via/track centerline
contacts and same-layer endpoint/interior contacts split graph edges, while
public evidence retains each original routed segment once (including its
whole length and width). Pad-internal convex copper is normalized before
whole-component cycle detection. A benign parallel via array -- a hub
fanning into two or more via legs whose far ends tie back together, current
splitting and immediately recombining for capacity, not an alternate route
-- is recognized and contracted before that check (Issue #5197); a loop
built entirely from track copper, or one where the hub's own legs leave a
residual route uncontracted, remains ambiguous.

Unsupported custom/trapezoid pads, repeated physical pad numbers, and copper
on absent stackup layers make the declaration unresolved. Supported pad
extents are circles, rectangles, capsules (oval), and rounded rectangles.
Width-only copper overlaps, arcs, zones, and pad-interior contacts without a
track/via node are outside this bounded centerline model; a resolved result is not a native connectivity or ampacity
claim. These contacts require a separate physical geometry model.

Consumers:

* :class:`kicad_tools.validate.rules.path_ampacity.PathAmpacityRule` --
  per-path IPC-2221 width check + unresolved/ambiguous/uncovered
  reporting (the "independent final-copper audit"), wired into
  :meth:`kicad_tools.validate.checker.DRCChecker.check_path_ampacity` and
  reachable from ``kct check --current-paths`` (Issue #5124).
* :func:`kicad_tools.pcb.reinforce.reinforce_net` -- an optional
  ``current_paths`` argument gates which chained runs are eligible for
  buttress-wire anchoring, so a declared sense/measurement branch is never
  silently reinforced (or bridged to the force path) even when
  ``all_runs=True`` would otherwise anchor it.
* :func:`kicad_tools.cli.route_cmd.run_post_route_drc` -- ``kct route
  --current-paths`` loads the same sidecar, runs the per-branch check in the
  post-route DRC, and **re-emits** the declarations as a
  ``current_paths.json`` sidecar next to the routed board
  (:func:`kicad_tools.cli.route_cmd._write_current_paths_sidecar`).  That
  re-emission is what makes route-time intent and the later independent
  final-copper audit read *identical* declarations: a subsequent bare
  ``kct check`` auto-discovers the routed board's sidecar rather than
  silently running with ``path_ampacity`` inactive, so any disagreement
  between the two is a real copper difference and never a difference in
  what was declared.

Deliberately NOT a consumer (yet): ``router/pathfinder.py``'s route-time
trace-width selection. Issue #5124 Acceptance Criterion 1 offers an
explicit escape valve -- "OR a documented decision that width selection
stays declarative-only ... rather than route-time-enforced, with
rationale" -- and this module takes that path, for two reasons:

1. **Precedent already exists.** :attr:`~kicad_tools.router.rules
   .NetClassRouting.target_ampacity` is the closest existing analogue (a
   declared current a net class must support) and it does NOT feed the
   pathfinder's A* trace-width choice either -- it only hard-avoids
   inner-plane layers and drives ``.kicad_dru`` generation. Route-time
   width stays governed by the coarser ``NetClassRouting.trace_width``
   scalar; ``target_ampacity`` is checked post-route
   (:meth:`~kicad_tools.validate.checker.DRCChecker.check_ampacity`).
   ``CurrentPathSpec`` follows the same declarative/checked-post-route
   split its whole-net sibling already established.
2. **The router's net decomposition would make edge-matching an
   unreliable, misleading mechanism.** ``Router.route()`` and its
   siblings route one pad-to-pad edge at a time
   (``start: Pad, end: Pad``); a multi-terminal net (any T-network with
   a trunk plus one or more sense taps) is decomposed into a spanning
   tree of such edges by the router's own topology choice, which is not
   guaranteed to reproduce a declared spec's exact ``source``/``sink``
   pad pair as a single routed edge. Route-time width selection keyed on
   an exact edge match would silently no-op on precisely the multi-tap
   topologies this issue exists to model (Kelvin shunts, sense taps),
   while *looking* like route-time enforcement -- worse than being
   honestly declarative.

The post-route :class:`PathAmpacityRule` audit remains authoritative: it
re-derives each declared path's actual copper from the finished board via
:func:`resolve_current_path` (graph BFS over real routed segments), so it
catches a narrow trunk regardless of which edges the router chose. A
future increment could add route-time width *hints* for the common case
where a spec's endpoints DO match a single routed edge, without changing
this module's contract -- tracked under Issue #5124.

Sidecar format (mirrors the ``net_class_map.json`` convention already
established for :class:`~kicad_tools.router.rules.NetClassRouting`)::

    {
      "paths": [
        {
          "name": "AC_NEUTRAL_TRUNK",
          "net": "/AC_NEUTRAL",
          "source": {"ref": "J1", "pad": "2"},
          "sink": {"ref": "J2", "pad": "2"},
          "continuous_a": 15.0,
          "pulsed_a": null,
          "reinforcement_eligible": true,
          "notes": "15A mains neutral force path"
        },
        {
          "name": "AC_NEUTRAL_ZC_SENSE",
          "net": "/AC_NEUTRAL",
          "source": {"ref": "J1", "pad": "2"},
          "sink": {"ref": "U3", "pad": "3"},
          "continuous_a": 0.01,
          "reinforcement_eligible": false,
          "notes": "INA181 sense input -- must not be reinforced"
        }
      ]
    }

A bare JSON list of path objects (no ``"paths"`` wrapper) is also
accepted.
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.core.geometry import point_to_segment_distance, segments_intersect
from kicad_tools.core.layers import COPPER_LAYER_ORDER, via_spans_layer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kicad_tools.schema.pcb import PCB, Segment

__all__ = [
    "CURRENT_PATHS_SIDECAR_BASENAME",
    "CurrentPathAudit",
    "CurrentPathSpec",
    "PathEndpoint",
    "PathResolution",
    "ResolvedEndpoint",
    "audit_current_paths",
    "current_paths_sidecar_candidates",
    "discover_current_paths_sidecar",
    "dump_current_path_specs",
    "load_current_path_specs",
    "parse_current_path_specs",
    "reinforcement_eligible_segment_ids",
    "resolve_current_path",
]

# Coordinate rounding for graph-node identity. Matches the tolerance
# ``pcb/reinforce.py::_chain_polylines`` already uses to re-match router
# segments back to schema segments.
_COORD_DECIMALS = 6

# Slack on pad-extent containment (:func:`_pad_covers`), in mm. Absorbs
# floating-point error in the pad-center/rotation transform only -- it is far
# below any manufacturable feature, so it never turns "near the pad" into "on
# the pad".
_PAD_EPS = 1e-6


@dataclass(frozen=True)
class PathEndpoint:
    """One ``RefDes.pad`` terminal of a declared current path."""

    ref: str
    pad: str

    def label(self) -> str:
        return f"{self.ref}.{self.pad}"

    def to_dict(self) -> dict[str, str]:
        return {"ref": self.ref, "pad": self.pad}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PathEndpoint:
        ref = data.get("ref")
        pad = data.get("pad")
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"current-path endpoint missing 'ref': {data!r}")
        if not isinstance(pad, str) or not pad:
            raise ValueError(f"current-path endpoint missing 'pad': {data!r}")
        return cls(ref=ref, pad=pad)


@dataclass(frozen=True)
class CurrentPathSpec:
    """A user-declared current-path intent between two stable pad endpoints.

    Distinct from :attr:`kicad_tools.router.rules.NetClassRouting
    .target_ampacity`: a single net can carry several ``CurrentPathSpec``
    entries describing physically distinct copper branches (a high-current
    trunk plus one or more low-current sense/measurement taps) that share
    the electrical net but must never be treated as carrying the same
    current or receiving the same reinforcement treatment.

    Attributes:
        name: Stable, human-readable identifier for this path (used in
            audit output and violation messages).
        net_name: The KiCad net this path's endpoints must resolve onto.
            Endpoint resolution fails closed (``"unresolved"``) if either
            pad no longer sits on this net.
        source: The current-source endpoint.
        sink: The current-sink (load) endpoint.
        continuous_a: Steady-state current this branch is declared to
            carry, in amps.
        pulsed_a: Optional peak/pulsed current, in amps, for branches with
            a distinct transient rating (e.g. inrush or switching ripple).
            Not yet consumed by :class:`~kicad_tools.validate.rules
            .path_ampacity.PathAmpacityRule` (continuous-only check for
            this increment) -- carried through so a later increment can
            add a transient/duty-cycle-aware check without a schema
            change.
        reinforcement_eligible: Whether :func:`kicad_tools.pcb.reinforce
            .reinforce_net` may anchor buttress-wire anchors along copper
            covered by this path. ``False`` for sense/measurement/Kelvin
            branches -- a force path and a Kelvin sense tap sharing a net
            must never be bridged by a shared reinforcement anchor.
        notes: Free-text rationale, surfaced in audit output only.
    """

    name: str
    net_name: str
    source: PathEndpoint
    sink: PathEndpoint
    continuous_a: float
    pulsed_a: float | None = None
    reinforcement_eligible: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "net": self.net_name,
            "source": self.source.to_dict(),
            "sink": self.sink.to_dict(),
            "continuous_a": self.continuous_a,
            "pulsed_a": self.pulsed_a,
            "reinforcement_eligible": self.reinforcement_eligible,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> CurrentPathSpec:
        name = data.get("name")
        net_name = data.get("net") or data.get("net_name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"current-path spec missing 'name': {data!r}")
        if not isinstance(net_name, str) or not net_name:
            raise ValueError(f"current-path spec {name!r} missing 'net'")
        source_data = data.get("source")
        sink_data = data.get("sink")
        if not isinstance(source_data, dict) or not isinstance(sink_data, dict):
            raise ValueError(f"current-path spec {name!r} missing 'source'/'sink'")
        continuous_a = data.get("continuous_a")
        if not isinstance(continuous_a, (int, float)):
            raise ValueError(f"current-path spec {name!r} missing numeric 'continuous_a'")
        pulsed_a = data.get("pulsed_a")
        if pulsed_a is not None and not isinstance(pulsed_a, (int, float)):
            raise ValueError(f"current-path spec {name!r} has non-numeric 'pulsed_a'")
        return cls(
            name=name,
            net_name=net_name,
            source=PathEndpoint.from_dict(source_data),
            sink=PathEndpoint.from_dict(sink_data),
            continuous_a=float(continuous_a),
            pulsed_a=float(pulsed_a) if pulsed_a is not None else None,
            reinforcement_eligible=bool(data.get("reinforcement_eligible", False)),
            notes=str(data.get("notes", "")),
        )


def parse_current_path_specs(data: object) -> list[CurrentPathSpec]:
    """Parse already-loaded JSON (a list or a ``{"paths": [...]}`` dict).

    Accepts a bare list of path objects, or the sidecar's ``{"paths": [...]}``
    wrapper form.
    """
    if isinstance(data, dict):
        raw = data.get("paths", [])
    elif isinstance(data, list):
        raw = data
    else:
        raise ValueError(
            f"current-paths sidecar must be a list or a {{'paths': [...]}} "
            f"object, got {type(data).__name__}"
        )
    if not isinstance(raw, list):
        raise ValueError("current-paths sidecar 'paths' key must be a list")
    return [CurrentPathSpec.from_dict(entry) for entry in raw]


def load_current_path_specs(path: str | Path) -> list[CurrentPathSpec]:
    """Load declared current-path specs from a JSON sidecar file."""
    text = Path(path).read_text()
    return parse_current_path_specs(json.loads(text))


def dump_current_path_specs(specs: Sequence[CurrentPathSpec]) -> dict[str, object]:
    """Serialize specs back to the sidecar's ``{"paths": [...]}`` form."""
    return {"paths": [spec.to_dict() for spec in specs]}


# The bare, board-agnostic sidecar name. Mirrors
# ``kicad_tools.sidecars.NET_CLASS_MAP_SIDECAR_BASENAME`` -- declared,
# committed board metadata (not a waiver list, which is why this follows
# the un-prefixed ``net_class_map.json`` naming convention rather than the
# leading-dot ``.kct_waivers.json`` / ``.courtyard_waivers.json`` style used
# for opt-in suppression sidecars).
CURRENT_PATHS_SIDECAR_BASENAME = "current_paths.json"


def _current_paths_sidecar_names(pcb_stem: str) -> list[str]:
    """Sidecar filenames to probe in one directory, in probe order.

    Mirrors ``kicad_tools.sidecars.net_class_map_sidecar_names``: the
    stem-keyed name (``<pcb_stem>.current_paths.json``) wins over the bare
    ``current_paths.json`` within a directory, since it is evidence about
    *this* board rather than a generic file.
    """
    if not pcb_stem:
        return [CURRENT_PATHS_SIDECAR_BASENAME]
    return [f"{pcb_stem}.{CURRENT_PATHS_SIDECAR_BASENAME}", CURRENT_PATHS_SIDECAR_BASENAME]


def current_paths_sidecar_candidates(pcb_path: str | Path) -> list[Path]:
    """Enumerate candidate ``current_paths.json`` sidecar paths for a board.

    Mirrors :func:`kicad_tools.sidecars.net_class_map_sidecar_candidates`
    exactly: the board directory, a sibling ``output/``, and
    ``../output/``, crossed with the stem-keyed-then-bare name order from
    :func:`_current_paths_sidecar_names`, de-duplicated and nearer
    directories winning over farther ones.

    Args:
        pcb_path: Path to the ``*.kicad_pcb`` being checked/routed.

    Returns:
        Candidate paths in probe order (existence not checked).
    """
    pcb_path = Path(pcb_path)
    pcb_dir = pcb_path.parent
    directories = [pcb_dir, pcb_dir / "output", pcb_dir.parent / "output"]
    names = _current_paths_sidecar_names(pcb_path.stem)
    candidates: list[Path] = []
    seen: set[Path] = set()
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def discover_current_paths_sidecar(pcb_path: str | Path) -> Path | None:
    """Return the first existing ``current_paths.json`` sidecar for a board.

    Args:
        pcb_path: Path to the ``*.kicad_pcb`` being checked/routed.

    Returns:
        The first candidate (from :func:`current_paths_sidecar_candidates`)
        that exists as a file, or ``None`` when none is found.
    """
    for candidate in current_paths_sidecar_candidates(pcb_path):
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class ResolvedEndpoint:
    """A :class:`PathEndpoint` resolved against a loaded board."""

    ref: str
    pad: str
    position: tuple[float, float]
    net_number: int
    net_name: str


# Resolution status values. Each is reported explicitly -- there is no
# implicit "resolved" default and no consumer is allowed to treat an
# absent/None status as "assume resolved" (fail-closed, per the issue's
# "endpoint mapping breaks -> fails closed" acceptance criterion).
STATUS_RESOLVED = "resolved"
STATUS_UNRESOLVED = "unresolved"
STATUS_AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class PathResolution:
    """Outcome of resolving one :class:`CurrentPathSpec` against a board.

    Attributes:
        spec: The spec that was resolved.
        status: One of ``"resolved"``, ``"unresolved"`` (endpoint pad
            missing / moved off the declared net / no continuous copper
            between the endpoints), or ``"ambiguous"`` (the endpoints ARE
            connected, but the net's routed copper contains a loop
            reachable from them -- current could split across parallel
            branches, so a single linear path cannot be trusted).
        reason: Human-readable explanation. Empty only when resolved.
        source: The resolved source endpoint, when resolvable.
        sink: The resolved sink endpoint, when resolvable.
        segments: The ordered copper segments covering this path. Only
            populated when ``status == "resolved"``.
        length_mm: Cumulative length of ``segments``.
    """

    spec: CurrentPathSpec
    status: str
    reason: str = ""
    source: ResolvedEndpoint | None = None
    sink: ResolvedEndpoint | None = None
    segments: tuple[Segment, ...] = ()
    length_mm: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_RESOLVED


def _node_key(point: tuple[float, float]) -> tuple[float, float]:
    return (round(point[0], _COORD_DECIMALS), round(point[1], _COORD_DECIMALS))


def _seg_length(seg: Segment) -> float:
    (x1, y1), (x2, y2) = seg.start, seg.end
    return math.hypot(x2 - x1, y2 - y1)


def _net_segments(pcb: PCB, net_name: str) -> list[Segment]:
    """Segments carrying ``net_name``, matching by name OR resolved number.

    Mirrors the net-resolution idiom in ``pcb/reinforce.py::reinforce_net``
    -- some boards carry name-only segments that never got a net-table
    entry synced.
    """
    net_obj = pcb.get_net_by_name(net_name)
    return [
        s
        for s in pcb.segments
        if s.net_name == net_name or (net_obj is not None and s.net_number == net_obj.number)
    ]


# Copper node identity includes the layer. Pad normalization below may replace
# a node with another copper node, but never merges layers by XY alone.
_Node = tuple[float, float, str]


@dataclass(eq=False)
class _GraphEdge:
    """One graph edge; several split edges may retain the same routed segment."""

    segment: Segment | None = None  # None is real via barrel copper


@dataclass
class _CopperGraph:
    adjacency: dict[_Node, list[tuple[_Node, _GraphEdge]]] = field(default_factory=dict)
    pads: dict[tuple[str, str], _Node] = field(default_factory=dict)
    internal: dict[_Node, list[Segment]] = field(default_factory=dict)


def _copper_node(point: tuple[float, float], layer: str) -> _Node:
    return (*_node_key(point), layer)


def _build_graph(segments: list[Segment], pcb: PCB, net_name: str) -> _CopperGraph:
    """Build exact centerline contacts and normalize convex pad-internal copper.

    Supported contacts are same-layer track endpoints (including a T landing
    in a segment interior), via centers on a track, and nodes inside supported
    pad copper. Width-only overlaps, zone/arc connectivity and arbitrary pad
    primitives are not modeled; no proximity tolerance invents a connection.
    This is not a general PCB connectivity solver.
    """
    layers = {layer.name for layer in pcb.copper_layers}
    net = pcb.get_net_by_name(net_name)
    vias = [
        via
        for via in pcb.vias
        if (via.net_name == net_name or (net is not None and via.net_number == net.number))
        and len(via.layers) == 2
        and len(set(via.layers)) == 2
        and all(layer in layers for layer in via.layers)
    ]
    contacts: dict[str, set[tuple[float, float]]] = {layer: set() for layer in layers}
    for seg in segments:
        contacts.setdefault(seg.layer, set()).update((_node_key(seg.start), _node_key(seg.end)))
    # Proper same-layer centerline crossings are also contacts. Ignoring
    # them can hide a real parallel route even when neither trace ends there.
    for i, first in enumerate(segments):
        for second in segments[i + 1 :]:
            if first.layer != second.layer or not segments_intersect(
                *first.start, *first.end, *second.start, *second.end
            ):
                continue
            dx, dy = first.end[0] - first.start[0], first.end[1] - first.start[1]
            ex, ey = second.end[0] - second.start[0], second.end[1] - second.start[1]
            ox, oy = second.start[0] - first.start[0], second.start[1] - first.start[1]
            t = (ox * ey - oy * ex) / (dx * ey - dy * ex)
            point = _node_key((first.start[0] + t * dx, first.start[1] + t * dy))
            contacts[first.layer].add(point)
    for via in vias:
        for layer in layers:
            if via_spans_layer(via.layers, layer):
                contacts[layer].add(_node_key(via.position))

    edges: list[tuple[_Node, _Node, _GraphEdge]] = []
    for seg in segments:
        points = [
            point
            for point in contacts[seg.layer]
            if point_to_segment_distance(*point, *seg.start, *seg.end) <= _PAD_EPS
        ]
        points.sort(key=lambda point: math.dist(point, seg.start))
        for point_a, point_b in zip(points, points[1:], strict=False):
            edges.append(
                (
                    _copper_node(point_a, seg.layer),
                    _copper_node(point_b, seg.layer),
                    _GraphEdge(seg),
                )
            )
    for via in vias:
        nodes = [
            _copper_node(via.position, layer)
            for layer in COPPER_LAYER_ORDER
            if layer in layers and via_spans_layer(via.layers, layer)
        ]
        for a, b in zip(nodes, nodes[1:], strict=False):
            edges.append((a, b, _GraphEdge()))

    # Contract only contacts inside one physical convex pad. Adding a pad hub
    # plus the original internal track edges instead manufactures false cycles.
    # External branches remain separate edges, so real parallel returns survive.
    parents = {node: node for a, b, _ in edges for node in (a, b)}

    def root(node: _Node) -> _Node:
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    pad_contacts: dict[tuple[str, str], list[_Node]] = {}
    internal_edges: set[_GraphEdge] = set()
    wholly_internal_ids: set[int] = set()
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_name != net_name or pad.type == "np_thru_hole":
                continue
            nodes = [
                node
                for node in parents
                if (node[2] in pad.layers or "*.Cu" in pad.layers)
                and _pad_covers(pcb, fp.reference, pad.number, node[:2])
            ]
            # Only a plated through-hole pad establishes interlayer copper.
            groups = (
                [nodes]
                if pad.type == "thru_hole"
                else [[node for node in nodes if node[2] == layer] for layer in layers]
            )
            for group in groups:
                if group:
                    members = set(group)
                    internal_edges.update(
                        edge for a, b, edge in edges if a in members and b in members
                    )
                    wholly_internal_ids.update(
                        id(seg)
                        for seg in segments
                        if _copper_node(seg.start, seg.layer) in members
                        and _copper_node(seg.end, seg.layer) in members
                    )
                    hub = root(group[0])
                    for node in group[1:]:
                        parents[root(node)] = hub
            pad_contacts[(fp.reference, pad.number)] = nodes

    graph = _CopperGraph()
    for key, nodes in pad_contacts.items():
        hubs = {root(node) for node in nodes}
        if len(hubs) == 1:
            graph.pads[key] = hubs.pop()
    for a, b, edge in edges:
        ra, rb = root(a), root(b)
        graph.adjacency.setdefault(ra, [])
        graph.adjacency.setdefault(rb, [])
        if edge in internal_edges and a != b:
            # A split piece inside a pad cannot cover its original segment's
            # external tail. That original needs a traversed external edge;
            # otherwise a dangling sense spur becomes trunk/reinforcement.
            if edge.segment is not None and id(edge.segment) in wholly_internal_ids:
                graph.internal.setdefault(ra, []).append(edge.segment)
            continue
        graph.adjacency[ra].append((rb, edge))
        graph.adjacency[rb].append((ra, edge))
    return graph


def _bfs_path(graph: _CopperGraph, start: _Node, goal: _Node) -> list[Segment] | None:
    """Find a route, reporting each original routed segment exactly once."""
    visited = {start}
    queue = deque([start])
    parent: dict[_Node, tuple[_Node, _GraphEdge]] = {}
    while queue:
        cur = queue.popleft()
        if cur == goal:
            break
        for nxt, edge in graph.adjacency.get(cur, []):
            if nxt not in visited:
                visited.add(nxt)
                parent[nxt] = (cur, edge)
                queue.append(nxt)
    if goal not in visited:
        return None
    evidence = list(graph.internal.get(goal, []))
    node = goal
    while node != start:
        prev, edge = parent[node]
        if edge.segment is not None:
            evidence.append(edge.segment)
        evidence.extend(graph.internal.get(prev, []))
        node = prev
    # A via may split an original segment into several traversed graph edges.
    # Audit/ampacity/reinforcement still operate on whole original segments.
    return list({id(seg): seg for seg in reversed(evidence)}.values())


@dataclass
class _ViaArray:
    nodes: set[_Node]
    edges: set[_GraphEdge]
    members: list[Segment]


def _endpoint_via_array(graph: _CopperGraph, pcb: PCB, endpoint: PathEndpoint, net_name: str) -> _ViaArray | None:
    """Prove straight parallel pad stubs, real barrels and one receiving trunk.

    Stub length and via span are bounded by the endpoint pad diagonal. This is
    an explicit supported-subset limit, never a current-sharing assumption.
    """
    hub = graph.pads.get((endpoint.ref, endpoint.pad))
    fp = pcb.get_footprint(endpoint.ref)
    if hub is None or fp is None:
        return None
    pad = next((p for p in fp.pads if p.number == endpoint.pad), None)
    if pad is None or pad.type != "smd" or hub[2] not in {"F.Cu", "B.Cu"}:
        return None
    if any(node == hub and key != (endpoint.ref, endpoint.pad) for key, node in graph.pads.items()):
        return None
    far_layer = "B.Cu" if hub[2] == "F.Cu" else "F.Cu"
    arms = graph.adjacency.get(hub, [])
    if len(arms) < 2:
        return None
    bound = math.hypot(*pad.size)
    nodes = {hub}
    edges: set[_GraphEdge] = set()
    members: list[Segment] = list(graph.internal.get(hub, []))
    far_nodes = []
    direction = None
    for top, edge in arms:
        seg = edge.segment
        if seg is None or seg.layer != hub[2] or top in nodes or len(graph.adjacency[top]) != 2:
            return None
        anchors = [p for p in (seg.start, seg.end) if _pad_covers(pcb, endpoint.ref, endpoint.pad, p)]
        if len(anchors) != 1:
            return None
        anchor = anchors[0]
        tip = seg.end if anchor == seg.start else seg.start
        if _node_key(tip) != top[:2] or _seg_length(seg) > bound + _PAD_EPS:
            return None
        delta = (tip[0] - anchor[0], tip[1] - anchor[1])
        if direction is None:
            direction = delta
        elif (abs(direction[0] * delta[1] - direction[1] * delta[0]) > _PAD_EPS
              or direction[0] * delta[0] + direction[1] * delta[1] <= 0):
            return None
        matching = [v for v in pcb.vias if _node_key(v.position) == top[:2]]
        if len(matching) != 1:
            return None
        via = matching[0]
        net = pcb.get_net_by_name(net_name)
        if set(via.layers) != {"F.Cu", "B.Cu"} or not (via.net_name == net_name or (net is not None and via.net_number == net.number)):
            return None
        members.append(seg)
        edges.add(edge)
        previous, current = edge, top
        while True:
            if current in nodes:
                return None
            nodes.add(current)
            onward = [(n, e) for n, e in graph.adjacency[current] if e is not previous]
            if current[2] == far_layer:
                far_nodes.append(current)
                break
            if len(onward) != 1 or onward[0][1].segment is not None:
                return None
            nxt, barrel = onward[0]
            if nxt[:2] != top[:2]:
                return None
            edges.add(barrel)
            previous, current = barrel, nxt
    if max(math.dist(a[:2], b[:2]) for a in far_nodes for b in far_nodes) > bound + _PAD_EPS:
        return None
    candidates = [e.segment for _, e in graph.adjacency[far_nodes[0]]
                  if e.segment is not None and e.segment.layer == far_layer
                  and all(point_to_segment_distance(*n[:2], *e.segment.start, *e.segment.end) <= _PAD_EPS for n in far_nodes)]
    candidates = list({id(seg): seg for seg in candidates}.values())
    if len(candidates) != 1:
        return None
    trunk = candidates[0]
    ordered = sorted(far_nodes, key=lambda n: math.dist(n[:2], trunk.start))
    first, last = ordered[0], ordered[-1]
    for node in graph.adjacency:
        if node[2] == far_layer and point_to_segment_distance(*node[:2], *first[:2], *last[:2]) <= _PAD_EPS:
            nodes.add(node)
    if any(node in nodes and node != hub for node in graph.pads.values()):
        return None
    trunk_edges = set()
    for node in nodes:
        for other, edge in graph.adjacency[node]:
            if other in nodes and edge not in edges:
                if edge.segment is not trunk:
                    return None
                trunk_edges.add(edge)
    reached, pending = {first}, [first]
    while pending:
        for other, edge in graph.adjacency[pending.pop()]:
            if edge in trunk_edges and other not in reached:
                reached.add(other)
                pending.append(other)
    if not set(far_nodes) <= reached:
        return None
    edges.update(trunk_edges)
    exits = [(n, other, e) for n in nodes for other, e in graph.adjacency[n] if other not in nodes]
    if len(exits) != 1:
        return None
    members.append(trunk)
    return _ViaArray(nodes, edges, list({id(seg): seg for seg in members}.values()))


def _component_has_cycle(graph: _CopperGraph, start: _Node, arrays: Sequence[_ViaArray] = ()) -> bool:
    """Contract only physically proved arrays; keep every other cycle visible."""
    roots = {node: next(iter(array.nodes)) for array in arrays for node in array.nodes}
    visited_nodes, visited_roots = {start}, {roots.get(start, start)}
    seen_edges: set[_GraphEdge] = set()
    contracted = {edge for array in arrays for edge in array.edges}
    stack = [start]
    while stack:
        cur = stack.pop()
        for nxt, edge in graph.adjacency.get(cur, []):
            if edge not in contracted:
                seen_edges.add(edge)
            if nxt not in visited_nodes:
                visited_nodes.add(nxt)
                visited_roots.add(roots.get(nxt, nxt))
                stack.append(nxt)
    return len(seen_edges) > len(visited_roots) - 1


def _pad_covers(pcb: PCB, ref: str, pad_number: str, point: tuple[float, float]) -> bool:
    """True if ``point`` lies on the named pad's copper.

    Exact extent test in the pad's own rotated frame rather than a fixed
    tolerance: pads are not points, and a router may legitimately terminate a
    trace anywhere inside one. On board09 the ``+5V_OUT`` force path enters
    the 2.29 x 2.03 mm shunt pad ``RSH1.4`` through THREE stubs at 0.015 mm,
    0.785 mm and 0.815 mm from its center -- all plainly on the pad, none at
    its center.

    ``pad.rotation`` is absolute (it already includes the footprint's
    rotation -- see :class:`~kicad_tools.schema.pcb.Pad`), so the point is
    un-rotated by that angle alone.
    """
    fp = pcb.get_footprint(ref)
    if fp is None:
        return False
    pad = next((p for p in fp.pads if p.number == pad_number), None)
    if pad is None:
        return False
    center = pcb.get_pad_position(ref, pad_number)
    if center is None:
        return False
    try:
        half_w = float(pad.size[0]) / 2.0
        half_h = float(pad.size[1]) / 2.0
    except (TypeError, ValueError, IndexError):
        return False
    if half_w <= 0.0 or half_h <= 0.0:
        return False

    dx = point[0] - center[0]
    dy = point[1] - center[1]
    angle = math.radians(-float(getattr(pad, "rotation", 0.0) or 0.0))
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    local_x = dx * cos_a - dy * sin_a
    local_y = dx * sin_a + dy * cos_a

    if pad.shape == "circle":
        radius = min(half_w, half_h)
        return math.hypot(local_x, local_y) <= radius + _PAD_EPS
    if pad.shape == "oval":
        # KiCad ovals are capsules, not ellipses.
        radius = min(half_w, half_h)
        dx = max(abs(local_x) - (half_w - radius), 0.0)
        dy = max(abs(local_y) - (half_h - radius), 0.0)
        return math.hypot(dx, dy) <= radius + _PAD_EPS
    if pad.shape == "rect":
        return abs(local_x) <= half_w + _PAD_EPS and abs(local_y) <= half_h + _PAD_EPS
    if pad.shape == "roundrect":
        ratio = pad.roundrect_rratio
        if not 0 <= ratio <= 0.5:
            return False
        radius = 2 * min(half_w, half_h) * ratio
        dx = max(abs(local_x) - (half_w - radius), 0.0)
        dy = max(abs(local_y) - (half_h - radius), 0.0)
        return math.hypot(dx, dy) <= radius + _PAD_EPS
    # Custom/trapezoid copper cannot be inferred from its bounding rectangle.
    return False


def _resolve_endpoint(
    pcb: PCB, endpoint: PathEndpoint, expected_net: str
) -> tuple[ResolvedEndpoint | None, str | None]:
    fp = pcb.get_footprint(endpoint.ref)
    if fp is None:
        return None, f"component {endpoint.ref!r} not found on board"
    matches = [p for p in fp.pads if p.number == endpoint.pad]
    if len(matches) > 1:
        return (
            None,
            f"pad {endpoint.label()} has multiple physical occurrences; contact is unsupported",
        )
    pad = matches[0] if matches else None
    if pad is None:
        return None, f"pad {endpoint.label()} not found on component {endpoint.ref!r}"
    if pad.net_name != expected_net:
        return None, (
            f"pad {endpoint.label()} resolves to net {pad.net_name!r}, expected {expected_net!r}"
        )
    pos = pcb.get_pad_position(endpoint.ref, endpoint.pad)
    if pos is None:
        return None, f"could not compute board position for pad {endpoint.label()}"
    return (
        ResolvedEndpoint(
            ref=endpoint.ref,
            pad=endpoint.pad,
            position=pos,
            net_number=pad.net_number,
            net_name=pad.net_name,
        ),
        None,
    )


def resolve_current_path(pcb: PCB, spec: CurrentPathSpec) -> PathResolution:
    """Resolve one declared current-path spec against a loaded board.

    Fails closed at every step: a missing/replaced/off-net pad, a pad with
    no routed copper reaching it, or a net whose copper contains a loop
    reachable from the endpoints all produce an explicit non-``"resolved"``
    status rather than a silent fallback.

    Args:
        pcb: A loaded :class:`~kicad_tools.schema.pcb.PCB`.
        spec: The declared current-path intent to resolve.

    Returns:
        A :class:`PathResolution` describing the outcome.
    """
    source, source_err = _resolve_endpoint(pcb, spec.source, spec.net_name)
    if source is None:
        return PathResolution(
            spec=spec,
            status=STATUS_UNRESOLVED,
            reason=source_err or "source endpoint unresolved",
        )
    sink, sink_err = _resolve_endpoint(pcb, spec.sink, spec.net_name)
    if sink is None:
        return PathResolution(
            spec=spec,
            status=STATUS_UNRESOLVED,
            reason=sink_err or "sink endpoint unresolved",
            source=source,
        )
    if source.net_number != sink.net_number:
        return PathResolution(
            spec=spec,
            status=STATUS_UNRESOLVED,
            reason=(
                f"source {spec.source.label()} and sink {spec.sink.label()} resolve to "
                f"different nets ({source.net_name!r} vs {sink.net_name!r})"
            ),
            source=source,
            sink=sink,
        )

    net_segments = _net_segments(pcb, spec.net_name)
    if not net_segments:
        return PathResolution(
            spec=spec,
            status=STATUS_UNRESOLVED,
            reason=f"net {spec.net_name!r} has no routed copper",
            source=source,
            sink=sink,
        )

    # Unknown shapes or repeated physical pad numbers might hide alternate
    # routes. Refuse the net rather than silently ignoring their copper.
    unsupported = None
    for fp in pcb.footprints:
        seen_numbers: set[str] = set()
        for pad in fp.pads:
            if pad.net_name != spec.net_name or pad.type == "np_thru_hole":
                continue
            if pad.number in seen_numbers or pad.shape not in {
                "rect",
                "roundrect",
                "circle",
                "oval",
            }:
                unsupported = f"unsupported physical pad contact at {fp.reference}.{pad.number}"
            seen_numbers.add(pad.number)
    layers = {layer.name for layer in pcb.copper_layers}
    if any(seg.layer not in layers for seg in net_segments):
        unsupported = "routed copper uses a layer absent from the board stackup"
    if unsupported:
        return PathResolution(
            spec=spec, status=STATUS_UNRESOLVED, reason=unsupported, source=source, sink=sink
        )

    adjacency = _build_graph(net_segments, pcb, spec.net_name)

    if spec.source == spec.sink:
        # Degenerate self-path: a declaration whose source and sink are the
        # same pad covers no copper, so it resolves trivially without needing
        # anything attached to the graph at all (preserved from before the
        # pad-extent attachment below existed).
        return PathResolution(
            spec=spec, status=STATUS_RESOLVED, source=source, sink=sink, segments=(), length_mm=0.0
        )

    # Attach each endpoint by its pad's own extent, not by an exact center
    # hit: a trace terminating anywhere inside the pad is electrically on it,
    # and several traces landing on one pad are shorted by it. Without this,
    # an ordinary sub-millimetre router offset reports a perfectly good force
    # path as "unresolved" -- a FALSE fail-closed, which teaches users to
    # delete declarations and is every bit as unsafe as the silent pass this
    # rule exists to prevent.
    start = adjacency.pads.get((source.ref, source.pad))
    goal = adjacency.pads.get((sink.ref, sink.pad))

    if start is None:
        return PathResolution(
            spec=spec,
            status=STATUS_UNRESOLVED,
            reason=f"source pad {spec.source.label()} has no routed copper touching it",
            source=source,
            sink=sink,
        )
    if goal is None:
        return PathResolution(
            spec=spec,
            status=STATUS_UNRESOLVED,
            reason=f"sink pad {spec.sink.label()} has no routed copper touching it",
            source=source,
            sink=sink,
        )

    path_segments = _bfs_path(adjacency, start, goal)
    if path_segments is None:
        return PathResolution(
            spec=spec,
            status=STATUS_UNRESOLVED,
            reason="no continuous copper path found between declared endpoints",
            source=source,
            sink=sink,
        )

    arrays = []
    for endpoint in (spec.source, spec.sink):
        array = _endpoint_via_array(adjacency, pcb, endpoint, spec.net_name)
        if array is not None and not any(array.nodes & previous.nodes for previous in arrays):
            arrays.append(array)
    if _component_has_cycle(adjacency, start, arrays):
        return PathResolution(
            spec=spec,
            status=STATUS_AMBIGUOUS,
            reason=(
                "declared endpoints sit on a net whose routed copper contains a "
                "loop/parallel return path reachable from them -- current split "
                "between branches cannot be determined from a single resolved path"
            ),
            source=source,
            sink=sink,
        )

    # Every member remains physical evidence checked at the full current.
    path_segments = list({id(seg): seg for seg in [*path_segments, *(seg for array in arrays for seg in array.members)]}.values())

    return PathResolution(
        spec=spec,
        status=STATUS_RESOLVED,
        source=source,
        sink=sink,
        segments=tuple(path_segments),
        length_mm=sum(_seg_length(s) for s in path_segments),
    )


@dataclass
class CurrentPathAudit:
    """Independent post-write audit of a set of declared current paths.

    Attributes:
        resolutions: One :class:`PathResolution` per input spec, in input
            order.
        uncovered: ``{net_name: [Segment, ...]}`` -- for every net that has
            at least one declared path, any routed segment on that net not
            covered by any *resolved* path. Empty for a net whose declared
            paths fully account for its routed copper. A net with zero
            declared paths is not reported here at all (it is entirely out
            of this audit's declared scope, not "clean").
    """

    resolutions: list[PathResolution] = field(default_factory=list)
    uncovered: dict[str, list[Segment]] = field(default_factory=dict)

    @property
    def all_resolved(self) -> bool:
        return all(r.ok for r in self.resolutions)

    @property
    def unresolved(self) -> list[PathResolution]:
        return [r for r in self.resolutions if r.status == STATUS_UNRESOLVED]

    @property
    def ambiguous(self) -> list[PathResolution]:
        return [r for r in self.resolutions if r.status == STATUS_AMBIGUOUS]

    @property
    def fully_covered(self) -> bool:
        """True iff every net with a declared path has zero uncovered copper."""
        return not any(self.uncovered.values())


def audit_current_paths(pcb: PCB, specs: Sequence[CurrentPathSpec]) -> CurrentPathAudit:
    """Resolve every declared path and report unmodeled/uncovered copper.

    This is the "independent final-copper audit" the issue requires:
    given only the routed board and the declared intent, it re-derives
    which physical copper belongs to which declared branch (or to none),
    without relying on any state carried from route time. Comparing this
    against a route-time resolution of the same specs (also just a call to
    :func:`resolve_current_path`/this function) is how a caller verifies
    route-time intent and final-copper audit agree.

    Args:
        pcb: A loaded :class:`~kicad_tools.schema.pcb.PCB`.
        specs: Declared current-path intents to audit.

    Returns:
        A :class:`CurrentPathAudit`.
    """
    resolutions = [resolve_current_path(pcb, spec) for spec in specs]

    covered_ids: dict[str, set[int]] = {}
    for resolution in resolutions:
        if not resolution.ok:
            continue
        bucket = covered_ids.setdefault(resolution.spec.net_name, set())
        bucket.update(id(seg) for seg in resolution.segments)

    uncovered: dict[str, list[Segment]] = {}
    nets_with_specs = {spec.net_name for spec in specs}
    for net_name in nets_with_specs:
        net_segments = _net_segments(pcb, net_name)
        covered = covered_ids.get(net_name, set())
        missing = [seg for seg in net_segments if id(seg) not in covered]
        if missing:
            uncovered[net_name] = missing

    return CurrentPathAudit(resolutions=resolutions, uncovered=uncovered)


def reinforcement_eligible_segment_ids(pcb: PCB, specs: Sequence[CurrentPathSpec]) -> set[int]:
    """Segment ids that MAY receive a buttress-wire anchor.

    Deliberately an **allow-list**, not a block-list: a segment is eligible
    only when it is covered by a *resolved*, reinforcement-**eligible**
    path. Everything else -- copper covered by a resolved but
    reinforcement-**ineligible** path (a Kelvin sense tap, a feedback
    branch), copper covered by an *unresolved* or *ambiguous* path
    (regardless of its declared eligibility), and copper not covered by
    any declared path at all -- is excluded.

    The allow-list direction is what makes this fail closed for the "pad
    moved/replaced" case: if a *trunk*'s own endpoint mapping breaks, its
    copper silently drops OUT of the eligible set (reinforcement stops
    rather than continuing against a declaration nobody can any longer
    verify); if a *sense* spec's mapping breaks, its copper was never
    eligible in the first place, so there is no path by which a broken
    ineligible declaration could accidentally re-enable reinforcement of
    copper it exists to protect.

    Args:
        pcb: A loaded :class:`~kicad_tools.schema.pcb.PCB`.
        specs: Declared current-path intents (already filtered to a single
            net by the caller, typically).

    Returns:
        The set of ``id(segment)`` values that may be anchored.
    """
    eligible: set[int] = set()
    for spec in specs:
        if not spec.reinforcement_eligible:
            continue
        resolution = resolve_current_path(pcb, spec)
        if not resolution.ok:
            continue
        eligible.update(id(seg) for seg in resolution.segments)
    return eligible

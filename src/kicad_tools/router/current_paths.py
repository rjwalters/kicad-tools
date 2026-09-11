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
(:func:`_pad_covers` / :func:`_bind_pad`): a router may terminate a trace
anywhere inside a pad's copper, and a wide power pad is routinely entered
by several stubs at once, all shorted by the pad itself. Binding on an
exact center match instead reported such boards ``unresolved`` -- a *false*
fail-closed, which is not conservative at all: it teaches users to delete
declarations, and a deleted declaration is exactly the silent pass this
module exists to prevent. Copper outside the pad extent still never
attaches, so the fail-closed direction is preserved where it is real.

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


def _build_graph(
    segments: list[Segment],
) -> dict[tuple[float, float], list[tuple[tuple[float, float], Segment]]]:
    adjacency: dict[tuple[float, float], list[tuple[tuple[float, float], Segment]]] = {}
    for seg in segments:
        a, b = _node_key(seg.start), _node_key(seg.end)
        adjacency.setdefault(a, []).append((b, seg))
        adjacency.setdefault(b, []).append((a, seg))
    return adjacency


def _bfs_path(
    adjacency: dict[tuple[float, float], list[tuple[tuple[float, float], Segment]]],
    start: tuple[float, float],
    goal: tuple[float, float],
) -> list[Segment] | None:
    """Shortest (fewest-hop) segment chain from ``start`` to ``goal``, or None."""
    if start == goal:
        return []
    visited = {start}
    queue: deque[tuple[float, float]] = deque([start])
    parent: dict[tuple[float, float], tuple[tuple[float, float], Segment]] = {}
    while queue:
        cur = queue.popleft()
        if cur == goal:
            break
        for nxt, seg in adjacency.get(cur, []):
            if nxt in visited:
                continue
            visited.add(nxt)
            parent[nxt] = (cur, seg)
            queue.append(nxt)
    if goal not in visited:
        return None
    path_segments: list[Segment] = []
    node = goal
    while node != start:
        prev, seg = parent[node]
        # Pad shorts are pseudo-edges standing for a pad's own copper, not
        # routed segments: they must not appear in the covered-segment set
        # (nothing can check their width, and they are not "uncovered copper"
        # a designer could route differently).
        if not isinstance(seg, _PadShort):
            path_segments.append(seg)
        node = prev
    path_segments.reverse()
    return path_segments


def _component_has_cycle(
    adjacency: dict[tuple[float, float], list[tuple[tuple[float, float], Segment]]],
    start: tuple[float, float],
) -> bool:
    """True if the connected component containing ``start`` has a cycle.

    A cycle anywhere in the component reachable from a declared endpoint
    means the net's copper offers more than one electrical route between
    some pair of points in that component -- i.e. current entering at
    ``start`` could split across parallel branches. This is a deliberately
    coarse, whole-component check (not limited to routes between the
    declared source and sink): the issue requires that a parallel return
    or alternate-mode path never be silently collapsed into a single
    confident result, and erring toward "ambiguous" is the fail-closed
    direction.
    """
    visited = {start}
    stack = [start]
    seen_segment_ids: set[int] = set()
    edge_count = 0
    while stack:
        cur = stack.pop()
        for nxt, seg in adjacency.get(cur, []):
            seg_id = id(seg)
            if seg_id not in seen_segment_ids:
                seen_segment_ids.add(seg_id)
                edge_count += 1
            if nxt not in visited:
                visited.add(nxt)
                stack.append(nxt)
    return edge_count > len(visited) - 1


class _PadShort:
    """A zero-length pseudo-edge standing for a pad's own copper.

    Every trace endpoint landing inside one pad is the *same* electrical
    node -- the pad shorts them. Modelling that as an explicit edge (rather
    than by rewriting node keys) keeps :func:`_component_has_cycle`'s
    edge-vs-node counting honest: each instance is distinct, so ``id()``
    dedup counts pad shorts exactly once each, and a pad that fans out to
    branches which never rejoin still counts as a tree.
    """

    __slots__ = ()


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

    if pad.shape in ("circle", "oval"):
        return (local_x / half_w) ** 2 + (local_y / half_h) ** 2 <= 1.0 + _PAD_EPS
    # rect / roundrect / trapezoid / custom: the bounding rectangle. Ignoring a
    # roundrect's clipped corners over-approximates by at most a corner radius,
    # which is far below any clearance a trace could legally end in.
    return abs(local_x) <= half_w + _PAD_EPS and abs(local_y) <= half_h + _PAD_EPS


def _bind_pad(
    adjacency: dict[tuple[float, float], list[tuple[tuple[float, float], Segment]]],
    pcb: PCB,
    ref: str,
    pad_number: str,
    position: tuple[float, float],
) -> tuple[float, float] | None:
    """Attach a pad to the copper graph, returning its node (or None).

    All graph nodes lying on the pad are shorted together through a hub node
    at the pad's center, because the pad's own copper connects them. Returns
    ``None`` -- the fail-closed outcome -- only when no copper touches the pad
    at all.
    """
    hub = _node_key(position)
    on_pad = [n for n in adjacency if _pad_covers(pcb, ref, pad_number, n)]
    if not on_pad:
        return None
    for node in on_pad:
        if node == hub:
            continue
        short = _PadShort()
        adjacency.setdefault(hub, []).append((node, short))  # type: ignore[arg-type]
        adjacency.setdefault(node, []).append((hub, short))  # type: ignore[arg-type]
    return hub


def _resolve_endpoint(
    pcb: PCB, endpoint: PathEndpoint, expected_net: str
) -> tuple[ResolvedEndpoint | None, str | None]:
    fp = pcb.get_footprint(endpoint.ref)
    if fp is None:
        return None, f"component {endpoint.ref!r} not found on board"
    pad = next((p for p in fp.pads if p.number == endpoint.pad), None)
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

    adjacency = _build_graph(net_segments)

    if _node_key(source.position) == _node_key(sink.position):
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
    start = _bind_pad(adjacency, pcb, source.ref, source.pad, source.position)
    goal = _bind_pad(adjacency, pcb, sink.ref, sink.pad, sink.position)

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

    if _component_has_cycle(adjacency, start):
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

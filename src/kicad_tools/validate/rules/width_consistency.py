"""Trace width-consistency audit (geometric, not ampacity).

A board can pass DRC and still carry many *unnecessary* width steps: a
router or a hand edit leaves a short wide stub in the middle of a thin
track, or necks a power trace down to signal width for a stretch where
nothing nearby forces it.  Each step is legal, but it is noise for review,
a current bottleneck nobody chose, and a sign the route was never
normalised.  This rule is a **heuristic triage aid** for those steps.  It
does not know electrical intent (current, impedance) -- that is what
:class:`~kicad_tools.validate.rules.ampacity.AmpacityRule` and
:class:`~kicad_tools.validate.rules.impedance.ImpedanceRule` are for.

Model
-----
Per copper layer and net, straight segments and arcs form a graph whose
nodes are track endpoints (endpoints within ``node_tolerance_mm`` of each
other are joined into one node).  A node is a
*stop* when it has other than two incident tracks (branch / dead end) or
touches a same-net pad or via.  A **chain** is the track sequence between
two stops; within a chain, consecutive tracks of equal width (within
``width_tolerance_mm``) form a **run**.  Only endpoint-to-endpoint joins
are modelled: a track ending on the *interior* of another track (a
T-junction without a node) or merely overlapping it is not followed, so
such chains end there as dead ends and are not audited.

Findings
--------
``width_island``
    A run strictly inside a chain whose neighbouring runs on **both**
    sides are narrower, and whose length is below ``max_island_length_mm``.
    These are almost always leftovers (e.g. a 0.4 mm stub in a 0.2 mm
    track); unify the width or extend the wide section on purpose.

``width_transition``
    A width change inside a chain whose two ends both land on pads/vias
    (a two-terminal point-to-point route; branches are left alone because
    different branch widths are often intentional).  The narrow run is
    *justified* when widening the whole narrow run to the wide width would
    bring it closer than the clearance to other-net copper (tracks, pads,
    vias) on the same layer, or when the narrow run ends on a pad smaller
    than the wide width (a pad escape).  Unjustified transitions are
    reported with the nearest other-net obstacle and the gap before and
    after widening, so a reviewer can triage quickly.  Justified ones are
    silent unless ``report_justified=True`` (then ``info``).  Each narrow
    run is reported once (a neck with wider copper on both sides is judged
    against the wider neighbour), and transitions that bound a reported
    island are folded into the island finding.

Clearance defaults to ``design_rules.min_clearance_mm``; pass
``clearance_mm`` to use a stricter netclass value.  Foreign-net **zone
fills are ignored** as obstacles: a pour re-flows around a widened track
on refill, so it never justifies a neck (and would otherwise "justify"
every track on a poured layer).  Board edges and keepout areas are not
considered.  Both findings are ``warning`` by default (``severity``);
waive per net or per track UUID through ``.kct_waivers.json``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely
from kicad_tools.core.layers import via_spans_layer as _via_spans_layer

from ..violations import DRCResults, DRCViolation
from .base import DRCRule
from .clearance import _pad_on_layer, _pad_polygon

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Segment

# Arcs are sampled for obstacle geometry only; lengths stay analytic.
_ARC_SAMPLE_ERROR_MM = 0.001

NodeKey = tuple[int, int]


@dataclass
class _Track:
    """One straight segment or arc, normalised for chain walking."""

    segment: Segment
    points: list[tuple[float, float]]
    length: float
    width: float
    net_label: str
    uuid: str

    @property
    def ends(self) -> tuple[tuple[float, float], tuple[float, float]]:
        return self.points[0], self.points[-1]


@dataclass
class _Run:
    tracks: list[_Track]
    width: float

    @property
    def length(self) -> float:
        return sum(t.length for t in self.tracks)


@dataclass
class _Chain:
    tracks: list[_Track]
    start: NodeKey
    end: NodeKey


@dataclass
class _Terminal:
    net_key: str
    geometry: Any
    label: str
    min_dimension: float  # smallest pad dimension; inf for vias


@dataclass
class _Obstacle:
    net_key: str
    geometry: Any
    label: str


class WidthConsistencyRule(DRCRule):
    """Flag width islands and unjustified width transitions on routed copper.

    Args:
        max_island_length_mm: Runs shorter than this that are bounded on
            both sides by narrower copper are reported as islands.
        width_tolerance_mm: Widths within this tolerance are "equal".
        clearance_mm: Clearance used to decide whether a neck-down is
            justified.  ``None`` uses ``design_rules.min_clearance_mm``.
        obstacle_search_mm: How far beyond the clearance to look for the
            nearest other-net obstacle when reporting a transition.
        severity: Severity for both finding types (``"warning"``).
        report_islands / report_transitions: Enable each finding type.
        report_justified: Also emit ``info`` findings for transitions the
            clearance check justifies.
        node_tolerance_mm: Endpoint snapping tolerance for connectivity.
    """

    rule_id = "width_consistency"
    name = "Trace Width Consistency"
    description = (
        "Heuristic audit of short width islands and width transitions that no "
        "nearby clearance constraint explains"
    )

    ISLAND_ID = "width_island"
    TRANSITION_ID = "width_transition"

    def __init__(
        self,
        *,
        max_island_length_mm: float = 3.0,
        width_tolerance_mm: float = 0.001,
        clearance_mm: float | None = None,
        obstacle_search_mm: float = 1.0,
        severity: str = "warning",
        report_islands: bool = True,
        report_transitions: bool = True,
        report_justified: bool = False,
        node_tolerance_mm: float = 0.0005,
    ) -> None:
        if severity not in ("error", "warning", "info"):
            raise ValueError(f"severity must be error/warning/info, got {severity!r}")
        self.max_island_length_mm = max_island_length_mm
        self.width_tolerance_mm = width_tolerance_mm
        self.clearance_mm = clearance_mm
        self.obstacle_search_mm = obstacle_search_mm
        self.severity = severity
        self.report_islands = report_islands
        self.report_transitions = report_transitions
        self.report_justified = report_justified
        self.node_tolerance_mm = node_tolerance_mm
        self._net_names: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Audit every copper layer; see the module docstring for the model."""
        require_shapely("trace width-consistency audit")

        results = DRCResults()
        results.rules_checked = 1
        results.rules_checked_by_rule[self.rule_id] = 1
        clearance = (
            self.clearance_mm if self.clearance_mm is not None else design_rules.min_clearance_mm
        )
        self._net_names = {net.number: net.name for net in pcb.nets.values()}

        for layer in pcb.copper_layers:
            by_net = self._tracks_by_net(pcb, layer.name)
            if not by_net:
                continue
            terminals, obstacles = self._layer_copper(pcb, layer.name)
            obstacle_index = self._index(obstacles)
            for net_key, tracks in by_net.items():
                net_terminals = [t for t in terminals if t.net_key == net_key]
                chains, terminal_nodes = self._chains(tracks, net_terminals)
                for chain in chains:
                    self._audit_chain(
                        chain,
                        layer.name,
                        net_key,
                        terminal_nodes,
                        net_terminals,
                        obstacle_index,
                        clearance,
                        results,
                    )
        return results

    # ------------------------------------------------------------------
    # Geometry collection
    # ------------------------------------------------------------------

    def _net_key(self, net_number: int, net_name: str) -> str | None:
        """Stable net identity (resolved name, else ``#N``); ``None`` if unassigned."""
        name = net_name or self._net_names.get(net_number, "")
        if name:
            return name
        return f"#{net_number}" if net_number else None

    def _tracks_by_net(self, pcb: PCB, layer: str) -> dict[str, list[_Track]]:
        from kicad_tools.schema.pcb import Arc

        by_net: dict[str, list[_Track]] = {}
        items: list[Segment] = [*pcb.segments_on_layer(layer), *pcb.arcs_on_layer(layer)]
        for seg in items:
            key = self._net_key(seg.net_number, seg.net_name)
            if key is None or seg.width <= 0:
                continue
            if isinstance(seg, Arc):
                points = seg.centerline_points(_ARC_SAMPLE_ERROR_MM)
                length = seg.length
            else:
                points = [seg.start, seg.end]
                length = math.dist(seg.start, seg.end)
            if length <= self.node_tolerance_mm:
                continue  # zero-length artifacts belong to zero_length_segment
            by_net.setdefault(key, []).append(
                _Track(
                    segment=seg,
                    points=points,
                    length=length,
                    width=seg.width,
                    net_label=seg.net_name or f"net {seg.net_number}",
                    uuid=seg.uuid,
                )
            )
        return by_net

    def _layer_copper(self, pcb: PCB, layer: str) -> tuple[list[_Terminal], list[_Obstacle]]:
        """Pads/vias (terminals) and all other-net-capable obstacles on a layer."""
        from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

        from kicad_tools.schema.pcb import Arc

        terminals: list[_Terminal] = []
        obstacles: list[_Obstacle] = []
        # Labels are read by humans next to sheet-absolute locations.
        ox, oy = getattr(pcb, "board_origin", (0.0, 0.0))
        for fp in pcb.footprints:
            for pad in fp.pads:
                if not _pad_on_layer(pad, layer):
                    continue
                poly = _pad_polygon(pad, fp)
                if poly is None or poly.is_empty:
                    continue
                label = f"pad {fp.reference}.{pad.number}"
                key = self._net_key(pad.net_number, pad.net_name)
                if key is not None:
                    terminals.append(_Terminal(key, poly, label, min(pad.size)))
                obstacles.append(_Obstacle(key or f"<unassigned {label}>", poly, label))
        for via in pcb.vias:
            if via.size <= 0 or not _via_spans_layer(via.layers, layer):
                continue
            disc = Point(via.position).buffer(via.size / 2.0, quad_segs=16)
            label = f"via @({via.position[0] + ox:.3f},{via.position[1] + oy:.3f})"
            key = self._net_key(via.net_number, via.net_name)
            if key is not None:
                terminals.append(_Terminal(key, disc, label, math.inf))
            obstacles.append(_Obstacle(key or f"<unassigned {label}>", disc, label))
        items: list[Segment] = [*pcb.segments_on_layer(layer), *pcb.arcs_on_layer(layer)]
        for seg in items:
            if seg.width <= 0:
                continue
            if isinstance(seg, Arc):
                line = LineString(seg.centerline_points(_ARC_SAMPLE_ERROR_MM))
            elif math.dist(seg.start, seg.end) > 0:
                line = LineString([seg.start, seg.end])
            else:
                line = Point(seg.start)
            label = f"track {seg.uuid}" if seg.uuid else f"track on {seg.net_name}"
            key = self._net_key(seg.net_number, seg.net_name)
            geom = line.buffer(seg.width / 2.0)
            obstacles.append(_Obstacle(key or f"<unassigned {label}>", geom, label))
        return terminals, obstacles

    @staticmethod
    def _index(obstacles: list[_Obstacle]) -> tuple[Any, list[_Obstacle]]:
        from shapely.strtree import STRtree  # type: ignore[import-untyped]

        return STRtree([o.geometry for o in obstacles]), obstacles

    # ------------------------------------------------------------------
    # Topology
    # ------------------------------------------------------------------

    def _node(self, point: tuple[float, float]) -> NodeKey:
        """Quantized grid bucket of *point* (``node_tolerance_mm`` cells)."""
        q = self.node_tolerance_mm
        return (round(point[0] / q), round(point[1] / q))

    def _end_nodes(self, tracks: list[_Track]) -> list[tuple[NodeKey, NodeKey]]:
        """Canonical node key of each track's two ends.

        Endpoints within ``node_tolerance_mm`` of each other share a node even
        when they round into different grid buckets (e.g. either side of a
        ``0.5 * q`` boundary).  Candidate pairs come from the 3x3 neighbouring
        buckets only, so the join stays local; joined endpoints are merged
        with union-find and each component is keyed by its smallest bucket
        (deterministic, and identical to the plain rounded key whenever no
        boundary is straddled).
        """
        tol = self.node_tolerance_mm
        points: list[tuple[float, float]] = [end for track in tracks for end in track.ends]
        buckets = [self._node(p) for p in points]
        by_bucket: dict[NodeKey, list[int]] = {}
        for i, key in enumerate(buckets):
            by_bucket.setdefault(key, []).append(i)

        parent = list(range(len(points)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, (bx, by) in enumerate(buckets):
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in by_bucket.get((bx + dx, by + dy), ()):
                        if j <= i or math.dist(points[i], points[j]) > tol:
                            continue
                        ri, rj = find(i), find(j)
                        if ri != rj:
                            parent[rj] = ri

        canonical: dict[int, NodeKey] = {}
        for i, key in enumerate(buckets):
            root = find(i)
            if root not in canonical or key < canonical[root]:
                canonical[root] = key
        keys = [canonical[find(i)] for i in range(len(points))]
        return [(keys[2 * t], keys[2 * t + 1]) for t in range(len(tracks))]

    def _chains(
        self, tracks: list[_Track], terminals: list[_Terminal]
    ) -> tuple[list[_Chain], set[NodeKey]]:
        """Split a net's tracks on one layer into stop-to-stop chains.

        Returns the chains and the set of nodes that touch a same-net
        pad or via.
        """
        from shapely.geometry import Point

        end_nodes = self._end_nodes(tracks)
        incident: dict[NodeKey, list[int]] = {}
        coords: dict[NodeKey, tuple[float, float]] = {}
        for i, track in enumerate(tracks):
            for end, key in zip(track.ends, end_nodes[i], strict=True):
                incident.setdefault(key, []).append(i)
                coords.setdefault(key, end)

        at_terminal = {
            key
            for key, xy in coords.items()
            if any(t.geometry.distance(Point(xy)) <= self.node_tolerance_mm for t in terminals)
        }
        stops = {k for k, v in incident.items() if len(v) != 2} | at_terminal

        chains: list[_Chain] = []
        used: set[int] = set()
        for start in sorted(stops):
            for first in incident[start]:
                if first in used:
                    continue
                ordered: list[_Track] = []
                node, idx = start, first
                while True:
                    used.add(idx)
                    track = tracks[idx]
                    a, b = end_nodes[idx]
                    nxt = b if a == node else a
                    ordered.append(track if a == node else self._reversed(track))
                    if nxt in stops:
                        break
                    candidates = [j for j in incident[nxt] if j != idx]
                    if not candidates or candidates[0] in used:
                        break
                    node, idx = nxt, candidates[0]
                chains.append(_Chain(ordered, start, nxt))
        # Closed loops with no stop are not chains between terminals; skip.
        return chains, at_terminal

    @staticmethod
    def _reversed(track: _Track) -> _Track:
        return _Track(
            segment=track.segment,
            points=list(reversed(track.points)),
            length=track.length,
            width=track.width,
            net_label=track.net_label,
            uuid=track.uuid,
        )

    def _runs(self, chain: _Chain) -> list[_Run]:
        runs: list[_Run] = []
        for track in chain.tracks:
            if runs and abs(runs[-1].width - track.width) <= self.width_tolerance_mm:
                runs[-1].tracks.append(track)
            else:
                runs.append(_Run([track], track.width))
        return runs

    # ------------------------------------------------------------------
    # Findings
    # ------------------------------------------------------------------

    def _audit_chain(
        self,
        chain: _Chain,
        layer: str,
        net_key: str,
        terminal_nodes: set[NodeKey],
        terminals: list[_Terminal],
        obstacle_index: tuple[Any, list[_Obstacle]],
        clearance: float,
        results: DRCResults,
    ) -> None:
        runs = self._runs(chain)
        if len(runs) < 2:
            return
        tol = self.width_tolerance_mm
        island_bounds: set[int] = set()  # boundary i sits between runs i and i+1

        for i in range(1, len(runs) - 1):
            run = runs[i]
            if not (runs[i - 1].width < run.width - tol and runs[i + 1].width < run.width - tol):
                continue
            if run.length >= self.max_island_length_mm:
                continue
            island_bounds.update((i - 1, i))
            if self.report_islands:
                results.add(self._island_violation(run, runs[i - 1], runs[i + 1], layer))

        if not self.report_transitions:
            return
        two_terminal = chain.start in terminal_nodes and chain.end in terminal_nodes
        if not two_terminal:
            return
        # One finding per narrow run: a neck with wider copper on both sides
        # is judged against the wider of its two neighbours.
        necks: dict[int, tuple[float, tuple[float, float]]] = {}
        for i in range(len(runs) - 1):
            if i in island_bounds:
                continue
            left, right = runs[i], runs[i + 1]
            narrow_idx = i if left.width < right.width else i + 1
            wide_width = max(left.width, right.width)
            at = left.tracks[-1].points[-1]
            if narrow_idx not in necks or wide_width > necks[narrow_idx][0]:
                necks[narrow_idx] = (wide_width, at)
        for narrow_idx, (wide_width, at) in sorted(necks.items()):
            finding = self._transition_finding(
                runs[narrow_idx],
                wide_width,
                at,
                layer,
                net_key,
                terminals,
                obstacle_index,
                clearance,
            )
            if finding is not None:
                results.add(finding)

    def _island_violation(self, run: _Run, before: _Run, after: _Run, layer: str) -> DRCViolation:
        from shapely.geometry import LineString

        line = LineString([p for t in run.tracks for p in t.points])
        mid = line.interpolate(0.5, normalized=True)
        net = run.tracks[0].net_label
        return DRCViolation(
            rule_id=self.ISLAND_ID,
            severity=self.severity,
            message=(
                f"Width island on '{net}' ({layer}): {run.width:.3f} mm for "
                f"{run.length:.3f} mm between {before.width:.3f} mm and "
                f"{after.width:.3f} mm copper (< {self.max_island_length_mm:.3f} mm); "
                f"use one width for the run or make the wide section deliberate"
            ),
            location=(round(mid.x, 4), round(mid.y, 4)),
            layer=layer,
            actual_value=round(run.length, 4),
            required_value=self.max_island_length_mm,
            items=tuple(t.uuid for t in run.tracks if t.uuid),
            nets=(net,),
        )

    def _transition_finding(
        self,
        narrow: _Run,
        wide_width: float,
        at: tuple[float, float],
        layer: str,
        net_key: str,
        terminals: list[_Terminal],
        obstacle_index: tuple[Any, list[_Obstacle]],
        clearance: float,
    ) -> DRCViolation | None:
        from shapely.geometry import MultiLineString, Point

        tree, obstacles = obstacle_index
        centerline = MultiLineString([t.points for t in narrow.tracks])
        actual = centerline.buffer(narrow.width / 2.0)
        proposal = centerline.buffer(wide_width / 2.0)
        search = proposal.buffer(clearance + self.obstacle_search_mm)

        nearest: tuple[float, float, str] | None = None  # (gap_after, gap_now, label)
        for idx in tree.query(search):
            obstacle = obstacles[int(idx)]
            if obstacle.net_key == net_key:
                continue
            gap_after = proposal.distance(obstacle.geometry)
            if nearest is None or gap_after < nearest[0]:
                nearest = (gap_after, actual.distance(obstacle.geometry), obstacle.label)

        reason = ""
        if nearest is not None and nearest[0] < clearance - 1e-6:
            reason = f"widening would leave {nearest[0]:.3f} mm to {nearest[2]}"
        else:
            ends = [Point(narrow.tracks[0].points[0]), Point(narrow.tracks[-1].points[-1])]
            for term in terminals:
                if term.min_dimension < wide_width and any(
                    term.geometry.distance(e) <= self.node_tolerance_mm for e in ends
                ):
                    reason = f"pad escape into {term.label} ({term.min_dimension:.3f} mm)"
                    break

        net = narrow.tracks[0].net_label
        head = (
            f"Width transition on '{net}' ({layer}): {narrow.width:.3f} mm run of "
            f"{narrow.length:.3f} mm meets {wide_width:.3f} mm"
        )
        if reason:
            if not self.report_justified:
                return None
            severity = "info"
            message = f"{head}; neck-down justified: {reason}"
        else:
            severity = self.severity
            if nearest is None:
                context = f"no other-net copper within {clearance + self.obstacle_search_mm:.3f} mm"
            else:
                context = (
                    f"nearest other-net copper {nearest[2]} is {nearest[1]:.3f} mm away "
                    f"({nearest[0]:.3f} mm if widened; clearance {clearance:.3f} mm)"
                )
            message = (
                f"{head}; no clearance constraint requires the neck-down -- {context}. "
                f"Consider widening the narrow run to {wide_width:.3f} mm"
            )
        return DRCViolation(
            rule_id=self.TRANSITION_ID,
            severity=severity,
            message=message,
            location=(round(at[0], 4), round(at[1], 4)),
            layer=layer,
            actual_value=round(nearest[1], 4) if nearest is not None else None,
            required_value=clearance,
            items=tuple(t.uuid for t in narrow.tracks if t.uuid),
            nets=(net,),
        )

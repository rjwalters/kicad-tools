"""Net connectivity validation for PCB designs.

This module provides validation to ensure all schematic net connections
are physically routed on the PCB. It detects unrouted segments and
partially connected nets (islands).

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.validate import ConnectivityValidator
    >>>
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> validator = ConnectivityValidator(pcb)
    >>> result = validator.validate()
    >>>
    >>> if result.has_issues:
    ...     for issue in result.issues:
    ...         print(f"{issue.severity}: {issue.message}")
    ...         print(f"  Fix: {issue.suggestion}")
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

# Optional geometry backend.  ``shapely`` is NOT a core dependency (it lives
# only under the ``geometry``/``dev`` extras in pyproject.toml), so we import
# it lazily/guardedly here.  When it is absent the label-free pour extractor
# (``_connect_pour_pads_label_free`` / step 2d) transparently falls back to
# the legacy declared-net pour grouping, so importing this module — and
# tracing autorouter segment/via copper — never hard-fails on a core-only
# install.
_SHAPELY_AVAILABLE = False
try:  # pragma: no cover - import guard exercised by environment, not tests
    from shapely.geometry import Point as _ShapelyPoint  # type: ignore[import-untyped]
    from shapely.geometry import Polygon as _ShapelyPolygon

    _SHAPELY_AVAILABLE = True
except ImportError:  # pragma: no cover
    pass


def _has_shapely() -> bool:
    """Return True when the optional ``shapely`` backend is importable."""
    return _SHAPELY_AVAILABLE


@dataclass(frozen=True)
class ConnectivityIssue:
    """Represents a single net connectivity issue.

    Attributes:
        severity: Either "error" or "warning"
        issue_type: Type of issue (unrouted, partial, isolated)
        net_name: Name of the affected net
        message: Human-readable description of the issue
        suggestion: Actionable fix suggestion
        connected_pads: List of connected pads (e.g., ["U1.3", "C1.1"])
        unconnected_pads: List of unconnected pads
        islands: Groups of connected pads (for partial connections)
    """

    severity: str
    issue_type: str
    net_name: str
    message: str
    suggestion: str
    connected_pads: tuple[str, ...] = ()
    unconnected_pads: tuple[str, ...] = ()
    islands: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        """Validate severity and issue_type values."""
        if self.severity not in ("error", "warning"):
            raise ValueError(f"severity must be 'error' or 'warning', got {self.severity!r}")
        valid_types = ("unrouted", "partial", "isolated")
        if self.issue_type not in valid_types:
            raise ValueError(f"issue_type must be one of {valid_types}, got {self.issue_type!r}")

    @property
    def is_error(self) -> bool:
        """Check if this is an error (not a warning)."""
        return self.severity == "error"

    @property
    def is_warning(self) -> bool:
        """Check if this is a warning (not an error)."""
        return self.severity == "warning"

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "severity": self.severity,
            "issue_type": self.issue_type,
            "net_name": self.net_name,
            "message": self.message,
            "suggestion": self.suggestion,
            "connected_pads": list(self.connected_pads),
            "unconnected_pads": list(self.unconnected_pads),
            "islands": [list(island) for island in self.islands],
        }


@dataclass
class ConnectivityResult:
    """Aggregates all net connectivity issues.

    Provides convenient access to issue counts and filtering.

    Attributes:
        issues: List of all connectivity issues found
        total_nets: Total number of nets analyzed
        connected_nets: Number of fully connected nets
    """

    issues: list[ConnectivityIssue] = field(default_factory=list)
    total_nets: int = 0
    connected_nets: int = 0
    zone_connected_nets: int = 0

    @property
    def has_issues(self) -> bool:
        """True if any issues were found."""
        return len(self.issues) > 0

    @property
    def is_fully_routed(self) -> bool:
        """True if no errors (warnings are allowed)."""
        return self.error_count == 0

    @property
    def error_count(self) -> int:
        """Count of issues with severity='error'."""
        return sum(1 for i in self.issues if i.is_error)

    @property
    def warning_count(self) -> int:
        """Count of issues with severity='warning'."""
        return sum(1 for i in self.issues if i.is_warning)

    @property
    def errors(self) -> list[ConnectivityIssue]:
        """List of only error issues."""
        return [i for i in self.issues if i.is_error]

    @property
    def warnings(self) -> list[ConnectivityIssue]:
        """List of only warning issues."""
        return [i for i in self.issues if i.is_warning]

    @property
    def unrouted(self) -> list[ConnectivityIssue]:
        """Issues with completely unrouted segments."""
        return [i for i in self.issues if i.issue_type == "unrouted"]

    @property
    def partial(self) -> list[ConnectivityIssue]:
        """Issues with partially connected nets (islands)."""
        return [i for i in self.issues if i.issue_type == "partial"]

    @property
    def isolated(self) -> list[ConnectivityIssue]:
        """Issues with isolated pads."""
        return [i for i in self.issues if i.issue_type == "isolated"]

    @property
    def unconnected_pad_count(self) -> int:
        """Total number of unconnected pads."""
        return sum(len(i.unconnected_pads) for i in self.issues)

    def __iter__(self):
        """Iterate over all issues."""
        return iter(self.issues)

    def __len__(self) -> int:
        """Total number of issues."""
        return len(self.issues)

    def __bool__(self) -> bool:
        """True if there are any issues."""
        return len(self.issues) > 0

    def add(self, issue: ConnectivityIssue) -> None:
        """Add an issue to the results."""
        self.issues.append(issue)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "is_fully_routed": self.is_fully_routed,
            "total_nets": self.total_nets,
            "connected_nets": self.connected_nets,
            "zone_connected_nets": self.zone_connected_nets,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "unconnected_pads": self.unconnected_pad_count,
            "issues": [i.to_dict() for i in self.issues],
        }

    def summary(self) -> str:
        """Generate a human-readable summary."""
        status = "FULLY ROUTED" if self.is_fully_routed else "CONNECTIVITY ISSUES"
        parts = [
            f"Net Connectivity {status}: {self.error_count} errors, {self.warning_count} warnings"
        ]
        parts.append(f"  Nets: {self.connected_nets}/{self.total_nets} fully connected")
        if self.zone_connected_nets > 0:
            parts.append(
                f"  Zone-connected nets: {self.zone_connected_nets} (verified by geometry)"
            )

        if self.unrouted:
            parts.append(f"  Unrouted nets: {len(self.unrouted)}")
        if self.partial:
            parts.append(f"  Partial connections: {len(self.partial)}")
        if self.isolated:
            parts.append(f"  Isolated pads: {len(self.isolated)}")
        parts.append(f"  Total unconnected pads: {self.unconnected_pad_count}")

        return "\n".join(parts)


class ConnectivityValidator:
    """Validates net connectivity on PCB.

    Checks for:
    - Completely unrouted net segments
    - Partially connected nets (islands)
    - Isolated pads

    Example:
        >>> from kicad_tools.schema.pcb import PCB
        >>> from kicad_tools.validate import ConnectivityValidator
        >>>
        >>> pcb = PCB.load("board.kicad_pcb")
        >>> validator = ConnectivityValidator(pcb)
        >>> result = validator.validate()
        >>>
        >>> if not result.is_fully_routed:
        ...     for issue in result.errors:
        ...         print(f"{issue.net_name}: {issue.message}")

    Attributes:
        pcb: Loaded PCB object
    """

    # Tolerance for matching point positions (in mm).
    # A tolerance of 0.01 mm (10 um) absorbs floating-point coordinate
    # drift that accumulates during trace optimisation (ratio-based
    # shortening, chamfer insertion, etc.) while remaining well below
    # the smallest real-world pad-to-pad distances (~0.1 mm for 01005).
    POSITION_TOLERANCE = 0.01

    def __init__(self, pcb: str | Path | PCB) -> None:
        """Initialize the validator.

        Args:
            pcb: Path to PCB file or PCB object
        """
        from kicad_tools.schema.pcb import PCB as PCBClass

        if isinstance(pcb, (str, Path)):
            self.pcb = PCBClass.load(str(pcb))
        else:
            self.pcb = pcb

    def validate(self) -> ConnectivityResult:
        """Run connectivity validation on all nets.

        Returns:
            ConnectivityResult containing all issues found
        """
        result = ConnectivityResult()

        # Get all non-empty nets (skip net 0 which is unconnected)
        nets = {n: net for n, net in self.pcb.nets.items() if n != 0 and net.name}

        result.total_nets = len(nets)
        connected_count = 0
        zone_connected_count = 0

        # Determine whether the board has footprints.  If it does but a
        # named net has zero pads, the net assignments may have been
        # corrupted (all pads zeroed to net 0).  In that case the net
        # should NOT be counted as connected.
        has_footprints = len(self.pcb.footprints) > 0

        for net_number, net in nets.items():
            # Get all pads on this net
            pads = self._get_net_pads(net_number)

            if len(pads) == 0 and has_footprints:
                # A named net with no pads on a board that has footprints
                # is suspicious -- pad net assignments may have been
                # stripped.  Do NOT count as connected.
                continue

            if len(pads) < 2:
                # Single-pad nets are always "connected"
                connected_count += 1
                continue

            # Reset per-net zone tracking
            self._last_zone_connected_pads: set[str] = set()

            # Build connectivity graph from copper (segments, vias, zones)
            graph = self._build_connectivity_graph(net_number)

            # Check if all pads are connected
            islands = self._find_islands(graph, pads)

            if len(islands) <= 1:
                connected_count += 1
                # Track whether this net was connected via zone geometry
                if self._last_zone_connected_pads:
                    zone_connected_count += 1
                continue

            # Create issue for this net
            issue = self._create_issue(net.name, pads, islands)
            result.add(issue)

        result.connected_nets = connected_count
        result.zone_connected_nets = zone_connected_count
        return result

    def extract_pad_partition(self) -> list[frozenset[str]]:
        """Extract the *physical* pad partition from routed copper.

        This is the independent-LVS primitive (issue #3742): it floods the
        routed copper graph and returns the set of galvanically connected
        pad groups.

        **Autorouter copper (track segments + vias) is traced with zero
        reference to pad net labels** — two pads land in the same group iff
        physical copper connects them, regardless of what net the router
        *claims*.  This is the load-bearing soundness property: it catches a
        router that wires segments to the wrong pads while labeling them
        correctly (the board-00 rotation-convention bug, #3739).

        Zone pours are now traced label-free as well (issue #3761, see the
        ``2d`` block below): each ``filled_polygon`` is a poured copper island
        and a pad is tied to it iff the pad's copper geometrically overlaps
        the island's *hole-aware solid* region on a matching layer — clearance
        moats / thermal antipads carved out of the pour are excluded.  No
        pad/zone ``net_name`` is consulted, so a pad bonded to the wrong pour
        is no longer masked by a matching declared label.  This requires the
        optional ``shapely`` backend; when it is absent the pour leg falls
        back to the legacy declared-net grouping so core-only installs still
        import and run.

        Why this matters: the label-based comparator
        (:func:`kicad_tools.lvs.board_lvs.compare_netlists`) trusts the
        ``(net K "NAME")`` child the router writes onto each pad, so a router
        that mislabels its own copper passes.  This extractor never reads a
        net label — it derives connectivity purely from copper geometry — so
        the partition it returns reflects what manufacturing will actually
        see.  Diffing it against the schematic partition catches shorts
        (different schematic nets fused by copper) and opens (same schematic
        net split across copper islands) that the label-based path cannot.

        Note on coordinate convention: pad geometry still flows through
        :meth:`_transform_pad_position` / ``rotate_pad_offset``.  The gate's
        *correctness claim* does not rest on that transform being right —
        that is what the 90°/270° decoupling test asserts independently
        against kicad-cli (or a committed golden).  What this method
        guarantees is that the partition ignores *labels*, which is the
        load-bearing soundness property.

        Returns:
            A list of ``frozenset`` pad-id groups (``"REF.PAD"`` form, e.g.
            ``"U1.3"``).  Every footprint pad with a numeric pad number and a
            non-comment reference appears in exactly one group.  A pad with
            no copper touching it forms a singleton group.  Groups are
            returned sorted by their smallest member for determinism.
        """
        # 1. Collect every pad on the board (label-independent) with its
        #    board-frame position and layer set.
        pad_positions: dict[str, tuple[float, float]] = {}
        pad_layers: dict[str, list[str]] = {}
        for fp in self.pcb.footprints:
            if not fp.reference or fp.reference.startswith("#"):
                continue
            fp_x, fp_y = fp.position
            rotation = fp.rotation
            for pad in fp.pads:
                if pad.number is None or pad.number == "":
                    continue
                pad_id = f"{fp.reference}.{pad.number}"
                pad_positions[pad_id] = self._transform_pad_position(
                    pad.position, fp_x, fp_y, rotation
                )
                pad_layers[pad_id] = pad.layers

        # 2. Build a copper adjacency graph over pads, ignoring net labels.
        #    Every pad starts as its own node; copper fuses them.
        graph: dict[str, set[str]] = {pad_id: set() for pad_id in pad_positions}

        def _connect(a: str, b: str) -> None:
            if a != b:
                graph[a].add(b)
                graph[b].add(a)

        # 2a. Track segments: union the pads at each endpoint, and union
        #     pads sharing an endpoint.  Crucially we walk *all* segments
        #     (``self.pcb.segments``), not segments filtered by a net number,
        #     so a mislabeled segment still physically connects whatever it
        #     touches.
        segments = list(self.pcb.segments)
        for seg in segments:
            start_pads = self._find_pads_at_point(seg.start, pad_positions)
            end_pads = self._find_pads_at_point(seg.end, pad_positions)
            for sp in start_pads:
                for ep in end_pads:
                    _connect(sp, ep)
            for group in (start_pads, end_pads):
                for i, p in enumerate(group):
                    for other in group[i + 1 :]:
                        _connect(p, other)

        # 2b. Segment chains: pads connected through a chain of segments that
        #     share endpoints are galvanically connected even with no pad at
        #     the intermediate junctions.  Reuse the existing chain builder,
        #     which is itself label-agnostic (it only looks at endpoints).
        graph = self._build_segment_chains(segments, pad_positions, graph)

        # 2c. Vias: pads coincident with a via are connected (layer bridge).
        for via in self.pcb.vias:
            via_pads = self._find_pads_at_point(via.position, pad_positions)
            for i, p in enumerate(via_pads):
                for other in via_pads[i + 1 :]:
                    _connect(p, other)

        # 2d. Filled zones (copper pours) — LABEL-FREE (issue #3761).
        #
        #     SCOPE NOTE: the load-bearing soundness property of this
        #     extractor is that copper connectivity is derived from *physical
        #     geometry*, never from pad/zone net labels.  Steps 2a–2c trace
        #     autorouter copper (segments + vias) independently of labels;
        #     this step does the same for pours.
        #
        #     The previous model (the #3742 first slice) grouped pads by the
        #     zone's *declared* net (``pad_declared_net[pad] == zone.net_name``).
        #     That re-introduced a label dependency for the fill step and could
        #     MASK a real defect on pour-routed nets: a pad whose copper is
        #     physically bonded to the *wrong* pour island, but whose declared
        #     net matches a different (correct) pour, was partitioned by its
        #     label rather than by metal (issue #3761).
        #
        #     We now tie pads to pours purely geometrically.  Each
        #     ``filled_polygon`` is one poured copper *island*.  A pad is
        #     bonded to an island iff its copper geometry overlaps the
        #     island's *solid* region on a matching copper layer — clearance
        #     moats / thermal antipads carved out of the pour are real holes
        #     that the pad must NOT be tied across.
        #
        #     KiCad encodes a fill island's holes inside a single flat
        #     ``(pts ...)`` list: the outer hull and each carved-out loop are
        #     joined by a narrow bridge, so the boundary dips *around* every
        #     moat.  A ray-cast against that raw list mis-counts the bridge
        #     crossings and reports a moated-out pad as "inside" (the exact
        #     failure ``_point_in_polygon`` exhibits, verified on board 00).
        #     ``shapely`` resolves the bridged representation correctly:
        #     ``Polygon(pts).buffer(0)`` yields the true solid region with the
        #     moats excluded, so a hole-aware ``contains`` test is sound.
        #
        #     Pad-shape approximation: we test the pad's *size box* (board
        #     frame, footprint-rotated — see ``_pad_copper_polygon``) against
        #     the hole-aware solid region, not just the pad center.  The box
        #     is required, not a nicety: a thermally-relieved pad's center
        #     sits in the antipad moat (a hole), yet its copper edge reaches
        #     the thermal spokes / surrounding solid pour, so only the box
        #     intersects the solid region.  A pad fully moated out (clearance
        #     all around, no spoke) stays clear of the solid region and is
        #     correctly left untied.  Corner rounding (roundrect/oval) and
        #     per-pad rotation are ignored; an exact pad outline is a
        #     documented follow-up.
        #
        #     ``shapely`` is an optional dependency; when it is absent we fall
        #     back to the legacy declared-net pour grouping so core-only
        #     installs keep working (the soundness upgrade simply requires the
        #     ``geometry``/``dev`` extra to be installed).
        if _has_shapely():
            self._connect_pour_pads_label_free(pad_positions, pad_layers, _connect)
        else:  # pragma: no cover - exercised only on core-only installs
            self._connect_pour_pads_by_declared_net(pad_positions, pad_layers, _connect)

        # 2e. Coincident pads with no intervening copper still share metal
        #     if they occupy the same point (e.g. stacked pads).
        pad_ids = list(pad_positions)
        for i, p in enumerate(pad_ids):
            for other in pad_ids[i + 1 :]:
                if self._points_close(pad_positions[p], pad_positions[other]):
                    _connect(p, other)

        # 3. Flood-fill connected components of the pad graph.
        visited: set[str] = set()
        partition: list[frozenset[str]] = []
        for pad_id in pad_positions:
            if pad_id in visited:
                continue
            component: set[str] = set()
            queue = [pad_id]
            while queue:
                current = queue.pop()
                if current in visited:
                    continue
                visited.add(current)
                component.add(current)
                queue.extend(graph[current] - visited)
            partition.append(frozenset(component))

        partition.sort(key=lambda comp: min(comp))
        return partition

    def _get_net_pads(self, net_number: int) -> list[str]:
        """Get all pads on a specific net.

        Args:
            net_number: Net number to find pads for

        Returns:
            List of pad identifiers in format "REF.PAD" (e.g., "U1.3")
        """
        pads = []
        for fp in self.pcb.footprints:
            if not fp.reference or fp.reference.startswith("#"):
                continue
            for pad in fp.pads:
                if pad.net_number == net_number:
                    pads.append(f"{fp.reference}.{pad.number}")
        return sorted(pads)

    def _build_connectivity_graph(
        self,
        net_number: int,
    ) -> dict[str, set[str]]:
        """Build graph of copper connectivity for a net.

        Creates a graph where nodes are points (pad positions, track endpoints,
        via positions) and edges connect points that are electrically connected.

        Zone boundary polygon containment is used to detect pads connected
        through copper pours: if a pad position falls geometrically inside a
        zone boundary polygon on a matching copper layer, the pad is treated
        as electrically connected to every other pad within the same zone.
        This heuristic only applies to zones with at least one filled
        polygon — a zone that produced no filled copper (fill disabled, or
        fill enabled but fully shadowed/carved away) provides no
        connectivity (Issue #3514, mirroring Issue #3482).

        Args:
            net_number: Net number to analyze

        Returns:
            Adjacency list mapping point IDs to connected point IDs
        """
        graph: dict[str, set[str]] = defaultdict(set)

        # Get all pad positions and layer info for this net
        pad_positions: dict[str, tuple[float, float]] = {}
        pad_layers: dict[str, list[str]] = {}
        for fp in self.pcb.footprints:
            if not fp.reference or fp.reference.startswith("#"):
                continue
            # Get footprint position and rotation for pad position calculation
            fp_x, fp_y = fp.position
            rotation = fp.rotation

            for pad in fp.pads:
                if pad.net_number == net_number:
                    pad_id = f"{fp.reference}.{pad.number}"
                    # Transform pad position from footprint-local to board coordinates
                    pad_x, pad_y = self._transform_pad_position(pad.position, fp_x, fp_y, rotation)
                    pad_positions[pad_id] = (pad_x, pad_y)
                    pad_layers[pad_id] = pad.layers

        # Get all track segment endpoints for this net
        segments = list(self.pcb.segments_in_net(net_number))
        segment_points: list[tuple[float, float]] = []
        for seg in segments:
            segment_points.append(seg.start)
            segment_points.append(seg.end)

        # Get all via positions for this net
        vias = list(self.pcb.vias_in_net(net_number))
        via_positions = [via.position for via in vias]

        # Check zones for filled polygons on this net
        zone_points: list[tuple[float, float]] = []
        for zone in self.pcb.zones:
            if zone.net_number == net_number and zone.filled_polygons:
                # Sample points from filled polygons
                for poly in zone.filled_polygons:
                    zone_points.extend(poly)

        # All copper points
        all_copper_points = segment_points + via_positions + zone_points

        # Connect pads that are at the same location as copper
        for pad_id, pad_pos in pad_positions.items():
            for copper_pos in all_copper_points:
                if self._points_close(pad_pos, copper_pos):
                    # Find other pads at this copper point
                    for other_id, other_pos in pad_positions.items():
                        if other_id != pad_id and self._points_close(pad_pos, other_pos):
                            graph[pad_id].add(other_id)
                            graph[other_id].add(pad_id)

        # Connect pads through track segments
        for seg in segments:
            # Find pads at segment endpoints
            start_pads = self._find_pads_at_point(seg.start, pad_positions)
            end_pads = self._find_pads_at_point(seg.end, pad_positions)

            # Connect pads at start to pads at end
            for start_pad in start_pads:
                for end_pad in end_pads:
                    if start_pad != end_pad:
                        graph[start_pad].add(end_pad)
                        graph[end_pad].add(start_pad)

            # Also connect pads at each endpoint to themselves (for via chains)
            for pad in start_pads:
                for other in start_pads:
                    if pad != other:
                        graph[pad].add(other)
                        graph[other].add(pad)

            for pad in end_pads:
                for other in end_pads:
                    if pad != other:
                        graph[pad].add(other)
                        graph[other].add(pad)

        # Connect pads through via chains
        for via in vias:
            via_pads = self._find_pads_at_point(via.position, pad_positions)
            for pad in via_pads:
                for other in via_pads:
                    if pad != other:
                        graph[pad].add(other)
                        graph[other].add(pad)

        # Build full transitive closure through segment chains
        # Track endpoints can form chains connecting distant pads
        graph = self._build_segment_chains(segments, pad_positions, graph)

        # --- Zone boundary polygon containment checks ---
        # For each zone on this net, check if pads fall inside the zone
        # boundary polygon on a matching copper layer.  Pads within the
        # same zone are electrically connected via the copper pour.
        #
        # IMPORTANT (Issue #3514, mirroring the Issue #3482 fix in
        # NetStatusAnalyzer): the boundary polygon only implies connectivity
        # when the zone actually produced filled copper. A zone with fill
        # enabled but zero filled polygons (e.g. fully shadowed by a
        # higher-priority zone, or carved away entirely by clearances) — or a
        # boundary-only zone with fill disabled — contributes NO copper on
        # the manufactured board, so its boundary must not mark pads/vias as
        # connected. The boundary heuristic exists solely for thermal-relief
        # cutouts INSIDE filled copper, which presupposes the zone has at
        # least one filled polygon.
        zone_connected_pads: set[str] = set()
        for zone in self.pcb.zones:
            if zone.net_number != net_number:
                continue
            # Zero-fill zones provide no electrical connectivity at all
            # (Issue #3514): skip the boundary-containment heuristic.
            if not zone.filled_polygons:
                continue
            if not zone.polygon or len(zone.polygon) < 3:
                continue

            # Find all pads inside this zone boundary on a matching layer
            pads_in_zone: list[str] = []
            for pad_id, pad_pos in pad_positions.items():
                layers = pad_layers.get(pad_id, [])
                if not self._pad_layer_matches_zone(layers, zone.layer):
                    continue
                if self._point_in_polygon(pad_pos, zone.polygon):
                    pads_in_zone.append(pad_id)
                    zone_connected_pads.add(pad_id)

            # Also check vias inside zone boundary -- vias bridge layers,
            # so pads reachable through a via inside a zone are connected.
            for via in vias:
                if hasattr(via, "layers") and zone.layer in via.layers:
                    if self._point_in_polygon(via.position, zone.polygon):
                        # Find pads at via position on any layer
                        via_pads = self._find_pads_at_point(via.position, pad_positions)
                        pads_in_zone.extend(via_pads)
                        zone_connected_pads.update(via_pads)

            # Connect all pads in this zone to each other
            for i, pad in enumerate(pads_in_zone):
                for other in pads_in_zone[i + 1 :]:
                    if pad != other:
                        graph[pad].add(other)
                        graph[other].add(pad)

        # Store zone-connected pad set for reporting
        self._last_zone_connected_pads = zone_connected_pads

        return graph

    def _build_segment_chains(
        self,
        segments: list,
        pad_positions: dict[str, tuple[float, float]],
        graph: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        """Build connectivity through chains of connected segments.

        Segments that share endpoints form chains. Pads at any point
        in a chain are connected to all other pads in the chain.
        """
        if not segments:
            return graph

        # Build segment adjacency graph
        segment_graph: dict[int, set[int]] = defaultdict(set)
        for i, seg_a in enumerate(segments):
            for j, seg_b in enumerate(segments):
                if i != j:
                    # Check if segments share an endpoint
                    if (
                        self._points_close(seg_a.start, seg_b.start)
                        or self._points_close(seg_a.start, seg_b.end)
                        or self._points_close(seg_a.end, seg_b.start)
                        or self._points_close(seg_a.end, seg_b.end)
                    ):
                        segment_graph[i].add(j)
                        segment_graph[j].add(i)

        # Find connected components of segments
        visited: set[int] = set()
        components: list[set[int]] = []

        for i in range(len(segments)):
            if i in visited:
                continue
            component: set[int] = set()
            queue = [i]
            while queue:
                seg_idx = queue.pop()
                if seg_idx in visited:
                    continue
                visited.add(seg_idx)
                component.add(seg_idx)
                queue.extend(segment_graph[seg_idx] - visited)
            components.append(component)

        # For each component, find all pads and connect them
        for component in components:
            component_pads: set[str] = set()
            for seg_idx in component:
                seg = segments[seg_idx]
                component_pads.update(self._find_pads_at_point(seg.start, pad_positions))
                component_pads.update(self._find_pads_at_point(seg.end, pad_positions))

            # Connect all pads in this component
            pad_list = list(component_pads)
            for i, pad in enumerate(pad_list):
                for other in pad_list[i + 1 :]:
                    graph[pad].add(other)
                    graph[other].add(pad)

        return graph

    def _transform_pad_position(
        self,
        pad_local: tuple[float, float],
        fp_x: float,
        fp_y: float,
        rotation: float,
    ) -> tuple[float, float]:
        """Transform pad position from footprint-local to board coordinates.

        Args:
            pad_local: Pad position in footprint-local coordinates
            fp_x: Footprint X position
            fp_y: Footprint Y position
            rotation: Footprint rotation in degrees

        Returns:
            Pad position in board coordinates
        """
        from kicad_tools.core.geometry import rotate_pad_offset

        # Rotate pad position using KiCad's negated-angle convention
        # (see kicad_tools.core.geometry.rotate_pad_offset).
        px, py = pad_local
        rotated_x, rotated_y = rotate_pad_offset(px, py, rotation)

        # Translate to board coordinates
        board_x = fp_x + rotated_x
        board_y = fp_y + rotated_y

        return (board_x, board_y)

    def _find_pads_at_point(
        self,
        point: tuple[float, float],
        pad_positions: dict[str, tuple[float, float]],
    ) -> list[str]:
        """Find all pads at a given point.

        Args:
            point: Point to check
            pad_positions: Mapping of pad IDs to positions

        Returns:
            List of pad IDs at this point
        """
        return [
            pad_id
            for pad_id, pad_pos in pad_positions.items()
            if self._points_close(point, pad_pos)
        ]

    def _points_close(
        self,
        p1: tuple[float, float],
        p2: tuple[float, float],
    ) -> bool:
        """Check if two points are within tolerance distance."""
        dx = p1[0] - p2[0]
        dy = p1[1] - p2[1]
        return (dx * dx + dy * dy) < (self.POSITION_TOLERANCE * self.POSITION_TOLERANCE)

    @staticmethod
    def _point_in_polygon(
        point: tuple[float, float],
        polygon: list[tuple[float, float]],
    ) -> bool:
        """Test if point is inside polygon using ray casting algorithm.

        Args:
            point: (x, y) coordinates to test
            polygon: List of (x, y) vertices defining the polygon boundary

        Returns:
            True if point is inside polygon
        """
        n = len(polygon)
        if n < 3:
            return False

        x, y = point
        inside = False
        j = n - 1

        for i in range(n):
            xi, yi = polygon[i]
            xj, yj = polygon[j]

            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i

        return inside

    @staticmethod
    def _pad_layer_matches_zone(
        pad_layers: list[str],
        zone_layer: str,
    ) -> bool:
        """Check if a pad exists on the same copper layer as a zone.

        Handles wildcard layers like ``*.Cu`` which match any copper layer,
        allowing through-hole pads to match zones on any copper layer.

        Args:
            pad_layers: List of layers the pad exists on
            zone_layer: Layer the zone is on (e.g., ``F.Cu``, ``B.Cu``)

        Returns:
            True if the pad and zone share a copper layer
        """
        for pad_layer in pad_layers:
            if pad_layer == zone_layer:
                return True
            # Wildcard match: "*.Cu" matches any copper layer
            if pad_layer == "*.Cu" and zone_layer.endswith(".Cu"):
                return True
            # General wildcard: "*.Mask" etc.
            if pad_layer.startswith("*.") and zone_layer.endswith(pad_layer[1:]):
                return True
        return False

    # Erosion (mm) applied to a pad's copper box before the pour-overlap
    # test.  Sized just above the corner-graze scale and below a real thermal
    # spoke's penetration so that an oversized through-hole pad poking a
    # corner across a narrow clearance moat into a foreign pour does NOT bond,
    # while a genuine spoke/solid tie (which reaches well past the clearance
    # line) keeps a non-empty eroded overlap.  Verified on boards 00/03/05.
    POUR_PAD_ERODE: float = 0.1

    def _pad_copper_polygon(self, fp: Any, pad: Any) -> Any | None:
        """Build a board-frame shapely polygon approximating a pad's copper.

        The pad's ``size`` box is rotated by the footprint rotation (KiCad's
        negated-angle convention, matching :meth:`_transform_pad_position`)
        and translated to the board frame, then eroded inward by
        :data:`POUR_PAD_ERODE`.  This rectangular approximation is
        deliberately coarse — it ignores ``roundrect``/``oval`` corner
        rounding and per-pad rotation — but it is what lets the pour test see
        a *thermally-relieved* pad: such a pad's center sits in the antipad
        moat (a hole in the fill), yet its copper edge reaches the thermal
        spokes / surrounding solid pour, so the pad *polygon* intersects the
        solid region while the bare center point does not.  The inward erosion
        keeps an oversized pad's corner from grazing across a clearance moat
        into a foreign pour (which would manufacture a false short).  A pad
        fully moated out (clearance all around, no spoke) stays clear of the
        solid region and is correctly left untied.

        Returns ``None`` when shapely is unavailable.  Falls back to a
        zero-area point geometry when the pad has no positive size.
        """
        if not _has_shapely():
            return None
        import math

        cx, cy = self._transform_pad_position(
            pad.position, fp.position[0], fp.position[1], fp.rotation
        )
        w, h = pad.size
        if w <= 0 or h <= 0:
            return _ShapelyPoint((cx, cy))
        # Negated-angle convention (see rotate_pad_offset): the footprint
        # rotation maps the pad's local box into the board frame.
        a = math.radians(-fp.rotation)
        cos_a, sin_a = math.cos(a), math.sin(a)
        corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        pts = [(cx + ox * cos_a - oy * sin_a, cy + ox * sin_a + oy * cos_a) for ox, oy in corners]
        box = _ShapelyPolygon(pts)
        if self.POUR_PAD_ERODE > 0:
            eroded = box.buffer(-self.POUR_PAD_ERODE)
            # Erosion can empty a very small pad; keep the original box so the
            # pad is still testable rather than silently dropped.
            if not eroded.is_empty:
                return eroded
        return box

    @staticmethod
    def _fill_solid_region(points: list[tuple[float, float]]) -> Any | None:
        """Build a hole-aware shapely solid region from a fill point list.

        ``filled_polygons`` stores each poured island as a single flat
        ``(pts ...)`` list in which the outer hull and every carved-out
        clearance moat / thermal antipad are joined by narrow bridges (the
        boundary dips *around* each hole).  Constructing ``Polygon(points)``
        directly and then calling ``buffer(0)`` resolves that bridged
        representation into the true solid region with the moats excluded as
        real holes — exactly what a label-free "is this pad bonded to the
        pour?" test needs.

        Returns ``None`` when shapely is unavailable or the fill is
        degenerate (fewer than 3 points / empty geometry).
        """
        if not _has_shapely() or len(points) < 3:
            return None
        poly = _ShapelyPolygon(points)
        # buffer(0) is the canonical shapely idiom for repairing a
        # self-touching ring into a valid (multi)polygon with proper holes.
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            return None
        return poly

    def _connect_pour_pads_label_free(
        self,
        pad_positions: dict[str, tuple[float, float]],
        pad_layers: dict[str, list[str]],
        connect: Any,
    ) -> None:
        """Union pads bonded to the same poured copper island (label-free).

        For every ``filled_polygon`` of every zone, build the hole-aware
        solid region (:meth:`_fill_solid_region`) and collect all pads whose
        *copper geometry* overlaps that region on a matching copper layer.  No
        pad/zone ``net_name`` is consulted — pads are tied solely by shared
        metal, so a pad moated out of a pour is left isolated and a
        foreign-net pad whose copper bonds to the pour is fused into it.

        Pads are approximated by their size box (:meth:`_pad_copper_polygon`)
        rather than a bare center point: a thermally-relieved pad's center
        sits in the antipad hole, but its copper edge reaches the thermal
        spokes / solid pour, so the box intersects the solid region while the
        center alone would (wrongly) read as moated-out.

        Bonded pads are accumulated **per ``zone`` object across all of its
        ``filled_polygon`` indices**, then unioned once per zone — not per
        fill index.  KiCad stores a single poured zone as multiple
        ``filled_polygon`` entries when thermal reliefs / clearance moats
        fragment the copper (e.g. board 03's GND F.Cu zone is one main pour
        plus a dozen tiny per-pad fragments).  All fragments of one ``zone``
        are the same net by KiCad's data model — DRC guarantees retained
        fragments are electrically bonded — so unioning across a zone's fill
        islands cannot fuse two different nets (those are different ``zone``
        objects).  Without this, a pad alone in its own thermal-relief
        fragment would land in a singleton component and be reported as a
        false ``open``.
        """
        # Board-frame pad copper polygons, keyed by pad id.  Built once here
        # so each fill island can be tested against every candidate pad.
        pad_polygons: dict[str, Any] = {}
        for fp in self.pcb.footprints:
            if not fp.reference or fp.reference.startswith("#"):
                continue
            for pad in fp.pads:
                if pad.number is None or pad.number == "":
                    continue
                poly = self._pad_copper_polygon(fp, pad)
                if poly is not None:
                    pad_polygons[f"{fp.reference}.{pad.number}"] = poly

        for zone in self.pcb.zones:
            if not zone.filled_polygons:
                continue
            # Accumulate the bonded-pad set across ALL fill islands of this
            # zone (see method docstring): the zone is one net, and KiCad may
            # fragment its pour into many ``filled_polygon`` entries.  Order is
            # preserved and duplicates are dropped before unioning so a pad
            # bonded to several fragments is unioned exactly once.
            zone_bonded: list[str] = []
            for i, fill_pts in enumerate(zone.filled_polygons):
                region = self._fill_solid_region(fill_pts)
                if region is None:
                    continue
                fill_layer = zone.filled_polygon_layer(i)
                for pad_id in pad_positions:
                    if not self._pad_layer_matches_zone(pad_layers.get(pad_id, []), fill_layer):
                        continue
                    pad_geom = pad_polygons.get(pad_id)
                    if pad_geom is None:
                        continue
                    # ``intersects`` bonds a pad whose copper touches or
                    # overlaps the solid pour; a pad sitting in a carved moat
                    # (a hole), clear of every thermal spoke, does not
                    # intersect and is correctly excluded.  The pad box is
                    # eroded by ``_POUR_PAD_ERODE`` first so that a corner of
                    # an oversized through-hole pad merely *grazing* across a
                    # narrow clearance moat into a foreign pour does not count
                    # as a bond — only copper that penetrates past the
                    # clearance line (a real thermal spoke / solid tie) keeps
                    # a non-empty eroded overlap.  Verified on board 05: this
                    # removes all spurious multi-net bridges while preserving
                    # board 00's genuine thermal-relief ties.
                    if region.intersects(pad_geom):
                        zone_bonded.append(pad_id)
            # De-duplicate preserving first-seen order, then union the whole
            # zone's bonded set so pads on disjoint fill fragments of one zone
            # share a single component.
            seen: set[str] = set()
            bonded: list[str] = []
            for pad_id in zone_bonded:
                if pad_id not in seen:
                    seen.add(pad_id)
                    bonded.append(pad_id)
            for a, p in enumerate(bonded):
                for other in bonded[a + 1 :]:
                    connect(p, other)

    def _connect_pour_pads_by_declared_net(
        self,
        pad_positions: dict[str, tuple[float, float]],
        pad_layers: dict[str, list[str]],
        connect: Any,
    ) -> None:
        """Legacy declared-net pour grouping (shapely-absent fallback).

        Preserves the pre-#3761 behavior for core-only installs where the
        optional ``shapely`` backend is missing: pads enclosed by a zone's
        boundary on a matching layer are tied to the pour iff their declared
        net equals the zone's.  This re-introduces a label dependency for the
        fill step alone and cannot fabricate a false short (it only fuses
        same-declared-net pads), but it can mask one — hence the geometric
        path above is preferred whenever shapely is installed.
        """
        pad_declared_net: dict[str, str] = {}
        for fp in self.pcb.footprints:
            if not fp.reference or fp.reference.startswith("#"):
                continue
            for pad in fp.pads:
                if pad.number is None or pad.number == "":
                    continue
                pad_declared_net[f"{fp.reference}.{pad.number}"] = pad.net_name

        for zone in self.pcb.zones:
            if not zone.filled_polygons:
                continue
            if not zone.polygon or len(zone.polygon) < 3:
                continue
            if not zone.net_name:
                continue
            pads_in_zone: list[str] = []
            for pad_id, pad_pos in pad_positions.items():
                if pad_declared_net.get(pad_id) != zone.net_name:
                    continue
                if not self._pad_layer_matches_zone(pad_layers.get(pad_id, []), zone.layer):
                    continue
                if self._point_in_polygon(pad_pos, zone.polygon):
                    pads_in_zone.append(pad_id)
            for a, p in enumerate(pads_in_zone):
                for other in pads_in_zone[a + 1 :]:
                    connect(p, other)

    def _find_islands(
        self,
        graph: dict[str, set[str]],
        pads: list[str],
    ) -> list[list[str]]:
        """Find disconnected islands in connectivity graph.

        Uses BFS to find connected components among the given pads.

        Args:
            graph: Adjacency list of pad connectivity
            pads: List of pad IDs to check

        Returns:
            List of islands, each island is a list of connected pads
        """
        visited: set[str] = set()
        islands: list[list[str]] = []

        for pad in pads:
            if pad in visited:
                continue

            # BFS to find all connected pads
            island: list[str] = []
            queue = [pad]

            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)

                # Only include pads from our list
                if current in pads:
                    island.append(current)

                # Add neighbors
                for neighbor in graph.get(current, set()):
                    if neighbor not in visited:
                        queue.append(neighbor)

            if island:
                islands.append(sorted(island))

        return islands

    def _create_issue(
        self,
        net_name: str,
        pads: list[str],
        islands: list[list[str]],
    ) -> ConnectivityIssue:
        """Create a connectivity issue for a net with multiple islands.

        Args:
            net_name: Name of the net
            pads: All pads on this net
            islands: List of disconnected islands

        Returns:
            ConnectivityIssue describing the problem
        """
        # Sort islands by size (largest first)
        islands = sorted(islands, key=len, reverse=True)

        # Largest island is "connected", rest are "unconnected"
        connected = islands[0] if islands else []
        unconnected_islands = islands[1:] if len(islands) > 1 else []
        unconnected = []
        for island in unconnected_islands:
            unconnected.extend(island)

        if len(islands) == 2:
            # Two islands - partial connection
            issue_type = "partial"
            message = f"Net '{net_name}' has 2 disconnected islands"
            suggestion = (
                f"Connect islands (missing trace between {islands[0][-1]} and {islands[1][0]})"
            )
        else:
            # More than two islands
            issue_type = "partial"
            message = f"Net '{net_name}' has {len(islands)} disconnected islands"
            suggestion = f"Connect {len(islands)} islands to complete routing"

        return ConnectivityIssue(
            severity="error",
            issue_type=issue_type,
            net_name=net_name,
            message=message,
            suggestion=suggestion,
            connected_pads=tuple(connected),
            unconnected_pads=tuple(unconnected),
            islands=tuple(tuple(island) for island in islands),
        )

    def __repr__(self) -> str:
        """Return string representation."""
        net_count = self.pcb.net_count if self.pcb else 0
        return f"ConnectivityValidator(nets={net_count})"

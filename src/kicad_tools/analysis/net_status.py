"""Net connectivity status analysis for PCB designs.

This module provides detailed net connectivity status reporting, showing which nets
are complete, incomplete, or unrouted, with details on what's missing.

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.analysis.net_status import NetStatusAnalyzer
    >>>
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> analyzer = NetStatusAnalyzer(pcb)
    >>> result = analyzer.analyze()
    >>>
    >>> for net_status in result.incomplete:
    ...     print(f"{net_status.net_name}: {net_status.unconnected_count} unconnected")
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.core.layers import COPPER_LAYER_ORDER, via_spans_layer

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


@dataclass
class PadInfo:
    """Information about a pad on a net."""

    reference: str  # Component reference (e.g., "U1")
    pad_number: str  # Pad number (e.g., "2")
    position: tuple[float, float]  # Board coordinates (x, y)
    is_connected: bool  # Whether pad is connected to main routing
    layers: list[str] = field(default_factory=list)  # Layers pad exists on

    @property
    def full_name(self) -> str:
        """Full pad name as REF.PAD (e.g., 'C2.2')."""
        return f"{self.reference}.{self.pad_number}"


@dataclass
class NetStatus:
    """Status of a single net.

    Attributes:
        net_number: Net number in PCB
        net_name: Net name (e.g., "GND", "+3.3V")
        net_class: Net class if assigned
        total_pads: Total number of pads on this net
        connected_pads: List of connected pads
        unconnected_pads: List of unconnected pads
        is_plane_net: Whether this net is connected to a copper zone
        plane_layer: Primary layer of the copper zone if is_plane_net
        plane_layers: All layers with copper zones for this net
        has_routing: Whether this net has any trace segments
        has_vias: Whether this net has any vias
    """

    net_number: int
    net_name: str
    net_class: str = ""
    total_pads: int = 0
    connected_pads: list[PadInfo] = field(default_factory=list)
    unconnected_pads: list[PadInfo] = field(default_factory=list)
    is_plane_net: bool = False
    plane_layer: str = ""
    plane_layers: list[str] = field(default_factory=list)
    has_routing: bool = False
    has_vias: bool = False

    @property
    def connected_count(self) -> int:
        """Number of connected pads."""
        return len(self.connected_pads)

    @property
    def unconnected_count(self) -> int:
        """Number of unconnected pads."""
        return len(self.unconnected_pads)

    @property
    def connection_percentage(self) -> float:
        """Percentage of pads connected (0-100).

        Returns 0.0 when total_pads is 0 to avoid masking data corruption
        where all pad net assignments have been stripped.
        """
        if self.total_pads == 0:
            return 0.0
        return (self.connected_count / self.total_pads) * 100

    @property
    def status(self) -> str:
        """Net status: 'complete', 'incomplete', or 'unrouted'.

        A net with 0 pads is reported as 'unrouted' rather than 'complete'
        to avoid masking corruption where pad net assignments were stripped.
        A net with exactly 1 pad is genuinely complete (single-pad nets are
        valid in KiCad).
        """
        if self.total_pads == 0:
            return "unrouted"
        if self.total_pads == 1:
            return "complete"
        if self.unconnected_count == 0:
            return "complete"
        if self.connected_count == 0:
            return "unrouted"
        return "incomplete"

    @property
    def net_type(self) -> str:
        """Net type: 'plane', 'signal', or 'power'."""
        if self.is_plane_net:
            return "plane"
        # Common power net patterns
        if self.net_name.startswith(("+", "-", "V")) or self.net_name in (
            "GND",
            "AGND",
            "DGND",
            "VCC",
            "VDD",
            "VSS",
        ):
            return "power"
        return "signal"

    @property
    def is_advisory_incomplete(self) -> bool:
        """Return True iff this net is incomplete only because of an advisory
        residual (plane/pour stitching), not a genuine signal-net gap.

        Mirrors the audit-pipeline classifier (``DRCChecker.ADVISORY_RULE_IDS``
        contains ``connectivity``) and the auditor's ``_check_connectivity``
        logic that splits incomplete nets into ``truly_incomplete`` vs
        ``zone_connected``/``pour`` categories. A net is treated as advisory-
        incomplete when it is currently flagged ``incomplete`` AND any of:

        * It is connected to a copper zone (``is_plane_net`` is True), so the
          residual is a thermal-relief / stitching artifact rather than a
          missing signal trace.
        * Its ``net_type`` is ``plane`` or ``power`` (power nets are expected
          to be zone-filled regardless of whether a zone exists yet).

        The pour-classifier check via :mod:`kicad_tools.router.net_class` is
        threaded in at the :class:`NetStatusResult` level (it needs the full
        net-id map), so this property covers only the name/zone-based cases.
        """
        if self.status != "incomplete":
            return False
        if self.is_plane_net:
            return True
        if self.net_type in ("plane", "power"):
            return True
        return False

    @property
    def suggested_fix(self) -> str:
        """Suggest fix based on net type."""
        if self.is_plane_net:
            layers = self.plane_layers or ([self.plane_layer] if self.plane_layer else [])
            layers_info = ", ".join(layers) if layers else "unknown"
            return f"kct stitch board.kicad_pcb --net {self.net_name} (zones on {layers_info})"
        return f"Route traces to connect {self.unconnected_count} pads"

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "net_number": self.net_number,
            "net_name": self.net_name,
            "net_class": self.net_class,
            "status": self.status,
            "net_type": self.net_type,
            "total_pads": self.total_pads,
            "connected_count": self.connected_count,
            "unconnected_count": self.unconnected_count,
            "connection_percentage": round(self.connection_percentage, 1),
            "is_plane_net": self.is_plane_net,
            "is_advisory_incomplete": self.is_advisory_incomplete,
            "plane_layer": self.plane_layer,
            "plane_layers": list(self.plane_layers),
            "has_routing": self.has_routing,
            "has_vias": self.has_vias,
            "suggested_fix": self.suggested_fix if self.status != "complete" else "",
            "connected_pads": [
                {
                    "name": p.full_name,
                    "position": list(p.position),
                }
                for p in self.connected_pads
            ],
            "unconnected_pads": [
                {
                    "name": p.full_name,
                    "position": list(p.position),
                }
                for p in self.unconnected_pads
            ],
        }


@dataclass
class NetStatusResult:
    """Aggregates net status for all nets in a PCB.

    Attributes:
        nets: List of all analyzed net statuses
        total_nets: Total number of nets analyzed
        advisory_incomplete_names: Names of incomplete nets reclassified as
            advisory-only (plane/pour stitching residuals). Populated by
            :class:`NetStatusAnalyzer` after the router pour-net classifier
            runs; consulted by :attr:`blocking_incomplete` to filter the
            raw ``incomplete`` list down to genuine signal-net gaps.
    """

    nets: list[NetStatus] = field(default_factory=list)
    total_nets: int = 0
    advisory_incomplete_names: set[str] = field(default_factory=set)

    @property
    def complete(self) -> list[NetStatus]:
        """Nets that are fully connected."""
        return [n for n in self.nets if n.status == "complete"]

    @property
    def incomplete(self) -> list[NetStatus]:
        """Nets that are partially connected (raw count, includes advisory).

        Diagnostic consumers (e.g. ``kct net-status``) rely on this
        unfiltered view so plane-net stitching residuals remain visible.
        Gating verdicts should consult :attr:`blocking_incomplete` instead.
        """
        return [n for n in self.nets if n.status == "incomplete"]

    @property
    def blocking_incomplete(self) -> list[NetStatus]:
        """Incomplete nets that block routing-completion (advisory removed).

        Mirrors ``scripts/ci/check_routed_drc.py:_count_blocking_errors``:
        an incomplete net is dropped from this list when it is classified
        as a plane/pour residual (advisory connectivity), matching the
        ``DRCChecker.ADVISORY_RULE_IDS`` policy.
        """
        return [
            n
            for n in self.nets
            if n.status == "incomplete"
            and n.net_name not in self.advisory_incomplete_names
            and not n.is_advisory_incomplete
        ]

    @property
    def unrouted(self) -> list[NetStatus]:
        """Nets with no routing at all."""
        return [n for n in self.nets if n.status == "unrouted"]

    @property
    def complete_count(self) -> int:
        """Number of complete nets."""
        return len(self.complete)

    @property
    def incomplete_count(self) -> int:
        """Number of incomplete nets (raw, includes advisory)."""
        return len(self.incomplete)

    @property
    def blocking_incomplete_count(self) -> int:
        """Number of incomplete nets that block routing-complete verdict.

        Excludes plane/pour stitching residuals (``ADVISORY_RULE_IDS``
        connectivity) so the ship-ready gate matches ``check_routed_drc``.
        """
        return len(self.blocking_incomplete)

    @property
    def unrouted_count(self) -> int:
        """Number of unrouted nets."""
        return len(self.unrouted)

    @property
    def total_unconnected_pads(self) -> int:
        """Total number of unconnected pads across all nets."""
        return sum(n.unconnected_count for n in self.nets)

    def by_net_class(self) -> dict[str, list[NetStatus]]:
        """Group nets by net class."""
        result: dict[str, list[NetStatus]] = defaultdict(list)
        for net in self.nets:
            class_name = net.net_class or "Default"
            result[class_name].append(net)
        return dict(result)

    def get_net(self, net_name: str) -> NetStatus | None:
        """Get status for a specific net by name."""
        for net in self.nets:
            if net.net_name == net_name:
                return net
        return None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "total_nets": self.total_nets,
            "complete_count": self.complete_count,
            "incomplete_count": self.incomplete_count,
            "blocking_incomplete_count": self.blocking_incomplete_count,
            "unrouted_count": self.unrouted_count,
            "total_unconnected_pads": self.total_unconnected_pads,
            "advisory_incomplete_names": sorted(self.advisory_incomplete_names),
            "nets": [n.to_dict() for n in self.nets],
        }

    def summary(self) -> str:
        """Generate human-readable summary."""
        lines = [
            f"Net Status Summary: {self.total_nets} nets total",
            f"  Complete:   {self.complete_count} (100% connected)",
            f"  Incomplete: {self.incomplete_count} (partially connected)",
            f"  Unrouted:   {self.unrouted_count} (0% connected)",
            f"  Total unconnected pads: {self.total_unconnected_pads}",
        ]
        return "\n".join(lines)


def build_zone_net_map(pcb: PCB) -> dict[int, list[str]]:
    """Build mapping of net numbers to zone layers.

    This is a lightweight utility that scans all zones on a PCB and returns
    which copper layers have zones assigned to each net.  It does NOT require
    instantiating the full ``NetStatusAnalyzer``.

    Args:
        pcb: Loaded PCB object.

    Returns:
        Dict mapping net_number to list of zone layer names.  Nets without
        zones are absent from the dict.
    """
    zone_nets: dict[int, list[str]] = defaultdict(list)
    for zone in pcb.zones:
        if zone.net_number > 0 and zone.layer not in zone_nets[zone.net_number]:
            zone_nets[zone.net_number].append(zone.layer)
    return dict(zone_nets)


class NetStatusAnalyzer:
    """Analyzes net connectivity status on a PCB.

    Provides detailed status for each net including:
    - Complete/incomplete/unrouted classification
    - Identification of plane nets vs signal nets
    - Location of unconnected pads with coordinates
    - Suggested fixes

    Example:
        >>> pcb = PCB.load("board.kicad_pcb")
        >>> analyzer = NetStatusAnalyzer(pcb)
        >>> result = analyzer.analyze()
        >>> print(result.summary())
    """

    # Tolerance for matching point positions (in mm)
    POSITION_TOLERANCE = 0.01

    def __init__(self, pcb: str | Path | PCB) -> None:
        """Initialize the analyzer.

        Args:
            pcb: Path to PCB file or loaded PCB object
        """
        from kicad_tools.schema.pcb import PCB as PCBClass

        if isinstance(pcb, (str, Path)):
            self.pcb = PCBClass.load(str(pcb))
        else:
            self.pcb = pcb

    def analyze(self) -> NetStatusResult:
        """Analyze all nets and return status result.

        Returns:
            NetStatusResult containing status for all nets
        """
        result = NetStatusResult()

        # Get all non-empty nets (skip net 0 which is unconnected)
        nets = {n: net for n, net in self.pcb.nets.items() if n != 0 and net.name}
        result.total_nets = len(nets)

        # Build zone lookup for plane net detection
        zone_nets = self._build_zone_net_map()

        for net_number, net in nets.items():
            status = self._analyze_net(net_number, net.name, zone_nets)
            result.nets.append(status)

        # Sort by status (incomplete first, then unrouted, then complete)
        status_order = {"incomplete": 0, "unrouted": 1, "complete": 2}
        result.nets.sort(key=lambda n: (status_order.get(n.status, 3), n.net_name))

        # Reclassify pour-net residuals as advisory (mirrors the audit
        # pipeline's `_check_connectivity` second pass and the
        # `ADVISORY_RULE_IDS` policy at the CI gate). The router's
        # `classify_and_apply_rules` walks net names through the pour
        # classifier; nets it tags `is_pour_net=True` are expected to be
        # zone-filled even when no zone definition exists yet, so a
        # stranded pad on such a net is a stitching residual rather than
        # a missing signal trace.
        incomplete_names = {n.net_name for n in result.incomplete}
        if incomplete_names:
            try:
                from kicad_tools.router.net_class import classify_and_apply_rules

                net_id_by_name = {
                    net.name: net_id
                    for net_id, net in self.pcb.nets.items()
                    if net_id > 0 and net.name
                }
                pending_ids = {
                    net_id_by_name[n]: n for n in incomplete_names if n in net_id_by_name
                }
                if pending_ids:
                    rules = classify_and_apply_rules(pending_ids)
                    result.advisory_incomplete_names = {
                        n for n in incomplete_names if rules.get(n) and rules[n].is_pour_net
                    }
            except Exception:
                # Conservative: leave advisory_incomplete_names empty so
                # the gate stays strict if the classifier is unavailable.
                pass

        return result

    def _build_zone_net_map(self) -> dict[int, list[str]]:
        """Build mapping of net numbers to zone layers.

        Returns:
            Dict mapping net_number to list of zone layer names
        """
        return build_zone_net_map(self.pcb)

    def _analyze_net(
        self,
        net_number: int,
        net_name: str,
        zone_nets: dict[int, list[str]],
    ) -> NetStatus:
        """Analyze a single net.

        Args:
            net_number: Net number
            net_name: Net name
            zone_nets: Mapping of net numbers to zone layer lists

        Returns:
            NetStatus for this net
        """
        status = NetStatus(
            net_number=net_number,
            net_name=net_name,
        )

        # Check if this is a plane net
        if net_number in zone_nets:
            status.is_plane_net = True
            status.plane_layers = zone_nets[net_number]
            status.plane_layer = zone_nets[net_number][0]

        # Check for routing
        segments = list(self.pcb.segments_in_net(net_number))
        status.has_routing = len(segments) > 0

        # Check for vias
        vias = list(self.pcb.vias_in_net(net_number))
        status.has_vias = len(vias) > 0

        # Get all pads on this net with their positions
        pad_infos = self._get_net_pads_with_positions(net_number)
        status.total_pads = len(pad_infos)

        if len(pad_infos) < 2:
            # Single-pad nets are always "complete"
            status.connected_pads = pad_infos
            return status

        # Build connectivity graph
        graph = self._build_connectivity_graph(net_number, pad_infos)

        # Find connected components (islands)
        islands = self._find_islands(graph, [p.full_name for p in pad_infos])

        # Largest island is considered "connected"
        if islands:
            islands.sort(key=len, reverse=True)
            connected_names = set(islands[0])
        else:
            connected_names = set()

        # Classify pads
        for pad_info in pad_infos:
            pad_info.is_connected = pad_info.full_name in connected_names
            if pad_info.is_connected:
                status.connected_pads.append(pad_info)
            else:
                status.unconnected_pads.append(pad_info)

        # Sort unconnected pads by position for consistent output
        status.unconnected_pads.sort(key=lambda p: (p.reference, p.pad_number))

        return status

    def _get_net_pads_with_positions(self, net_number: int) -> list[PadInfo]:
        """Get all pads on a net with their board positions.

        Args:
            net_number: Net number to find pads for

        Returns:
            List of PadInfo objects
        """
        pads = []
        for fp in self.pcb.footprints:
            if not fp.reference or fp.reference.startswith("#"):
                continue

            fp_x, fp_y = fp.position
            rotation = fp.rotation

            for pad in fp.pads:
                if pad.net_number == net_number:
                    # Transform pad position to board coordinates
                    board_pos = self._transform_pad_position(pad.position, fp_x, fp_y, rotation)
                    pads.append(
                        PadInfo(
                            reference=fp.reference,
                            pad_number=pad.number,
                            position=board_pos,
                            is_connected=False,
                            layers=pad.layers,
                        )
                    )
        return pads

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
        angle = math.radians(rotation)
        px, py = pad_local
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)

        rotated_x = px * cos_a - py * sin_a
        rotated_y = px * sin_a + py * cos_a

        return (fp_x + rotated_x, fp_y + rotated_y)

    def _build_connectivity_graph(
        self,
        net_number: int,
        pad_infos: list[PadInfo],
    ) -> dict[str, set[str]]:
        """Build connectivity graph for a net.

        Args:
            net_number: Net number
            pad_infos: List of pad info objects

        Returns:
            Adjacency list mapping pad names to connected pad names
        """
        graph: dict[str, set[str]] = defaultdict(set)
        pad_positions = {p.full_name: p.position for p in pad_infos}
        pad_layers = {p.full_name: p.layers for p in pad_infos}

        # Get segments and vias for this net
        segments = list(self.pcb.segments_in_net(net_number))
        vias = list(self.pcb.vias_in_net(net_number))

        # Get zones for this net with their layers, filled polygons, and boundaries
        # We track both filled_polygons (actual copper) AND boundary polygon (zone outline)
        # because vias at pad positions may be in thermal clearance cutouts of filled
        # polygons but still within the zone boundary (and thus connected to the zone).
        #
        # IMPORTANT (Issue #3482): the boundary polygon only implies connectivity
        # when the zone actually produced filled copper. A zone with fill enabled
        # but zero filled polygons (e.g. fully shadowed by a higher-priority zone,
        # or carved away entirely by clearances) contributes NO copper, so its
        # boundary must not mark pads/vias as connected. The boundary heuristic
        # (Issue #479) exists solely for thermal-relief cutouts INSIDE filled
        # copper, which presupposes the zone has at least one filled polygon.
        net_zones: list[tuple[str, list[list[tuple[float, float]]]]] = []
        zone_boundaries: list[tuple[str, list[tuple[float, float]]]] = []
        for zone in self.pcb.zones:
            if zone.net_number == net_number:
                # Zero-fill zones provide no electrical connectivity at all
                # (Issue #3482): skip both boundary and copper collection.
                if not zone.filled_polygons:
                    continue
                # Collect boundary polygon (zone outline) for via-in-zone checking.
                # If no boundary polygon exists, use the convex hull of filled
                # polygons as a fallback boundary so that pad-in-zone checks
                # still work when kicad-cli omits the outline for filled zones.
                if zone.polygon:
                    zone_boundaries.append((zone.layer, zone.polygon))
                else:
                    # Use the bounding box of all filled polygon vertices as a
                    # conservative fallback boundary.
                    fallback = self._bounding_box_polygon(zone.filled_polygons)
                    if fallback:
                        zone_boundaries.append((zone.layer, fallback))
                # Collect filled polygons for copper overlap checks
                net_zones.append((zone.layer, zone.filled_polygons))

        # Build segment components for reuse
        segment_components = self._build_segment_components(segments)

        # Connect pads through segment chains
        for component in segment_components:
            component_pads: set[str] = set()
            for seg_idx in component:
                seg = segments[seg_idx]
                component_pads.update(self._find_pads_at_point(seg.start, pad_positions))
                component_pads.update(self._find_pads_at_point(seg.end, pad_positions))

            pad_list = list(component_pads)
            for i, pad in enumerate(pad_list):
                for other in pad_list[i + 1 :]:
                    graph[pad].add(other)
                    graph[other].add(pad)

        # Connect pads through vias (pads at same via position)
        for via in vias:
            via_pads = self._find_pads_at_point(via.position, pad_positions)
            for pad in via_pads:
                for other in via_pads:
                    if pad != other:
                        graph[pad].add(other)
                        graph[other].add(pad)

        # Connect segment chains to vias (Issue #1991 fix).
        # If a segment endpoint coincides with a via position, all pads in
        # that segment chain are electrically connected through the via.
        # Without this link the pad->trace->via chain breaks whenever the
        # zone-connection check fails (e.g. zone on inner layer, through-via
        # not recognized, or via near zone boundary edge).
        for component in segment_components:
            component_endpoints: list[tuple[float, float]] = []
            for seg_idx in component:
                seg = segments[seg_idx]
                component_endpoints.append(seg.start)
                component_endpoints.append(seg.end)

            # Check if any endpoint of this segment chain touches a via
            for via in vias:
                touches_via = any(
                    self._points_close(ep, via.position) for ep in component_endpoints
                )
                if touches_via:
                    # Collect all pads in this segment chain
                    chain_pads: set[str] = set()
                    for seg_idx in component:
                        seg = segments[seg_idx]
                        chain_pads.update(self._find_pads_at_point(seg.start, pad_positions))
                        chain_pads.update(self._find_pads_at_point(seg.end, pad_positions))
                    # Also include any pads at the via position itself
                    chain_pads.update(self._find_pads_at_point(via.position, pad_positions))
                    # Connect all pads in this chain through the via
                    chain_list = list(chain_pads)
                    for i, pad in enumerate(chain_list):
                        for other in chain_list[i + 1 :]:
                            graph[pad].add(other)
                            graph[other].add(pad)

        # Build bounding-box indices for zone polygons to avoid O(n*v) point-in-polygon
        # checks for zones whose bounding box doesn't contain the query point.
        boundary_bboxes = [
            (layer, boundary, self._polygon_bbox(boundary)) for layer, boundary in zone_boundaries
        ]
        filled_bboxes = [
            (layer, polys, [self._polygon_bbox(p) for p in polys]) for layer, polys in net_zones
        ]

        # Find zone connection points (via positions that touch zones)
        # Check BOTH filled polygons AND zone boundaries because:
        # - Filled polygons have thermal clearance cutouts around pads
        # - Stitching vias are placed at pad positions (inside those cutouts)
        # - But they ARE still within the zone boundary and thus connected
        zone_connection_points: set[tuple[float, float]] = set()

        # Check vias against zone boundaries (Issue #479 fix)
        # This catches vias in thermal clearance cutouts that are still in the zone.
        # Use _via_spans_layer to handle through-vias whose layer list is
        # ["F.Cu", "B.Cu"] but which electrically connect all intermediate layers.
        for zone_layer, boundary, bbox in boundary_bboxes:
            for via in vias:
                if not self._via_spans_layer(via.layers, zone_layer):
                    continue
                if not self._point_in_bbox(via.position, bbox):
                    continue
                if self._point_in_polygon(via.position, boundary):
                    zone_connection_points.add(via.position)

        # Also check vias against filled polygons (original behavior)
        for zone_layer, filled_polys, poly_bboxes in filled_bboxes:
            for via in vias:
                if not self._via_spans_layer(via.layers, zone_layer):
                    continue
                for poly, bbox in zip(filled_polys, poly_bboxes, strict=False):
                    if not self._point_in_bbox(via.position, bbox):
                        continue
                    if self._point_in_polygon(via.position, poly):
                        zone_connection_points.add(via.position)
                        break

        # Connect pads through via-to-zone connectivity
        # A pad is zone-connected if:
        # 1. It's directly at a zone connection point, OR
        # 2. It's in a segment chain that touches a zone connection point
        zone_connected_pads: set[str] = set()

        # Pads directly at zone connection points
        for zcp in zone_connection_points:
            zone_connected_pads.update(self._find_pads_at_point(zcp, pad_positions))

        # Pads that directly overlap with zone boundaries or filled polygons (Issue #441, #479)
        # This handles through-hole pads that connect to inner layer zones
        # Check boundaries first (catches pads in thermal clearance cutouts)
        for pad_id, pad_pos in pad_positions.items():
            layers = pad_layers.get(pad_id, [])
            # Check zone boundaries (Issue #479 fix)
            for zone_layer, boundary, bbox in boundary_bboxes:
                if not self._pad_layer_matches_zone(layers, zone_layer):
                    continue
                if not self._point_in_bbox(pad_pos, bbox):
                    continue
                if self._point_in_polygon(pad_pos, boundary):
                    zone_connected_pads.add(pad_id)
                    break
            # Also check filled polygons (original behavior)
            for zone_layer, filled_polys, poly_bboxes in filled_bboxes:
                if not self._pad_layer_matches_zone(layers, zone_layer):
                    continue
                for poly, bbox in zip(filled_polys, poly_bboxes, strict=False):
                    if not self._point_in_bbox(pad_pos, bbox):
                        continue
                    if self._point_in_polygon(pad_pos, poly):
                        zone_connected_pads.add(pad_id)
                        break

        # Pads connected via segment chains that touch zone connection points
        for component in segment_components:
            touches_zone = False
            for seg_idx in component:
                seg = segments[seg_idx]
                for zcp in zone_connection_points:
                    if self._points_close(seg.start, zcp) or self._points_close(seg.end, zcp):
                        touches_zone = True
                        break
                if touches_zone:
                    break

            if touches_zone:
                # All pads in this segment chain are connected to the zone
                for seg_idx in component:
                    seg = segments[seg_idx]
                    zone_connected_pads.update(self._find_pads_at_point(seg.start, pad_positions))
                    zone_connected_pads.update(self._find_pads_at_point(seg.end, pad_positions))

        # Connect all zone-connected pads to each other
        zone_pad_list = list(zone_connected_pads)
        for i, pad in enumerate(zone_pad_list):
            for other in zone_pad_list[i + 1 :]:
                graph[pad].add(other)
                graph[other].add(pad)

        # Connect pads at same copper positions (segments and vias only).
        # Zone polygon vertices are NOT sampled here because the pad-to-zone
        # and via-to-zone polygon containment checks above already handle zone
        # connectivity properly.  The previous zone_points[:1000] sampling cap
        # caused false-positive incomplete reports on boards with many filled
        # polygons (Issue #2035).
        all_copper = [seg.start for seg in segments] + [seg.end for seg in segments]
        all_copper.extend([via.position for via in vias])

        for pad_id, pad_pos in pad_positions.items():
            for copper_pos in all_copper:
                if self._points_close(pad_pos, copper_pos):
                    # Find other pads at this copper point
                    for other_id, other_pos in pad_positions.items():
                        if other_id != pad_id and self._points_close(copper_pos, other_pos):
                            graph[pad_id].add(other_id)
                            graph[other_id].add(pad_id)

        return graph

    def _build_segment_components(self, segments: list) -> list[set[int]]:
        """Build connected components of segments.

        Args:
            segments: List of trace segments

        Returns:
            List of sets, each containing segment indices in a connected component
        """
        if not segments:
            return []

        # Build segment adjacency graph
        segment_graph: dict[int, set[int]] = defaultdict(set)
        for i, seg_a in enumerate(segments):
            for j, seg_b in enumerate(segments):
                if i != j:
                    if (
                        self._points_close(seg_a.start, seg_b.start)
                        or self._points_close(seg_a.start, seg_b.end)
                        or self._points_close(seg_a.end, seg_b.start)
                        or self._points_close(seg_a.end, seg_b.end)
                    ):
                        segment_graph[i].add(j)
                        segment_graph[j].add(i)

        # Find connected components
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

        return components

    def _find_pads_at_point(
        self,
        point: tuple[float, float],
        pad_positions: dict[str, tuple[float, float]],
    ) -> list[str]:
        """Find pads at a given point.

        Args:
            point: Point to check
            pad_positions: Mapping of pad names to positions

        Returns:
            List of pad names at this point
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
        """Check if two points are within tolerance."""
        dx = p1[0] - p2[0]
        dy = p1[1] - p2[1]
        return (dx * dx + dy * dy) < (self.POSITION_TOLERANCE * self.POSITION_TOLERANCE)

    # Canonical copper layer ordering from top to bottom.
    # Promoted to ``kicad_tools.core.layers`` in Issue #3487 so the DRC
    # clearance rule shares the same via-span semantics; kept here as a
    # class attribute for backwards compatibility with existing tests.
    _COPPER_LAYER_ORDER: list[str] = list(COPPER_LAYER_ORDER)

    def _via_spans_layer(self, via_layers: list[str], target_layer: str) -> bool:
        """Check if a via spans (connects to) a target copper layer.

        In KiCad, a via with layers ["F.Cu", "B.Cu"] is a through-via that
        connects ALL intermediate copper layers (In1.Cu, In2.Cu, etc.), not
        just the two listed layers.  A blind/buried via ["F.Cu", "In1.Cu"]
        connects F.Cu and In1.Cu only (no intermediates beyond those two).

        Delegates to :func:`kicad_tools.core.layers.via_spans_layer`
        (single source of truth shared with the DRC clearance rule;
        Issue #3487).

        Args:
            via_layers: List of layer names from the via (e.g., ["F.Cu", "B.Cu"])
            target_layer: Layer name to check (e.g., "In1.Cu")

        Returns:
            True if the via electrically connects the target layer
        """
        return via_spans_layer(via_layers, target_layer)

    def _pad_layer_matches_zone(
        self,
        pad_layers: list[str],
        zone_layer: str,
    ) -> bool:
        """Check if a pad exists on the same layer as a zone.

        Handles wildcard layers like "*.Cu" which match any copper layer.

        Args:
            pad_layers: List of layers the pad exists on
            zone_layer: Layer the zone is on (e.g., "In1.Cu", "B.Cu")

        Returns:
            True if the pad and zone share a layer
        """
        for pad_layer in pad_layers:
            # Exact match
            if pad_layer == zone_layer:
                return True
            # Wildcard match: "*.Cu" matches any copper layer
            if pad_layer == "*.Cu" and zone_layer.endswith(".Cu"):
                return True
            # Also handle "*.Mask" style wildcards
            if pad_layer.startswith("*.") and zone_layer.endswith(pad_layer[1:]):
                return True
        return False

    @staticmethod
    def _polygon_bbox(
        polygon: list[tuple[float, float]],
    ) -> tuple[float, float, float, float]:
        """Compute axis-aligned bounding box of a polygon.

        Returns:
            (min_x, min_y, max_x, max_y) tuple
        """
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        return (min(xs), min(ys), max(xs), max(ys))

    @staticmethod
    def _point_in_bbox(
        point: tuple[float, float],
        bbox: tuple[float, float, float, float],
    ) -> bool:
        """Fast check whether a point is inside a bounding box.

        Args:
            point: (x, y) coordinates
            bbox: (min_x, min_y, max_x, max_y) bounding box

        Returns:
            True if the point is within the bounding box (inclusive).
        """
        return bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]

    @staticmethod
    def _bounding_box_polygon(
        filled_polygons: list[list[tuple[float, float]]],
    ) -> list[tuple[float, float]]:
        """Build a bounding-box rectangle from multiple filled polygons.

        Used as a fallback zone boundary when the zone outline polygon is
        absent (some KiCad versions omit it for zones that were filled by
        kicad-cli).

        Args:
            filled_polygons: List of filled polygon vertex lists.

        Returns:
            Four-vertex polygon representing the bounding box, or empty list.
        """
        all_x: list[float] = []
        all_y: list[float] = []
        for poly in filled_polygons:
            for px, py in poly:
                all_x.append(px)
                all_y.append(py)
        if not all_x:
            return []
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)
        return [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)]

    def _point_in_polygon(
        self,
        point: tuple[float, float],
        polygon: list[tuple[float, float]],
    ) -> bool:
        """Test if point is inside polygon using ray casting algorithm.

        Args:
            point: (x, y) coordinates to test
            polygon: List of (x, y) vertices

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

    def _find_islands(
        self,
        graph: dict[str, set[str]],
        pads: list[str],
    ) -> list[list[str]]:
        """Find disconnected islands in connectivity graph.

        Args:
            graph: Adjacency list
            pads: List of pad names

        Returns:
            List of islands (connected components)
        """
        visited: set[str] = set()
        islands: list[list[str]] = []

        for pad in pads:
            if pad in visited:
                continue

            island: list[str] = []
            queue = [pad]

            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)

                if current in pads:
                    island.append(current)

                for neighbor in graph.get(current, set()):
                    if neighbor not in visited:
                        queue.append(neighbor)

            if island:
                islands.append(sorted(island))

        return islands

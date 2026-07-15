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
from typing import TYPE_CHECKING, Any

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
    has_filled_zone: bool = False  # A zone on this net produced fill copper

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
            "has_filled_zone": self.has_filled_zone,
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

    # Tolerance for matching point positions (in mm).
    #
    # This is an endpoint-proximity radius, NOT a geometric-contact test:
    # in the default (non-strict) mode, two copper elements (segment
    # endpoints, pad centers, via centers) are unioned whenever their
    # reference points land within this radius, even if their actual copper
    # shapes (segment width, pad size) do not touch.  KiCad's connectivity
    # engine instead requires real geometric overlap, so the default model
    # can report a net "complete" that KiCad (``kicad-cli pcb drc``) reports
    # as having unconnected items (Issue #4176).  Pass ``strict=True`` to use
    # real shapely copper-shape intersection and match KiCad.
    POSITION_TOLERANCE = 0.01

    def __init__(self, pcb: str | Path | PCB, *, strict: bool = False) -> None:
        """Initialize the analyzer.

        Args:
            pcb: Path to PCB file or loaded PCB object.
            strict: When ``True``, segment↔segment, segment↔pad, and
                segment↔via connectivity is decided by real geometric copper
                contact (shapely polygon intersection: a segment is its
                centerline buffered by ``width / 2``; a pad/via is its copper
                shape) instead of the ``POSITION_TOLERANCE`` endpoint-proximity
                radius.  This matches KiCad's connectivity semantics
                (Issue #4176).  The default (``False``) preserves the legacy
                tolerance-based behavior so existing consumers are unaffected.
                Requires ``shapely``; strict mode fails loud (rather than
                silently degrading to the tolerance model) if it is
                unavailable.
        """
        from kicad_tools.schema.pcb import PCB as PCBClass

        if isinstance(pcb, (str, Path)):
            self.pcb = PCBClass.load(str(pcb))
        else:
            self.pcb = pcb
        self.strict = strict
        # Strict-mode geometry caches (Issue #4176), keyed by object id.
        self._segment_poly_cache: dict[int, Any] = {}
        self._via_geom_cache: dict[int, Any] = {}
        # Per-analyzer pad copper polygon cache (keyed ``REF.PAD``); ``None``
        # until first built lazily in :meth:`_pad_polys`.
        self._pad_poly_cache: dict[str, Any] | None = None
        if strict:
            from kicad_tools._shapely import require_shapely

            require_shapely("net-status --strict real-geometry connectivity")

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
            # A zone that produced no filled copper (fill disabled, or fully
            # carved away) provides no connectivity (Issue #3482) and is NOT a
            # basis for suppressing a connectivity error (Issue #3914): only a
            # zone with real fill copper makes an incomplete pour net advisory.
            status.has_filled_zone = any(
                zone.net_number == net_number and zone.filled_polygons for zone in self.pcb.zones
            )

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
        # KiCad applies the footprint orientation as a NEGATED angle vs
        # standard CCW math (verified vs pcbnew, issue #3739).
        angle = math.radians(-rotation)
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

        # Connect pads through segment chains.
        #
        # Default mode: a pad joins the chain when its center is within
        # POSITION_TOLERANCE of a segment endpoint.  Strict mode (Issue #4176):
        # a pad joins the chain when its real copper polygon intersects any
        # segment's copper polygon in the chain — an endpoint landing merely
        # *near* a pad's copper edge no longer bonds.
        for component in segment_components:
            component_pads: set[str] = set()
            for seg_idx in component:
                seg = segments[seg_idx]
                if self.strict:
                    component_pads.update(
                        self._find_pads_touching_geom(self._segment_poly(seg), pad_positions)
                    )
                else:
                    component_pads.update(self._find_pads_at_point(seg.start, pad_positions))
                    component_pads.update(self._find_pads_at_point(seg.end, pad_positions))

            pad_list = list(component_pads)
            for i, pad in enumerate(pad_list):
                for other in pad_list[i + 1 :]:
                    graph[pad].add(other)
                    graph[other].add(pad)

        # Connect pads through vias (pads whose copper the via barrel pierces).
        # Default: pad center within POSITION_TOLERANCE of the via center.
        # Strict (Issue #4176): pad copper polygon intersects the via copper.
        for via in vias:
            if self.strict:
                via_pads = self._find_pads_touching_geom(self._via_geom(via), pad_positions)
            else:
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

            # Check if this segment chain's copper touches a via.
            # Default: an endpoint within POSITION_TOLERANCE of the via center.
            # Strict (Issue #4176): a segment copper polygon in the chain
            # intersects the via copper geometry.
            for via in vias:
                if self.strict:
                    via_geom = self._via_geom(via)
                    touches_via = any(
                        self._geoms_touch(self._segment_poly(segments[seg_idx]), via_geom)
                        for seg_idx in component
                    )
                else:
                    touches_via = any(
                        self._points_close(ep, via.position) for ep in component_endpoints
                    )
                if touches_via:
                    # Collect all pads in this segment chain
                    chain_pads: set[str] = set()
                    for seg_idx in component:
                        seg = segments[seg_idx]
                        if self.strict:
                            chain_pads.update(
                                self._find_pads_touching_geom(
                                    self._segment_poly(seg), pad_positions
                                )
                            )
                        else:
                            chain_pads.update(self._find_pads_at_point(seg.start, pad_positions))
                            chain_pads.update(self._find_pads_at_point(seg.end, pad_positions))
                    # Also include any pads at the via position itself
                    if self.strict:
                        chain_pads.update(
                            self._find_pads_touching_geom(self._via_geom(via), pad_positions)
                        )
                    else:
                        chain_pads.update(self._find_pads_at_point(via.position, pad_positions))
                    # Connect all pads in this chain through the via
                    chain_list = list(chain_pads)
                    for i, pad in enumerate(chain_list):
                        for other in chain_list[i + 1 :]:
                            graph[pad].add(other)
                            graph[other].add(pad)

        # --- Zone / pour connectivity via per-fill-island grouping (#3914) ---
        # The previous model added every pad inside a zone *boundary* polygon
        # to one global ``zone_connected_pads`` set and connected them all to
        # each other, treating the whole zone outline as a single copper
        # component.  A pour whose fill has a discontinuity (two isolated copper
        # islands) therefore still reported every boundary-interior pad as
        # mutually connected -> a false ``complete`` even when kicad-cli
        # reported unconnected items.  We now group pads by the poured copper
        # *island* their copper actually overlaps (hole-aware solid region,
        # matching ``ConnectivityValidator.extract_pad_partition``), so a pad
        # reachable only through island A is not fused with a pad on a separate
        # island B, and a pad sitting in the zone outline but not on any real
        # fill copper is not spuriously connected at all.
        self._apply_zone_connectivity(
            net_number,
            graph,
            pad_positions,
            pad_layers,
            vias,
            segments,
            segment_components,
            net_zones,
            zone_boundaries,
        )

        # Connect pads that share a piece of copper (segments and vias only).
        # Zone polygon vertices are NOT sampled here because the pad-to-zone
        # and via-to-zone polygon containment checks above already handle zone
        # connectivity properly.  The previous zone_points[:1000] sampling cap
        # caused false-positive incomplete reports on boards with many filled
        # polygons (Issue #2035).
        #
        # Default: two pads bond when both centers land within
        # POSITION_TOLERANCE of the same segment endpoint / via center.
        # Strict (Issue #4176): two pads bond when both copper polygons
        # intersect the same segment / via copper geometry.
        if self.strict:
            copper_geoms = [self._segment_poly(seg) for seg in segments]
            copper_geoms.extend(self._via_geom(via) for via in vias)
            for geom in copper_geoms:
                touching = self._find_pads_touching_geom(geom, pad_positions)
                for i, pad_id in enumerate(touching):
                    for other_id in touching[i + 1 :]:
                        graph[pad_id].add(other_id)
                        graph[other_id].add(pad_id)
        else:
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

    def _connectivity_geometry(self) -> Any:
        """Return a cached :class:`ConnectivityValidator` for geometry reuse.

        ``NetStatusAnalyzer`` reuses the validator's hole-aware pour geometry
        (``_pad_copper_polygon`` / ``_fill_solid_region`` / ``_via_copper_geom``)
        so the zone-connectivity model here stays byte-for-byte consistent with
        the reference partition extractor (Issue #3914).  The validator is
        constructed from the already-loaded PCB object (no re-parse) and cached
        for the lifetime of this analyzer.
        """
        cv = getattr(self, "_cv_geometry", None)
        if cv is None:
            from kicad_tools.validate.connectivity import ConnectivityValidator

            cv = ConnectivityValidator(self.pcb)
            self._cv_geometry = cv
        return cv

    def _apply_zone_connectivity(
        self,
        net_number: int,
        graph: dict[str, set[str]],
        pad_positions: dict[str, tuple[float, float]],
        pad_layers: dict[str, list[str]],
        vias: list,
        segments: list,
        segment_components: list[set[int]],
        net_zones: list[tuple[str, list[list[tuple[float, float]]]]],
        zone_boundaries: list[tuple[str, list[tuple[float, float]]]],
    ) -> None:
        """Wire zone/pour copper into the graph, one group per fill island.

        Computes per-fill-island pad groups (Issue #3914) and unions each
        group into ``graph``.  Pads on separate poured copper islands are NOT
        connected, so a discontinuous fill reports ``incomplete`` instead of a
        false ``complete``.  When ``shapely`` is unavailable the hole-aware
        island geometry cannot be computed, so we fall back to the legacy
        boundary-polygon bulk-connect (one global zone component) to preserve
        pre-#3914 behaviour on core-only installs.
        """
        groups = self._build_fill_island_groups(
            net_number, pad_positions, pad_layers, vias, segments, segment_components
        )
        if groups is None:
            legacy = self._legacy_zone_connected_pads(
                pad_positions,
                pad_layers,
                vias,
                segments,
                segment_components,
                net_zones,
                zone_boundaries,
            )
            groups = [legacy] if legacy else []

        for group in groups:
            members = list(group)
            for i, pad in enumerate(members):
                for other in members[i + 1 :]:
                    graph[pad].add(other)
                    graph[other].add(pad)

    def _build_fill_island_groups(
        self,
        net_number: int,
        pad_positions: dict[str, tuple[float, float]],
        pad_layers: dict[str, list[str]],
        vias: list,
        segments: list,
        segment_components: list[set[int]],
    ) -> list[set[str]] | None:
        """Group this net's pads by the poured copper island they overlap.

        Each ``filled_polygon`` is a poured copper island.  A pad is bonded to
        a zone's fill iff its copper box overlaps the island's hole-aware solid
        region (:meth:`ConnectivityValidator._fill_solid_region`) on a matching
        copper layer -- clearance moats / thermal antipads carved out of the
        pour are real holes the pad must not be tied across.  The pad *box*
        (not its bare centre) is tested so a thermally-relieved pad, whose
        centre sits in the antipad moat but whose copper edge reaches the
        thermal spokes, still bonds.

        A zone's fill fragments are unioned into a single group: KiCad
        fragments one pour into many ``filled_polygon`` entries around thermal
        reliefs, and all fragments of one ``zone`` object are the same net and
        DRC-bonded (this avoids a false ``open`` for a pad alone in its own
        thermal fragment).  Crucially the union is **per zone object**, not
        global across all zones, so two pads covered by genuinely separate
        pours (or separate zones) are only connected when a via / trace bridges
        them.

        Vias whose copper penetrates a fill island bond the pads reached
        through them (directly, or via a segment chain ending at the via) into
        that island, closing the ``pad -> trace -> stitch via -> pour`` path.

        Segment chains whose copper *touches* a fill island bond their pads to
        it too, and chains are unified transitively through shared vias so a
        cross-layer path bonds correctly (Issue #4229): the common real-world
        shape is a signal pad on F.Cu that reaches a plane on B.Cu through
        ``pad -> F.Cu trace -> through-via -> B.Cu trace -> pour`` where the via
        itself sits in a thermal antipad (its copper does NOT penetrate the
        pour) and only the far B.Cu trace endpoint lands on the poured copper.
        The per-via penetration test alone misses that pad; bonding via the
        chain-touches-fill path recovers it, matching KiCad's zone-aware
        connectivity (kicad-cli reports 0 unconnected for exactly this case).

        Returns ``None`` when ``shapely`` is unavailable (the caller then uses
        the legacy boundary-based fallback); otherwise a list of pad-id groups,
        one per bonded fill island.
        """
        from kicad_tools._shapely import has_shapely

        if not has_shapely():
            return None

        cv = self._connectivity_geometry()

        # Board-frame eroded copper box per pad on this net.  Reuse the
        # reference validator's geometry so the model matches
        # ``extract_pad_partition`` exactly.
        pad_polys: dict[str, Any] = {}
        for fp in self.pcb.footprints:
            if not fp.reference or fp.reference.startswith("#"):
                continue
            for pad in fp.pads:
                if pad.net_number != net_number:
                    continue
                if pad.number is None or pad.number == "":
                    continue
                pad_id = f"{fp.reference}.{pad.number}"
                if pad_id not in pad_positions:
                    continue
                poly = cv._pad_copper_polygon(fp, pad)
                if poly is not None:
                    pad_polys[pad_id] = poly

        # Copper-circle geometry per via.  Two variants:
        #  * ``via_geom`` (eroded, ``POUR_PAD_ERODE`` inset) is used for the
        #    fill-*penetration* test so a via merely grazing the pour edge does
        #    not spuriously bond -- matching ``ConnectivityValidator``.
        #  * ``via_raw`` (un-eroded, real copper radius) is used only to bond a
        #    *same-net* pad to a via that already penetrates the pour.  The
        #    erosion inset on both the pad box and the via disc otherwise opens
        #    a sub-``2*POUR_PAD_ERODE`` false gap between a stitching via and an
        #    adjacent same-net pad whose copper genuinely overlaps it (the
        #    board-06 U1.32 case, Issue #4229).  Using the raw radius here is
        #    safe: the group is single-net, so a looser pad<->via bond can only
        #    unify pads that are already the same net -- it can never manufacture
        #    a cross-net short (that is what the eroded penetration test guards).
        from shapely.geometry import Point as _ShapelyPoint  # type: ignore[import-untyped]

        via_geoms = []
        for via in vias:
            radius = max(getattr(via, "size", 0.0) or 0.0, 0.0) / 2.0
            eroded = cv._via_copper_geom(via.position, radius)
            raw = _ShapelyPoint(*via.position).buffer(radius) if radius > 0 else None
            via_geoms.append((via, eroded, raw))

        # Unify segment components that share a via into "extended chains"
        # (Issue #4229).  ``_build_segment_components`` chains segments only
        # when their own copper touches, so a through-via joining an F.Cu trace
        # to a B.Cu trace leaves them in separate components even though they
        # are one electrical net.  Merging components that both touch the same
        # via recovers the cross-layer ``pad -> trace -> via -> trace -> pour``
        # path so a pad on one layer bonds to a pour on another.
        extended_chains = self._merge_chains_via_vias(segments, segment_components, vias)

        # Pre-compute, per extended chain, the pads it reaches and the copper
        # layers/geometry it presents to the pour tests.
        chain_pads: list[set[str]] = []
        chain_seg_indices: list[set[int]] = []
        for chain in extended_chains:
            pads_in_chain: set[str] = set()
            for s in chain:
                pads_in_chain.update(self._find_pads_at_point(segments[s].start, pad_positions))
                pads_in_chain.update(self._find_pads_at_point(segments[s].end, pad_positions))
            chain_pads.append(pads_in_chain)
            chain_seg_indices.append(chain)

        groups: list[set[str]] = []
        for zone in self.pcb.zones:
            if zone.net_number != net_number or not zone.filled_polygons:
                continue
            bonded: set[str] = set()
            for i, fill_pts in enumerate(zone.filled_polygons):
                region = cv._fill_solid_region(fill_pts)
                if region is None:
                    continue
                fill_layer = zone.filled_polygon_layer(i)

                # Direct pad bonds: pad copper box overlaps the solid fill.
                for pad_id, pad_geom in pad_polys.items():
                    if not self._pad_layer_matches_zone(pad_layers.get(pad_id, []), fill_layer):
                        continue
                    if region.intersects(pad_geom):
                        bonded.add(pad_id)

                # Via bonds: a via whose copper penetrates this fill island ties
                # the pads reached through it (directly, through a same-net pad
                # whose copper overlaps the via, or via a segment chain ending
                # at the via) into the same island.
                for via, via_geom, via_raw in via_geoms:
                    if not self._via_spans_layer(via.layers, fill_layer):
                        continue
                    if not region.intersects(via_geom):
                        continue
                    bonded.update(self._find_pads_at_point(via.position, pad_positions))
                    # Same-net pad whose real copper overlaps this pour-bonded
                    # via (raw radius -- see via_geoms note): the stitching via
                    # sits beside the pad, not dead-centre (board-06 U1.32).
                    if via_raw is not None:
                        for pad_id, pad_geom in pad_polys.items():
                            if pad_id in bonded:
                                continue
                            if via_raw.intersects(pad_geom):
                                bonded.add(pad_id)
                    for chain, pads_in_chain in zip(chain_seg_indices, chain_pads, strict=True):
                        touches = any(
                            self._segment_touches_via(segments[s], via, via_geom) for s in chain
                        )
                        if touches:
                            bonded.update(pads_in_chain)

                # Segment-chain bonds: an extended chain whose own copper touches
                # this fill island (a trace endpoint lands on the poured copper)
                # ties every pad reachable through the chain into the island.
                # This recovers the board-06/board-05 plane-pad case where the
                # via sits in an antipad and only the far trace reaches the pour
                # (Issue #4229).
                for chain, pads_in_chain in zip(chain_seg_indices, chain_pads, strict=True):
                    if not pads_in_chain:
                        continue
                    if any(
                        self._via_spans_layer([segments[s].layer], fill_layer)
                        and region.intersects(self._segment_poly(segments[s]))
                        for s in chain
                    ):
                        bonded.update(pads_in_chain)

            if bonded:
                groups.append(bonded)
        return groups

    def _merge_chains_via_vias(
        self,
        segments: list,
        segment_components: list[set[int]],
        vias: list,
    ) -> list[set[int]]:
        """Merge segment components that share a via into extended chains.

        ``_build_segment_components`` only chains segments whose own copper
        touches, so two traces on different layers joined solely by a
        through-via land in separate components.  KiCad treats them as one
        electrical chain; this union-find over "components that both touch the
        same via" reproduces that so a cross-layer ``pad -> trace -> via ->
        trace -> pour`` path is recognised as connected (Issue #4229).

        The merge is purely additive connectivity: it never fuses two chains
        that don't share a via, so genuinely separate copper stays separate and
        the #3914 "don't union disjoint fills" guarantee is unaffected.
        """
        n = len(segment_components)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for via in vias:
            via_geom = self._via_geom(via)
            touching: list[int] = []
            for ci, component in enumerate(segment_components):
                if any(self._segment_touches_via(segments[s], via, via_geom) for s in component):
                    touching.append(ci)
            for other in touching[1:]:
                union(touching[0], other)

        merged: dict[int, set[int]] = defaultdict(set)
        for ci, component in enumerate(segment_components):
            merged[find(ci)].update(component)
        return list(merged.values())

    def _segment_touches_via(self, seg: Any, via: Any, via_geom: Any) -> bool:
        """Return ``True`` when a segment is electrically joined to a via.

        A segment endpoint within ``POSITION_TOLERANCE`` of the via centre is a
        clean join, but a real board frequently lands a trace endpoint on the
        via *annular ring* rather than dead-centre (Issue #4229): the endpoint
        may sit >tolerance from the centre yet still be covered by the via's
        copper.  When shapely geometry is available we additionally treat the
        via as joined to the segment when the via copper disc intersects the
        segment copper, which matches KiCad's connectivity.
        """
        if self._points_close(seg.start, via.position) or self._points_close(seg.end, via.position):
            return True
        if via_geom is None:
            return False
        seg_poly = self._segment_poly(seg)
        return self._geoms_touch(seg_poly, via_geom)

    def _legacy_zone_connected_pads(
        self,
        pad_positions: dict[str, tuple[float, float]],
        pad_layers: dict[str, list[str]],
        vias: list,
        segments: list,
        segment_components: list[set[int]],
        net_zones: list[tuple[str, list[list[tuple[float, float]]]]],
        zone_boundaries: list[tuple[str, list[tuple[float, float]]]],
    ) -> set[str]:
        """Legacy boundary-based zone connectivity (shapely-absent fallback).

        Reproduces the pre-#3914 behaviour: every pad inside a zone boundary or
        filled polygon (plus pads reached through vias / segment chains that
        touch a zone) is collected into one global set that is then fully
        interconnected.  This is only used when ``shapely`` is unavailable and
        the hole-aware per-island model cannot run.
        """
        boundary_bboxes = [
            (layer, boundary, self._polygon_bbox(boundary)) for layer, boundary in zone_boundaries
        ]
        filled_bboxes = [
            (layer, polys, [self._polygon_bbox(p) for p in polys]) for layer, polys in net_zones
        ]

        zone_connection_points: set[tuple[float, float]] = set()

        for zone_layer, boundary, bbox in boundary_bboxes:
            for via in vias:
                if not self._via_spans_layer(via.layers, zone_layer):
                    continue
                if not self._point_in_bbox(via.position, bbox):
                    continue
                if self._point_in_polygon(via.position, boundary):
                    zone_connection_points.add(via.position)

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

        zone_connected_pads: set[str] = set()

        for zcp in zone_connection_points:
            zone_connected_pads.update(self._find_pads_at_point(zcp, pad_positions))

        for pad_id, pad_pos in pad_positions.items():
            layers = pad_layers.get(pad_id, [])
            for zone_layer, boundary, bbox in boundary_bboxes:
                if not self._pad_layer_matches_zone(layers, zone_layer):
                    continue
                if not self._point_in_bbox(pad_pos, bbox):
                    continue
                if self._point_in_polygon(pad_pos, boundary):
                    zone_connected_pads.add(pad_id)
                    break
            for zone_layer, filled_polys, poly_bboxes in filled_bboxes:
                if not self._pad_layer_matches_zone(layers, zone_layer):
                    continue
                for poly, bbox in zip(filled_polys, poly_bboxes, strict=False):
                    if not self._point_in_bbox(pad_pos, bbox):
                        continue
                    if self._point_in_polygon(pad_pos, poly):
                        zone_connected_pads.add(pad_id)
                        break

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
                for seg_idx in component:
                    seg = segments[seg_idx]
                    zone_connected_pads.update(self._find_pads_at_point(seg.start, pad_positions))
                    zone_connected_pads.update(self._find_pads_at_point(seg.end, pad_positions))

        return zone_connected_pads

    def _build_segment_components(self, segments: list) -> list[set[int]]:
        """Build connected components of segments.

        Args:
            segments: List of trace segments

        Returns:
            List of sets, each containing segment indices in a connected component

        In the default mode two segments are chained when any of their
        endpoints land within ``POSITION_TOLERANCE`` of each other.  In
        ``strict`` mode (Issue #4176) they are chained iff their real copper
        polygons (each segment's centerline buffered by ``width / 2``)
        intersect — matching KiCad, which does not bond two traces whose
        copper does not physically touch even when their endpoints are within
        the snap tolerance.
        """
        if not segments:
            return []

        if self.strict:
            polys = [self._segment_poly(seg) for seg in segments]

        # Build segment adjacency graph
        segment_graph: dict[int, set[int]] = defaultdict(set)
        for i, seg_a in enumerate(segments):
            for j, seg_b in enumerate(segments):
                if i != j:
                    if self.strict:
                        connected = self._geoms_touch(polys[i], polys[j])
                    else:
                        connected = (
                            self._points_close(seg_a.start, seg_b.start)
                            or self._points_close(seg_a.start, seg_b.end)
                            or self._points_close(seg_a.end, seg_b.start)
                            or self._points_close(seg_a.end, seg_b.end)
                        )
                    if connected:
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

    # ------------------------------------------------------------------
    # Strict-mode real-geometry copper contact (Issue #4176)
    # ------------------------------------------------------------------
    # These helpers back ``strict=True`` connectivity: segment↔segment,
    # segment↔pad, and segment↔via unions require the copper *shapes* to
    # actually intersect (KiCad semantics) rather than the endpoint-proximity
    # radius the default model uses.  Pad and via copper polygons are reused
    # from ``ConnectivityValidator`` (``_pad_copper_polygon`` / ``_via_copper_geom``)
    # via ``_connectivity_geometry()``; the trace-segment polygon is the one
    # missing primitive, provided by ``geometry.copper.segment_copper_polygon``.
    # All caches are keyed off the object ``id`` of the segment/via so repeated
    # per-net calls do not rebuild the same polygon.

    @staticmethod
    def _geoms_touch(a: Any, b: Any) -> bool:
        """Return ``True`` when two shapely geometries intersect/touch.

        ``None`` (a degenerate/unbuildable geometry) never touches anything.
        """
        if a is None or b is None:
            return False
        return bool(a.intersects(b))

    def _segment_poly(self, seg: Any) -> Any:
        """Cached real copper polygon for a trace segment (strict mode)."""
        cache = self._segment_poly_cache
        key = id(seg)
        poly = cache.get(key)
        if poly is None and key not in cache:
            from kicad_tools.geometry.copper import segment_copper_polygon

            poly = segment_copper_polygon(seg.start, seg.end, seg.width)
            cache[key] = poly
        return poly

    def _via_geom(self, via: Any) -> Any:
        """Cached real copper geometry for a via (strict mode).

        Reuses :meth:`ConnectivityValidator._via_copper_geom` so the via copper
        model stays consistent with the reference partition extractor.
        """
        cache = self._via_geom_cache
        key = id(via)
        geom = cache.get(key)
        if geom is None and key not in cache:
            cv = self._connectivity_geometry()
            radius = max(getattr(via, "size", 0.0) or 0.0, 0.0) / 2.0
            geom = cv._via_copper_geom(via.position, radius)
            cache[key] = geom
        return geom

    def _pad_polys(self) -> dict[str, Any]:
        """Board-frame copper polygon per pad, keyed by ``REF.PAD`` (strict).

        Built once per analyzer over every footprint pad, reusing
        :meth:`ConnectivityValidator._pad_copper_polygon` for shape/rotation
        parity with the reference partition extractor.  Pads with unbuildable
        geometry are stored as ``None`` and simply never match.
        """
        if self._pad_poly_cache is not None:
            return self._pad_poly_cache
        cache: dict[str, Any] = {}
        cv = self._connectivity_geometry()
        for fp in self.pcb.footprints:
            if not fp.reference or fp.reference.startswith("#"):
                continue
            for pad in fp.pads:
                if pad.number is None or pad.number == "":
                    continue
                cache[f"{fp.reference}.{pad.number}"] = cv._pad_copper_polygon(fp, pad)
        self._pad_poly_cache = cache
        return cache

    def _find_pads_touching_geom(
        self,
        geom: Any,
        pad_positions: dict[str, tuple[float, float]],
    ) -> list[str]:
        """Strict analogue of ``_find_pads_at_point``: pads whose copper touches ``geom``.

        Only pads present in ``pad_positions`` (i.e. on the net being analyzed)
        are considered, matching the point-based helper's contract.
        """
        if geom is None:
            return []
        pad_polys = self._pad_polys()
        return [
            pad_id for pad_id in pad_positions if self._geoms_touch(geom, pad_polys.get(pad_id))
        ]

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

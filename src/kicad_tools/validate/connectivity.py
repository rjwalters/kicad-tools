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

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kicad_tools.validate.spatial import candidate_pairs

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

# Geometry backend.  ``shapely`` is a core dependency (issue #3824), but
# this module keeps a graceful fallback: when shapely is unavailable (a
# broken/partial install) the label-free pour extractor
# (``_connect_pour_pads_label_free`` / step 2d) transparently falls back to
# the legacy declared-net pour grouping, so importing this module — and
# tracing autorouter segment/via copper — never hard-fails.  The boolean
# probe is delegated to the shared guard in :mod:`kicad_tools._shapely`.
from kicad_tools._shapely import has_shapely as _has_shapely

# Shared, checker-agnostic trace-copper geometry (issue #4176).  These are
# pure analytic helpers with no shapely dependency, so same-layer track
# contact is decided identically on core-only installs.
from kicad_tools.geometry.copper import (
    point_segment_distance as _point_segment_distance_impl,
)
from kicad_tools.geometry.copper import (
    segments_copper_touch,
)

if _has_shapely():  # pragma: no cover - import guard exercised by environment
    from shapely.geometry import LineString as _ShapelyLineString  # type: ignore[import-untyped]
    from shapely.geometry import Point as _ShapelyPoint
    from shapely.geometry import Polygon as _ShapelyPolygon


# Sentinel layer-set meaning "every copper layer".  Used to model a ``*.Cu``
# through-hole pad as a universal copper bridge in the layer-aware segment
# chainer (issue #3783): a multi-layer pad joins copper on any layer at its
# position, so any two copper segments meeting there are fused regardless of
# their individual layers.
_ALL_COPPER_LAYERS: frozenset[str] = frozenset({"*.Cu"})


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
        valid_types = ("unrouted", "partial", "isolated", "zone_island")
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
    def zone_islands(self) -> list[ConnectivityIssue]:
        """Filled zone islands with no same-net copper attachment."""
        return [i for i in self.issues if i.issue_type == "zone_island"]

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
        if self.zone_islands:
            parts.append(f"  Orphaned zone islands: {len(self.zone_islands)}")
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
            self.pcb_path: Path | None = Path(pcb)
            self.pcb = PCBClass.load(str(pcb))
        else:
            self.pcb_path = None
            self.pcb = pcb
        self._refresh_pad_identities()

    def _refresh_pad_identities(self) -> None:
        """Index physical occurrences independently of optional KiCad UUIDs."""
        pads = [
            (fi, pi, fp.reference, str(pad.number))
            for fi, fp in enumerate(self.pcb.footprints)
            if fp.reference and not fp.reference.startswith("#")
            for pi, pad in enumerate(fp.pads)
            if pad.number is not None and pad.number != ""
        ]
        counts = Counter(f"{ref}.{number}" for _, _, ref, number in pads)
        self._pad_ids = {}
        self.pad_bindings: dict[str, tuple[str, str]] = {}
        for fi, pi, ref, number in pads:
            logical = f"{ref}.{number}"
            node = logical if counts[logical] == 1 else f"__pad:{fi}:{pi}"
            self._pad_ids[fi, pi] = node
            self.pad_bindings[node] = (ref, number)

    def _pad_id(self, footprint_index: int, pad_index: int) -> str:
        return self._pad_ids[footprint_index, pad_index]

    def _pad_display(self, node: str) -> str:
        binding = self.pad_bindings.get(node)
        return ".".join(binding) if binding is not None else node

    def validate(self, *, reconcile_native: bool = False) -> ConnectivityResult:
        """Run connectivity validation on all nets.

        Returns:
            ConnectivityResult containing all issues found
        """
        self._refresh_pad_identities()
        result = ConnectivityResult()

        # Issue #4498: model every same-net copper item as a graph node on
        # zone-bearing nets.  The old pad partition collapsed an entire zone
        # after the first pad touched it, hiding zone↔zone, zone↔pad and
        # pad↔pad disconnects within the same declared net.
        item_relationships = self._find_unconnected_item_relationships()
        for issue in (issue for issues in item_relationships.values() for issue in issues):
            result.add(issue)

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
            if net_number in item_relationships:
                if not item_relationships[net_number]:
                    connected_count += 1
                    zone_connected_count += 1
                continue

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
        if reconcile_native:
            # Issue #4551: map native relationships onto the internal
            # classification instead of discarding it.  The per-net lookup
            # carries the internal ``unrouted``/``partial``/``isolated``
            # verdicts so that a native pad-bearing relationship keeps its
            # internal type (and the reconciled result's per-type counts stay
            # populated) while the native relationship *count* remains
            # authoritative for the opt-in path (#4498 fleet parity).
            internal_types = {
                issue.net_name: issue.issue_type
                for issue in result.issues
                if issue.issue_type in ("unrouted", "partial", "isolated")
            }
            native_issues = self._native_unconnected_relationships(
                internal_types=internal_types,
                internal_issue_count=len(result.issues),
            )
            if native_issues is not None:
                result.issues = native_issues
        return result

    def _native_unconnected_relationships(
        self,
        internal_types: dict[str, str] | None = None,
        internal_issue_count: int | None = None,
    ) -> list[ConnectivityIssue] | None:
        """Return KiCad's per-item relationships, or ``None`` when unavailable.

        This is the explicit high-fidelity reconciliation path permitted by
        #4498.  The internal graph remains available in KiCad-less installs;
        callers that require native parity opt in with
        ``validate(reconcile_native=True)``.

        Fleet characterization (2026-07-30): the internal graph exactly
        reproduces board03's 12 relationships and reports 60 on board05 versus
        KiCad's 57.  The residual three are thermal-spoke associations that are
        not represented as explicit solid geometry in the persisted board.
        Refill-time connectivity is therefore authoritative for the opt-in
        path; it reports board05's exact current 57 without weakening the
        KiCad-less fail-visible model.

        Args:
            internal_types: Optional ``net_name -> issue_type`` lookup built
                from the internal classification (``unrouted`` / ``partial``
                / ``isolated``).  A native relationship with a pad endpoint
                inherits the internal type for its net; zone-only
                relationships (and nets the internal analysis did not flag)
                keep ``zone_island``.
            internal_issue_count: Number of internal issues that will be
                silently substituted if this method returns ``None``.  Only
                used to make the unexpected-exit-code warning actionable.

        Returns ``None`` on the two *expected* degradations (PCB passed as an
        object, kicad-cli not installed) without emitting anything; an
        *unexpected* kicad-cli exit code also returns ``None`` but emits a
        ``RuntimeWarning`` naming the exit code and the substituted internal
        count (issue #4551 — the silent fallback made a kicad-cli crash
        indistinguishable from the documented KiCad-less path).
        """
        if self.pcb_path is None:
            return None

        import subprocess
        import tempfile

        from kicad_tools.cli.runner import find_kicad_cli
        from kicad_tools.drc import DRCReport

        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            return None
        with tempfile.NamedTemporaryFile(suffix=".json") as report_file:
            proc = subprocess.run(
                [
                    str(kicad_cli),
                    "pcb",
                    "drc",
                    "--refill-zones",
                    "--format",
                    "json",
                    "-o",
                    report_file.name,
                    str(self.pcb_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if proc.returncode not in (0, 5):
                import warnings

                substituted = (
                    f"; substituting the internal analysis ({internal_issue_count} issue(s))"
                    if internal_issue_count is not None
                    else ""
                )
                warnings.warn(
                    "kicad-cli pcb drc exited with unexpected code "
                    f"{proc.returncode} during native connectivity "
                    f"reconciliation{substituted}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                return None
            report = DRCReport.load(report_file.name)

        # Connectivity relationships live in the report's dedicated
        # ``unconnected_items`` collection, NOT in ``violations`` -- the
        # geometric collection keeps its pre-#4498 meaning for every
        # ``run_geometric_drc`` consumer.
        lookup = internal_types or {}
        issues: list[ConnectivityIssue] = []
        for index, violation in enumerate(report.connectivity_items()):
            item_ids = tuple(violation.items) or (f"native-unconnected[{index}]",)
            net_name = violation.nets[0] if violation.nets else "<native>"
            # Issue #4551: classify by endpoint kind + internal lookup rather
            # than hardcoding zone_island.  Zone-only relationships keep
            # today's meaning; a relationship with a pad endpoint inherits the
            # internal classification for its net when one exists, so the
            # reconciled result's unrouted/partial/isolated views stay
            # populated on unrouted boards.
            pad_endpoints = tuple(
                item_id for item_id in violation.items if not item_id.startswith("Zone ")
            )
            issue_type = lookup.get(net_name, "zone_island") if pad_endpoints else "zone_island"
            # kicad-cli's top-level description is the generic "Missing
            # connection between items"; the real endpoint refs live in the
            # per-item descriptions.  Compose them into the message so the
            # default table, JSON, and summary() all show actual pad/zone
            # references (issue #4551).
            message = violation.message
            if message == "Missing connection between items" and len(item_ids) >= 2:
                message = f"Missing connection between {item_ids[0]} and {item_ids[1]}"
            issues.append(
                ConnectivityIssue(
                    severity="error",
                    issue_type=issue_type,
                    net_name=net_name,
                    message=message,
                    suggestion="Connect the items reported by kicad-cli",
                    unconnected_pads=pad_endpoints,
                    islands=tuple((item_id,) for item_id in item_ids),
                )
            )
        return issues

    def _find_unconnected_item_relationships(self) -> dict[int, list[ConnectivityIssue]]:
        """Return missing relationships between physical same-net components.

        Pads, vias, segments, and each individual ``filled_polygon`` are graph
        nodes.  Nodes union only when their copper intersects on a shared layer;
        a via or through-hole pad is one node carrying all bridged layers.  For
        each zone-bearing net, one issue is emitted for every component beyond
        the first, matching KiCad's ratsnest relationship granularity instead
        of emitting one coarse issue per net.

        The label-only fallback cannot distinguish filled islands and retains
        the pre-#4498 pad model when Shapely is unavailable.
        """
        if not _has_shapely():
            return {}

        from kicad_tools.geometry.copper import segment_copper_polygon

        modeled_nets = {
            zone.net_number for zone in self.pcb.zones if zone.net_number and zone.filled_polygons
        }
        relationships: dict[int, list[ConnectivityIssue]] = {}
        copper_layers = [layer.name for layer in self.pcb.copper_layers] or ["F.Cu", "B.Cu"]

        for net_number in modeled_nets:
            # (stable id, human description, {layer: solid geometry}, terminal)
            items: list[tuple[str, str, dict[str, Any], bool]] = []
            pad_records: list[tuple[int, list[str], Any]] = []
            zone_records: list[tuple[Any, str, list[int]]] = []
            for fp in self.pcb.footprints:
                for pad in fp.pads:
                    if pad.net_number != net_number:
                        continue
                    geom = self._pad_copper_polygon(fp, pad)
                    if geom is not None:
                        layers = [
                            layer
                            for layer in copper_layers
                            if self._pad_layer_matches_zone(pad.layers, layer)
                        ]
                        pad_id = f"{fp.reference}.{pad.number}"
                        items.append(
                            (
                                f"pad:{pad_id}",
                                f"Pad {pad_id}",
                                dict.fromkeys(layers, geom),
                                True,
                            )
                        )
                        pad_records.append((len(items) - 1, pad.layers, geom))

            for via_index, via in enumerate(self.pcb.vias):
                if via.net_number != net_number:
                    continue
                radius = max(getattr(via, "size", 0.0) or 0.0, 0.0) / 2.0
                geom = self._via_copper_geom(via.position, radius)
                if geom is not None:
                    items.append(
                        (
                            f"via:{via_index}",
                            f"Via {via_index}",
                            dict.fromkeys(self._via_bridged_layers(via.layers), geom),
                            False,
                        )
                    )

            for seg_index, seg in enumerate(self.pcb.segments):
                if seg.net_number != net_number:
                    continue
                geom = segment_copper_polygon(seg.start, seg.end, seg.width)
                if geom is not None:
                    items.append(
                        (
                            f"segment:{seg_index}",
                            f"Segment {seg_index}",
                            {seg.layer: geom},
                            False,
                        )
                    )

            for zone_index, zone in enumerate(self.pcb.zones):
                if zone.net_number != net_number:
                    continue
                fill_indices: list[int] = []
                for fill_index, fill_pts in enumerate(zone.filled_polygons):
                    region = self._fill_solid_region(fill_pts)
                    if region is None:
                        continue
                    layer = zone.filled_polygon_layer(fill_index)
                    item_id = f"zone[{zone_index}].fill[{fill_index}]@{layer}"
                    items.append((item_id, f"Zone island {item_id}", {layer: region}, True))
                    fill_indices.append(len(items) - 1)
                boundary = self._fill_solid_region(zone.polygon)
                if boundary is not None and fill_indices:
                    zone_records.append((boundary, zone.layer, fill_indices))

            parent = list(range(len(items)))

            def find(index: int) -> int:
                while parent[index] != index:
                    parent[index] = parent[parent[index]]
                    index = parent[index]
                return index

            def union(left: int, right: int) -> None:
                left_root, right_root = find(left), find(right)
                if left_root != right_root:
                    parent[right_root] = left_root

            for left, (_, _, left_geoms, _) in enumerate(items):
                for right in range(left + 1, len(items)):
                    right_geoms = items[right][2]
                    if any(
                        left_geoms[layer].intersects(right_geoms[layer])
                        for layer in left_geoms.keys() & right_geoms.keys()
                    ):
                        union(left, right)

            # Persisted fill polygons omit thermal spokes around same-net pads.
            # Preserve the established #479 boundary behavior without
            # collapsing every island in the zone: a pad inside the zone
            # boundary bonds only to the nearest fill island on that layer.
            for boundary, layer, fill_indices in zone_records:
                layer_fills = [index for index in fill_indices if layer in items[index][2]]
                if not layer_fills:
                    continue
                for pad_index, pad_layers, pad_geom in pad_records:
                    if not self._pad_layer_matches_zone(pad_layers, layer):
                        continue
                    if not boundary.intersects(pad_geom):
                        continue
                    nearest = min(
                        layer_fills,
                        key=lambda index: pad_geom.distance(items[index][2][layer]),
                    )
                    union(pad_index, nearest)

            components: dict[int, list[int]] = {}
            for index in range(len(items)):
                components.setdefault(find(index), []).append(index)
            # KiCad ratsnest relationships are between electrically meaningful
            # endpoints (pads and zone islands); track/via-only fragments merely
            # carry connectivity between them.
            terminal_components = [
                component
                for component in components.values()
                if any(items[index][3] for index in component)
            ]
            terminal_components.sort(
                key=lambda component: min(items[index][0] for index in component)
            )

            net = self.pcb.nets.get(net_number)
            net_name = net.name if net is not None else f"net-{net_number}"
            issues: list[ConnectivityIssue] = []
            if terminal_components:
                anchor = terminal_components[0]
                anchor_item = next(index for index in anchor if items[index][3])
                for component in terminal_components[1:]:
                    remote_item = next(index for index in component if items[index][3])
                    left_id, left_desc = items[anchor_item][0:2]
                    right_id, right_desc = items[remote_item][0:2]
                    issues.append(
                        ConnectivityIssue(
                            severity="error",
                            issue_type="zone_island",
                            net_name=net_name,
                            message=f"Missing connection between {left_desc} and {right_desc}",
                            suggestion="Connect the same-net copper components",
                            islands=((left_id,), (right_id,)),
                        )
                    )
            relationships[net_number] = issues

        return relationships

    def extract_pad_partition(self) -> list[frozenset[str]]:
        """Return logical REF.PAD groups, retaining distinct physical islands.

        A duplicate pad number may appear in several groups. Consumers must
        not merge those groups by logical identity. For occurrence counts and
        explicit schematic bindings use :meth:`extract_pad_occurrences`.
        Unique-number boards retain the historical representation.
        """
        groups, bindings = self.extract_pad_occurrences()
        return [frozenset(".".join(bindings[node]) for node in group) for group in groups]

    def extract_pad_occurrences(
        self,
    ) -> tuple[list[frozenset[str]], dict[str, tuple[str, str]]]:
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
            A pair of occurrence groups and an explicit occurrence-to-(ref, pin)
            binding dictionary. Every footprint pad with a nonempty number and a
            non-comment reference appears in exactly one group.  A pad with
            no copper touching it forms a singleton group.  Groups are
            returned sorted by their smallest member for determinism.
        """
        self._refresh_pad_identities()
        # 1. Collect every pad on the board (label-independent) with its
        #    board-frame position and layer set.
        pad_positions: dict[str, tuple[float, float]] = {}
        pad_layers: dict[str, list[str]] = {}
        for fi, fp in enumerate(self.pcb.footprints):
            if not fp.reference or fp.reference.startswith("#"):
                continue
            fp_x, fp_y = fp.position
            rotation = fp.rotation
            for pi, pad in enumerate(fp.pads):
                if pad.number is None or pad.number == "":
                    continue
                pad_id = self._pad_id(fi, pi)
                pad_positions[pad_id] = self._transform_pad_position(
                    pad.position, fp_x, fp_y, rotation
                )
                pad_layers[pad_id] = pad.layers

        # 1b. Synthetic via *nodes* (issue #3794).  A via that lands inside a
        #     pour's solid region is the bridge a pad reaches the pour through
        #     when the pour is on a *different* layer than the pad (the
        #     ``pad -> F.Cu trace -> stitch via -> B.Cu pour`` path on board
        #     04, whose GND pour is B.Cu-only).  Steps 2a–2c only union *pads*,
        #     and step 2d only tests *pad* boxes against the pour — so a via
        #     offset from its pad and reached by a trace is invisible to the
        #     pour bond and the GND pads strand as false ``opens``.
        #
        #     We model each via as a first-class graph node placed at the via
        #     position and carrying the via's bridged copper layer span.  The
        #     existing segment chainer (2b) then connects any pad whose trace
        #     ends at the via to that node, and the pour bonder (2d) unions a
        #     via node that overlaps a pour's solid region into that island —
        #     so the pads chained to the via inherit the pour bond.  Via nodes
        #     are flagged in ``synthetic_nodes`` and dropped from the returned
        #     partition (3); they only carry connectivity, they are not pads.
        #
        #     The #5133 pour graph tests actual via annuli against hole-aware
        #     solids on spanned layers. A zone label or an XY-only match is
        #     never a substitute for physical fill contact.
        synthetic_nodes: set[str] = set()
        for via_index, via in enumerate(self.pcb.vias):
            node_id = f"__via{via_index}"
            pad_positions[node_id] = via.position
            pad_layers[node_id] = sorted(self._via_bridged_layers(via.layers))
            synthetic_nodes.add(node_id)

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
        #
        #     Endpoint→pad matching is LAYER-AWARE (softstart false-short
        #     fix): a segment endpoint only bonds a pad whose copper exists
        #     on the segment's layer.  A B.Cu / inner-layer trace that ends
        #     directly under an F.Cu-only SMD pad's XY is a legal, DRC-clean
        #     routing pattern with NO copper contact — matching it by XY
        #     alone fused foreign nets (e.g. a SRC_POS B.Cu trace ending
        #     under U5.1's VGATE pad merged VGATE↔SRC_POS↔SRC_NEG on a
        #     DRC-clean board).  Through-hole pads (``*.Cu``) and synthetic
        #     via nodes (expanded copper span) still bridge every layer
        #     they span.
        segments = list(self.pcb.segments)
        for seg in segments:
            start_pads = self._find_pads_at_point(
                seg.start, pad_positions, pad_layers=pad_layers, layer=seg.layer
            )
            end_pads = self._find_pads_at_point(
                seg.end, pad_positions, pad_layers=pad_layers, layer=seg.layer
            )
            for sp in start_pads:
                for ep in end_pads:
                    _connect(sp, ep)
            for group in (start_pads, end_pads):
                for i, p in enumerate(group):
                    for other in group[i + 1 :]:
                        _connect(p, other)

        # 2a2. Via-barrel/track overlap (softstart false-open fix).  A track
        #      that passes over (or ends near) a via so that its swept copper
        #      overlaps the via barrel is galvanically bonded to the via even
        #      though no *endpoint* coincides with the via centre — KiCad's
        #      own connectivity treats this as connected and routers emit it
        #      (e.g. softstart NRST_FS_POS hops F.Cu→In2.Cu through a thru
        #      via that both tracks merely graze mid-segment).  Bond the
        #      via's synthetic node into every layer-compatible segment that
        #      overlaps its barrel; the chain builder below then carries the
        #      connectivity across the whole chain.  A strictly positive
        #      contact depth (> 1 µm) is required, so copper separated by a
        #      real clearance moat (≥ 0.1 mm on any DRC-clean board) can
        #      never fuse.
        segment_extra_nodes: dict[int, set[str]] = {}
        for via_index, via in enumerate(self.pcb.vias):
            node_id = f"__via{via_index}"
            via_span = self._via_bridged_layers(via.layers)
            via_radius = (getattr(via, "size", 0.0) or 0.0) / 2.0
            if via_radius <= 0:
                continue
            for seg_index, seg in enumerate(segments):
                if seg.layer not in via_span:
                    continue
                reach = via_radius + (seg.width or 0.0) / 2.0 - 1e-3
                if reach <= 0:
                    continue
                dist = self._point_segment_distance(via.position, seg.start, seg.end)
                if dist < reach:
                    segment_extra_nodes.setdefault(seg_index, set()).add(node_id)
                    for pad_id in self._find_pads_at_point(
                        seg.start, pad_positions, pad_layers=pad_layers, layer=seg.layer
                    ) + self._find_pads_at_point(
                        seg.end, pad_positions, pad_layers=pad_layers, layer=seg.layer
                    ):
                        _connect(node_id, pad_id)

        # 2a3. Geometric segment-in-pad-copper bonding (issues #4678, #5060).
        #      Step 2a only bonds a segment ENDPOINT to a pad whose *center*
        #      is within ``POSITION_TOLERANCE`` (0.01 mm) of it.  A trace that
        #      legally terminates inside a pad's copper but away from the pad
        #      center — routers do this routinely on wide pads — was invisible
        #      to that test (#4678, the tapeout Gate 1 wedge), and so was a pad
        #      straddled by the *interior* of a continuous trace running past
        #      it (#5060, board 09's inline SOIC pins on +3V3 / PMOS_SOURCE).
        #      Bond a pad to any segment whose swept copper reaches into its
        #      eroded copper outline on a shared copper layer, measured over
        #      the segment's whole length so the result is invariant under
        #      splitting the trace at the pad.  The ``POUR_PAD_ERODE`` inset
        #      plus the ``> 1 µm`` penetration guard (mirroring steps 2c2 /
        #      2a2) mean copper across a real clearance moat (≥ 0.1 mm on any
        #      DRC-clean board) can never fuse: only true galvanic contact is
        #      added, exactly as kicad-cli and the strict net-status model
        #      report it.  Bonds are injected via ``segment_extra_nodes`` so
        #      the chain builder (2b) carries them across the whole chain.
        #      Requires shapely for the eroded pad outlines; core-only
        #      installs keep the legacy center-proximity behavior.
        if _has_shapely():
            self._connect_segment_in_pad(segments, pad_layers, segment_extra_nodes)

        # 2b. Segment chains: pads connected through a chain of segments whose
        #     copper physically touches are galvanically connected even with
        #     no pad at the intermediate junctions.  Reuse the existing chain
        #     builder, which is itself label-agnostic (it only looks at
        #     geometry, never at net numbers).  Contact is decided over each
        #     segment's full swept copper (issue #5060), so a mid-track
        #     T-branch chains whether or not the trunk carries an explicit
        #     vertex there.
        #     It is layer-aware (issue #3783): cross-layer hops require a via /
        #     multi-layer pad bridge, so pad_layers is passed through.
        #     ``segment_extra_nodes`` carries the 2a2 via-barrel and 2a3
        #     segment-in-pad bonds into each segment's chain component.
        graph = self._build_segment_chains(
            segments,
            pad_positions,
            graph,
            pad_layers,
            segment_extra_nodes=segment_extra_nodes,
        )

        # 2c. Vias: pads coincident with a via are connected (layer bridge).
        for via in self.pcb.vias:
            via_span = self._via_bridged_layers(via.layers)
            via_pads = [
                pad_id
                for pad_id in self._find_pads_at_point(via.position, pad_positions)
                if pad_layers is None
                or any(
                    layer in via_span
                    for layer in self._copper_layers_of(pad_layers.get(pad_id, []))
                )
            ]
            for i, p in enumerate(via_pads):
                for other in via_pads[i + 1 :]:
                    _connect(p, other)

        # 2c2. Raw annulus/pad overlap (#5133). The via centre can lie
        # outside a pad while its ring overlaps that pad's physical edge.
        # Supported pad shapes use their actual outline and layer span;
        # unsupported geometry retains the earlier centre-only behavior.
        if _has_shapely():
            self._connect_via_in_pad(pad_positions, pad_layers, synthetic_nodes, _connect)

        # 2d. Label-free physical fill components (#5133). Trace copper and
        # raw via annuli can bond a terminal into a pour away from its centre.
        # Each disconnected solid is separate, even within one zone object;
        # existing pad-to-fill erosion and terminal-contact policies remain.
        if _has_shapely():
            self._connect_pour_pads_label_free(
                pad_positions,
                pad_layers,
                _connect,
                synthetic_nodes,
                graph=graph,
                segment_extra_nodes=segment_extra_nodes,
            )
        else:  # pragma: no cover - exercised only on core-only installs
            self._connect_pour_pads_by_declared_net(pad_positions, pad_layers, _connect)

        # 2e. Coincident pads/via-nodes with no intervening copper still share
        #     metal if they occupy the same point AND share a copper layer.
        #     The layer gate mirrors steps 2a/2c/2c2: an XY match alone is not
        #     enough — a blind/buried via stacked under a foreign pad on a
        #     disjoint layer span must NOT fuse (HDI false-connect, issue
        #     #4022). ``pad_layers`` is populated for both real pads and
        #     synthetic via nodes at this point, so no extra plumbing is needed.
        pad_ids = list(pad_positions)
        for i, p in enumerate(pad_ids):
            for other in pad_ids[i + 1 :]:
                if not self._points_close(pad_positions[p], pad_positions[other]):
                    continue
                if not (
                    self._copper_layers_of(pad_layers.get(p, []))
                    & self._copper_layers_of(pad_layers.get(other, []))
                ):
                    continue
                _connect(p, other)

        # 3. Flood-fill connected components of the pad graph.  Synthetic via
        #    nodes (1b) participate in the flood so they carry connectivity
        #    across a pour, but they are stripped from each component before it
        #    is emitted — only real pads belong in the returned partition.
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
            real_pads = component - synthetic_nodes
            if real_pads:
                partition.append(frozenset(real_pads))

        partition.sort(key=lambda comp: min(comp))
        return partition, dict(self.pad_bindings)

    def _get_net_pads(self, net_number: int) -> list[str]:
        """Get all pads on a specific net.

        Args:
            net_number: Net number to find pads for

        Returns:
            List of pad identifiers in format "REF.PAD" (e.g., "U1.3")
        """
        pads = []
        for fi, fp in enumerate(self.pcb.footprints):
            if not fp.reference or fp.reference.startswith("#"):
                continue
            for pi, pad in enumerate(fp.pads):
                if pad.net_number == net_number and (fi, pi) in self._pad_ids:
                    pads.append(self._pad_id(fi, pi))
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
        for fi, fp in enumerate(self.pcb.footprints):
            if not fp.reference or fp.reference.startswith("#"):
                continue
            # Get footprint position and rotation for pad position calculation
            fp_x, fp_y = fp.position
            rotation = fp.rotation

            for pi, pad in enumerate(fp.pads):
                if pad.net_number == net_number and (fi, pi) in self._pad_ids:
                    pad_id = self._pad_id(fi, pi)
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
                        if (
                            other_id != pad_id
                            and self._points_close(pad_pos, other_pos)
                            and (
                                self._copper_layers_of(pad_layers[pad_id])
                                & self._copper_layers_of(pad_layers[other_id])
                            )
                        ):
                            graph[pad_id].add(other_id)
                            graph[other_id].add(pad_id)

        # Connect pads through track segments
        for seg in segments:
            # Find pads at segment endpoints (layer-gated, mirroring the
            # extract_pad_partition fix: a trace endpoint at the XY of a
            # pad with no copper on the trace's layer passes *under* the
            # pad and is not a connection, so it must not count the pad
            # as reached when checking net completeness)
            start_pads = self._find_pads_at_point(
                seg.start, pad_positions, pad_layers=pad_layers, layer=seg.layer
            )
            end_pads = self._find_pads_at_point(
                seg.end, pad_positions, pad_layers=pad_layers, layer=seg.layer
            )

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
        # Track endpoints can form chains connecting distant pads.
        # Layer-aware (issue #3783): cross-layer hops require a via /
        # multi-layer pad bridge at the shared point.
        graph = self._build_segment_chains(segments, pad_positions, graph, pad_layers)

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

    def _copper_layer_order(self) -> list[str]:
        """Return the board's copper layers in physical stack order.

        KiCad's canonical copper order is ``F.Cu``, then the inner layers
        ``In1.Cu, In2.Cu, ...`` (ascending), then ``B.Cu``.  The numeric
        ``Layer.number`` does NOT encode physical order (B.Cu is index 2 even
        though it stacks last), so we derive the order by name.  This order
        lets a through-hole / multi-span via bridge *every* copper layer it
        physically passes through, not just the two endpoints named in
        ``via.layers`` (issue #3783): a standard ``["F.Cu","B.Cu"]`` via on a
        4-layer board electrically joins ``In1.Cu`` and ``In2.Cu`` too.
        """
        names = {layer.name for layer in self.pcb.copper_layers}
        if not names:
            # Fall back to the two outer layers, which exist on every board.
            names = {"F.Cu", "B.Cu"}

        def _inner_index(layer_name: str) -> int:
            digits = layer_name[2:-3]  # strip "In" prefix and ".Cu" suffix
            try:
                return int(digits)
            except ValueError:
                return 0

        inner = sorted(
            (name for name in names if name.startswith("In") and name.endswith(".Cu")),
            key=_inner_index,
        )

        order: list[str] = []
        if "F.Cu" in names:
            order.append("F.Cu")
        order.extend(inner)
        if "B.Cu" in names:
            order.append("B.Cu")
        return order

    def _via_bridged_layers(self, via_layers: list[str]) -> frozenset[str]:
        """Expand a via's named layer span into every copper layer it joins.

        ``via.layers`` records only the *endpoints* of the via's span (e.g.
        ``["F.Cu", "B.Cu"]`` for a through-hole via).  A through-hole / buried
        via physically connects every copper layer between (and including)
        those endpoints, so we expand the span across the board's physical
        copper order (issue #3783).  A degenerate or unrecognised span falls
        back to the named layers themselves.
        """
        copper_span = [layer_str for layer_str in via_layers if layer_str.endswith(".Cu")]
        order = self._copper_layer_order()
        indices = [order.index(layer_str) for layer_str in copper_span if layer_str in order]
        if len(indices) < 2:
            return frozenset(copper_span)
        lo, hi = min(indices), max(indices)
        return frozenset(order[lo : hi + 1])

    def _collect_layer_bridges(
        self,
        pad_positions: dict[str, tuple[float, float]] | None = None,
        pad_layers: dict[str, list[str]] | None = None,
    ) -> list[tuple[tuple[float, float], frozenset[str]]]:
        """Collect points where a via (or multi-layer pad) bridges copper layers.

        A via electrically joins the copper layers listed in ``via.layers``
        (e.g. ``["F.Cu", "B.Cu"]``) at its position.  A through-hole /
        multi-layer pad (``*.Cu`` or two or more explicit ``.Cu`` layers)
        bridges all copper layers it spans at its position.

        This index is consulted by :meth:`_build_segment_chains` so that two
        copper segments meeting at a shared XY point are only fused across
        *different* copper layers when a real layer bridge exists there.  A
        via-less F.Cu/B.Cu crossover (two traces that merely cross at the same
        XY on opposite layers, with nothing joining them) is a legal,
        DRC-clean layer crossover and must NOT be fused (issue #3783).

        Args:
            pad_positions: Optional pad-id -> board-frame position mapping.
            pad_layers: Optional pad-id -> layer-list mapping.  When both pad
                maps are supplied, multi-layer pads also contribute bridges.

        Returns:
            A list of ``(point, layer_set)`` tuples.  ``layer_set`` is the
            frozenset of copper layers the bridge joins at ``point``.
        """
        bridges: list[tuple[tuple[float, float], frozenset[str]]] = []

        # Vias: bridge every copper layer the via physically passes through.
        # ``via.layers`` names only the span endpoints (e.g. ["F.Cu","B.Cu"]),
        # so expand the span across the stackup — a through-hole via joins the
        # inner layers too (issue #3783).
        for via in self.pcb.vias:
            via_layer_set = self._via_bridged_layers(via.layers)
            if len(via_layer_set) >= 2:
                bridges.append((via.position, via_layer_set))

        # Multi-layer pads (through-hole / ``*.Cu``): bridge every copper layer
        # they span.  ``*.Cu`` is treated as a universal copper bridge so any
        # two copper segments meeting at the pad are joined (mirroring the
        # wildcard handling in :meth:`_pad_layer_matches_zone`).
        if pad_positions is not None and pad_layers is not None:
            for pad_id, pad_layer_list in pad_layers.items():
                copper = [layer_str for layer_str in pad_layer_list if layer_str.endswith(".Cu")]
                if not copper:
                    continue
                wildcard = any(layer_str.startswith("*.") for layer_str in copper)
                if wildcard or len(set(copper)) >= 2:
                    pos = pad_positions.get(pad_id)
                    if pos is None:
                        continue
                    layer_set = _ALL_COPPER_LAYERS if wildcard else frozenset(copper)
                    bridges.append((pos, layer_set))

        return bridges

    def _layers_bridged_at(
        self,
        point: tuple[float, float],
        layer_a: str,
        layer_b: str,
        bridges: list[tuple[tuple[float, float], frozenset[str]]],
    ) -> bool:
        """Return True if a via/multi-layer pad bridges two layers at a point.

        ``layer_a`` and ``layer_b`` are joined at ``point`` when some bridge
        coincident with ``point`` spans both layers (``_ALL_COPPER_LAYERS``
        matches any copper layer, modelling a ``*.Cu`` through-hole pad).
        """
        for bridge_point, layer_set in bridges:
            if not self._points_close(point, bridge_point):
                continue
            a_ok = layer_set is _ALL_COPPER_LAYERS or layer_a in layer_set
            b_ok = layer_set is _ALL_COPPER_LAYERS or layer_b in layer_set
            if a_ok and b_ok:
                return True
        return False

    def _segments_chain_at_shared_point(
        self,
        seg_a: Any,
        seg_b: Any,
        bridges: list[tuple[tuple[float, float], frozenset[str]]],
    ) -> bool:
        """Decide whether two tracks are galvanically joined.

        Same-layer segments chain wherever their **swept copper** actually
        touches (issue #5060), not merely where their endpoints coincide.
        Different-layer segments chain only at a shared XY endpoint that a
        via / multi-layer pad bridges across their two layers — a bare
        cross-layer crossover does NOT chain (issue #3783).

        Why full-copper contact and not endpoints (issue #5060).  Physical
        connectivity must be invariant under splitting a straight track into
        collinear subsegments whose copper union is unchanged: an editor,
        router, or human may or may not have emitted an explicit vertex where
        a branch meets a trunk.  Endpoint-only adjacency is *not* invariant —
        a T-junction whose branch lands on a trunk's interior, or a wide
        trace overlapped side-on, chained only once someone happened to split
        the trunk at that point.  On board 09 that produced five false
        ``open`` findings on ``+3V3`` / ``PMOS_SOURCE`` while native KiCad DRC
        reported zero violations, and splitting the very same copper (0.0 mm²
        union change, Shapely-verified) made them disappear.

        :func:`~kicad_tools.geometry.copper.segments_copper_touch` decides the
        same-layer case: two capsules of radius ``width / 2`` intersect iff
        their centerlines lie within the sum of their radii.  It is
        *label-free* (net numbers are never consulted, so a mislabeled or
        foreign-net segment physically touching still fuses and surfaces as a
        short) and *conservative*: a strictly positive copper gap — the
        ≥ 0.1 mm clearance moat any DRC-clean board maintains between foreign
        nets — exceeds the reach and never chains.  Splitting either segment
        cannot manufacture contact, because every subsegment centerline is a
        subset of the original.

        The legacy ``POSITION_TOLERANCE`` endpoint coincidence is retained as
        an additional same-layer trigger so degenerate width-less fixtures
        keep chaining exactly as before.
        """
        same_layer = seg_a.layer == seg_b.layer
        if same_layer and segments_copper_touch(
            seg_a.start,
            seg_a.end,
            seg_a.width or 0.0,
            seg_b.start,
            seg_b.end,
            seg_b.width or 0.0,
        ):
            return True
        for pa in (seg_a.start, seg_a.end):
            for pb in (seg_b.start, seg_b.end):
                if not self._points_close(pa, pb):
                    continue
                if same_layer:
                    return True
                # Cross-layer: require an actual layer bridge at the point.
                if self._layers_bridged_at(pa, seg_a.layer, seg_b.layer, bridges):
                    return True
        return False

    def _build_segment_chains(
        self,
        segments: list,
        pad_positions: dict[str, tuple[float, float]],
        graph: dict[str, set[str]],
        pad_layers: dict[str, list[str]] | None = None,
        segment_extra_nodes: dict[int, set[str]] | None = None,
    ) -> dict[str, set[str]]:
        """Build connectivity through chains of connected segments.

        Segments whose same-layer copper physically touches form chains —
        see :meth:`_segments_chain_at_shared_point`.  Pads at a chain
        endpoint, or bonded anywhere along it via ``segment_extra_nodes``,
        are connected to every other pad in the chain.

        The chain builder is **layer-aware** (issue #3783): two segments on
        *different* copper layers are only chained where they share an XY
        endpoint if a via (``via.layers`` spanning both layers) or a
        multi-layer pad actually bridges the layers at that point.  Two
        traces that merely cross at the same XY on opposite layers with no
        via — a legal, DRC-clean layer crossover — are NOT fused.  The #5060
        full-copper contact test applies to the SAME-layer case only, so
        cross-layer copper that merely overlaps in XY still requires a real
        via / PTH bridge at a shared endpoint.

        Pad membership in a chain is ALSO layer-gated when ``pad_layers``
        is supplied (softstart false-short fix): a chain endpoint only
        claims a pad whose copper exists on that segment's layer.  Without
        the gate, an inner/B.Cu trace ending at the XY of an F.Cu-only SMD
        pad pulled that pad — and its whole net — into a foreign chain.

        ``segment_extra_nodes`` (segment index -> node ids) injects
        additional graph nodes (via-barrel overlap bonds from step 2a2 of
        :meth:`extract_pad_partition`) into the chain component that owns
        the segment.
        """
        if not segments:
            return graph

        # Per-point layer bridges (vias + multi-layer pads) used to gate
        # cross-layer chain hops.
        bridges = self._collect_layer_bridges(pad_positions, pad_layers)

        # Build segment adjacency graph
        segment_graph: dict[int, set[int]] = defaultdict(set)
        # Enclose each full copper capsule, not just its centerline. The exact
        # predicate also joins width-only side/T contacts; the query margin
        # separately preserves legacy endpoint tolerance and layer bridges.
        bounds = [
            (
                min(seg.start[0], seg.end[0]) - max(seg.width or 0.0, 0.0) / 2,
                min(seg.start[1], seg.end[1]) - max(seg.width or 0.0, 0.0) / 2,
                max(seg.start[0], seg.end[0]) + max(seg.width or 0.0, 0.0) / 2,
                max(seg.start[1], seg.end[1]) + max(seg.width or 0.0, 0.0) / 2,
            )
            for seg in segments
        ]
        for i, j in candidate_pairs(bounds, self.POSITION_TOLERANCE):
            if self._segments_chain_at_shared_point(segments[i], segments[j], bridges):
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
                component_pads.update(
                    self._find_pads_at_point(
                        seg.start, pad_positions, pad_layers=pad_layers, layer=seg.layer
                    )
                )
                component_pads.update(
                    self._find_pads_at_point(
                        seg.end, pad_positions, pad_layers=pad_layers, layer=seg.layer
                    )
                )
                if segment_extra_nodes:
                    component_pads.update(segment_extra_nodes.get(seg_idx, ()))

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
        pad_layers: dict[str, list[str]] | None = None,
        layer: str | None = None,
    ) -> list[str]:
        """Find all pads at a given point, optionally gated by copper layer.

        Args:
            point: Point to check
            pad_positions: Mapping of pad IDs to positions
            pad_layers: Optional pad-id -> layer-list mapping.  Only
                consulted when ``layer`` is also given.
            layer: Optional copper layer the *toucher* (e.g. a track
                segment) lives on.  When given together with
                ``pad_layers``, a pad only matches if its copper exists on
                that layer (``*.Cu`` and expanded via spans match any
                copper layer).  XY coincidence across disjoint layers is
                NOT a connection — a trace may legally run under an
                SMD pad on another layer (softstart false-short fix).

        Returns:
            List of pad IDs at this point
        """
        hits = [
            pad_id
            for pad_id, pad_pos in pad_positions.items()
            if self._points_close(point, pad_pos)
        ]
        if layer is None or pad_layers is None:
            return hits
        return [
            pad_id
            for pad_id in hits
            if pad_id not in pad_layers or self._pad_copper_on_layer(pad_layers[pad_id], layer)
        ]

    @staticmethod
    def _pad_copper_on_layer(layers: list[str] | frozenset[str], layer: str) -> bool:
        """True iff a pad/via node with ``layers`` has copper on ``layer``."""
        return layer in layers or "*.Cu" in layers

    def _copper_layers_of(self, layers: list[str] | frozenset[str]) -> frozenset[str]:
        """Expand a pad's layer list to the concrete copper layers it spans.

        ``*.Cu`` (through-hole pads) expands to every copper layer in the
        board's stackup; explicit ``.Cu`` entries pass through unchanged.
        """
        out: set[str] = set()
        for layer_str in layers:
            if layer_str == "*.Cu":
                out.update(self._copper_layer_order())
            elif layer_str.endswith(".Cu"):
                out.add(layer_str)
        return frozenset(out)

    @staticmethod
    def _point_segment_distance(
        point: tuple[float, float],
        seg_start: tuple[float, float],
        seg_end: tuple[float, float],
    ) -> float:
        """Shortest distance from ``point`` to the segment ``seg_start-seg_end``.

        Thin delegate to the shared
        :func:`kicad_tools.geometry.copper.point_segment_distance` primitive.
        """
        return _point_segment_distance_impl(point, seg_start, seg_end)

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

    def _pad_copper_polygon(self, fp: Any, pad: Any, shape_aware: bool = False) -> Any | None:
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

        ``shape_aware=True`` replaces the size box with the pad's actual
        outline family *before* eroding — ``circle`` → a disk of radius
        ``min(w, h) / 2``, ``oval`` → a stadium (the centerline of the longer
        local axis buffered by ``min(w, h) / 2``), everything else
        (``rect`` / ``roundrect`` / ``custom`` / unknown) → the same box as
        before.  This matters because the box **over-reaches real copper on
        the diagonals** of a round pad: for a circle of diameter ``d`` the
        eroded box corner sits ``(d / 2 − POUR_PAD_ERODE)·√2`` from the
        center, which exceeds the true copper radius ``d / 2`` by more than
        the 0.1016 mm minimum clearance once ``d ≳ 1.2 mm`` (0.211 mm of
        over-reach on a 1.7 mm header pad).  Any *distance-to-copper* test
        against that phantom corner can bond across a legal clearance moat.
        See :meth:`_connect_segment_in_pad` for the failure this guards.

        The shape-aware outline is inscribed in the same rotated size box, so
        it is always a **subset** of the box geometry: enabling it can only
        make a bond test stricter, never looser.

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

        def _to_board(ox: float, oy: float) -> tuple[float, float]:
            return (cx + ox * cos_a - oy * sin_a, cy + ox * sin_a + oy * cos_a)

        shape = (getattr(pad, "shape", "") or "").lower() if shape_aware else ""
        base: Any
        if shape in ("circle", "oval"):
            radius = min(w, h) / 2.0
            # Stadium half-length: 0 for a circle (and for a "square" oval,
            # which KiCad renders as a circle), positive for a true oval.
            half = abs(w - h) / 2.0
            if half <= 0.0:
                base = _ShapelyPoint((cx, cy)).buffer(radius)
            else:
                ends = [(-half, 0.0), (half, 0.0)] if w >= h else [(0.0, -half), (0.0, half)]
                base = _ShapelyLineString([_to_board(*e) for e in ends]).buffer(radius)
        else:
            corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
            base = _ShapelyPolygon([_to_board(*c) for c in corners])
        if self.POUR_PAD_ERODE > 0:
            eroded = base.buffer(-self.POUR_PAD_ERODE)
            # Erosion can empty a very small pad (min dimension <= 0.2 mm);
            # keep the un-eroded outline so the pad is still testable rather
            # than silently dropped.  That collapses the erosion margin for
            # such pads, but the box-vs-true-copper excess is then bounded by
            # the half-diagonal of a <= 0.2 mm box (< 0.042 mm) — under any
            # realistic minimum clearance — so no DRC-clean false connect can
            # slip through the un-eroded fallback either.
            if not eroded.is_empty:
                return eroded
        return base

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

    def _synthetic_via_radii(self, synthetic_nodes: set[str]) -> dict[str, float]:
        """Map each ``__via{index}`` node to its copper radius (``size / 2``).

        The synthetic via node ids created in :meth:`extract_pad_partition`
        (step 1b) are ``f"__via{index}"`` in board via order, so we recover the
        copper radius by re-enumerating ``self.pcb.vias``.  Only nodes present
        in ``synthetic_nodes`` are returned.
        """
        radii: dict[str, float] = {}
        for via_index, via in enumerate(self.pcb.vias):
            node_id = f"__via{via_index}"
            if node_id in synthetic_nodes:
                radii[node_id] = max(getattr(via, "size", 0.0) or 0.0, 0.0) / 2.0
        return radii

    def _via_copper_geom(self, pos: tuple[float, float], radius: float) -> Any:
        """Build a shapely geometry approximating a via's copper (issue #3909).

        Returns the via's copper *circle* (a disk of ``radius`` about ``pos``,
        eroded inward by :data:`POUR_PAD_ERODE` to match the pad-box treatment)
        so a via whose copper ring overlaps a foreign pour's solid region bonds
        into that island and surfaces the short.  Falls back to a bare
        ``Point`` when the via has no positive size (degenerate) so the legacy
        centre-in-solid bond (issue #3794) still fires.
        """
        if radius <= 0:
            return _ShapelyPoint(pos)
        circle = _ShapelyPoint(pos).buffer(radius)
        if self.POUR_PAD_ERODE > 0:
            eroded = circle.buffer(-self.POUR_PAD_ERODE)
            # Erosion can empty a very small via; keep the full circle so the
            # via is still testable rather than silently dropped.
            if not eroded.is_empty:
                return eroded
        return circle

    def _eroded_pad_polygons(self, shape_aware: bool = False) -> dict[str, Any]:
        """Board-frame eroded copper polygon per physical pad node.

        Build :meth:`_pad_copper_polygon` geometry with the existing
        ``POUR_PAD_ERODE`` inset, omitting pads without a reference or number.
        ``None`` geometries are filtered out; degenerate pads may be points.

        ``shape_aware`` is forwarded to :meth:`_pad_copper_polygon`. Trace
        contact (2a3) and pad-to-fill contact (2d) request shape-aware geometry.
        Via-to-pad contact (2c2) uses this mapping only as the legacy fallback
        for unsupported shapes; supported shapes use raw pad outlines and
        actual via annuli in :meth:`_connect_via_in_pad`.
        """
        pad_polygons: dict[str, Any] = {}
        for fi, fp in enumerate(self.pcb.footprints):
            if not fp.reference or fp.reference.startswith("#"):
                continue
            for pi, pad in enumerate(fp.pads):
                if pad.number is None or pad.number == "":
                    continue
                poly = self._pad_copper_polygon(fp, pad, shape_aware=shape_aware)
                if poly is not None:
                    pad_polygons[self._pad_id(fi, pi)] = poly
        return pad_polygons

    def _connect_segment_in_pad(
        self,
        segments: list,
        pad_layers: dict[str, list[str]],
        segment_extra_nodes: dict[int, set[str]],
    ) -> None:
        """Bond track copper overlapping pad copper (issues #4678, #5060).

        A track *endpoint* that lies inside a pad's copper is galvanic contact
        with that pad — KiCad's own connectivity, ``kct net-status`` (strict)
        and ``kct route --complete`` all treat it as connected.  The legacy
        center-proximity test (step 2a, ``POSITION_TOLERANCE`` = 0.01 mm from
        the pad *center*) missed it, so the LVS copper leg reported a false
        ``open`` that blocked tapeout Gate 1 with no override.

        Issue #5060 extends the same test from the two endpoints to the
        **whole swept segment**.  A pad sitting inline on a continuous trace
        (a SOIC pin whose copper is straddled by an uninterrupted wide track
        running past it) touches only that track's *interior*, so the
        endpoint form left it stranded in its own component — a false
        ``open`` on board 09's ``+3V3`` / ``PMOS_SOURCE`` that vanished as
        soon as the very same copper was split at the pad, with a
        Shapely-verified 0.0 mm² change to the buffered trace union.
        Measuring the distance from the pad polygon to the entire centerline
        makes the bond invariant under such splits in both directions: the
        minimum is attained at some centerline point, which lies on exactly
        one subsegment, and no subsegment reaches anywhere the whole
        centerline did not — so inserting a collinear vertex can neither
        create nor destroy a pad bond.

        Bond condition (conservative by construction):

        * shared copper layer — the pad's copper must exist on the segment's
          layer (``*.Cu`` through-hole pads match any copper layer), and
        * the segment centerline enters the pad's **eroded, shape-aware**
          copper outline (:meth:`_pad_copper_polygon` with
          ``shape_aware=True``: circle → disk, oval → stadium,
          rect/roundrect → size box, each inset by ``POUR_PAD_ERODE`` =
          0.1 mm), or the swept trace copper (KiCad tracks are the
          centerline buffered by ``width / 2``, with rounded ends)
          penetrates that eroded outline by **more than 1 µm** (the
          step-2a2 strictly-positive contact-depth guard).

        Soundness (and why the geometry must be shape-aware).  This step
        measures a *distance* from arbitrary trace copper to the pad
        polygon, so the polygon has to be contained in the pad's real copper
        in **every** direction, not just on axis.  The plain size box is not:
        for a circle pad of diameter ``d`` its eroded corner sticks out to
        ``(d / 2 − 0.1)·√2``, over-reaching true copper by more than the
        0.1016 mm minimum clearance for any ``d ≳ 1.2 mm`` (0.211 mm on a
        1.7 mm header pad).  Against that phantom corner a 45° endpoint at a
        DRC-legal 0.13–0.21 mm copper gap measured ``dist == 0`` and bonded —
        masking a real open on a same-net trace, and minting a phantom short
        on a foreign one.  Both directions were reproduced on DRC-clean
        geometry (PR #4710 review) and are now regression-tested.

        With the shape-aware outline the eroded polygon is contained in the
        true copper with ≥ 0.1 mm of margin **in every direction** (rect and
        roundrect are exact-or-inside by construction; circle/oval are the
        true outline inset by 0.1 mm).  DRC clearance keeps a *foreign*
        trace's copper ≥ 0.1016 mm from that copper, and its centerline sits
        a further ``width / 2`` beyond its own copper edge, so the measured
        distance stays above ``width / 2`` and the bond does not fire.  That
        argument is about a *clearance to copper* and so is unaffected by the
        #5060 widening from the two endpoints to the whole centerline: DRC
        holds along the entire trace, not only at its vertices.  The claim is
        therefore scoped, not absolute:
        it holds for the pad-shape families modelled here, and a ``custom``
        (polygon-primitive) pad still falls back to the size box — such a pad
        can in principle over-reach on a concave outline, which is a known,
        documented approximation rather than a soundness proof.

        Bonds are recorded in ``segment_extra_nodes`` (segment index → node
        ids), which :meth:`_build_segment_chains` unions into the segment's
        chain component — so the pad joins every pad reachable through the
        chain, exactly like the 2a2 via-barrel bonds.  Requires ``shapely``
        (guarded by the caller); shapely-absent installs keep the legacy
        center-proximity behavior.
        """
        # Shape-aware geometry (circle -> disk, oval -> stadium, rect /
        # roundrect -> box): this step measures distance from arbitrary trace
        # vertices, so the box's diagonal over-reach on round pads would bond
        # across a legal clearance moat (see the soundness note above).
        pad_polygons = self._eroded_pad_polygons(shape_aware=True)
        if not pad_polygons:
            return

        # Cheap bounding prefilter: skip the shapely test unless the centerline
        # is within the pad box's half-diagonal plus the trace copper radius
        # of the pad center.  Uses the *uneroded box* bound — a superset of
        # every shape-aware outline — so it can only over-admit (the exact
        # eroded test below decides), never miss.
        pad_bounds: dict[str, tuple[float, float, float]] = {}
        for fi, fp in enumerate(self.pcb.footprints):
            if not fp.reference or fp.reference.startswith("#"):
                continue
            for pi, pad in enumerate(fp.pads):
                if pad.number is None or pad.number == "":
                    continue
                pad_id = self._pad_id(fi, pi)
                if pad_id not in pad_polygons:
                    continue
                cx, cy = self._transform_pad_position(
                    pad.position, fp.position[0], fp.position[1], fp.rotation
                )
                w, h = pad.size
                pad_bounds[pad_id] = (cx, cy, math.hypot(w, h) / 2.0)

        for seg_index, seg in enumerate(segments):
            cap_radius = max((seg.width or 0.0) / 2.0, 0.0)
            centerline = (
                _ShapelyPoint(seg.start)
                if seg.start == seg.end
                else _ShapelyLineString([seg.start, seg.end])
            )
            for pad_id, (cx, cy, bound) in pad_bounds.items():
                # The whole segment must pass the broad-phase test, not only
                # its endpoints: an inline pad can be arbitrarily far from
                # either vertex of otherwise identical unsplit copper.
                if self._point_segment_distance((cx, cy), seg.start, seg.end) > bound + cap_radius:
                    continue
                if not self._pad_copper_on_layer(pad_layers.get(pad_id, []), seg.layer):
                    continue
                dist = pad_polygons[pad_id].distance(centerline)
                # Retain the existing eroded, shape-aware pad and positive
                # contact-depth guards while considering the full trace.
                if dist == 0.0 or dist < cap_radius - 1e-3:
                    segment_extra_nodes.setdefault(seg_index, set()).add(pad_id)

    def _physical_via_annulus(self, via: Any) -> Any:
        """Raw annular copper, without eroding away a narrow plating ring."""
        radius = max(via.size, 0.0) / 2.0
        outer = _ShapelyPoint(via.position).buffer(radius, quad_segs=64)
        if via.drill > 0:
            outer = outer.difference(
                _ShapelyPoint(via.position).buffer(via.drill / 2.0, quad_segs=64)
            )
        return outer

    def _connect_via_in_pad(
        self,
        pad_positions: dict[str, tuple[float, float]],
        pad_layers: dict[str, list[str]],
        synthetic_nodes: set[str],
        connect: Any,
    ) -> None:
        """Bond supported physical pad copper to overlapping via annuli.

        A via centre need not enter a pad: Board06 U1.32 overlaps an off-centre
        annulus by positive area. The former eroded-pad/centre test missed it.
        Use raw supported shapes, not a widened box or proximity allowance;
        unrelated pad/trace and pad/pour erosion policies remain unchanged.
        """
        from kicad_tools.validate.rules.clearance import _pad_polygon

        pads = self._eroded_pad_polygons()
        raw_keys: set[str] = set()
        for fi, fp in enumerate(self.pcb.footprints):
            if not fp.reference or fp.reference.startswith("#"):
                continue
            for pi, pad in enumerate(fp.pads):
                if pad.number is None or pad.number == "":
                    continue
                if pad.shape not in {"rect", "roundrect", "circle", "oval", "obround"}:
                    continue  # No new raw bounding-box fallback for custom copper.
                key = self._pad_id(fi, pi)
                if key not in pad_positions:
                    continue
                geom = _pad_polygon(pad, fp)
                if geom is not None:
                    if pad.drill > 0:
                        geom = geom.difference(
                            _ShapelyPoint(pad_positions[key]).buffer(pad.drill / 2, quad_segs=64)
                        )
                    pads[key] = geom
                    raw_keys.add(key)

        for index, via in enumerate(self.pcb.vias):
            node = f"__via{index}"
            if node not in synthetic_nodes:
                continue
            annulus = self._physical_via_annulus(via)
            for key, geom in pads.items():
                if not (
                    self._copper_layers_of(pad_layers.get(key, []))
                    & self._copper_layers_of(pad_layers.get(node, []))
                ):
                    continue
                # Positive area proves copper overlap without treating a gap
                # or polygon approximation's tangent as a conductive bridge.
                if key in raw_keys:
                    touches = annulus.intersection(geom).area > 0
                else:
                    # Unsupported shapes retain their old centre-only policy.
                    touches = geom.intersects(_ShapelyPoint(via.position))
                if touches:
                    connect(node, key)

    def _connect_pour_pads_label_free(
        self,
        pad_positions: dict[str, tuple[float, float]],
        pad_layers: dict[str, list[str]],
        connect: Any,
        synthetic_nodes: set[str] | None = None,
        *,
        graph: dict[str, set[str]] | None = None,
        segment_extra_nodes: dict[int, set[str]] | None = None,
    ) -> None:
        """Trace actual fill/track/via contacts, never zone ownership.

        Each solid fill component is a distinct node. Full-width trace copper
        can enter a fill even when neither endpoint/via/pad lies in it (U1.17).
        Existing terminal bonds and pad/trace tolerances are retained; raw via
        annuli supply the physical layer bridges. Pad-to-fill geometry keeps
        its existing erosion policy. No declared net is consulted.
        """
        from shapely.strtree import STRtree  # type: ignore[import-untyped]

        from kicad_tools.geometry.copper import segment_copper_polygon

        # (terminal id or None, kind, copper layers, geometry)
        items: list[tuple[str | None, str, frozenset[str], Any]] = []
        terminal_index: dict[str, int] = {}
        synthetic_nodes = synthetic_nodes or set()
        for key, geom in self._eroded_pad_polygons(shape_aware=True).items():
            if key in pad_positions:
                terminal_index[key] = len(items)
                items.append((key, "pad", self._copper_layers_of(pad_layers[key]), geom))
        for index, via in enumerate(self.pcb.vias):
            key = f"__via{index}"
            if key in synthetic_nodes:
                terminal_index[key] = len(items)
                items.append(
                    (
                        key,
                        "via",
                        self._via_bridged_layers(via.layers),
                        self._physical_via_annulus(via),
                    )
                )
        segment_indices: dict[int, int] = {}
        for index, seg in enumerate(self.pcb.segments):
            geom = segment_copper_polygon(seg.start, seg.end, seg.width)
            if geom is not None:
                segment_indices[index] = len(items)
                items.append((None, "segment", frozenset({seg.layer}), geom))
        for zone in self.pcb.zones:
            for index, points in enumerate(zone.filled_polygons):
                region = self._fill_solid_region(points)
                if region is None:
                    continue
                # A repaired flat contour can itself contain separate solids.
                solids = region.geoms if region.geom_type == "MultiPolygon" else [region]
                for solid in solids:
                    items.append(
                        (None, "fill", frozenset({zone.filled_polygon_layer(index)}), solid)
                    )
        if not items:
            return
        parent = list(range(len(items)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            parent[find(right)] = find(left)

        # Carry the already-established pad/trace/via bonds into this graph.
        for key, neighbors in (graph or {}).items():
            if key in terminal_index:
                for neighbor in neighbors:
                    if neighbor in terminal_index:
                        union(terminal_index[key], terminal_index[neighbor])
        for index, seg in enumerate(self.pcb.segments):
            if index not in segment_indices:
                continue
            terminals = set((segment_extra_nodes or {}).get(index, set()))
            for point in (seg.start, seg.end):
                terminals.update(
                    self._find_pads_at_point(
                        point, pad_positions, pad_layers=pad_layers, layer=seg.layer
                    )
                )
            for terminal in terminals:
                if terminal in terminal_index:
                    union(segment_indices[index], terminal_index[terminal])

        tree = STRtree([item[3] for item in items])
        for left, (_, kind, layers, geom) in enumerate(items):
            for candidate in tree.query(geom, predicate="intersects"):
                right = int(candidate)
                if right <= left or not layers.intersection(items[right][2]):
                    continue
                other_kind = items[right][1]
                if "fill" not in (kind, other_kind) and (kind, other_kind) != (
                    "segment",
                    "segment",
                ):
                    # Via/trace and pad/conductor bonds came from existing
                    # steps, including their contact-depth guards. Do not
                    # bypass those guards with a new raw intersection edge.
                    continue
                union(left, right)
        fill_components = {find(index) for index, item in enumerate(items) if item[1] == "fill"}
        components: dict[int, list[str]] = defaultdict(list)
        for key, index in terminal_index.items():
            root = find(index)
            if root in fill_components:
                components[root].append(key)
        for keys in components.values():
            for key in keys[1:]:
                connect(keys[0], key)

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
        for fi, fp in enumerate(self.pcb.footprints):
            if not fp.reference or fp.reference.startswith("#"):
                continue
            for pi, pad in enumerate(fp.pads):
                if pad.number is None or pad.number == "":
                    continue
                pad_declared_net[self._pad_id(fi, pi)] = pad.net_name

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
        # Public diagnostics retain logical pin names; repeated names in
        # separate islands describe physically distinct lands of one pin.
        islands = [[self._pad_display(node) for node in island] for island in islands]
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

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

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kicad_tools.validate.spatial import candidate_pairs

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

# Geometry backend.  ``shapely`` is a core dependency (issue #3824), but
# this module keeps a graceful fallback: when shapely is unavailable (a
# broken/partial install) the shared copper-contact graph
# (:mod:`kicad_tools.validate.copper_contact`) is skipped and the legacy
# centre-matching extractor with declared-net pour grouping runs instead,
# so importing this module — and
# tracing autorouter segment/via copper — never hard-fails.  The boolean
# probe is delegated to the shared guard in :mod:`kicad_tools._shapely`.
from kicad_tools._shapely import has_shapely as _has_shapely
from kicad_tools.geometry.copper import (
    PAD_FALLBACK_ERODE,
    pad_copper_outline,
    segments_copper_touch,
)

# Shared, checker-agnostic trace-copper geometry (issue #4176).  These are
# pure analytic helpers with no shapely dependency, so same-layer track
# contact is decided identically on core-only installs.
from kicad_tools.geometry.copper import (
    point_segment_distance as _point_segment_distance_impl,
)
from kicad_tools.validate.copper_contact import (
    CopperContactGraph,
    CopperItem,
    build_copper_contact_graph,
    describe_item,
)

if _has_shapely():  # pragma: no cover - import guard exercised by environment
    from shapely.geometry import Point as _ShapelyPoint  # type: ignore[import-untyped]
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

    @property
    def counts_as_unconnected(self) -> bool:
        """True if this issue is an unconnected item (Issue #6495).

        This is the single definition every consumer (``summary()``, the
        ``kct validate --connectivity`` counts and ``kct fix-drc``'s
        regressed-net attribution) uses.  Only errors count: a lone fill
        island is a warning that kicad-cli does not report (#6487), so it is
        visible but never unconnected.
        """
        return self.is_error

    @property
    def is_lone_fill_island(self) -> bool:
        """True for a lone fill island warning (a fill nothing else touches)."""
        return self.issue_type == "zone_island" and self.is_warning

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
    def unconnected(self) -> list[ConnectivityIssue]:
        """Issues that count as unconnected items (errors only, Issue #6495)."""
        return [i for i in self.issues if i.counts_as_unconnected]

    @property
    def unconnected_zone_islands(self) -> list[ConnectivityIssue]:
        """``zone_island`` issues that count as unconnected items."""
        return [i for i in self.zone_islands if i.counts_as_unconnected]

    @property
    def lone_fill_islands(self) -> list[ConnectivityIssue]:
        """Lone fill island warnings: visible, but never unconnected (#6487)."""
        return [i for i in self.issues if i.is_lone_fill_island]

    @property
    def strict_warning_count(self) -> int:
        """Warnings that fail ``--strict``; lone fill islands are excluded.

        kicad-cli does not report a lone fill island, so the native-reconciled
        path never sees one.  Excluding them here makes the ``--strict`` exit
        code identical with and without kicad-cli (Issue #6495).
        """
        return sum(1 for i in self.issues if i.is_warning and not i.is_lone_fill_island)

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
        if self.unconnected_zone_islands:
            parts.append(f"  Orphaned zone islands: {len(self.unconnected_zone_islands)}")
        if self.lone_fill_islands:
            parts.append(f"  Lone fill islands: {len(self.lone_fill_islands)} (warning)")
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

        # Issues #6452 / #6453 / #6470: every net runs on the shared
        # copper-contact graph (exact pad outlines, every fill of every zone,
        # pad-to-pad overlap, orphan fragments counted).  The legacy
        # pad-centre graph survives only for core-only installs.
        use_contact_graph = _has_shapely()

        for net_number, net in nets.items():
            if use_contact_graph:
                verdict = self._contact_graph_net_issues(net_number, net.name, has_footprints)
                if verdict is None:
                    continue
                issues, has_fill = verdict
                for issue in issues:
                    result.add(issue)
                # A lone-fill-island warning does not make the net open.
                if any(issue.is_error for issue in issues):
                    continue
                connected_count += 1
                if has_fill:
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
            graph = self._build_connectivity_graph_legacy(net_number)

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

        Since the shared copper-contact model (Issue #6452) the internal
        count equals ``kicad-cli pcb drc``'s per net on every committed routed
        board (``tests/test_copper_contact_parity.py``), e.g. board05's 57.
        This opt-in path differs in that it **refills** zones first, so it
        reports what KiCad would see after a refill rather than the copper
        saved in the file.

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
            from kicad_tools.cli.runner import last_kicad_cli_lookup

            cli_lookup = last_kicad_cli_lookup()
            if cli_lookup is not None and cli_lookup.probe_failed:
                # Installed-but-unresponsive is not the documented KiCad-less
                # path: never drop the refill cross-check silently (#5932).
                import warnings

                warnings.warn(
                    f"{cli_lookup.reason}; skipping the kicad-cli --refill-zones "
                    "cross-check and using the internal zone analysis",
                    RuntimeWarning,
                    stacklevel=2,
                )
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

    def _contact_graph(self, net_number: int | None) -> CopperContactGraph:
        """The shared copper-contact graph (Issue #6452), net-scoped or label-free."""
        return build_copper_contact_graph(self, net_number)

    def _contact_graph_net_issues(
        self,
        net_number: int,
        net_name: str,
        has_footprints: bool,
    ) -> tuple[list[ConnectivityIssue], bool] | None:
        """Issues for one net from the shared copper-contact graph.

        Returns ``(issues, has_fill)``, or ``None`` for a named net with no
        pads on a board that has footprints (stripped pad assignments are not
        counted as connected).

        Every same-net copper item is a node (:mod:`.copper_contact`).  The
        number of missing relationships is ``counted components - 1``, where
        a counted component holds a pad, via or track.  That includes
        fragments that reach no pad (a detached track or via, or a fill
        island with only a track or via on it), because ``kicad-cli pcb
        drc`` reports each as an unconnected item.  A *lone* fill island --
        a component made only of fill copper -- is not counted: kicad-cli
        does not report it.

        * A net with fill copper reports one ``zone_island`` error per
          counted component beyond the first, naming one endpoint of each
          (Issue #4498).  There is no zone-boundary or nearest-fill bond: a
          pad is on a fill only when its copper overlaps it (the #479
          heuristic under-reported opens, Issue #6470).  Each lone fill
          island is reported as a ``zone_island`` **warning** instead, so
          the floating copper stays visible without failing the net.
        * A net without fill reports one ``partial`` issue whose ``islands``
          are the pad groups (largest first) followed by any pad-less
          fragments; ``unconnected_pads`` lists only real pads.
        """
        pads = self._get_net_pads(net_number)
        graph = self._contact_graph(net_number)
        has_fill = any(item.kind == "fill" for item in graph.items)
        if not has_fill and not pads and has_footprints:
            return None
        components = graph.components()
        if len(components) <= 1:
            return [], has_fill

        def _rank(item: CopperItem) -> tuple[int, str]:
            order = {"pad": 0, "fill": 1, "via": 2, "track": 3}[item.kind]
            return (order, describe_item(self, item)[0])

        if has_fill:
            representatives = [
                min((graph.items[i] for i in members), key=_rank)
                for members in graph.counted_components()
            ]
            ordered = sorted(representatives, key=_rank)
            issues: list[ConnectivityIssue] = []
            warnings: list[ConnectivityIssue] = []
            # kicad-cli does not report a fill island that nothing touches,
            # so it must not be an error here; keep it visible as a warning.
            lone_fills = sorted(
                (
                    min((graph.items[i] for i in members), key=_rank)
                    for members in graph.fill_only_components()
                ),
                key=_rank,
            )
            for item in lone_fills:
                item_id, item_desc = describe_item(self, item)
                warnings.append(
                    ConnectivityIssue(
                        severity="warning",
                        issue_type="zone_island",
                        net_name=net_name,
                        message=f"{item_desc} touches no other {net_name} copper",
                        suggestion=(
                            "Remove the floating island or connect it "
                            "(kicad-cli does not report a lone fill island)"
                        ),
                        islands=((item_id,),),
                    )
                )
            if not ordered:
                return warnings, has_fill
            anchor_id, anchor_desc = describe_item(self, ordered[0])
            for item in ordered[1:]:
                item_id, item_desc = describe_item(self, item)
                issues.append(
                    ConnectivityIssue(
                        severity="error",
                        issue_type="zone_island",
                        net_name=net_name,
                        message=f"Missing connection between {anchor_desc} and {item_desc}",
                        suggestion="Connect the same-net copper components",
                        islands=((anchor_id,), (item_id,)),
                    )
                )
            return issues + warnings, has_fill

        pad_node = {(fi, pi): node for (fi, pi), node in self._pad_ids.items()}
        islands: list[list[str]] = []
        orphans: list[tuple[str, ...]] = []
        for members in components:
            nodes = sorted(
                pad_node[graph.items[i].key]
                for i in members
                if graph.items[i].kind == "pad" and graph.items[i].key in pad_node
            )
            if nodes:
                islands.append(nodes)
            else:
                rep = min((graph.items[i] for i in members), key=_rank)
                orphans.append((describe_item(self, rep)[1],))
        if len(islands) <= 1 and not orphans:
            return [], has_fill
        return [self._create_issue(net_name, pads, islands, orphans=orphans)], has_fill

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

        With shapely installed (the normal case) the partition is the
        label-free copper-contact graph of :mod:`.copper_contact` (Issue
        #6452): exact pad outlines with no erosion (#6453), pad-to-pad overlap
        bonds (an exposed pad over its thermal-via pads, #6435), and fills of
        different zones bridged by any via or track touching both.  Core-only
        installs keep the legacy centre-matching extractor.
        """
        self._refresh_pad_identities()
        if not _has_shapely():  # pragma: no cover - exercised only on core-only installs
            return self._extract_pad_occurrences_legacy()
        graph = self._contact_graph(None)
        placed: set[str] = set()
        partition: list[frozenset[str]] = []
        for pads in graph.pad_components():
            nodes = frozenset(self._pad_ids[it.key] for it in pads if it.key in self._pad_ids)
            if nodes:
                partition.append(nodes)
                placed |= nodes
        partition.extend(frozenset({node}) for node in self._pad_ids.values() if node not in placed)
        partition.sort(key=lambda comp: min(comp))
        return partition, dict(self.pad_bindings)

    def _extract_pad_occurrences_legacy(
        self,
    ) -> tuple[list[frozenset[str]], dict[str, tuple[str, str]]]:
        """Core-only (shapely-absent) pad partition: centre/endpoint matching.

        The pre-#6452 extractor minus its shapely-only steps.  Pour copper
        falls back to declared-net grouping
        (:meth:`_connect_pour_pads_by_declared_net`).
        """
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

        # 2d. Pour copper: declared-net grouping (the label-free fill graph
        #     needs shapely, see :mod:`.copper_contact`).
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
        """Pad adjacency graph for one net from the shared copper-contact graph.

        Pads in one copper component are mutually adjacent.  Sets
        ``_last_zone_connected_pads`` to the pads whose component holds fill
        copper.  Core-only installs use :meth:`_build_connectivity_graph_legacy`.
        """
        if not _has_shapely():  # pragma: no cover - exercised only on core-only installs
            return self._build_connectivity_graph_legacy(net_number)
        contact = self._contact_graph(net_number)
        graph: dict[str, set[str]] = defaultdict(set)
        zone_pads: set[str] = set()
        for members in contact.components():
            items = [contact.items[i] for i in members]
            nodes = [
                self._pad_ids[it.key]
                for it in items
                if it.kind == "pad" and it.key in self._pad_ids
            ]
            for node in nodes:
                graph[node].update(other for other in nodes if other != node)
            if any(it.kind == "fill" for it in items):
                zone_pads.update(nodes)
        self._last_zone_connected_pads = zone_pads
        return graph

    def _build_connectivity_graph_legacy(
        self,
        net_number: int,
    ) -> dict[str, set[str]]:
        """Build graph of copper connectivity for a net (core-only fallback).

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

    # Erosion (mm) for pad shapes the shared outline primitive does not model
    # exactly (trapezoid, chamfered roundrect, custom), and for the eroded via
    # disk legacy net-status still uses.  Exact outlines are never eroded
    # (Issue #6453); see :data:`kicad_tools.geometry.copper.PAD_FALLBACK_ERODE`.
    POUR_PAD_ERODE: float = PAD_FALLBACK_ERODE

    def _pad_copper_polygon(
        self,
        fp: Any,
        pad: Any,
        shape_aware: bool = True,
    ) -> Any | None:
        """Board-frame shapely geometry of a pad's copper.

        Thin wrapper over :func:`kicad_tools.geometry.copper.pad_copper_outline`
        (Issues #6452, #6453): the exact rect / roundrect / circle / oval
        outline oriented by the pad's absolute angle, with no erosion.
        ``shape_aware`` is accepted for backwards compatibility and ignored --
        every caller now gets the real outline.  Returns ``None`` when shapely
        is unavailable.
        """
        del shape_aware
        return pad_copper_outline(pad, fp.position, fp.rotation)

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

    def _physical_via_annulus(self, via: Any) -> Any:
        """Raw annular copper, without eroding away a narrow plating ring."""
        radius = max(via.size, 0.0) / 2.0
        outer = _ShapelyPoint(via.position).buffer(radius, quad_segs=64)
        if via.drill > 0:
            outer = outer.difference(
                _ShapelyPoint(via.position).buffer(via.drill / 2.0, quad_segs=64)
            )
        return outer

    @staticmethod
    def _adjacent_fill_pairs(
        regions: list[Any],
        fill_layers: list[str],
        reach: float,
    ) -> list[tuple[int, int]]:
        """Position pairs of same-layer fill solids within ``reach`` of each other.

        ``reach`` is the summed stroke half-width the two stored outlines are
        inflated by to recover real copper (Issue #5362).  Uses an ``STRtree``
        ``dwithin`` query so a zone fractured into hundreds of
        ``filled_polygon`` entries costs O(n log n) rather than O(n^2)
        pairwise ``distance`` calls.  Returns *positions within the inputs*,
        not board-level indices.
        """
        from shapely.strtree import STRtree  # type: ignore[import-untyped]

        indices = [i for i, region in enumerate(regions) if region is not None]
        if len(indices) < 2:
            return []
        tree = STRtree([regions[i] for i in indices])
        pairs: list[tuple[int, int]] = []
        for position, i in enumerate(indices):
            for candidate in tree.query(regions[i], predicate="dwithin", distance=reach):
                other = int(candidate)
                if other <= position:
                    continue
                j = indices[other]
                if fill_layers[i] == fill_layers[j]:
                    pairs.append((i, j))
        return pairs

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
        orphans: list[tuple[str, ...]] | None = None,
    ) -> ConnectivityIssue:
        """Create a connectivity issue for a net with multiple islands.

        Args:
            net_name: Name of the net
            pads: All pads on this net
            islands: List of disconnected pad islands
            orphans: Copper fragments that reach no pad (an orphan track or
                via), one description tuple each.  They follow the pad
                islands in ``islands`` -- each is a missing relationship
                kicad-cli reports -- but never enter ``unconnected_pads``.

        Returns:
            ConnectivityIssue describing the problem
        """
        orphans = list(orphans or [])
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

        total = len(islands) + len(orphans)
        issue_type = "partial"
        if len(islands) <= 1:
            message = (
                f"Net '{net_name}' has {len(orphans)} copper fragment(s) not connected to any pad"
            )
            suggestion = "Remove or connect the stranded track/via copper"
        elif total == 2:
            # Two islands - partial connection
            message = f"Net '{net_name}' has 2 disconnected islands"
            suggestion = (
                f"Connect islands (missing trace between {islands[0][-1]} and {islands[1][0]})"
            )
        else:
            # More than two islands
            message = f"Net '{net_name}' has {total} disconnected islands"
            suggestion = f"Connect {total} islands to complete routing"

        return ConnectivityIssue(
            severity="error",
            issue_type=issue_type,
            net_name=net_name,
            message=message,
            suggestion=suggestion,
            connected_pads=tuple(connected),
            unconnected_pads=tuple(unconnected),
            islands=tuple(tuple(island) for island in islands) + tuple(orphans),
        )

    def __repr__(self) -> str:
        """Return string representation."""
        net_count = self.pcb.net_count if self.pcb else 0
        return f"ConnectivityValidator(nets={net_count})"

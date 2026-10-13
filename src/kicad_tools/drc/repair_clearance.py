"""Trace clearance repair tool - nudge traces to fix DRC clearance violations.

This module provides non-destructive repair of clearance violations by
computing minimal displacements for traces and vias rather than deleting them.

For each clearance violation:
1. Identify the two objects (trace segment, via, pad)
2. Calculate required displacement to achieve minimum clearance + margin
3. Check if displacement is feasible (doesn't create new violations)
4. Apply the smallest valid displacement
5. Optionally re-run DRC to verify

Usage:
    from kicad_tools.drc.repair_clearance import ClearanceRepairer

    repairer = ClearanceRepairer("board.kicad_pcb")
    results = repairer.repair_from_report(report, max_displacement=0.1)
    repairer.save("board-fixed.kicad_pcb")

Honest accounting (Issues #6434 / #6431)
----------------------------------------
A violation is counted as *repaired* only when the pair it names has been
**re-measured** after the move and the measured edge-to-edge gap meets the
rule.  The two items are resolved from what the report says they are (the
kicad-cli item ``uuid``, the ``kct check`` geometry descriptor, or the item
type, net and position) -- never "the first track within 1.5 mm", which used
to resolve a via or pad location to the offending track itself and slide that
track along its own axis.  The move direction is the gradient of the measured
gap, i.e. perpendicular to a track and away from the other copper.  A move
that does not clear the pair, or that pushes the moved copper below the
clearance to some third item it used to clear, is undone and the pair is
listed in :attr:`RepairResult.unrepaired`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.geometry import rotate_pad_offset
from ..router.clearance_kernel import KPad, KSegment, KShape, KVia, copper_gap, make_pad
from ..schema.pcb import (
    _find_all_footprints,
    footprint_node_frame,
    transform_footprint_zone_nodes,
)
from ..sexp import SExp, parse_file
from .net_compat import resolve_net_atom
from .report import DRCReport
from .violation import DRCViolation, Location, ViolationType, _extract_component_refs

if TYPE_CHECKING:
    from .local_rerouter import LocalRerouter


# --- Item descriptions -> board copper (Issue #6434) -----------------------
#
# kicad-cli names each item of a violation ("Track [SPEED] on B.Cu, length
# 8.4 mm", "Via [GND] on F.Cu - B.Cu", "PTH pad 2 [+5V] of J4") and, in its
# JSON report, carries the item's uuid.  ``kct check`` names copper by
# geometry instead ("Trace@B.Cu:w0.2:171.9/114.8~171.9/123.2", "Via@x/y:...",
# "J4-2"; see :mod:`kicad_tools.validate.copper_refs`).  Both say exactly which
# item is meant, so the repairer resolves from them before it ever falls back
# to a proximity search.
_KICAD_TRACK = re.compile(r"^\s*(?:track|segment)\b", re.IGNORECASE)
_KICAD_VIA = re.compile(r"^\s*via\b", re.IGNORECASE)
_KICAD_PAD = re.compile(
    r"\bpad\s+(?P<num>[^\s\[]+)\s+(?:\[[^\]]*\]\s+)?of\s+(?P<ref>[^\s,]+)", re.IGNORECASE
)
_KICAD_NET = re.compile(r"\[([^\]]*)\]")
_KICAD_LAYER = re.compile(r"\bon\s+([A-Za-z0-9_.]+\.Cu)\b")
_KCT_TRACE = re.compile(
    r"^Trace@(?P<layer>[^:]+):w(?P<w>[-\d.]+):"
    r"(?P<x1>-?[\d.]+)/(?P<y1>-?[\d.]+)~(?P<x2>-?[\d.]+)/(?P<y2>-?[\d.]+)$"
)
_KCT_VIA = re.compile(r"^Via@(?P<x>-?[\d.]+)/(?P<y>-?[\d.]+):")
_KCT_PAD = re.compile(r"^(?P<ref>[^\s@:]+)-(?P<num>[^\s@:-]+)$")

# ``kct check`` descriptors are quantised to 1 um.
_DESCRIPTOR_TOLERANCE_MM = 0.0015
# Search radius for type/net-matched lookups around a reported item position.
_ITEM_SEARCH_RADIUS_MM = 1.5
# Measured-gap comparisons: KiCad reports to 0.1 um.
_GAP_EPSILON_MM = 1e-6


@dataclass(eq=False)
class _Copper:
    """One piece of board copper the repairer can measure (and maybe move)."""

    node: SExp
    kind: str  # "segment", "via" or "pad"
    net: str
    fp_node: SExp | None = None  # owning footprint (pads only)

    @property
    def movable(self) -> bool:
        return self.kind in ("segment", "via") and not _is_locked(self.node)


def _is_locked(node: SExp) -> bool:
    """KiCad's lock flag: ``(locked yes)`` (8+) or a bare ``locked`` atom (6/7)."""
    if any(str(atom) == "locked" for atom in node.get_atoms()):
        return True
    for child in node.children:
        if child.name == "locked":
            atoms = child.get_atoms()
            return not atoms or str(atoms[0]) in ("yes", "true")
    return False


def _format_mm(value: float) -> str:
    """A coordinate as KiCad writes it: up to 6 decimals, no trailing zeros."""
    text = f"{round(value, 6):.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _write_xy(point_node: SExp, x: float, y: float) -> None:
    """Set a ``(start|end|at x y ...)`` pair so it serialises at full precision.

    The generic float formatter keeps six significant digits, which rounds a
    coordinate above 100 mm to the micron (149.1876 -> 149.188): the saved
    board would differ from the geometry the repairer measured, and a dragged
    0.3 um stub would collapse to a point (Issue #6434).
    """
    for index, value in ((0, x), (1, y)):
        point_node.set_value(index, value)
        point_node.children[index]._original_str = _format_mm(value)


def _bbox_of(shape: KShape) -> tuple[float, float, float, float]:
    """Axis-aligned bounds of a kernel shape (copper extent included)."""
    if isinstance(shape, KSegment):
        r = shape.width / 2
        return (
            min(shape.x1, shape.x2) - r,
            min(shape.y1, shape.y2) - r,
            max(shape.x1, shape.x2) + r,
            max(shape.y1, shape.y2) + r,
        )
    if isinstance(shape, KVia):
        r = shape.diameter / 2
        return (shape.x - r, shape.y - r, shape.x + r, shape.y + r)
    assert isinstance(shape, KPad)
    xs = [p[0] for p in shape.core] or [shape.cx]
    ys = [p[1] for p in shape.core] or [shape.cy]
    r = shape.corner_radius
    return (min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)


def _boxes_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float], pad: float
) -> bool:
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])


@dataclass
class NudgeResult:
    """Record of a nudge applied to fix a clearance violation."""

    object_type: str  # "segment" or "via"
    x: float
    y: float
    net_name: str
    layer: str
    displacement_x: float
    displacement_y: float
    displacement_mm: float
    old_clearance_mm: float
    new_clearance_mm: float
    uuid: str
    # Every coordinate this nudge wrote, so it can be undone exactly even when
    # it dragged connected track ends along (Issue #6434):
    # ``(object_type, uuid, point, old_x, old_y, new_x, new_y)`` where
    # ``point`` is ``"start"``, ``"end"`` or ``"at"``.
    edits: list[tuple[str, str, str, float, float, float, float]] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.object_type} [{self.net_name}] at ({self.x:.4f}, {self.y:.4f}) on {self.layer}: "
            f"nudged {self.displacement_mm:.4f}mm "
            f"(clearance {self.old_clearance_mm:.4f} -> {self.new_clearance_mm:.4f}mm)"
        )


@dataclass
class FootprintNudgeResult:
    """Record of a footprint nudge applied to fix a pad-pad clearance violation."""

    reference: str
    x: float
    y: float
    displacement_x: float
    displacement_y: float
    displacement_mm: float
    old_clearance_mm: float
    new_clearance_mm: float
    other_reference: str

    def __str__(self) -> str:
        return (
            f"footprint [{self.reference}] at ({self.x:.4f}, {self.y:.4f}): "
            f"nudged {self.displacement_mm:.4f}mm away from {self.other_reference} "
            f"(clearance {self.old_clearance_mm:.4f} -> {self.new_clearance_mm:.4f}mm)"
        )


@dataclass
class UnrepairedPair:
    """A violation the repairer could not clear, with the reason (Issue #6434)."""

    items: list[str]
    nets: list[str]
    required_mm: float | None
    reported_mm: float | None
    measured_mm: float | None
    reason: str
    violation_id: int = field(default=0, repr=False, compare=False)

    def __str__(self) -> str:
        names = " <-> ".join(self.items) if self.items else "(unidentified items)"
        values = []
        if self.measured_mm is not None:
            values.append(f"measured {self.measured_mm:.4f} mm")
        elif self.reported_mm is not None:
            values.append(f"reported {self.reported_mm:.4f} mm")
        if self.required_mm is not None:
            values.append(f"required {self.required_mm:.4f} mm")
        value_str = f" ({', '.join(values)})" if values else ""
        return f"{names}{value_str}: {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": list(self.items),
            "nets": list(self.nets),
            "required_mm": self.required_mm,
            "reported_mm": self.reported_mm,
            "measured_mm": None if self.measured_mm is None else round(self.measured_mm, 4),
            "reason": self.reason,
        }


@dataclass
class RepairResult:
    """Summary of a clearance repair operation."""

    total_violations: int = 0
    repaired: int = 0
    skipped_no_location: int = 0
    skipped_not_clearance: int = 0
    skipped_no_delta: int = 0
    skipped_exceeds_max: int = 0
    skipped_infeasible: int = 0
    # Moves were tried but none brought the re-measured gap up to the rule
    # (or every one would have created a new violation) -- Issue #6434.
    skipped_not_cleared: int = 0
    relocated_vias: int = 0
    endpoint_nudges: int = 0
    local_rerouted: int = 0
    cluster_rerouted: int = 0
    skipped_no_local_route: int = 0
    footprint_nudges: int = 0
    footprint_skipped_locked: int = 0
    footprint_skipped_connector: int = 0
    footprint_skipped_same_component: int = 0
    footprint_skipped_exceeds_max: int = 0
    # Dragged track pieces collapsed to a point and deleted (Issue #6434).
    removed_zero_length: int = 0
    nudges: list[NudgeResult] = field(default_factory=list)
    footprint_nudge_results: list[FootprintNudgeResult] = field(default_factory=list)
    unrepaired: list[UnrepairedPair] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        """Fraction of violations that were repaired."""
        if self.total_violations == 0:
            return 1.0
        return self.repaired / self.total_violations

    def summary(self) -> str:
        """Generate a human-readable summary."""
        lines = [
            f"Clearance Repair: {self.repaired}/{self.total_violations} violations fixed",
        ]
        if self.endpoint_nudges > 0:
            lines.append(f"  Endpoint nudges (via-preserving): {self.endpoint_nudges}")
        if self.relocated_vias > 0:
            lines.append(f"  Via relocations: {self.relocated_vias}")
        if self.local_rerouted > 0:
            lines.append(f"  Local reroutes: {self.local_rerouted}")
        if self.cluster_rerouted > 0:
            lines.append(f"  Cluster reroutes: {self.cluster_rerouted}")
        if self.footprint_nudges > 0:
            lines.append(f"  Footprint nudges (pad-pad): {self.footprint_nudges}")
        if self.footprint_skipped_locked > 0:
            lines.append(f"  Skipped (footprint locked): {self.footprint_skipped_locked}")
        if self.footprint_skipped_connector > 0:
            lines.append(f"  Skipped (connector footprint): {self.footprint_skipped_connector}")
        if self.footprint_skipped_same_component > 0:
            lines.append(
                f"  Skipped (same component pads): {self.footprint_skipped_same_component}"
            )
        if self.skipped_exceeds_max > 0:
            lines.append(f"  Skipped (exceeds max displacement): {self.skipped_exceeds_max}")
        if self.skipped_infeasible > 0:
            lines.append(f"  Skipped (infeasible): {self.skipped_infeasible}")
        if self.skipped_not_cleared > 0:
            lines.append(f"  Not cleared (re-measured): {self.skipped_not_cleared}")
        if self.skipped_no_local_route > 0:
            lines.append(f"  Skipped (no local route): {self.skipped_no_local_route}")
        if self.skipped_no_location > 0:
            lines.append(f"  Skipped (no location): {self.skipped_no_location}")
        if self.skipped_no_delta > 0:
            lines.append(f"  Skipped (no delta info): {self.skipped_no_delta}")
        if self.removed_zero_length > 0:
            lines.append(f"  Collapsed zero-length tracks removed: {self.removed_zero_length}")
        for pair in self.unrepaired:
            lines.append(f"  Unrepaired: {pair}")
        return "\n".join(lines)


class ClearanceRepairer:
    """Repairs clearance violations by nudging traces and vias.

    Unlike DRCFixer which deletes traces, this tool computes minimal
    displacements to achieve the required clearance without removing
    any routing.
    """

    def __init__(self, pcb_path: str | Path, project_pcb_path: str | Path | None = None):
        """Load a PCB file for repair.

        Args:
            pcb_path: Path to .kicad_pcb file
            project_pcb_path: The board whose sibling ``.kicad_pro`` carries
                the netclass clearances (defaults to ``pcb_path``).  Pass the
                *input* board when repairing a copy written elsewhere, so the
                collateral-damage check still knows the project's classes.
        """
        self.path = Path(pcb_path)
        self.doc = parse_file(self.path)
        self.modified = False

        # Build net index
        self.nets: dict[int, str] = {}
        self.net_names: dict[str, int] = {}
        self._parse_nets()

        # Nudge history for oscillation detection: maps UUID -> (cumulative_dx, cumulative_dy)
        self._nudge_history: dict[str, tuple[float, float]] = {}

        # Issue #6434: copper index, measurement caches and the edit journal
        # that lets a trial move be measured and undone exactly.
        self._project_pcb_path = Path(project_pcb_path) if project_pcb_path else self.path
        self._index: list[_Copper] | None = None
        self._by_node: dict[int, _Copper] = {}
        self._by_uuid: dict[str, _Copper] = {}
        self._shape_cache: dict[int, tuple[KShape, tuple[float, float, float, float]]] = {}
        self._journal: list[tuple[SExp, SExp, float, float]] | None = None
        self._net_clearance: dict[str, float] | None = None
        self._net_clearance_loaded = False
        self._report_min_required = 0.0
        self._report_max_required = 0.2
        # (violation, target, other, required) for every counted nudge repair,
        # re-measured once more after the whole report has been processed.
        self._repaired_pairs: list[tuple[DRCViolation, _Copper, _Copper, float]] = []
        self._initial_gaps: dict[int, float] = {}
        # Segments a kept move edited: candidates for zero-length cleanup.
        self._edited_segments: dict[int, SExp] = {}
        # (violation, a, b, required, counter) for every nudge-phase failure
        # whose items were resolved: re-measured at the end, since a later
        # move (a shared or dragged track) can clear it after all.
        self._failed_pairs: list[tuple[DRCViolation, _Copper, _Copper, float, str]] = []

    def _parse_nets(self):
        """Parse net definitions from the PCB.

        Only iterates top-level children of the PCB root node to avoid
        finding nested ``(net N)`` attribute nodes inside zones, segments,
        vias, and pads.  Top-level net definitions always have the form
        ``(net <number> "<name>")``.
        """
        for child in self.doc.children:
            if child.name != "net":
                continue
            atoms = child.get_atoms()
            if len(atoms) < 2:
                continue
            try:
                net_num = int(atoms[0])
            except (ValueError, TypeError):
                continue
            net_name = str(atoms[1])
            self.nets[net_num] = net_name
            self.net_names[net_name] = net_num

        if self.nets:
            return
        # KiCad 10 boards may carry name-only ``(net "GND")`` atoms and no
        # top-level net table.  Every net would then resolve to number 0, so
        # "same net" comparisons (here and in the local rerouter) would see
        # one net for everything -- or none.  Number the names in file order;
        # the numbers are never written back (Issue #6434).
        for node in self.doc.iter_all():
            if node.name != "net" or node is self.doc:
                continue
            atoms = node.get_atoms()
            if len(atoms) != 1:
                continue
            name = str(atoms[0])
            if not name or name.lstrip("-").isdigit() or name in self.net_names:
                continue
            number = len(self.nets) + 1
            self.nets[number] = name
            self.net_names[name] = number

    def repair_from_report(
        self,
        report: DRCReport,
        max_displacement: float = 0.1,
        margin: float = 0.01,
        prefer: str = "move-trace",
        dry_run: bool = False,
        local_reroute: bool = False,
        local_grid_padding: float = 0.5,
        nudge_footprints: bool = False,
    ) -> RepairResult:
        """Repair clearance violations using a DRC report.

        Args:
            report: Parsed DRC report with violations
            max_displacement: Maximum allowed nudge distance in mm
            margin: Extra clearance margin beyond minimum in mm
            prefer: Which object to move when both are movable
                    ("move-trace" or "move-via")
            dry_run: If True, compute repairs but don't modify PCB
            local_reroute: If True, attempt local A* rerouting for
                          infeasible violations after nudge phase
            local_grid_padding: Padding around segment bounding box for
                               local reroute grid (mm, default: 0.5)
            nudge_footprints: If True, attempt footprint nudges for
                             pad-pad clearance violations (Phase 3)

        Returns:
            RepairResult with details of all repairs
        """
        result = RepairResult()
        self._repaired_pairs = []
        self._failed_pairs = []

        clearances = [
            v
            for v in report.by_type(ViolationType.CLEARANCE)
            if not self._is_zone_fill_violation(v)
        ]
        segment_via_clearances = [
            v
            for v in report.by_type(ViolationType.CLEARANCE_SEGMENT_VIA)
            if not self._is_zone_fill_violation(v)
        ]
        pad_segment_clearances = [
            v
            for v in report.by_type(ViolationType.CLEARANCE_PAD_SEGMENT)
            if not self._is_zone_fill_violation(v)
        ]
        pad_via_clearances = [
            v
            for v in report.by_type(ViolationType.CLEARANCE_PAD_VIA)
            if not self._is_zone_fill_violation(v)
        ]
        # ``kct check`` names track/track and via/via pairs explicitly where
        # kicad-cli reports them as plain ``clearance`` (Issue #6431).
        clearances += [
            v
            for vtype in (ViolationType.CLEARANCE_SEGMENT_SEGMENT, ViolationType.CLEARANCE_VIA_VIA)
            for v in report.by_type(vtype)
            if not self._is_zone_fill_violation(v)
        ]
        all_clearances = (
            clearances + segment_via_clearances + pad_segment_clearances + pad_via_clearances
        )
        result.total_violations = len(all_clearances)

        # Thresholds for the collateral-damage check when the project carries
        # no netclass table: the report's own requirements bound the rules.
        required_values = [
            v.required_value_mm for v in all_clearances if v.required_value_mm is not None
        ]
        if required_values:
            self._report_min_required = min(required_values)
            self._report_max_required = max(required_values)

        # Gap of every resolvable pair before anything moves: a pair that was
        # in violation and measures clear by the time it is reached was
        # cleared by an earlier nudge (shared track, dragged neighbour); one
        # that never measured in violation is a stale report.
        self._initial_gaps = {}
        for violation in all_clearances:
            a, b = self._resolve_pair(violation)
            if a is not None and b is not None:
                self._initial_gaps[id(violation)] = self._measure(a, b)

        # Violations that the nudge phase could not clear, with the counter
        # they were booked under (so a successful reroute can move them over).
        failed: list[tuple[DRCViolation, str]] = []

        # ``prefer`` orders the candidates; the other movable item is still
        # tried when the preferred one cannot clear the pair (Issue #6434:
        # ``--prefer move-via`` used to be overridden for every track/via
        # pair).  Pads never move.
        for violation in clearances + segment_via_clearances:
            outcome = self._repair_single_violation(
                violation, result, max_displacement, margin, prefer, dry_run
            )
            if outcome in ("infeasible", "not_cleared"):
                failed.append((violation, outcome))

        for violation in pad_segment_clearances:
            outcome = self._repair_single_violation(
                violation, result, max_displacement, margin, "move-trace", dry_run
            )
            if outcome in ("infeasible", "not_cleared"):
                failed.append((violation, outcome))

        for violation in pad_via_clearances:
            outcome = self._repair_single_violation(
                violation, result, max_displacement, margin, "move-via", dry_run
            )
            if outcome in ("infeasible", "not_cleared"):
                failed.append((violation, outcome))

        # Phase 2: Local rerouting for violations nudging could not clear
        if local_reroute and failed:
            self._run_local_reroute_phase(failed, result, margin, dry_run, local_grid_padding)

        # A later move can push copper back into an earlier pair: re-measure
        # every counted nudge repair against the final geometry.
        self._reverify_repaired_pairs(result)
        if not dry_run:
            self._drop_collapsed_segments(result)

        # Phase 3: Footprint nudging for pad-pad violations
        if nudge_footprints:
            self._repair_pad_pad_violations(report, result, max_displacement, margin, dry_run)

        return result

    def _run_local_reroute_phase(
        self,
        tagged_violations: list[tuple[DRCViolation, str]],
        result: RepairResult,
        margin: float,
        dry_run: bool,
        local_grid_padding: float,
    ) -> None:
        """Attempt local A* rerouting for violations that nudging could not fix.

        Each violation is tagged with the counter the nudge phase booked it
        under (``"infeasible"`` or ``"not_cleared"``); a reroute that is
        verified to clear the pair moves it from that counter to ``repaired``.

        For each violation, identifies the segment and tries to reroute it
        around the obstacle using a local A* grid.

        Violations are first grouped by spatial proximity so that clustered
        violations (e.g. multiple vias near connected segments) can be
        rerouted with awareness of all obstacles in the cluster.
        """
        from .local_rerouter import LocalRerouter

        rerouter = LocalRerouter(
            doc=self.doc,
            nets=self.nets,
            resolution=0.05,
            padding=local_grid_padding,
        )

        # Group violations by spatial proximity for cluster-aware rerouting
        clusters = self._group_violations_by_proximity(tagged_violations)

        for cluster in clusters:
            if len(cluster) == 1:
                # Single violation -- use existing per-violation reroute
                violation, source = cluster[0]
                self._attempt_local_reroute(violation, result, rerouter, margin, dry_run, source)
            else:
                # Multi-violation cluster -- attempt cluster-aware reroute
                self._attempt_cluster_reroute(cluster, result, rerouter, margin, dry_run)

    def _group_violations_by_proximity(
        self,
        tagged_violations: list[tuple[DRCViolation, str]],
        cluster_radius: float | None = None,
    ) -> list[list[tuple[DRCViolation, str]]]:
        """Group violations by spatial proximity using greedy distance clustering.

        Violations whose primary locations are within ``cluster_radius`` of any
        existing member of the cluster are merged into the same group.  The
        default radius is ``2 * max_clearance_radius`` where the clearance
        radius is estimated from the violations' required_value_mm (falling
        back to 0.2 mm).

        Uses a simple greedy clustering algorithm similar to
        ``ThermalAnalyzer._cluster_sources()`` in ``analysis/thermal.py``.

        Args:
            tagged_violations: List of (violation, source_tag) tuples.
            cluster_radius: Maximum distance (mm) between two violation
                locations for them to be in the same cluster.  If *None*,
                a default of ``2 * max(required_value_mm)`` is used.

        Returns:
            List of clusters, each cluster being a list of (violation, tag)
            tuples.
        """
        if not tagged_violations:
            return []

        # Determine cluster radius from violations if not specified
        if cluster_radius is None:
            max_clearance = max(
                (v.required_value_mm or 0.2 for v, _ in tagged_violations),
                default=0.2,
            )
            cluster_radius = 2.0 * max_clearance

        # Extract primary locations for distance computation
        positions: list[tuple[float, float] | None] = []
        for violation, _ in tagged_violations:
            loc = violation.primary_location
            if loc is not None:
                positions.append((loc.x_mm, loc.y_mm))
            else:
                positions.append(None)

        clusters: list[list[tuple[DRCViolation, str]]] = []
        assigned: set[int] = set()

        for i, (violation_i, source_i) in enumerate(tagged_violations):
            if i in assigned:
                continue

            pos_i = positions[i]
            cluster: list[tuple[DRCViolation, str]] = [(violation_i, source_i)]
            assigned.add(i)

            if pos_i is None:
                clusters.append(cluster)
                continue

            # Greedy expansion: check unassigned violations for proximity
            # to any member already in the cluster.
            cluster_positions = [pos_i]
            changed = True
            while changed:
                changed = False
                for j in range(len(tagged_violations)):
                    if j in assigned:
                        continue
                    pos_j = positions[j]
                    if pos_j is None:
                        continue

                    # Check distance to every existing cluster member
                    for cp in cluster_positions:
                        dx = pos_j[0] - cp[0]
                        dy = pos_j[1] - cp[1]
                        dist = math.sqrt(dx * dx + dy * dy)
                        if dist <= cluster_radius:
                            cluster.append(tagged_violations[j])
                            assigned.add(j)
                            cluster_positions.append(pos_j)
                            changed = True
                            break

            clusters.append(cluster)

        return clusters

    def _attempt_cluster_reroute(
        self,
        cluster: list[tuple[DRCViolation, str]],
        result: RepairResult,
        rerouter: LocalRerouter,
        margin: float,
        dry_run: bool,
    ) -> None:
        """Attempt to reroute violations in a cluster with shared obstacle awareness.

        For each violation in the cluster, the obstacles from the *other*
        violations in the same cluster are passed as ``extra_obstacles`` to
        ``reroute_segment()``.  This ensures the A* search avoids all nearby
        obstacles in one pass rather than treating each violation in isolation.

        If cluster-aware rerouting fails for a violation, it falls back to
        the standard per-violation ``_attempt_local_reroute()``.
        """
        # Pre-extract obstacle info for every violation in the cluster
        cluster_obstacles: list[tuple[float, float, float] | None] = []
        for violation, _ in cluster:
            obs_info = self._extract_obstacle_info(violation, margin)
            cluster_obstacles.append(obs_info)

        for idx, (violation, source) in enumerate(cluster):
            # Build extra_obstacles from all *other* cluster members
            extra: list[tuple[float, float, float]] = []
            for other_idx, obs in enumerate(cluster_obstacles):
                if other_idx != idx and obs is not None:
                    extra.append(obs)

            success = self._attempt_local_reroute_with_extras(
                violation, result, rerouter, margin, dry_run, source, extra
            )
            if not success:
                # Fall back to standard per-violation reroute (without extras)
                self._attempt_local_reroute(violation, result, rerouter, margin, dry_run, source)

    def _reroute_plan(
        self, violation: DRCViolation
    ) -> tuple[_Copper, _Copper, float, float, float] | None:
        """The segment to reroute and its obstacle ``(x, y, radius)``.

        Resolved through :meth:`_resolve_pair` (Issue #6434), so the obstacle
        is the *other* item of the violation, never the segment itself.
        """
        a, b = self._resolve_pair(violation)
        if a is None or b is None:
            return None
        if a.kind == "segment":
            seg, obstacle = a, b
        elif b.kind == "segment":
            seg, obstacle = b, a
        else:
            return None
        if obstacle.kind == "via":
            obs_x, obs_y = self._via_xy(obstacle.node)
            size_node = obstacle.node.find("size")
            radius = float(size_node.get_first_atom()) / 2 if size_node else 0.3
        elif obstacle.kind == "pad":
            shape = self._shape_of(obstacle)
            assert isinstance(shape, KPad)
            obs_x, obs_y = shape.cx, shape.cy
            radius = self._pad_obstacle_radius(obstacle.node)
        else:
            # Segment obstacle: the point on it closest to the rerouted track.
            sx1, sy1, sx2, sy2 = self._segment_xy(seg.node)
            ox1, oy1, ox2, oy2 = self._segment_xy(obstacle.node)
            mid_x, mid_y = (sx1 + sx2) / 2, (sy1 + sy2) / 2
            closest = self._closest_point_on_segment(ox1, oy1, ox2, oy2, mid_x, mid_y)
            assert closest is not None
            obs_x, obs_y = closest[0], closest[1]
            radius = self._get_node_width(obstacle.node) / 2
        return seg, obstacle, obs_x, obs_y, radius

    def _extract_obstacle_info(
        self,
        violation: DRCViolation,
        margin: float,
    ) -> tuple[float, float, float] | None:
        """Extract (x, y, radius) obstacle tuple from a violation.

        Returns the position and radius of the non-segment object in the
        violation, or None if the obstacle cannot be determined.
        """
        plan = self._reroute_plan(violation)
        if plan is None:
            return None
        _seg, _obstacle, obs_x, obs_y, radius = plan
        return (obs_x, obs_y, radius)

    def _pad_obstacle_radius(self, pad_node: SExp) -> float:
        """Estimate the effective radius of a pad for obstacle avoidance.

        Uses the larger of the pad's width and height divided by 2.
        Falls back to 0.5 mm if the size cannot be determined.
        """
        size_node = pad_node.find("size")
        if size_node:
            atoms = size_node.get_atoms()
            w = float(atoms[0]) if atoms else 1.0
            h = float(atoms[1]) if len(atoms) > 1 else w
            return max(w, h) / 2
        return 0.5

    def _attempt_local_reroute_with_extras(
        self,
        violation: DRCViolation,
        result: RepairResult,
        rerouter: LocalRerouter,
        margin: float,
        dry_run: bool,
        source: str,
        extra_obstacles: list[tuple[float, float, float]],
    ) -> bool:
        """Attempt local reroute with extra obstacle awareness.

        Same as ``_attempt_local_reroute`` but passes ``extra_obstacles``
        to ``rerouter.reroute_segment()``.

        Returns True if rerouting succeeded, False otherwise.  Does **not**
        update ``result`` counters on failure (the caller handles fallback).
        """
        return self._reroute_violation(
            violation, result, rerouter, margin, dry_run, source, extra_obstacles, cluster=True
        )

    def _attempt_local_reroute(
        self,
        violation: DRCViolation,
        result: RepairResult,
        rerouter: LocalRerouter,
        margin: float,
        dry_run: bool,
        source: str = "infeasible",
    ) -> None:
        """Attempt to locally reroute a single violation nudging could not clear.

        Args:
            source: The counter the nudge phase booked the violation under
                (``"infeasible"`` or ``"not_cleared"``).
        """
        if not self._reroute_violation(
            violation, result, rerouter, margin, dry_run, source, None, cluster=False
        ):
            result.skipped_no_local_route += 1

    def _reroute_violation(
        self,
        violation: DRCViolation,
        result: RepairResult,
        rerouter: LocalRerouter,
        margin: float,
        dry_run: bool,
        source: str,
        extra_obstacles: list[tuple[float, float, float]] | None,
        cluster: bool,
    ) -> bool:
        """Reroute the violation's segment around its obstacle.

        A reroute counts as a repair only when the obstacle's re-measured gap
        to every piece of the rerouted net meets the rule (Issue #6434).
        Returns whether the violation now counts as repaired.
        """
        if violation.delta_mm is None:
            return False
        plan = self._reroute_plan(violation)
        if plan is None:
            return False
        seg, obstacle, obs_x, obs_y, obstacle_radius = plan
        seg_node = seg.node

        width_node = seg_node.find("width")
        trace_width = float(width_node.get_first_atom()) if width_node else 0.25

        required_clearance = violation.required_value_mm or 0.2
        trace_clearance = required_clearance + margin

        # For seg-seg violations, pass the obstacle segment as a same-net obstacle
        same_net_obs: list[SExp] | None = None
        if obstacle.kind == "segment":
            same_net_obs = [obstacle.node]

        kwargs: dict[str, Any] = {}
        if extra_obstacles is not None:
            kwargs["extra_obstacles"] = extra_obstacles
        seg_layer_node = seg_node.find("layer")
        seg_layer = str(seg_layer_node.get_first_atom()) if seg_layer_node else ""
        reroute_result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=obs_x,
            obstacle_y=obs_y,
            obstacle_radius=obstacle_radius,
            trace_width=trace_width,
            trace_clearance=trace_clearance,
            dry_run=dry_run,
            same_net_obstacle_segs=same_net_obs,
            **kwargs,
        )
        if not reroute_result.success:
            return False

        if not dry_run:
            self.modified = True
            # The rerouter replaced copper: rebuild the index before measuring.
            self._invalidate_index()
            measured = self._measure_net_to_item(seg.net, seg_layer, obstacle)
            if measured is None or measured + _GAP_EPSILON_MM < required_clearance:
                reason = (
                    "local reroute could not be re-measured (no copper of the net found)"
                    if measured is None
                    else f"local reroute left {measured:.4f} mm to the obstacle"
                )
                self._note_unrepaired(result, violation, measured, reason, replace=True)
                return True  # geometry changed; do not try a second reroute

        result.local_rerouted += 1
        if cluster:
            result.cluster_rerouted += 1
        result.repaired += 1
        if source == "not_cleared":
            result.skipped_not_cleared -= 1
        else:
            result.skipped_infeasible -= 1
        self._drop_unrepaired(result, violation)
        return True

    def _would_oscillate(self, uuid_str: str, dx: float, dy: float) -> bool:
        """Check if a proposed nudge would reverse a previous nudge direction.

        A nudge oscillates when the dot product of the proposed displacement
        with the cumulative displacement so far is negative, meaning the object
        is being pushed back toward where it came from.

        Args:
            uuid_str: UUID of the object being nudged.
            dx: Proposed displacement in X.
            dy: Proposed displacement in Y.

        Returns:
            True if the nudge would reverse direction (oscillation detected).
        """
        if not uuid_str or uuid_str not in self._nudge_history:
            return False
        prev_dx, prev_dy = self._nudge_history[uuid_str]
        dot = prev_dx * dx + prev_dy * dy
        return dot < 0

    def _record_nudge(self, uuid_str: str, dx: float, dy: float) -> None:
        """Record a nudge in the history for oscillation detection.

        Args:
            uuid_str: UUID of the object being nudged.
            dx: Applied displacement in X.
            dy: Applied displacement in Y.
        """
        if not uuid_str:
            return
        if uuid_str in self._nudge_history:
            prev_dx, prev_dy = self._nudge_history[uuid_str]
            self._nudge_history[uuid_str] = (prev_dx + dx, prev_dy + dy)
        else:
            self._nudge_history[uuid_str] = (dx, dy)

    # ------------------------------------------------------------------
    # Copper index, shapes and measurement (Issue #6434)
    # ------------------------------------------------------------------

    def _invalidate_index(self) -> None:
        """Forget the copper index (copper was added, removed or re-placed)."""
        self._index = None
        self._by_node = {}
        self._by_uuid = {}
        self._shape_cache = {}

    def _ensure_index(self) -> list[_Copper]:
        """Every segment, via and pad on the board, built once and cached."""
        if self._index is not None:
            return self._index
        index: list[_Copper] = []
        by_uuid: dict[str, _Copper] = {}
        for child in self.doc.children:
            if child.name not in ("segment", "via"):
                continue
            copper = _Copper(node=child, kind=child.name, net=self._node_net(child))
            index.append(copper)
            uuid = self._node_uuid(child)
            if uuid:
                by_uuid[uuid] = copper
        for fp_node in _find_all_footprints(self.doc):
            for pad_node in fp_node.find_all("pad"):
                if not pad_node.find("at"):
                    continue
                copper = _Copper(
                    node=pad_node, kind="pad", net=self._node_net(pad_node), fp_node=fp_node
                )
                index.append(copper)
                uuid = self._node_uuid(pad_node)
                if uuid:
                    by_uuid[uuid] = copper
        self._index = index
        self._by_node = {id(c.node): c for c in index}
        self._by_uuid = by_uuid
        self._shape_cache = {}
        return index

    @staticmethod
    def _node_uuid(node: SExp) -> str:
        uuid_node = node.find("uuid")
        if not uuid_node:
            return ""
        atom = uuid_node.get_first_atom()
        return str(atom).strip('"') if atom is not None else ""

    def _node_net(self, node: SExp) -> str:
        """Net name of a segment, via or pad (KiCad 8 numeric or 9+ name form)."""
        net_node = node.find("net")
        if not net_node:
            return ""
        atoms = net_node.get_atoms()
        _num, name = resolve_net_atom(str(atoms[0]) if atoms else None, self.nets, self.net_names)
        if not name and len(atoms) > 1:
            name = str(atoms[1])
        return "" if name == "<no net>" else name

    @staticmethod
    def _xy(node: SExp | None) -> tuple[float, float]:
        if not node:
            return (0.0, 0.0)
        atoms = node.get_atoms()
        x = float(atoms[0]) if atoms else 0.0
        y = float(atoms[1]) if len(atoms) > 1 else 0.0
        return (x, y)

    def _segment_xy(self, node: SExp) -> tuple[float, float, float, float]:
        sx, sy = self._xy(node.find("start"))
        ex, ey = self._xy(node.find("end"))
        return (sx, sy, ex, ey)

    def _via_xy(self, node: SExp) -> tuple[float, float]:
        return self._xy(node.find("at"))

    def _segment_layer(self, node: SExp) -> str:
        layer_node = node.find("layer")
        return str(layer_node.get_first_atom()) if layer_node else ""

    def _pad_layers(self, node: SExp) -> list[str]:
        layers_node = node.find("layers")
        return [str(a) for a in layers_node.get_atoms()] if layers_node else []

    def _build_shape(self, copper: _Copper) -> KShape:
        node = copper.node
        if copper.kind == "segment":
            sx, sy, ex, ey = self._segment_xy(node)
            return KSegment(sx, sy, ex, ey, self._get_node_width(node))
        if copper.kind == "via":
            vx, vy = self._via_xy(node)
            size_node = node.find("size")
            size = float(size_node.get_first_atom()) if size_node else 0.6
            return KVia(vx, vy, size)
        # Pad: footprint-local offset rotated by KiCad's forward transform; the
        # pad's own ``(at x y rot)`` angle is already absolute (issue #3902).
        assert copper.fp_node is not None
        fp_atoms = copper.fp_node.find("at").get_atoms()  # type: ignore[union-attr]
        fx = float(fp_atoms[0]) if fp_atoms else 0.0
        fy = float(fp_atoms[1]) if len(fp_atoms) > 1 else 0.0
        frot = float(fp_atoms[2]) if len(fp_atoms) > 2 else 0.0
        pad_atoms = node.find("at").get_atoms()  # type: ignore[union-attr]
        lx = float(pad_atoms[0]) if pad_atoms else 0.0
        ly = float(pad_atoms[1]) if len(pad_atoms) > 1 else 0.0
        prot = float(pad_atoms[2]) if len(pad_atoms) > 2 else 0.0
        ox, oy = rotate_pad_offset(lx, ly, frot)
        head = node.get_atoms()
        shape_name = str(head[2]) if len(head) > 2 else "rect"
        size_node = node.find("size")
        size_atoms = size_node.get_atoms() if size_node else []
        w = float(size_atoms[0]) if size_atoms else 0.0
        h = float(size_atoms[1]) if len(size_atoms) > 1 else w
        rratio_node = node.find("roundrect_rratio")
        rratio = float(rratio_node.get_first_atom()) if rratio_node else 0.25
        return make_pad(shape_name, w, h, rratio, prot, fx + ox, fy + oy)

    def _shape_of(self, copper: _Copper) -> KShape:
        cached = self._shape_cache.get(id(copper.node))
        if cached is not None:
            return cached[0]
        shape = self._build_shape(copper)
        self._shape_cache[id(copper.node)] = (shape, _bbox_of(shape))
        return shape

    def _bbox(self, copper: _Copper) -> tuple[float, float, float, float]:
        cached = self._shape_cache.get(id(copper.node))
        if cached is None:
            self._shape_of(copper)
            cached = self._shape_cache[id(copper.node)]
        return cached[1]

    def _measure(self, a: _Copper, b: _Copper) -> float:
        """Exact edge-to-edge copper gap between two items (shared kernel)."""
        return copper_gap(self._shape_of(a), self._shape_of(b))

    def _copper_layers(self, copper: _Copper) -> set[str] | None:
        """Copper layers an item occupies; ``None`` means every copper layer."""
        if copper.kind == "segment":
            return {self._segment_layer(copper.node)}
        if copper.kind == "via":
            return None
        layers = self._pad_layers(copper.node)
        if any(layer in ("*.Cu", "F&B.Cu") for layer in layers):
            return None
        return {layer for layer in layers if layer.endswith(".Cu")}

    def _interacts(self, a: _Copper, b: _Copper) -> bool:
        la, lb = self._copper_layers(a), self._copper_layers(b)
        if la is None or lb is None:
            return True
        return bool(la & lb)

    def _load_net_clearances(self) -> dict[str, float] | None:
        """Per-net clearance from the project's netclasses (``None`` if unknown)."""
        if self._net_clearance_loaded:
            return self._net_clearance
        self._net_clearance_loaded = True
        pro_path = self._project_pcb_path.with_suffix(".kicad_pro")
        try:
            from ..core.project_clearance import resolve_project_clearances

            project = json.loads(pro_path.read_text(encoding="utf-8"))
            resolved = resolve_project_clearances(project, list(self.net_names))
        except Exception:  # noqa: BLE001 - no/unsupported project: use report bounds
            return None
        self._net_clearance = {name: value.clearance for name, value in resolved.items()}
        default = None
        try:
            for item in project["net_settings"]["classes"]:
                if item.get("name") == "Default" and "clearance" in item:
                    default = float(item["clearance"])
        except Exception:  # noqa: BLE001
            default = None
        self._net_clearance[""] = default if default is not None else self._report_min_required
        return self._net_clearance

    def _pair_requirement(self, net_a: str, net_b: str) -> float:
        """Clearance two foreign nets need, for the collateral-damage check.

        The project's netclasses when they can be read (``max`` of the two
        nets' classes, never below the smallest requirement in the report);
        otherwise the strictest requirement in the report, which errs on the
        side of declining a move.
        """
        table = self._load_net_clearances()
        if not table:
            return self._report_max_required
        default = table.get("", self._report_min_required)
        return max(table.get(net_a, default), table.get(net_b, default), self._report_min_required)

    # ------------------------------------------------------------------
    # Resolving a violation's two items (Issue #6434)
    # ------------------------------------------------------------------

    @staticmethod
    def _item_locations(violation: DRCViolation) -> list[Location | None]:
        """Per-item positions: kicad-cli may prepend a violation-level ``pos``."""
        n = len(violation.items)
        locs = violation.locations
        if len(locs) == n:
            return list(locs)
        if len(locs) == n + 1:
            return list(locs[1:])
        return [locs[i] if i < len(locs) else None for i in range(n)]

    def _nearest(
        self,
        candidates: list[_Copper],
        x: float,
        y: float,
    ) -> _Copper | None:
        best: _Copper | None = None
        best_d = math.inf
        probe = KVia(x, y, 0.0)
        for copper in candidates:
            d = copper_gap(self._shape_of(copper), probe)
            if d < best_d:
                best, best_d = copper, d
        return best

    def _resolve_item(
        self,
        desc: str,
        uuid: str,
        loc: Location | None,
        other: _Copper | None,
    ) -> _Copper | None:
        """The board item a report item description names, or ``None``."""
        index = self._ensure_index()
        exclude = other.node if other is not None else None

        if uuid:
            copper = self._by_uuid.get(uuid)
            if copper is not None and copper.node is not exclude:
                return copper

        def anchor() -> tuple[float, float] | None:
            if loc is not None:
                return (loc.x_mm, loc.y_mm)
            if other is not None:
                shape = self._shape_of(other)
                b = _bbox_of(shape)
                return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            return None

        text = desc.strip()
        m = _KCT_TRACE.match(text)
        if m:
            p1 = (float(m["x1"]), float(m["y1"]))
            p2 = (float(m["x2"]), float(m["y2"]))
            tol = _DESCRIPTOR_TOLERANCE_MM
            for copper in index:
                if copper.kind != "segment" or copper.node is exclude:
                    continue
                if self._segment_layer(copper.node) != m["layer"]:
                    continue
                sx, sy, ex, ey = self._segment_xy(copper.node)
                fwd = (
                    math.hypot(sx - p1[0], sy - p1[1]) <= tol
                    and math.hypot(ex - p2[0], ey - p2[1]) <= tol
                )
                rev = (
                    math.hypot(sx - p2[0], sy - p2[1]) <= tol
                    and math.hypot(ex - p1[0], ey - p1[1]) <= tol
                )
                if fwd or rev:
                    return copper
            return None
        m = _KCT_VIA.match(text)
        if m:
            vx, vy = float(m["x"]), float(m["y"])
            for copper in index:
                if copper.kind != "via" or copper.node is exclude:
                    continue
                cx, cy = self._via_xy(copper.node)
                if math.hypot(cx - vx, cy - vy) <= _DESCRIPTOR_TOLERANCE_MM:
                    return copper
            return None

        pad_match = _KICAD_PAD.search(text) or _KCT_PAD.match(text)
        if pad_match:
            ref, num = pad_match["ref"], pad_match["num"]
            pads = [
                c
                for c in index
                if c.kind == "pad"
                and c.node is not exclude
                and c.fp_node is not None
                and self._footprint_ref(c.fp_node) == ref
                and str(c.node.get_atoms()[0] if c.node.get_atoms() else "") == num
            ]
            if len(pads) == 1:
                return pads[0]
            if pads:
                point = anchor()
                return self._nearest(pads, *point) if point else pads[0]
            if _KICAD_PAD.search(text):
                return None
            # A "REF-NUM" shaped string that names no pad: fall through.

        nets = _KICAD_NET.findall(text)
        net = nets[0] if nets else None
        if net == "<no net>":
            net = ""
        layer_match = _KICAD_LAYER.search(text)
        if _KICAD_TRACK.match(text):
            kind = "segment"
        elif _KICAD_VIA.match(text):
            kind = "via"
        else:
            return None
        if loc is None:
            return None
        candidates = []
        for copper in index:
            if copper.kind != kind or copper.node is exclude:
                continue
            if net is not None and copper.net != net:
                continue
            if kind == "segment" and layer_match:
                if self._segment_layer(copper.node) != layer_match.group(1):
                    continue
            box = self._bbox(copper)
            if not _boxes_overlap(
                box, (loc.x_mm, loc.y_mm, loc.x_mm, loc.y_mm), _ITEM_SEARCH_RADIUS_MM
            ):
                continue
            candidates.append(copper)
        return self._nearest(candidates, loc.x_mm, loc.y_mm)

    def _footprint_ref(self, fp_node: SExp) -> str:
        """Reference designator of a footprint node (``property`` or ``fp_text``)."""
        for child in fp_node.children:
            if child.name == "property":
                atoms = child.get_atoms()
                if len(atoms) >= 2 and str(atoms[0]) == "Reference":
                    return str(atoms[1])
            elif child.name == "fp_text":
                atoms = child.get_atoms()
                if len(atoms) >= 2 and str(atoms[0]) == "reference":
                    return str(atoms[1])
        return ""

    def _nearest_any(
        self,
        loc: Location,
        nets: list[str],
        exclude: _Copper | None,
    ) -> _Copper | None:
        """Legacy fallback: nearest copper of a violation net at a location.

        Every item type is considered and the nearest wins (the old lookup
        returned the first *segment* within 1.5 mm, which made a via or pad
        location resolve to the track beside it).  An item on the same net as
        ``exclude`` is skipped: a clearance violation is between two nets.
        """
        index = self._ensure_index()
        candidates = []
        for copper in index:
            if exclude is not None and (
                copper.node is exclude.node or (exclude.net and copper.net == exclude.net)
            ):
                continue
            if nets and copper.net not in nets and copper.kind != "via":
                continue
            if nets and copper.kind == "via" and copper.net and copper.net not in nets:
                continue
            if loc.layer and copper.kind == "segment":
                if self._segment_layer(copper.node) != loc.layer:
                    continue
            if loc.layer and copper.kind == "pad":
                layers = self._copper_layers(copper)
                if layers is not None and loc.layer not in layers:
                    continue
            box = self._bbox(copper)
            if not _boxes_overlap(
                box, (loc.x_mm, loc.y_mm, loc.x_mm, loc.y_mm), _ITEM_SEARCH_RADIUS_MM
            ):
                continue
            candidates.append(copper)
        return self._nearest(candidates, loc.x_mm, loc.y_mm)

    def _resolve_pair(self, violation: DRCViolation) -> tuple[_Copper | None, _Copper | None]:
        """Resolve the two items of a clearance violation to board copper.

        Exact identifiers first (kicad-cli uuid, ``kct check`` descriptor,
        ``Pad N of REF``), then a type-, net- and layer-matched nearest search
        around the item's reported position, then -- for reports that carry
        no item descriptions -- the nearest copper of the violation's nets.
        The two sides are never the same item.
        """
        a: _Copper | None = None
        b: _Copper | None = None
        items = violation.items
        if len(items) >= 2:
            uuids = list(getattr(violation, "item_uuids", []) or [])
            uuids += [""] * (len(items) - len(uuids))
            locs = self._item_locations(violation)
            a = self._resolve_item(items[0], uuids[0], locs[0], None)
            b = self._resolve_item(items[1], uuids[1], locs[1], a)
            if a is None and b is not None:
                a = self._resolve_item(items[0], uuids[0], locs[0], b)
            if a is not None and b is not None:
                return a, b
        # Fallback on reported locations (one per item, or a single marker).
        locations = violation.locations
        if not locations:
            return a, b
        loc_a = locations[0]
        loc_b = locations[1] if len(locations) > 1 else locations[0]
        if a is None:
            a = self._nearest_any(loc_a, violation.nets, b)
        if b is None:
            b = self._nearest_any(loc_b, violation.nets, a)
        if a is not None and b is not None and a.node is b.node:
            b = None
        return a, b

    # ------------------------------------------------------------------
    # Repairing one violation
    # ------------------------------------------------------------------

    def _note_unrepaired(
        self,
        result: RepairResult,
        violation: DRCViolation,
        measured: float | None,
        reason: str,
        replace: bool = False,
    ) -> None:
        if replace:
            self._drop_unrepaired(result, violation)
        result.unrepaired.append(
            UnrepairedPair(
                items=list(violation.items),
                nets=list(violation.nets),
                required_mm=violation.required_value_mm,
                reported_mm=violation.actual_value_mm,
                measured_mm=measured,
                reason=reason,
                violation_id=id(violation),
            )
        )

    @staticmethod
    def _drop_unrepaired(result: RepairResult, violation: DRCViolation) -> None:
        result.unrepaired = [u for u in result.unrepaired if u.violation_id != id(violation)]

    def _repair_single_violation(
        self,
        violation: DRCViolation,
        result: RepairResult,
        max_displacement: float,
        margin: float,
        prefer: str,
        dry_run: bool,
    ) -> str:
        """Attempt to repair a single clearance violation.

        Returns the outcome: ``"repaired"``, ``"no_location"``,
        ``"no_delta"``, ``"exceeds_max"``, ``"infeasible"`` or
        ``"not_cleared"`` -- the counter it was booked under.
        """
        if not violation.locations and len(violation.items) < 2:
            result.skipped_no_location += 1
            self._note_unrepaired(result, violation, None, "no location in the report")
            return "no_location"

        delta = violation.delta_mm
        required = violation.required_value_mm
        if delta is None or required is None:
            result.skipped_no_delta += 1
            self._note_unrepaired(result, violation, None, "no required/actual values")
            return "no_delta"

        if delta + margin > max_displacement:
            result.skipped_exceeds_max += 1
            self._note_unrepaired(
                result,
                violation,
                None,
                f"needs {delta + margin:.4f} mm, above --max-displacement {max_displacement}",
            )
            return "exceeds_max"

        a, b = self._resolve_pair(violation)
        if a is None or b is None:
            result.skipped_infeasible += 1
            self._note_unrepaired(
                result, violation, None, "could not identify both copper items on the board"
            )
            return "infeasible"

        before = self._measure(a, b)
        if before + _GAP_EPSILON_MM >= required:
            initial = self._initial_gaps.get(id(violation))
            if initial is not None and initial + _GAP_EPSILON_MM < required and not dry_run:
                # Cleared as a side effect of an earlier move in this run.
                result.repaired += 1
                self._repaired_pairs.append((violation, a, b, required))
                return "repaired"
            # Our measurement says the pair already clears: either the report
            # is stale or the two engines disagree.  Nothing to move, and no
            # basis for claiming a repair.
            result.skipped_not_cleared += 1
            self._note_unrepaired(
                result,
                violation,
                before,
                "measured gap already meets the rule (stale report?); nothing moved",
            )
            return "not_cleared"

        # ``prefer`` picks the first item to try; the other movable item is
        # the fallback.  Pads and locked tracks/vias never move.
        movable = [c for c in (a, b) if c.movable]
        primary = self._choose_target(
            self._legacy_tuple(a) if a.movable else None,
            self._legacy_tuple(b) if b.movable else None,
            prefer,
        )
        order: list[tuple[_Copper, _Copper]] = []
        if primary is not None:
            first = a if primary[0] is a.node else b
            order.append((first, b if first is a else a))
            second = order[0][1]
            if second in movable:
                order.append((second, first))

        reasons: list[str] = []
        exceeded = False
        for target, other in order:
            outcome = self._try_nudge(
                violation, target, other, required, margin, max_displacement, dry_run, result
            )
            if isinstance(outcome, NudgeResult):
                result.nudges.append(outcome)
                result.repaired += 1
                if not dry_run:
                    self._repaired_pairs.append((violation, target, other, required))
                return "repaired"
            if outcome.startswith("exceeds"):
                exceeded = True
            reasons.append(f"moving the {target.kind}: {outcome}")

        if not order:
            result.skipped_infeasible += 1
            self._note_unrepaired(
                result, violation, before, "neither item can move (pads or locked copper)"
            )
            if not dry_run:
                self._failed_pairs.append((violation, a, b, required, "infeasible"))
            return "infeasible"
        if exceeded and all(
            r.split(": ", 1)[1].startswith(("exceeds", "infeasible")) for r in reasons
        ):
            result.skipped_exceeds_max += 1
            self._note_unrepaired(result, violation, before, "; ".join(reasons))
            if not dry_run:
                self._failed_pairs.append((violation, a, b, required, "exceeds_max"))
            return "exceeds_max"
        if all(r.split(": ", 1)[1].startswith("infeasible") for r in reasons):
            result.skipped_infeasible += 1
            self._note_unrepaired(result, violation, before, "; ".join(reasons))
            if not dry_run:
                self._failed_pairs.append((violation, a, b, required, "infeasible"))
            return "infeasible"
        result.skipped_not_cleared += 1
        self._note_unrepaired(result, violation, before, "; ".join(reasons))
        if not dry_run:
            self._failed_pairs.append((violation, a, b, required, "not_cleared"))
        return "not_cleared"

    def _legacy_tuple(self, copper: _Copper) -> tuple[SExp, str, float, float, str, str]:
        """``(node, type, x, y, layer, net)`` -- the shape older helpers take."""
        shape = self._shape_of(copper)
        box = _bbox_of(shape)
        layer = self._segment_layer(copper.node) if copper.kind == "segment" else ""
        return (
            copper.node,
            copper.kind,
            (box[0] + box[2]) / 2,
            (box[1] + box[3]) / 2,
            layer,
            copper.net,
        )

    def _pin_state(self, node: SExp, via_points: list[tuple[float, float]]) -> tuple[bool, bool]:
        """Whether each end of a segment is anchored (and so stays put).

        An end is anchored when it sits on a via, or inside a pad of its own
        net (Issue #6434): sliding a track end off its pad's centre keeps
        KiCad's connectivity but not the stricter ``kct check`` connectivity
        model, so the track pivots about the pad instead.
        """
        sx, sy, ex, ey = self._segment_xy(node)
        own = self._by_node.get(id(node))
        net = own.net if own is not None else self._node_net(node)
        layer = self._segment_layer(node)
        return (
            self._end_anchored(sx, sy, net, layer, via_points)
            or self._joined_to_locked(node, sx, sy),
            self._end_anchored(ex, ey, net, layer, via_points)
            or self._joined_to_locked(node, ex, ey),
        )

    def _joined_to_locked(self, node: SExp, x: float, y: float) -> bool:
        """Whether a locked same-net track end is joined to ``node``'s end at ``(x, y)``.

        Dragging that end would move the locked track, so the end is held in
        place instead -- the track pivots about it (Issue #6434).
        """
        return any(
            _is_locked(seg_node) for seg_node, _ep, _ox, _oy in self._joined_ends(node, x, y)
        )

    def _end_anchored(
        self, x: float, y: float, net: str, layer: str, via_points: list[tuple[float, float]]
    ) -> bool:
        tol = 0.001
        if any(math.hypot(x - vx, y - vy) <= tol for vx, vy in via_points):
            return True
        if not net:
            return False
        probe = KVia(x, y, 0.0)
        point_box = (x, y, x, y)
        for copper in self._ensure_index():
            if copper.kind != "pad" or copper.net != net:
                continue
            if not _boxes_overlap(self._bbox(copper), point_box, 0.0):
                continue
            layers = self._copper_layers(copper)
            if layers is not None and layer not in layers:
                continue
            if copper_gap(self._shape_of(copper), probe) < 0.0:
                return True
        return False

    def _moved_shape(
        self,
        target: _Copper,
        dx: float,
        dy: float,
        pins: tuple[bool, bool],
    ) -> KShape:
        """The target's copper after ``_apply_nudge(dx, dy)``, without moving it."""
        shape = self._shape_of(target)
        if isinstance(shape, KVia):
            return KVia(shape.x + dx, shape.y + dy, shape.diameter)
        assert isinstance(shape, KSegment)
        start_pinned, end_pinned = pins
        if start_pinned and end_pinned:
            return shape
        x1, y1, x2, y2 = shape.x1, shape.y1, shape.x2, shape.y2
        if not start_pinned:
            x1, y1 = x1 + dx, y1 + dy
        if not end_pinned:
            x2, y2 = x2 + dx, y2 + dy
        return KSegment(x1, y1, x2, y2, shape.width)

    def _plan_displacement(
        self,
        target: _Copper,
        other: _Copper,
        goal: float,
        max_displacement: float,
        pins: tuple[bool, bool],
    ) -> tuple[float, float] | str:
        """Smallest move that raises the target/other gap to ``goal``.

        Steps along the gradient of the measured gap -- for a track against a
        via or pad that is perpendicular to the track, away from the other
        copper -- and re-solves until the predicted gap reaches the goal, so
        an end pinned at a via (a rotation, not a translation) is handled too.
        """
        other_shape = self._shape_of(other)

        def gap(dx: float, dy: float) -> float:
            return copper_gap(self._moved_shape(target, dx, dy, pins), other_shape)

        dx = dy = 0.0
        eps = 1e-3
        for _ in range(8):
            g = gap(dx, dy)
            if g >= goal - _GAP_EPSILON_MM:
                return (dx, dy)
            gx = (gap(dx + eps, dy) - gap(dx - eps, dy)) / (2 * eps)
            gy = (gap(dx, dy + eps) - gap(dx, dy - eps)) / (2 * eps)
            gain = math.hypot(gx, gy)
            if gain < 0.05:
                if target.kind == "segment" and all(pins):
                    return "infeasible: both track ends are anchored (via or pad)"
                return "infeasible: no move direction increases the gap"
            step = (goal - g) / gain
            dx += gx / gain * step
            dy += gy / gain * step
            if math.hypot(dx, dy) > max_displacement + _GAP_EPSILON_MM:
                return f"exceeds --max-displacement ({math.hypot(dx, dy):.4f} mm needed)"
        if gap(dx, dy) >= goal - _GAP_EPSILON_MM:
            return (dx, dy)
        return "infeasible: the move does not converge"

    def _try_nudge(
        self,
        violation: DRCViolation,
        target: _Copper,
        other: _Copper,
        required: float,
        margin: float,
        max_displacement: float,
        dry_run: bool,
        result: RepairResult,
    ) -> NudgeResult | str:
        """Move ``target`` away from ``other``, keep it only if it measures clear.

        Returns the :class:`NudgeResult` of a kept move, or the reason the
        move was declined (the board is left exactly as it was).
        """
        via_points = [self._via_xy(c.node) for c in self._ensure_index() if c.kind == "via"]
        pins = (
            self._pin_state(target.node, via_points) if target.kind == "segment" else (False, False)
        )
        before = self._measure(target, other)
        plan = self._plan_displacement(target, other, required + margin, max_displacement, pins)
        if isinstance(plan, str):
            return plan
        dx, dy = plan
        uuid_str = self._node_uuid(target.node)
        if self._would_oscillate(uuid_str, dx, dy):
            return "infeasible: would reverse an earlier nudge of the same item"

        ref_shape = self._shape_of(target)
        ref_box = _bbox_of(ref_shape)
        ref_x, ref_y = (ref_box[0] + ref_box[2]) / 2, (ref_box[1] + ref_box[3]) / 2

        self._journal = []
        try:
            self._apply_nudge(target.node, target.kind, dx, dy, result=None, drag_connected=True)
        finally:
            journal, self._journal = self._journal, None
        locked = next((n for n, _p, _x, _y in journal if _is_locked(n)), None)
        if locked is not None:
            self._rollback(journal)
            return (
                f"infeasible: the move would drag locked {locked.name} "
                f"{self._node_uuid(locked) or '<no uuid>'}"
            )
        lost = self._lost_contact(journal)
        if lost is not None:
            self._rollback(journal)
            return f"infeasible: the move would {lost}"
        measured = self._measure(target, other)
        if measured + _GAP_EPSILON_MM < required:
            self._rollback(journal)
            return f"not cleared: measured {measured:.4f} mm after the move"
        collateral = self._collateral_damage(journal)
        if collateral is not None:
            self._rollback(journal)
            return f"not cleared: the move would {collateral}"

        edits = self._journal_edits(journal)
        if dry_run:
            self._rollback(journal)
        else:
            self.modified = True
            for item_node, _point, _ox, _oy in journal:
                if item_node.name == "segment":
                    self._edited_segments[id(item_node)] = item_node
            if target.kind == "via":
                result.relocated_vias += 1
            elif any(pins):
                result.endpoint_nudges += 1
        self._record_nudge(uuid_str, dx, dy)

        layer = (
            self._segment_layer(target.node)
            if target.kind == "segment"
            else " - ".join(self._pad_layers(target.node))
        )
        return NudgeResult(
            object_type=target.kind,
            x=round(ref_x, 4),
            y=round(ref_y, 4),
            net_name=target.net,
            layer=layer,
            displacement_x=dx,
            displacement_y=dy,
            displacement_mm=math.hypot(dx, dy),
            old_clearance_mm=before,
            new_clearance_mm=measured,
            uuid=uuid_str,
            edits=edits,
        )

    def _journal_view(
        self, journal: list[tuple[SExp, SExp, float, float]]
    ) -> tuple[dict[int, _Copper], Callable[[_Copper], KShape]]:
        """Items a journalled move touched, and a function giving pre-move shapes."""
        touched: dict[int, _Copper] = {}
        old_points: dict[int, tuple[float, float]] = {}
        for item_node, point_node, ox, oy in journal:
            copper = self._by_node.get(id(item_node))
            if copper is not None:
                touched[id(item_node)] = copper
            old_points.setdefault(id(point_node), (ox, oy))

        def old_shape(copper: _Copper) -> KShape:
            shape = self._shape_of(copper)
            if isinstance(shape, KVia):
                ox, oy = old_points.get(id(copper.node.find("at")), (shape.x, shape.y))
                return KVia(ox, oy, shape.diameter)
            if isinstance(shape, KSegment):
                x1, y1 = old_points.get(id(copper.node.find("start")), (shape.x1, shape.y1))
                x2, y2 = old_points.get(id(copper.node.find("end")), (shape.x2, shape.y2))
                return KSegment(x1, y1, x2, y2, shape.width)
            return shape

        return touched, old_shape

    def _lost_contact(self, journal: list[tuple[SExp, SExp, float, float]]) -> str | None:
        """Describe a same-net joint the journalled move broke, if any.

        The items the move touched and the same-net copper (tracks, vias,
        pads) they overlap before or after the move form a small graph.  Every
        pair joined before the move must still be connected afterwards,
        directly or through another item of the graph, so a T-junction stub
        or a track end resting on a via cannot be left behind (Issue #6434).
        A contact that merely moves to a dragged neighbour is not a break.
        """
        if not journal:
            return None
        touched, old_shape = self._journal_view(journal)
        index = self._ensure_index()
        members: dict[int, _Copper] = dict(touched)
        for moved in touched.values():
            if not moved.net:
                continue
            new, prev = self._shape_of(moved), old_shape(moved)
            box_new, box_old = _bbox_of(new), _bbox_of(prev)
            box = (
                min(box_new[0], box_old[0]),
                min(box_new[1], box_old[1]),
                max(box_new[2], box_old[2]),
                max(box_new[3], box_old[3]),
            )
            for neighbour in index:
                if neighbour.node is moved.node or neighbour.net != moved.net:
                    continue
                if id(neighbour.node) in members:
                    continue
                if _boxes_overlap(self._bbox(neighbour), box, 0.01) and self._interacts(
                    moved, neighbour
                ):
                    members[id(neighbour.node)] = neighbour

        items = list(members.values())

        def edges(shape_of: Callable[[_Copper], KShape]) -> list[tuple[int, int]]:
            found = []
            for i, a in enumerate(items):
                for j in range(i + 1, len(items)):
                    b = items[j]
                    if a.net != b.net or not a.net or not self._interacts(a, b):
                        continue
                    if copper_gap(shape_of(a), shape_of(b)) <= _GAP_EPSILON_MM:
                        found.append((i, j))
            return found

        def before_shape(copper: _Copper) -> KShape:
            return old_shape(copper) if id(copper.node) in touched else self._shape_of(copper)

        parent = list(range(len(items)))

        def find(k: int) -> int:
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        for i, j in edges(self._shape_of):
            parent[find(i)] = find(j)
        for i, j in edges(before_shape):
            if find(i) != find(j):
                a, b = items[i], items[j]
                return (
                    f"disconnect {a.kind} {self._node_uuid(a.node) or '<no uuid>'} "
                    f"from {b.kind} {self._node_uuid(b.node) or '<no uuid>'} [{a.net}]"
                )
        return None

    def _collateral_damage(self, journal: list[tuple[SExp, SExp, float, float]]) -> str | None:
        """Describe a new clearance violation the journalled move created, if any.

        Every item the move touched (the target plus dragged track ends) is
        compared with the foreign copper around it: a pair that now measures
        below its requirement *and* closer than before the move rejects it.
        """
        if not journal:
            return None
        touched, old_shape = self._journal_view(journal)

        index = self._ensure_index()
        reach = self._report_max_required + 0.5
        for moved in touched.values():
            new = self._shape_of(moved)
            prev = old_shape(moved)
            box_new, box_old = _bbox_of(new), _bbox_of(prev)
            box = (
                min(box_new[0], box_old[0]),
                min(box_new[1], box_old[1]),
                max(box_new[2], box_old[2]),
                max(box_new[3], box_old[3]),
            )
            for neighbour in index:
                if neighbour.node is moved.node:
                    continue
                if moved.net and neighbour.net == moved.net:
                    continue
                if not _boxes_overlap(self._bbox(neighbour), box, reach):
                    continue
                if not self._interacts(moved, neighbour):
                    continue
                need = self._pair_requirement(moved.net, neighbour.net)
                gap_new = copper_gap(new, self._shape_of(neighbour))
                if gap_new + _GAP_EPSILON_MM >= need:
                    continue
                neighbour_old = (
                    old_shape(neighbour)
                    if id(neighbour.node) in touched
                    else self._shape_of(neighbour)
                )
                gap_old = copper_gap(prev, neighbour_old)
                if gap_new < gap_old - _GAP_EPSILON_MM:
                    label = neighbour.net or "<no net>"
                    return (
                        f"bring {moved.kind} [{moved.net or '<no net>'}] to {gap_new:.4f} mm "
                        f"of {neighbour.kind} [{label}] (needs {need:.4f} mm)"
                    )
        return None

    def _drop_collapsed_segments(self, result: RepairResult) -> None:
        """Delete edited tracks a drag collapsed to a single point.

        Dragging both ends of a sub-micron stub can round them onto the same
        coordinate, which ``kct check`` flags as ``zero_length_segment``.  The
        dot is removed only when every same-net item it touches also covers
        its centre, so the removal cannot split a joint.
        """
        if not self._edited_segments:
            return
        self._ensure_index()
        removed = 0
        for node in self._edited_segments.values():
            dot = self._by_node.get(id(node))
            if dot is None:
                continue
            sx, sy, ex, ey = self._segment_xy(node)
            if math.hypot(ex - sx, ey - sy) > _GAP_EPSILON_MM:
                continue
            shape = self._shape_of(dot)
            centre = KVia(sx, sy, 0.0)
            box = self._bbox(dot)
            contacts = [
                c
                for c in self._ensure_index()
                if c.node is not node
                and c.net == dot.net
                and _boxes_overlap(self._bbox(c), box, 0.0)
                and self._interacts(dot, c)
                and copper_gap(shape, self._shape_of(c)) <= _GAP_EPSILON_MM
            ]
            if not contacts or any(
                copper_gap(self._shape_of(c), centre) > _GAP_EPSILON_MM for c in contacts
            ):
                continue
            if self.doc.remove(node):
                removed += 1
                self._invalidate_index()
                self._ensure_index()
        self._edited_segments = {}
        result.removed_zero_length += removed

    def _set_point(self, item_node: SExp, point_node: SExp, x: float, y: float) -> None:
        """Write a coordinate pair, journalling the old value when a trial is open."""
        if self._journal is not None:
            ox, oy = self._xy(point_node)
            self._journal.append((item_node, point_node, ox, oy))
        _write_xy(point_node, x, y)
        self._shape_cache.pop(id(item_node), None)

    def _rollback(self, journal: list[tuple[SExp, SExp, float, float]]) -> None:
        for item_node, point_node, ox, oy in reversed(journal):
            _write_xy(point_node, ox, oy)
            self._shape_cache.pop(id(item_node), None)

    def _journal_edits(
        self, journal: list[tuple[SExp, SExp, float, float]]
    ) -> list[tuple[str, str, str, float, float, float, float]]:
        """Collapse a journal into ``(type, uuid, point, old_x, old_y, new_x, new_y)``."""
        first_old: dict[int, tuple[SExp, SExp, float, float]] = {}
        for item_node, point_node, ox, oy in journal:
            first_old.setdefault(id(point_node), (item_node, point_node, ox, oy))
        edits = []
        for item_node, point_node, ox, oy in first_old.values():
            nx, ny = self._xy(point_node)
            edits.append(
                (
                    str(item_node.name),
                    self._node_uuid(item_node),
                    str(point_node.name),
                    ox,
                    oy,
                    nx,
                    ny,
                )
            )
        return edits

    def _measure_net_to_item(self, net: str, layer: str, other: _Copper) -> float | None:
        """Smallest gap between ``other`` and any copper of ``net`` it interacts with."""
        index = self._ensure_index()
        other_now = self._by_node.get(id(other.node), other)
        best: float | None = None
        for copper in index:
            if copper.kind == "pad" or copper.net != net or copper.node is other_now.node:
                continue
            if copper.kind == "segment" and layer and self._segment_layer(copper.node) != layer:
                continue
            if not self._interacts(copper, other_now):
                continue
            gap = self._measure(copper, other_now)
            if best is None or gap < best:
                best = gap
        return best

    def _reverify_repaired_pairs(self, result: RepairResult) -> None:
        """Re-measure every pair against the final geometry.

        A counted repair that a later move undid goes back to unrepaired; a
        failed pair that a later move cleared (a shared or dragged track) is
        counted as repaired.  Either way the count matches the board.
        """
        counters = {
            "infeasible": "skipped_infeasible",
            "exceeds_max": "skipped_exceeds_max",
            "not_cleared": "skipped_not_cleared",
        }
        self._ensure_index()
        for violation, a, b, required, counter in self._failed_pairs:
            if id(a.node) not in self._by_node or id(b.node) not in self._by_node:
                continue  # replaced by a reroute; that path accounts for itself
            if not any(u.violation_id == id(violation) for u in result.unrepaired):
                continue  # already moved to repaired (e.g. by a local reroute)
            if self._measure(a, b) + _GAP_EPSILON_MM >= required:
                setattr(result, counters[counter], getattr(result, counters[counter]) - 1)
                result.repaired += 1
                self._drop_unrepaired(result, violation)
                self._repaired_pairs.append((violation, a, b, required))
        self._failed_pairs = []
        for violation, target, other, required in self._repaired_pairs:
            if self._by_node.get(id(target.node)) is None:
                # The target was replaced by a reroute; measure net-to-item.
                measured = self._measure_net_to_item(
                    target.net, self._segment_layer(target.node), other
                )
            else:
                measured = self._measure(target, other)
            if measured is not None and measured + _GAP_EPSILON_MM < required:
                result.repaired -= 1
                result.skipped_not_cleared += 1
                self._note_unrepaired(
                    result,
                    violation,
                    measured,
                    "cleared, then a later repair moved copper back into violation",
                )
        self._repaired_pairs = []

    def _find_vias_near(
        self,
        x: float,
        y: float,
        radius: float,
        nets: list[str] | None,
    ) -> list[tuple[SExp, str, float, float, str, str]]:
        """Find vias near a point.

        Returns list of (node, "via", x, y, layer_str, net_name).
        """
        results = []

        for via_node in self.doc.find_all("via"):
            at_node = via_node.find("at")
            if not at_node:
                continue

            at_atoms = at_node.get_atoms()
            vx = float(at_atoms[0]) if at_atoms else 0
            vy = float(at_atoms[1]) if len(at_atoms) > 1 else 0

            dist = math.sqrt((vx - x) ** 2 + (vy - y) ** 2)
            if dist > radius:
                continue

            net_node = via_node.find("net")
            net_num, net_name = resolve_net_atom(
                net_node.get_first_atom() if net_node else None,
                self.nets,
                self.net_names,
            )

            # Relax net filter: allow vias with no net (net 0 / empty name)
            # or vias whose net is "<no net>", since these commonly appear
            # in segment-to-via clearance violations after fix-vias.
            if nets and net_name not in nets:
                if net_name and net_name != "<no net>":
                    continue

            # Vias span layers, use "F.Cu - B.Cu" as layer description
            layers_node = via_node.find("layers")
            layer_str = ""
            if layers_node:
                layer_atoms = layers_node.get_atoms()
                layer_str = " - ".join(str(a) for a in layer_atoms)

            results.append((via_node, "via", vx, vy, layer_str, net_name))

        return results

    def _find_pads_near(
        self,
        x: float,
        y: float,
        radius: float,
        layer: str | None,
        nets: list[str] | None,
    ) -> list[tuple[SExp, str, float, float, str, str]]:
        """Find pads near a point.

        Pads are nested inside footprint nodes in the s-expression tree.
        Their positions are in footprint-local coordinates and must be
        transformed to board coordinates using the footprint's position
        and rotation.

        Returns list of (pad_node, "pad", abs_x, abs_y, pad_layer, net_name).
        """
        results = []

        for fp_node in _find_all_footprints(self.doc):
            # Get footprint position and rotation
            fp_at = fp_node.find("at")
            if not fp_at:
                continue
            fp_atoms = fp_at.get_atoms()
            fp_x = float(fp_atoms[0]) if fp_atoms else 0.0
            fp_y = float(fp_atoms[1]) if len(fp_atoms) > 1 else 0.0
            fp_rot = float(fp_atoms[2]) if len(fp_atoms) > 2 else 0.0

            # KiCad applies the footprint orientation as a NEGATED angle vs
            # standard CCW math (verified vs pcbnew, issue #3739).
            angle_rad = math.radians(-fp_rot)
            cos_a = math.cos(angle_rad)
            sin_a = math.sin(angle_rad)

            for pad_node in fp_node.find_all("pad"):
                pad_at = pad_node.find("at")
                if not pad_at:
                    continue
                pad_atoms = pad_at.get_atoms()
                local_x = float(pad_atoms[0]) if pad_atoms else 0.0
                local_y = float(pad_atoms[1]) if len(pad_atoms) > 1 else 0.0

                # Transform from footprint-local to board coordinates
                abs_x = fp_x + local_x * cos_a - local_y * sin_a
                abs_y = fp_y + local_x * sin_a + local_y * cos_a

                dist = math.sqrt((abs_x - x) ** 2 + (abs_y - y) ** 2)
                if dist > radius:
                    continue

                # Check layer match
                pad_layers_node = pad_node.find("layers")
                pad_layers: list[str] = []
                if pad_layers_node:
                    pad_layers = [str(a) for a in pad_layers_node.get_atoms()]

                if layer:
                    # Pad matches if it's on the specified layer or on all copper layers
                    if layer not in pad_layers and "*.Cu" not in pad_layers:
                        continue

                # Check net match
                pad_net_node = pad_node.find("net")
                pad_net_num = 0
                if pad_net_node:
                    first_atom = pad_net_node.get_first_atom()
                    if first_atom is not None:
                        try:
                            pad_net_num = int(first_atom)
                        except (ValueError, TypeError):
                            pad_net_num = 0
                pad_net_name = self.nets.get(pad_net_num, "")

                if nets and pad_net_name not in nets:
                    continue

                pad_layer = pad_layers[0] if pad_layers else ""
                results.append((pad_node, "pad", abs_x, abs_y, pad_layer, pad_net_name))

        return results

    def _choose_target(
        self,
        obj1: tuple[SExp, str, float, float, str, str] | None,
        obj2: tuple[SExp, str, float, float, str, str] | None,
        prefer: str,
    ) -> tuple[SExp, str, float, float, str, str] | None:
        """Choose which object to nudge.

        Args:
            obj1: First object (node, type, x, y, layer, net)
            obj2: Second object (node, type, x, y, layer, net)
            prefer: "move-trace" or "move-via"

        Returns:
            The chosen object to move, or None if neither is movable
        """
        if obj1 is None and obj2 is None:
            return None
        if obj1 is None:
            return obj2
        if obj2 is None:
            return obj1

        _, type1, _, _, _, _ = obj1
        _, type2, _, _, _, _ = obj2

        # Pads are immovable -- always move the other object.
        # If both are pads, neither can be moved.
        if type1 == "pad" and type2 == "pad":
            return None
        if type1 == "pad":
            return obj2
        if type2 == "pad":
            return obj1

        if prefer == "move-trace":
            if type1 == "segment":
                return obj1
            if type2 == "segment":
                return obj2
            # Both are vias or other types, move the first one
            return obj1
        elif prefer == "move-via":
            if type1 == "via":
                return obj1
            if type2 == "via":
                return obj2
            return obj1
        else:
            return obj1

    @staticmethod
    def _get_node_width(node: SExp) -> float:
        """Extract the width of a segment or via node.

        For segments, reads the ``(width ...)`` child.  For vias, reads the
        ``(size ...)`` child.  Returns a sensible default (0.25mm) when the
        width child is missing.
        """
        width_node = node.find("width")
        if width_node:
            return float(width_node.get_first_atom())
        size_node = node.find("size")
        if size_node:
            return float(size_node.get_first_atom())
        return 0.25  # default trace width

    def _compute_nudge(
        self,
        obj_x: float,
        obj_y: float,
        other_x: float,
        other_y: float,
        required_displacement: float,
    ) -> tuple[float, float, float] | None:
        """Compute the displacement vector to achieve required clearance.

        The nudge moves obj away from other by the required amount.

        Args:
            obj_x, obj_y: Position of the object to move
            other_x, other_y: Position of the object to move away from
            required_displacement: How far to move (mm)

        Returns:
            (dx, dy, distance) or None if objects are coincident
        """
        dx = obj_x - other_x
        dy = obj_y - other_y
        dist = math.sqrt(dx * dx + dy * dy)

        if dist < 1e-10:
            # Objects are at the same position, cannot determine direction
            # Default to moving in +x direction
            return (required_displacement, 0.0, required_displacement)

        # Normalize direction vector and scale to required displacement
        scale = required_displacement / dist
        nudge_x = dx * scale
        nudge_y = dy * scale
        nudge_dist = math.sqrt(nudge_x * nudge_x + nudge_y * nudge_y)

        return (nudge_x, nudge_y, nudge_dist)

    def _find_via_positions(self) -> list[tuple[float, float]]:
        """Return a list of (x, y) positions for all vias in the PCB."""
        return [self._via_xy(c.node) for c in self._ensure_index() if c.kind == "via"]

    def _find_connected_segments(
        self,
        x: float,
        y: float,
        tolerance: float = 0.001,
    ) -> list[tuple[SExp, str]]:
        """Find all segments with an endpoint coinciding with a position.

        Returns a list of (segment_node, endpoint) tuples where endpoint is
        "start" or "end" indicating which end of the segment sits at (x, y).

        Args:
            x: X coordinate to match
            y: Y coordinate to match
            tolerance: Maximum distance in mm for a match (default 0.001)
        """
        results: list[tuple[SExp, str]] = []

        for copper in self._ensure_index():
            if copper.kind != "segment":
                continue
            sx, sy, ex, ey = self._segment_xy(copper.node)
            if math.sqrt((sx - x) ** 2 + (sy - y) ** 2) <= tolerance:
                results.append((copper.node, "start"))
            if math.sqrt((ex - x) ** 2 + (ey - y) ** 2) <= tolerance:
                results.append((copper.node, "end"))

        return results

    def _apply_nudge(
        self,
        node: SExp,
        obj_type: str,
        dx: float,
        dy: float,
        result: RepairResult | None = None,
        drag_connected: bool = False,
    ) -> None:
        """Apply a displacement to a PCB object.

        For segments: if an endpoint sits at a via position, only the non-via
        endpoint is moved (endpoint-only nudge) to avoid disconnecting the net.
        If both endpoints sit at vias, the segment is skipped as infeasible.
        Otherwise both endpoints are moved (full-segment nudge).  With
        ``drag_connected`` the ends of other tracks that met a moved end move
        with it, so the net stays connected (Issue #6434).

        For vias: moves the via position and also updates the endpoints of all
        connected segments so that net connectivity is preserved.
        """
        if obj_type == "via":
            at_node = node.find("at")
            if at_node:
                old_x, old_y = self._xy(at_node)
                new_x = round(old_x + dx, 4)
                new_y = round(old_y + dy, 4)

                # Segments connected to the old via position move with it.
                connected = self._find_connected_segments(old_x, old_y)
                self._set_point(node, at_node, new_x, new_y)
                for seg_node, endpoint in connected:
                    ep_node = seg_node.find(endpoint)
                    if ep_node:
                        self._set_point(seg_node, ep_node, new_x, new_y)

                if result is not None:
                    result.relocated_vias += 1

        elif obj_type == "segment":
            start_node = node.find("start")
            end_node = node.find("end")

            if not (start_node and end_node):
                return

            sx, sy = self._xy(start_node)
            ex, ey = self._xy(end_node)

            # Check which endpoints are anchored (on a via, or inside a pad of
            # the track's own net -- see ``_pin_state``)
            start_at_via, end_at_via = self._pin_state(node, self._find_via_positions())

            if start_at_via and end_at_via:
                # Both endpoints at vias -- moving either end disconnects a net.
                # This is logged as infeasible by the caller; we do nothing here.
                return

            moves: list[tuple[SExp, float, float]] = []
            if not start_at_via:
                moves.append((start_node, sx, sy))
            if not end_at_via:
                moves.append((end_node, ex, ey))
            # Gather every dragged end before anything moves, so a stub is
            # found against the track's original position.
            dragged: dict[int, tuple[SExp, SExp, float, float, float, float]] = {}
            if drag_connected:
                for _point_node, px, py in moves:
                    for seg_node, ep_node, ox, oy in self._joined_ends(node, px, py):
                        # Shift by the same delta: a coincident end lands
                        # exactly on the new point, an overlapping one keeps
                        # its offset.
                        dragged.setdefault(id(ep_node), (seg_node, ep_node, ox, oy, dx, dy))
                start_d = (0.0, 0.0) if start_at_via else (dx, dy)
                end_d = (0.0, 0.0) if end_at_via else (dx, dy)
                for seg_node, ep_node, ox, oy, t in self._mid_span_stubs(node):
                    if id(ep_node) in dragged:
                        continue
                    # A T-junction: the stub end follows the track at the
                    # point it meets it (Issue #6434).
                    ddx = (1 - t) * start_d[0] + t * end_d[0]
                    ddy = (1 - t) * start_d[1] + t * end_d[1]
                    dragged[id(ep_node)] = (seg_node, ep_node, ox, oy, ddx, ddy)
            for point_node, px, py in moves:
                self._set_point(node, point_node, round(px + dx, 4), round(py + dy, 4))
            for seg_node, ep_node, ox, oy, ddx, ddy in dragged.values():
                self._set_point(seg_node, ep_node, round(ox + ddx, 4), round(oy + ddy, 4))

            if (start_at_via or end_at_via) and result is not None:
                result.endpoint_nudges += 1

    def _joined_ends(
        self, node: SExp, px: float, py: float
    ) -> list[tuple[SExp, SExp, float, float]]:
        """Same-net track ends joined to ``node``'s end at ``(px, py)``.

        KiCad joins two tracks when one's end lies inside the other's copper,
        not only when the ends coincide, so an end within half the narrower
        track width counts (Issue #6434: dragging only exact matches left a
        routed joint 0.05 mm apart and broke the net).
        """
        own = self._by_node.get(id(node))
        net = own.net if own is not None else self._node_net(node)
        width = self._get_node_width(node)
        layer = self._segment_layer(node)
        joined: list[tuple[SExp, SExp, float, float]] = []
        for copper in self._ensure_index():
            if copper.kind != "segment" or copper.node is node or copper.net != net:
                continue
            if self._segment_layer(copper.node) != layer:
                continue
            reach = max(0.001, min(width, self._get_node_width(copper.node)) / 2)
            for name in ("start", "end"):
                ep_node = copper.node.find(name)
                if ep_node is None:
                    continue
                ox, oy = self._xy(ep_node)
                if math.hypot(ox - px, oy - py) <= reach:
                    joined.append((copper.node, ep_node, ox, oy))
        return joined

    def _mid_span_stubs(self, node: SExp) -> list[tuple[SExp, SExp, float, float, float]]:
        """Same-net track ends that meet ``node`` part-way along it (T-junctions).

        Returns ``(segment, end_node, x, y, t)`` where ``t`` in ``[0, 1]`` is
        where the end projects onto ``node``'s centreline.  Ends joined at
        ``node``'s own ends are left to :meth:`_joined_ends`.
        """
        sx, sy, ex, ey = self._segment_xy(node)
        own = self._by_node.get(id(node))
        net = own.net if own is not None else self._node_net(node)
        if not net:
            return []
        layer = self._segment_layer(node)
        width = self._get_node_width(node)
        lx, ly = ex - sx, ey - sy
        length_sq = lx * lx + ly * ly
        if length_sq <= 0.0:
            return []
        stubs: list[tuple[SExp, SExp, float, float, float]] = []
        for copper in self._ensure_index():
            if copper.kind != "segment" or copper.node is node or copper.net != net:
                continue
            if self._segment_layer(copper.node) != layer:
                continue
            reach_end = max(0.001, min(width, self._get_node_width(copper.node)) / 2)
            for name in ("start", "end"):
                ep_node = copper.node.find(name)
                if ep_node is None:
                    continue
                ox, oy = self._xy(ep_node)
                if (
                    math.hypot(ox - sx, oy - sy) <= reach_end
                    or math.hypot(ox - ex, oy - ey) <= reach_end
                ):
                    continue
                t = ((ox - sx) * lx + (oy - sy) * ly) / length_sq
                if not 0.0 <= t <= 1.0:
                    continue
                px, py = sx + t * lx, sy + t * ly
                if math.hypot(ox - px, oy - py) <= width / 2 + 1e-6:
                    stubs.append((copper.node, ep_node, ox, oy, t))
        return stubs

    def _find_node_by_uuid(
        self,
        uuid: str,
        obj_type: str,
    ) -> SExp | None:
        """Find a top-level segment or via node by its UUID.

        Args:
            uuid: UUID string to match (compared against the first atom of
                the ``(uuid ...)`` child).
            obj_type: ``"segment"`` or ``"via"`` — the S-expression tag to
                look for.

        Returns:
            The matching :class:`SExp` node, or ``None`` if not found.
        """
        if not uuid:
            return None
        for node in self.doc.find_all(obj_type):
            uuid_node = node.find("uuid")
            if not uuid_node:
                continue
            atom = uuid_node.get_first_atom()
            if atom is None:
                continue
            if str(atom).strip('"') == str(uuid).strip('"'):
                return node
        return None

    def _undo_nudge(self, nudge: NudgeResult) -> bool:
        """Reverse a previously-applied nudge.

        Locates the object by UUID and applies the inverse displacement
        ``(-displacement_x, -displacement_y)``.  For vias, mirrors
        ``_apply_nudge``'s behavior of also updating the endpoints of any
        segments that currently terminate at the post-nudge via position
        (so the via and its attached traces move back together).

        For segments where one endpoint was pinned at a via during the
        original nudge, only the moved endpoint is reverted.  The same
        ``via_positions`` check used by ``_apply_nudge`` is applied to the
        *current* board state to detect this asymmetric case.

        Args:
            nudge: The :class:`NudgeResult` to undo.

        Returns:
            ``True`` if the undo succeeded, ``False`` otherwise (e.g. the
            UUID could not be located in the current document).  Callers
            should treat ``False`` as a trigger for the bulk-snapshot
            fallback so the file state is never left half-reverted.
        """
        if nudge.edits:
            # Issue #6434: restore every coordinate the nudge wrote (it may
            # have dragged connected track ends along with the target).
            targets: list[tuple[SExp, SExp, float, float]] = []
            for obj_type, uuid, point, ox, oy, _nx, _ny in nudge.edits:
                item = self._find_node_by_uuid(uuid, obj_type)
                if item is None:
                    return False
                point_node = item.find(point)
                if point_node is None:
                    return False
                targets.append((item, point_node, ox, oy))
            for item, point_node, ox, oy in targets:
                self._set_point(item, point_node, ox, oy)
            return True
        if not nudge.uuid:
            return False
        node = self._find_node_by_uuid(nudge.uuid, nudge.object_type)
        if node is None:
            return False

        # Inverse displacement.
        dx = -nudge.displacement_x
        dy = -nudge.displacement_y

        if nudge.object_type == "via":
            at_node = node.find("at")
            if not at_node:
                return False
            at_atoms = at_node.get_atoms()
            cur_x = float(at_atoms[0]) if at_atoms else 0.0
            cur_y = float(at_atoms[1]) if len(at_atoms) > 1 else 0.0
            new_x = round(cur_x + dx, 4)
            new_y = round(cur_y + dy, 4)

            # Find segments currently anchored at the via's post-nudge
            # position before we move the via -- otherwise we'd be looking
            # for segments at the pre-nudge position which no longer exists.
            connected = self._find_connected_segments(cur_x, cur_y)

            at_node.set_value(0, new_x)
            at_node.set_value(1, new_y)

            for seg_node, endpoint in connected:
                ep_node = seg_node.find(endpoint)
                if ep_node:
                    ep_node.set_value(0, new_x)
                    ep_node.set_value(1, new_y)
            return True

        if nudge.object_type == "segment":
            start_node = node.find("start")
            end_node = node.find("end")
            if not (start_node and end_node):
                return False

            start_atoms = start_node.get_atoms()
            end_atoms = end_node.get_atoms()
            sx = float(start_atoms[0]) if start_atoms else 0.0
            sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0.0
            ex = float(end_atoms[0]) if end_atoms else 0.0
            ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0.0

            # Mirror _apply_nudge's via-pinning logic against the current
            # board state.  If a segment endpoint is currently at a via,
            # only the other (moved) endpoint gets the inverse displacement.
            via_positions = self._find_via_positions()
            tolerance = 0.001
            start_at_via = any(
                math.sqrt((sx - vx) ** 2 + (sy - vy) ** 2) <= tolerance for vx, vy in via_positions
            )
            end_at_via = any(
                math.sqrt((ex - vx) ** 2 + (ey - vy) ** 2) <= tolerance for vx, vy in via_positions
            )

            if start_at_via and end_at_via:
                # Both endpoints at vias -- _apply_nudge would have done
                # nothing here, so there's nothing to undo.  Treat as
                # success so the orchestrator continues.
                return True

            if start_at_via:
                end_node.set_value(0, round(ex + dx, 4))
                end_node.set_value(1, round(ey + dy, 4))
            elif end_at_via:
                start_node.set_value(0, round(sx + dx, 4))
                start_node.set_value(1, round(sy + dy, 4))
            else:
                start_node.set_value(0, round(sx + dx, 4))
                start_node.set_value(1, round(sy + dy, 4))
                end_node.set_value(0, round(ex + dx, 4))
                end_node.set_value(1, round(ey + dy, 4))
            return True

        return False

    def _closest_point_on_segment(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        px: float,
        py: float,
    ) -> tuple[float, float, float] | None:
        """Find the closest point on a line segment to a given point.

        Returns: (closest_x, closest_y, distance) or None
        """
        dx = x2 - x1
        dy = y2 - y1
        seg_len_sq = dx * dx + dy * dy

        if seg_len_sq < 1e-10:
            # Segment is a point
            dist = math.sqrt((x1 - px) ** 2 + (y1 - py) ** 2)
            return (x1, y1, dist)

        # Project point onto line, clamped to segment
        t = ((px - x1) * dx + (py - y1) * dy) / seg_len_sq
        t = max(0.0, min(1.0, t))

        cx = x1 + t * dx
        cy = y1 + t * dy
        dist = math.sqrt((cx - px) ** 2 + (cy - py) ** 2)

        return (cx, cy, dist)

    @staticmethod
    def _is_zone_fill_violation(violation: DRCViolation) -> bool:
        """Check whether a violation involves a zone fill.

        Zone fill polygon edges are regenerated by KiCad whenever the
        board is opened or DRC is run, so nudging geometry near zones is
        pointless.  Additionally, ``_find_object_at()`` cannot locate
        zone fill edges as movable objects, causing every zone-related
        violation to be skipped as infeasible.

        Returns True if any item description references a zone.
        """
        for item in violation.items:
            item_lower = item.lower()
            if item_lower.startswith("zone") or "zone " in item_lower:
                return True
        return False

    def _repair_pad_pad_violations(
        self,
        report: DRCReport,
        result: RepairResult,
        max_displacement: float,
        margin: float,
        dry_run: bool,
    ) -> None:
        """Phase 3: Repair pad-pad clearance violations by nudging footprints.

        For each CLEARANCE_PAD_PAD violation between different components,
        selects the smaller/less-connected footprint and nudges it away
        from the other to resolve the clearance deficit.

        Skips:
        - Same-component violations (inherent to footprint geometry)
        - Locked footprints (both locked -> skip entirely)
        - Connector footprints (J-prefix references)
        - Violations exceeding max_displacement
        """
        pad_pad_violations = [
            v
            for v in report.by_type(ViolationType.CLEARANCE_PAD_PAD)
            if not self._is_zone_fill_violation(v)
        ]

        for violation in pad_pad_violations:
            # Skip same-component violations
            if violation.is_same_component_pad_clearance():
                result.footprint_skipped_same_component += 1
                continue

            delta = violation.delta_mm
            if delta is None:
                result.skipped_no_delta += 1
                result.total_violations += 1
                continue

            required_displacement = delta + margin
            result.total_violations += 1

            if required_displacement > max_displacement:
                result.footprint_skipped_exceeds_max += 1
                continue

            if len(violation.locations) < 2:
                result.skipped_no_location += 1
                continue

            # Extract component references from the violation
            refs = _extract_component_refs(violation.items)
            if len(refs) < 2:
                result.skipped_infeasible += 1
                continue

            ref_list = sorted(refs)

            # Find the footprint nodes for each reference
            fp1_info = self._find_footprint_by_ref(ref_list[0])
            fp2_info = self._find_footprint_by_ref(ref_list[1])

            if fp1_info is None or fp2_info is None:
                result.skipped_infeasible += 1
                continue

            fp1_node, fp1_ref, fp1_x, fp1_y, fp1_locked, fp1_pad_count, fp1_is_connector = fp1_info
            fp2_node, fp2_ref, fp2_x, fp2_y, fp2_locked, fp2_pad_count, fp2_is_connector = fp2_info

            # Skip if both are locked
            if fp1_locked and fp2_locked:
                result.footprint_skipped_locked += 1
                continue

            # Select nudge target: prefer smaller/less-connected, skip locked/connectors
            target, other = self._select_nudge_target(fp1_info, fp2_info, result)
            if target is None:
                continue

            t_node, t_ref, t_x, t_y, _, _, _ = target
            o_node, o_ref, o_x, o_y, _, _, _ = other

            # Use pad locations from the violation for displacement direction
            loc1 = violation.locations[0]
            loc2 = violation.locations[1]

            # Determine which location belongs to the target footprint
            refs_from_items = []
            for item in violation.items:
                item_refs = _extract_component_refs([item])
                refs_from_items.append(item_refs)

            # Use violation locations for direction computation
            # First item/location corresponds to first pad, second to second
            if refs_from_items and any(t_ref.upper() in r for r in refs_from_items[:1]):
                pad_x, pad_y = loc1.x_mm, loc1.y_mm
                other_pad_x, other_pad_y = loc2.x_mm, loc2.y_mm
            else:
                pad_x, pad_y = loc2.x_mm, loc2.y_mm
                other_pad_x, other_pad_y = loc1.x_mm, loc1.y_mm

            # Compute displacement vector (move target footprint away from other)
            nudge = self._compute_nudge(
                pad_x, pad_y, other_pad_x, other_pad_y, required_displacement
            )
            if nudge is None:
                result.skipped_infeasible += 1
                continue

            dx, dy, dist = nudge

            actual_clearance = violation.actual_value_mm or 0.0

            fp_nudge_result = FootprintNudgeResult(
                reference=t_ref,
                x=t_x,
                y=t_y,
                displacement_x=dx,
                displacement_y=dy,
                displacement_mm=dist,
                old_clearance_mm=actual_clearance,
                new_clearance_mm=actual_clearance + dist,
                other_reference=o_ref,
            )
            result.footprint_nudge_results.append(fp_nudge_result)

            if not dry_run:
                # Issue #6434: re-measure the two pads after the move and keep
                # it only if the pair now clears the rule.
                pads = self._resolve_pair(violation)
                fp_before = self._xy(t_node.find("at"))
                self._nudge_footprint(t_node, dx, dy)
                fp_after = self._xy(t_node.find("at"))
                undo_dx, undo_dy = fp_before[0] - fp_after[0], fp_before[1] - fp_after[1]
                self._shape_cache = {}
                required = violation.required_value_mm
                if pads[0] is None or pads[1] is None or required is None:
                    self._nudge_footprint(t_node, undo_dx, undo_dy)
                    self._shape_cache = {}
                    result.footprint_nudge_results.pop()
                    result.skipped_infeasible += 1
                    self._note_unrepaired(
                        result, violation, None, "could not identify both pads to re-measure"
                    )
                    continue
                measured = self._measure(pads[0], pads[1])
                if measured + _GAP_EPSILON_MM < required:
                    self._nudge_footprint(t_node, undo_dx, undo_dy)
                    self._shape_cache = {}
                    result.footprint_nudge_results.pop()
                    result.skipped_not_cleared += 1
                    self._note_unrepaired(
                        result,
                        violation,
                        measured,
                        f"moving footprint {t_ref} left the pads {measured:.4f} mm apart",
                    )
                    continue
                fp_nudge_result.new_clearance_mm = measured
                self.modified = True

            result.footprint_nudges += 1
            result.repaired += 1

    def _find_footprint_by_ref(
        self,
        reference: str,
    ) -> tuple[SExp, str, float, float, bool, int, bool] | None:
        """Find a footprint node by reference designator.

        Returns: (fp_node, reference, x, y, locked, pad_count, is_connector) or None.
        """
        ref_upper = reference.upper()

        for fp_node in _find_all_footprints(self.doc):
            # Check reference in property nodes (KiCad 8+)
            fp_ref = ""
            for prop in fp_node.find_all("property"):
                prop_name = prop.get_string(0)
                if prop_name == "Reference":
                    fp_ref = prop.get_string(1) or ""
                    break

            # Fall back to fp_text (KiCad 7)
            if not fp_ref:
                for fp_text in fp_node.find_all("fp_text"):
                    text_type = fp_text.get_string(0)
                    if text_type == "reference":
                        fp_ref = fp_text.get_string(1) or ""
                        break

            if fp_ref.upper() != ref_upper:
                continue

            # Get position. Direct child only -- pads, properties and
            # fp_texts carry their own (at) nodes, and a recursive
            # find() would misread a descendant's position if the
            # footprint-level (at) were absent (issue #3602).
            at_node = fp_node.find_child("at")
            if not at_node:
                continue
            at_atoms = at_node.get_atoms()
            fp_x = float(at_atoms[0]) if at_atoms else 0.0
            fp_y = float(at_atoms[1]) if len(at_atoms) > 1 else 0.0

            # Check locked status. Modern form (KiCad 9/10) is a
            # top-level ``(locked yes)`` child; the legacy KiCad-6 form
            # is a ``locked`` token inside ``(attr ...)``. Accept both
            # (issue #3457) -- direct children only, since pads can
            # carry their own (locked yes).
            locked = False
            for locked_node in fp_node.find_children("locked"):
                if (locked_node.get_string(0) or "yes") in ("yes", "true"):
                    locked = True
                    break
            if not locked:
                # Direct child only, matching the locked lookup above.
                attr_node = fp_node.find_child("attr")
                if attr_node:
                    for i in range(len(attr_node.children)):
                        token = attr_node.get_string(i)
                        if token == "locked":
                            locked = True
                            break

            # Count pads
            pad_count = len(list(fp_node.find_all("pad")))

            # Check if connector (J-prefix)
            is_connector = fp_ref.upper().startswith("J")

            return (fp_node, fp_ref, fp_x, fp_y, locked, pad_count, is_connector)

        return None

    def _select_nudge_target(
        self,
        fp1_info: tuple[SExp, str, float, float, bool, int, bool],
        fp2_info: tuple[SExp, str, float, float, bool, int, bool],
        result: RepairResult,
    ) -> tuple[
        tuple[SExp, str, float, float, bool, int, bool] | None,
        tuple[SExp, str, float, float, bool, int, bool] | None,
    ]:
        """Select which footprint to nudge for a pad-pad violation.

        Prefers the smaller (fewer pads) footprint. Skips locked footprints
        and connectors (J-prefix), falling back to the other when possible.

        Returns: (target, other) or (None, None) if neither can be nudged.
        """
        _, _, _, _, fp1_locked, fp1_pads, fp1_conn = fp1_info
        _, _, _, _, fp2_locked, fp2_pads, fp2_conn = fp2_info

        # Build candidates: (info, movable)
        fp1_movable = not fp1_locked and not fp1_conn
        fp2_movable = not fp2_locked and not fp2_conn

        # If neither is movable, check why and report
        if not fp1_movable and not fp2_movable:
            if fp1_locked or fp2_locked:
                result.footprint_skipped_locked += 1
            elif fp1_conn or fp2_conn:
                result.footprint_skipped_connector += 1
            return (None, None)

        # If only one is movable, use it
        if fp1_movable and not fp2_movable:
            return (fp1_info, fp2_info)
        if fp2_movable and not fp1_movable:
            return (fp2_info, fp1_info)

        # Both movable: prefer the one with fewer pads (smaller/less connected)
        if fp1_pads <= fp2_pads:
            return (fp1_info, fp2_info)
        return (fp2_info, fp1_info)

    def _nudge_footprint(
        self,
        fp_node: SExp,
        dx: float,
        dy: float,
    ) -> None:
        """Apply a displacement to a footprint's position in the S-expression tree.

        Also updates the endpoints of any trace segments connected to the
        footprint's pads, so that net connectivity is preserved.
        """
        at_node = fp_node.find("at")
        if not at_node:
            return

        at_atoms = at_node.get_atoms()
        old_x = float(at_atoms[0]) if at_atoms else 0.0
        old_y = float(at_atoms[1]) if len(at_atoms) > 1 else 0.0
        fp_rot = float(at_atoms[2]) if len(at_atoms) > 2 else 0.0

        new_x = round(old_x + dx, 4)
        new_y = round(old_y + dy, 4)

        # Compute absolute pad positions before the move.
        # KiCad applies the footprint orientation as a NEGATED angle vs
        # standard CCW math (verified vs pcbnew, issue #3739).
        angle_rad = math.radians(-fp_rot)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)

        pad_positions: list[tuple[float, float]] = []
        for pad_node in fp_node.find_all("pad"):
            pad_at = pad_node.find("at")
            if not pad_at:
                continue
            pad_atoms = pad_at.get_atoms()
            local_x = float(pad_atoms[0]) if pad_atoms else 0.0
            local_y = float(pad_atoms[1]) if len(pad_atoms) > 1 else 0.0

            abs_x = old_x + local_x * cos_a - local_y * sin_a
            abs_y = old_y + local_x * sin_a + local_y * cos_a
            pad_positions.append((abs_x, abs_y))

        # Move the footprint, and the zones (keepouts) embedded in it: KiCad
        # stores those in board coordinates, so they do not follow the
        # ``(at ...)`` on their own (Issue #6119).
        old_frame = footprint_node_frame(fp_node)
        at_node.set_value(0, new_x)
        at_node.set_value(1, new_y)
        transform_footprint_zone_nodes(fp_node, old_frame, footprint_node_frame(fp_node))

        # Update connected trace segment endpoints
        for old_pad_x, old_pad_y in pad_positions:
            new_pad_x = round(old_pad_x + dx, 4)
            new_pad_y = round(old_pad_y + dy, 4)

            connected = self._find_connected_segments(old_pad_x, old_pad_y)
            for seg_node, endpoint in connected:
                ep_node = seg_node.find(endpoint)
                if ep_node:
                    ep_node.set_value(0, new_pad_x)
                    ep_node.set_value(1, new_pad_y)

    def save(self, output_path: str | Path | None = None) -> None:
        """Save the modified PCB.

        Args:
            output_path: Path to save to. If None, overwrites the input file.
        """
        from ..core.sexp_file import save_pcb

        path = Path(output_path) if output_path else self.path
        save_pcb(self.doc, path)

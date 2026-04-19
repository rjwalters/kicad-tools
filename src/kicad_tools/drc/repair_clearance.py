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
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..sexp import SExp, parse_file
from .report import DRCReport
from .violation import DRCViolation, ViolationType

if TYPE_CHECKING:
    from .local_rerouter import LocalRerouter


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

    def __str__(self) -> str:
        return (
            f"{self.object_type} [{self.net_name}] at ({self.x:.4f}, {self.y:.4f}) on {self.layer}: "
            f"nudged {self.displacement_mm:.4f}mm "
            f"(clearance {self.old_clearance_mm:.4f} -> {self.new_clearance_mm:.4f}mm)"
        )


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
    relocated_vias: int = 0
    endpoint_nudges: int = 0
    local_rerouted: int = 0
    cluster_rerouted: int = 0
    skipped_no_local_route: int = 0
    nudges: list[NudgeResult] = field(default_factory=list)

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
        if self.skipped_exceeds_max > 0:
            lines.append(f"  Skipped (exceeds max displacement): {self.skipped_exceeds_max}")
        if self.skipped_infeasible > 0:
            lines.append(f"  Skipped (infeasible): {self.skipped_infeasible}")
        if self.skipped_no_local_route > 0:
            lines.append(f"  Skipped (no local route): {self.skipped_no_local_route}")
        if self.skipped_no_location > 0:
            lines.append(f"  Skipped (no location): {self.skipped_no_location}")
        if self.skipped_no_delta > 0:
            lines.append(f"  Skipped (no delta info): {self.skipped_no_delta}")
        return "\n".join(lines)


class ClearanceRepairer:
    """Repairs clearance violations by nudging traces and vias.

    Unlike DRCFixer which deletes traces, this tool computes minimal
    displacements to achieve the required clearance without removing
    any routing.
    """

    def __init__(self, pcb_path: str | Path):
        """Load a PCB file for repair.

        Args:
            pcb_path: Path to .kicad_pcb file
        """
        self.path = Path(pcb_path)
        self.doc = parse_file(self.path)
        self.modified = False

        # Build net index
        self.nets: dict[int, str] = {}
        self.net_names: dict[str, int] = {}
        self._parse_nets()

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

    def repair_from_report(
        self,
        report: DRCReport,
        max_displacement: float = 0.1,
        margin: float = 0.01,
        prefer: str = "move-trace",
        dry_run: bool = False,
        local_reroute: bool = False,
        local_grid_padding: float = 0.5,
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

        Returns:
            RepairResult with details of all repairs
        """
        result = RepairResult()

        clearances = [v for v in report.by_type(ViolationType.CLEARANCE)
                      if not self._is_zone_fill_violation(v)]
        segment_via_clearances = [v for v in report.by_type(ViolationType.CLEARANCE_SEGMENT_VIA)
                                  if not self._is_zone_fill_violation(v)]
        pad_segment_clearances = [v for v in report.by_type(ViolationType.CLEARANCE_PAD_SEGMENT)
                                  if not self._is_zone_fill_violation(v)]
        pad_via_clearances = [v for v in report.by_type(ViolationType.CLEARANCE_PAD_VIA)
                              if not self._is_zone_fill_violation(v)]
        all_clearances = (
            clearances + segment_via_clearances + pad_segment_clearances + pad_via_clearances
        )
        result.total_violations = len(all_clearances)

        # Track violations where nudge was explicitly skipped as infeasible
        skipped_violations: list[DRCViolation] = []

        for violation in clearances:
            before_infeasible = result.skipped_infeasible
            self._repair_single_violation(
                violation, result, max_displacement, margin, prefer, dry_run
            )
            if result.skipped_infeasible > before_infeasible:
                skipped_violations.append(violation)

        # For segment-to-via violations, always prefer moving the trace
        # (never the via, since it was just sized by fix-vias)
        for violation in segment_via_clearances:
            before_infeasible = result.skipped_infeasible
            self._repair_single_violation(
                violation, result, max_displacement, margin, "move-trace", dry_run
            )
            if result.skipped_infeasible > before_infeasible:
                skipped_violations.append(violation)

        # For pad-segment violations, move the segment (pads are immovable)
        for violation in pad_segment_clearances:
            before_infeasible = result.skipped_infeasible
            self._repair_single_violation(
                violation, result, max_displacement, margin, "move-trace", dry_run
            )
            if result.skipped_infeasible > before_infeasible:
                skipped_violations.append(violation)

        # For pad-via violations, move the via (pads are immovable)
        for violation in pad_via_clearances:
            before_infeasible = result.skipped_infeasible
            self._repair_single_violation(
                violation, result, max_displacement, margin, "move-via", dry_run
            )
            if result.skipped_infeasible > before_infeasible:
                skipped_violations.append(violation)

        # Phase 2: Local rerouting for infeasible violations
        if local_reroute:
            # Also find violations where nudge "succeeded" but _apply_nudge
            # silently did nothing because both endpoints sit at vias.
            # These are counted as "repaired" by the nudge phase but need rerouting.
            both_at_vias = self._find_both_endpoints_at_vias_violations(
                all_clearances, result, max_displacement, margin
            )

            all_reroute_candidates = []
            # Violations explicitly skipped as infeasible: counter adjustments
            # on success are: skipped_infeasible -= 1, repaired += 1
            for v in skipped_violations:
                all_reroute_candidates.append((v, "skipped"))
            # Violations where nudge was counted as repaired but segment didn't move:
            # counter adjustments on success are: repaired stays the same (already counted),
            # only local_rerouted += 1
            # Deduplicate: exclude any already queued via skipped_violations to avoid
            # processing the same violation twice.
            skipped_set = {id(v) for v in skipped_violations}
            for v in both_at_vias:
                if id(v) not in skipped_set:
                    all_reroute_candidates.append((v, "phantom_repair"))

            if all_reroute_candidates:
                self._run_local_reroute_phase(
                    all_reroute_candidates, result, margin, dry_run, local_grid_padding
                )

        return result

    def _find_both_endpoints_at_vias_violations(
        self,
        violations: list[DRCViolation],
        result: RepairResult,
        max_displacement: float,
        margin: float,
    ) -> list[DRCViolation]:
        """Identify violations where the nudge "repaired" a segment but
        _apply_nudge silently did nothing because both endpoints are at vias.

        These violations were counted as repaired but the segment wasn't
        actually moved. We need to reroute them instead.
        """
        via_positions = self._find_via_positions()
        tolerance = 0.001
        both_at_vias: list[DRCViolation] = []

        for violation in violations:
            if len(violation.locations) < 2:
                continue
            delta = violation.delta_mm
            if delta is None:
                continue
            if delta + margin > max_displacement:
                continue

            # Find the segment object for this violation
            loc1 = violation.locations[0]
            loc2 = violation.locations[1]
            obj1 = self._find_object_at(loc1.x_mm, loc1.y_mm, loc1.layer, violation.nets)
            obj2 = self._find_object_at(loc2.x_mm, loc2.y_mm, loc2.layer, violation.nets)

            # Check if either object is a segment with both endpoints at vias
            for obj in (obj1, obj2):
                if obj is None or obj[1] != "segment":
                    continue
                seg_node = obj[0]
                start_node = seg_node.find("start")
                end_node = seg_node.find("end")
                if not (start_node and end_node):
                    continue
                start_atoms = start_node.get_atoms()
                end_atoms = end_node.get_atoms()
                sx = float(start_atoms[0]) if start_atoms else 0
                sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0
                ex = float(end_atoms[0]) if end_atoms else 0
                ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0

                start_at_via = any(
                    math.sqrt((sx - vx) ** 2 + (sy - vy) ** 2) <= tolerance
                    for vx, vy in via_positions
                )
                end_at_via = any(
                    math.sqrt((ex - vx) ** 2 + (ey - vy) ** 2) <= tolerance
                    for vx, vy in via_positions
                )

                if start_at_via and end_at_via:
                    both_at_vias.append(violation)
                    break

        return both_at_vias

    def _run_local_reroute_phase(
        self,
        tagged_violations: list[tuple[DRCViolation, str]],
        result: RepairResult,
        margin: float,
        dry_run: bool,
        local_grid_padding: float,
    ) -> None:
        """Attempt local A* rerouting for violations that nudging could not fix.

        Each violation is tagged with its source:
        - "skipped": explicitly counted as skipped_infeasible by nudge phase
        - "phantom_repair": counted as repaired but _apply_nudge did nothing

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

    def _extract_obstacle_info(
        self,
        violation: DRCViolation,
        margin: float,
    ) -> tuple[float, float, float] | None:
        """Extract (x, y, radius) obstacle tuple from a violation.

        Returns the position and radius of the non-segment object in the
        violation, or None if the obstacle cannot be determined.
        """
        if len(violation.locations) < 2:
            return None

        loc1 = violation.locations[0]
        loc2 = violation.locations[1]

        obj1 = self._find_object_at(loc1.x_mm, loc1.y_mm, loc1.layer, violation.nets)
        obj2 = self._find_object_at(loc2.x_mm, loc2.y_mm, loc2.layer, violation.nets)

        # Identify which is the obstacle (non-segment) and its location
        if obj1 is not None and obj1[1] == "segment":
            obs_x, obs_y, obs_obj = loc2.x_mm, loc2.y_mm, obj2
        elif obj2 is not None and obj2[1] == "segment":
            obs_x, obs_y, obs_obj = loc1.x_mm, loc1.y_mm, obj1
        else:
            # Neither is a segment -- use first location as obstacle
            obs_x, obs_y, obs_obj = loc1.x_mm, loc1.y_mm, obj1

        obstacle_radius = 0.3  # Default fallback
        if obs_obj is not None and obs_obj[1] == "via":
            via_node = obs_obj[0]
            size_node = via_node.find("size")
            if size_node:
                obstacle_radius = float(size_node.get_first_atom()) / 2
        elif obs_obj is not None and obs_obj[1] == "segment":
            other_seg = obs_obj[0]
            width_node = other_seg.find("width")
            obstacle_radius = float(width_node.get_first_atom()) / 2 if width_node else 0.125
        elif obs_obj is not None and obs_obj[1] == "pad":
            obstacle_radius = self._pad_obstacle_radius(obs_obj[0])

        return (obs_x, obs_y, obstacle_radius)

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
        if len(violation.locations) < 2:
            return False

        loc1 = violation.locations[0]
        loc2 = violation.locations[1]

        seg_info = None
        obstacle_info = None

        obj1 = self._find_object_at(loc1.x_mm, loc1.y_mm, loc1.layer, violation.nets)
        obj2 = self._find_object_at(loc2.x_mm, loc2.y_mm, loc2.layer, violation.nets)

        if obj1 is not None and obj1[1] == "segment":
            seg_info = obj1
            obstacle_info = (loc2.x_mm, loc2.y_mm, obj2)
        elif obj2 is not None and obj2[1] == "segment":
            seg_info = obj2
            obstacle_info = (loc1.x_mm, loc1.y_mm, obj1)

        if seg_info is None:
            return False

        seg_node = seg_info[0]
        obs_x, obs_y, obs_obj = obstacle_info

        obstacle_radius = 0.3
        if obs_obj is not None and obs_obj[1] == "via":
            via_node = obs_obj[0]
            size_node = via_node.find("size")
            if size_node:
                obstacle_radius = float(size_node.get_first_atom()) / 2
        elif obs_obj is not None and obs_obj[1] == "segment":
            other_seg = obs_obj[0]
            width_node = other_seg.find("width")
            obstacle_radius = float(width_node.get_first_atom()) / 2 if width_node else 0.125
        elif obs_obj is not None and obs_obj[1] == "pad":
            obstacle_radius = self._pad_obstacle_radius(obs_obj[0])

        width_node = seg_node.find("width")
        trace_width = float(width_node.get_first_atom()) if width_node else 0.25

        required_clearance = violation.required_value_mm or 0.2
        trace_clearance = required_clearance + margin

        reroute_result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=obs_x,
            obstacle_y=obs_y,
            obstacle_radius=obstacle_radius,
            trace_width=trace_width,
            trace_clearance=trace_clearance,
            dry_run=dry_run,
            extra_obstacles=extra_obstacles,
        )

        if reroute_result.success:
            result.local_rerouted += 1
            result.cluster_rerouted += 1
            if source == "skipped":
                result.repaired += 1
                result.skipped_infeasible -= 1
            if not dry_run:
                self.modified = True
            return True

        return False

    def _attempt_local_reroute(
        self,
        violation: DRCViolation,
        result: RepairResult,
        rerouter: LocalRerouter,
        margin: float,
        dry_run: bool,
        source: str = "skipped",
    ) -> None:
        """Attempt to locally reroute a single infeasible violation.

        Args:
            source: "skipped" if the violation was counted as skipped_infeasible,
                    "phantom_repair" if it was counted as repaired but the nudge
                    silently did nothing (both endpoints at vias).
        """
        if len(violation.locations) < 2:
            result.skipped_no_local_route += 1
            return

        loc1 = violation.locations[0]
        loc2 = violation.locations[1]

        # Find segment and obstacle objects at the two violation locations
        seg_info = None
        obstacle_info = None

        obj1 = self._find_object_at(loc1.x_mm, loc1.y_mm, loc1.layer, violation.nets)
        obj2 = self._find_object_at(loc2.x_mm, loc2.y_mm, loc2.layer, violation.nets)

        if obj1 is not None and obj1[1] == "segment":
            seg_info = obj1
            obstacle_info = (loc2.x_mm, loc2.y_mm, obj2)
        elif obj2 is not None and obj2[1] == "segment":
            seg_info = obj2
            obstacle_info = (loc1.x_mm, loc1.y_mm, obj1)

        if seg_info is None:
            result.skipped_no_local_route += 1
            return

        seg_node = seg_info[0]
        obs_x, obs_y, obs_obj = obstacle_info

        # Determine obstacle radius
        obstacle_radius = 0.3  # Default fallback
        if obs_obj is not None and obs_obj[1] == "via":
            via_node = obs_obj[0]
            size_node = via_node.find("size")
            if size_node:
                obstacle_radius = float(size_node.get_first_atom()) / 2
        elif obs_obj is not None and obs_obj[1] == "segment":
            # Segment-to-segment: use the segment's width as obstacle radius
            other_seg = obs_obj[0]
            width_node = other_seg.find("width")
            obstacle_radius = float(width_node.get_first_atom()) / 2 if width_node else 0.125
        elif obs_obj is not None and obs_obj[1] == "pad":
            obstacle_radius = self._pad_obstacle_radius(obs_obj[0])

        # Get trace parameters
        width_node = seg_node.find("width")
        trace_width = float(width_node.get_first_atom()) if width_node else 0.25

        # Required clearance from violation info
        required_clearance = violation.required_value_mm or 0.2
        trace_clearance = required_clearance + margin

        reroute_result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=obs_x,
            obstacle_y=obs_y,
            obstacle_radius=obstacle_radius,
            trace_width=trace_width,
            trace_clearance=trace_clearance,
            dry_run=dry_run,
        )

        if reroute_result.success:
            result.local_rerouted += 1
            if source == "skipped":
                # Violation was counted as skipped_infeasible in nudge phase;
                # undo that and count as repaired instead.
                result.repaired += 1
                result.skipped_infeasible -= 1
            # For "phantom_repair" source: repaired was already incremented
            # in the nudge phase, so we only need local_rerouted.
            if not dry_run:
                self.modified = True
        else:
            result.skipped_no_local_route += 1
            if source == "phantom_repair":
                # The nudge phase already incremented repaired, but the segment
                # didn't actually move and local reroute also failed.  Undo the
                # phantom repaired count to avoid double-counting.
                result.repaired -= 1

    def _repair_single_violation(
        self,
        violation: DRCViolation,
        result: RepairResult,
        max_displacement: float,
        margin: float,
        prefer: str,
        dry_run: bool,
    ) -> None:
        """Attempt to repair a single clearance violation."""
        # Need at least two locations (the two objects that are too close)
        if len(violation.locations) < 2:
            if violation.primary_location is None:
                result.skipped_no_location += 1
                return
            # With only one location, we can still try to find nearby objects
            loc = violation.primary_location
            delta = violation.delta_mm
            if delta is None:
                result.skipped_no_delta += 1
                return
            self._repair_from_single_location(
                loc.x_mm,
                loc.y_mm,
                loc.layer,
                delta,
                margin,
                violation,
                result,
                max_displacement,
                prefer,
                dry_run,
            )
            return

        loc1 = violation.locations[0]
        loc2 = violation.locations[1]

        delta = violation.delta_mm
        if delta is None:
            result.skipped_no_delta += 1
            return

        # Required displacement is the clearance deficit plus margin
        required_displacement = delta + margin

        if required_displacement > max_displacement:
            result.skipped_exceeds_max += 1
            return

        # Find objects at the two locations
        obj1 = self._find_object_at(loc1.x_mm, loc1.y_mm, loc1.layer, violation.nets)
        obj2 = self._find_object_at(loc2.x_mm, loc2.y_mm, loc2.layer, violation.nets)

        if obj1 is None and obj2 is None:
            result.skipped_infeasible += 1
            return

        # Choose which object to move based on preference and movability
        target = self._choose_target(obj1, obj2, prefer)
        if target is None:
            result.skipped_infeasible += 1
            return

        obj_node, obj_type, obj_x, obj_y, obj_layer, obj_net = target

        # Determine the other location (the one we're moving away from)
        if obj1 is not None and obj1[0] is obj_node:
            other_x, other_y = loc2.x_mm, loc2.y_mm
        else:
            other_x, other_y = loc1.x_mm, loc1.y_mm

        # Calculate displacement vector (away from the other object)
        nudge = self._compute_nudge(obj_x, obj_y, other_x, other_y, required_displacement)
        if nudge is None:
            result.skipped_infeasible += 1
            return

        dx, dy, dist = nudge

        # Get UUID
        uuid_node = obj_node.find("uuid")
        uuid_str = uuid_node.get_first_atom() if uuid_node else ""

        # Get actual clearance values for reporting
        actual_clearance = violation.actual_value_mm or 0.0
        required_clearance = violation.required_value_mm or 0.0

        nudge_result = NudgeResult(
            object_type=obj_type,
            x=obj_x,
            y=obj_y,
            net_name=obj_net,
            layer=obj_layer,
            displacement_x=dx,
            displacement_y=dy,
            displacement_mm=dist,
            old_clearance_mm=actual_clearance,
            new_clearance_mm=actual_clearance + dist,
            uuid=uuid_str,
        )
        result.nudges.append(nudge_result)

        if not dry_run:
            self._apply_nudge(obj_node, obj_type, dx, dy, result=result)
            self.modified = True

        result.repaired += 1

    def _repair_from_single_location(
        self,
        x: float,
        y: float,
        layer: str,
        delta: float,
        margin: float,
        violation: DRCViolation,
        result: RepairResult,
        max_displacement: float,
        prefer: str,
        dry_run: bool,
    ) -> None:
        """Repair a violation with only one reported location.

        Finds both objects near the violation point and nudges the preferred one.
        """
        required_displacement = delta + margin

        if required_displacement > max_displacement:
            result.skipped_exceeds_max += 1
            return

        # Find objects near the violation location
        search_radius = 1.0  # mm
        segments = self._find_segments_near(x, y, search_radius, layer, violation.nets)
        vias = self._find_vias_near(x, y, search_radius, violation.nets)

        # Need at least two objects to have a clearance issue
        all_objects = []
        for seg_info in segments:
            all_objects.append(seg_info)
        for via_info in vias:
            all_objects.append(via_info)

        if len(all_objects) < 2:
            result.skipped_infeasible += 1
            return

        # Pick the movable target based on preference
        target = self._choose_target(all_objects[0], all_objects[1], prefer)
        if target is None:
            result.skipped_infeasible += 1
            return

        obj_node, obj_type, obj_x, obj_y, obj_layer, obj_net = target

        # The other object is the one we're moving away from
        other = all_objects[1] if all_objects[0][0] is obj_node else all_objects[0]
        _, _, other_x, other_y, _, _ = other

        nudge = self._compute_nudge(obj_x, obj_y, other_x, other_y, required_displacement)
        if nudge is None:
            result.skipped_infeasible += 1
            return

        dx, dy, dist = nudge

        uuid_node = obj_node.find("uuid")
        uuid_str = uuid_node.get_first_atom() if uuid_node else ""

        actual_clearance = violation.actual_value_mm or 0.0

        nudge_result = NudgeResult(
            object_type=obj_type,
            x=obj_x,
            y=obj_y,
            net_name=obj_net,
            layer=obj_layer,
            displacement_x=dx,
            displacement_y=dy,
            displacement_mm=dist,
            old_clearance_mm=actual_clearance,
            new_clearance_mm=actual_clearance + dist,
            uuid=uuid_str,
        )
        result.nudges.append(nudge_result)

        if not dry_run:
            self._apply_nudge(obj_node, obj_type, dx, dy, result=result)
            self.modified = True

        result.repaired += 1

    def _find_object_at(
        self,
        x: float,
        y: float,
        layer: str,
        nets: list[str],
    ) -> tuple[SExp, str, float, float, str, str] | None:
        """Find the nearest PCB object at a location.

        Uses a 1.5mm search radius to account for enlarged vias where the
        violation location (copper edge) can be offset from the via center
        by up to the via radius (~0.4mm for 0.8mm diameter vias).

        Returns: (node, type, x, y, layer, net_name) or None
        """
        search_radius = 1.5  # mm - large enough for enlarged vias

        # Check segments
        segments = self._find_segments_near(x, y, search_radius, layer, nets)
        if segments:
            return segments[0]

        # Check vias (use relaxed net matching for segment-via violations)
        vias = self._find_vias_near(x, y, search_radius, nets)
        if vias:
            return vias[0]

        # Check pads (pads are immovable, but we return them so
        # _choose_target() can select the *other* object to move)
        pads = self._find_pads_near(x, y, search_radius, layer, nets)
        if pads:
            return pads[0]

        return None

    def _find_segments_near(
        self,
        x: float,
        y: float,
        radius: float,
        layer: str | None,
        nets: list[str] | None,
    ) -> list[tuple[SExp, str, float, float, str, str]]:
        """Find track segments near a point.

        Returns list of (node, "segment", closest_x, closest_y, layer, net_name).
        """
        results = []

        for seg_node in self.doc.find_all("segment"):
            start_node = seg_node.find("start")
            end_node = seg_node.find("end")
            if not (start_node and end_node):
                continue

            start_atoms = start_node.get_atoms()
            end_atoms = end_node.get_atoms()

            sx = float(start_atoms[0]) if start_atoms else 0
            sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0
            ex = float(end_atoms[0]) if end_atoms else 0
            ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0

            # Find closest point on segment to the target
            closest = self._closest_point_on_segment(sx, sy, ex, ey, x, y)
            if closest is None:
                continue
            cx, cy, dist = closest

            if dist > radius:
                continue

            layer_node = seg_node.find("layer")
            seg_layer = layer_node.get_first_atom() if layer_node else ""
            if layer and seg_layer != layer:
                continue

            net_node = seg_node.find("net")
            net_num = int(net_node.get_first_atom()) if net_node else 0
            net_name = self.nets.get(net_num, "")

            if nets and net_name not in nets:
                continue

            results.append((seg_node, "segment", cx, cy, seg_layer, net_name))

        return results

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
            net_num = int(net_node.get_first_atom()) if net_node else 0
            net_name = self.nets.get(net_num, "")

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

        for fp_node in self.doc.find_all("footprint"):
            # Get footprint position and rotation
            fp_at = fp_node.find("at")
            if not fp_at:
                continue
            fp_atoms = fp_at.get_atoms()
            fp_x = float(fp_atoms[0]) if fp_atoms else 0.0
            fp_y = float(fp_atoms[1]) if len(fp_atoms) > 1 else 0.0
            fp_rot = float(fp_atoms[2]) if len(fp_atoms) > 2 else 0.0

            angle_rad = math.radians(fp_rot)
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
        positions: list[tuple[float, float]] = []
        for via_node in self.doc.find_all("via"):
            at_node = via_node.find("at")
            if at_node:
                at_atoms = at_node.get_atoms()
                vx = float(at_atoms[0]) if at_atoms else 0
                vy = float(at_atoms[1]) if len(at_atoms) > 1 else 0
                positions.append((vx, vy))
        return positions

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

        for seg_node in self.doc.find_all("segment"):
            start_node = seg_node.find("start")
            end_node = seg_node.find("end")
            if not (start_node and end_node):
                continue

            start_atoms = start_node.get_atoms()
            end_atoms = end_node.get_atoms()

            sx = float(start_atoms[0]) if start_atoms else 0
            sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0
            ex = float(end_atoms[0]) if end_atoms else 0
            ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0

            if math.sqrt((sx - x) ** 2 + (sy - y) ** 2) <= tolerance:
                results.append((seg_node, "start"))
            if math.sqrt((ex - x) ** 2 + (ey - y) ** 2) <= tolerance:
                results.append((seg_node, "end"))

        return results

    def _apply_nudge(
        self,
        node: SExp,
        obj_type: str,
        dx: float,
        dy: float,
        result: RepairResult | None = None,
    ) -> None:
        """Apply a displacement to a PCB object.

        For segments: if an endpoint sits at a via position, only the non-via
        endpoint is moved (endpoint-only nudge) to avoid disconnecting the net.
        If both endpoints sit at vias, the segment is skipped as infeasible.
        Otherwise both endpoints are moved (full-segment nudge).

        For vias: moves the via position and also updates the endpoints of all
        connected segments so that net connectivity is preserved.
        """
        if obj_type == "via":
            at_node = node.find("at")
            if at_node:
                at_atoms = at_node.get_atoms()
                old_x = float(at_atoms[0]) if at_atoms else 0
                old_y = float(at_atoms[1]) if len(at_atoms) > 1 else 0
                new_x = round(old_x + dx, 4)
                new_y = round(old_y + dy, 4)

                # Move the via
                at_node.set_value(0, new_x)
                at_node.set_value(1, new_y)

                # Update all segments connected to the old via position
                connected = self._find_connected_segments(old_x, old_y)
                for seg_node, endpoint in connected:
                    ep_node = seg_node.find(endpoint)
                    if ep_node:
                        ep_node.set_value(0, new_x)
                        ep_node.set_value(1, new_y)

                if result is not None:
                    result.relocated_vias += 1

        elif obj_type == "segment":
            start_node = node.find("start")
            end_node = node.find("end")

            if not (start_node and end_node):
                return

            start_atoms = start_node.get_atoms()
            end_atoms = end_node.get_atoms()
            sx = float(start_atoms[0]) if start_atoms else 0
            sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0
            ex = float(end_atoms[0]) if end_atoms else 0
            ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0

            # Check which endpoints sit at a via position
            via_positions = self._find_via_positions()
            tolerance = 0.001
            start_at_via = any(
                math.sqrt((sx - vx) ** 2 + (sy - vy) ** 2) <= tolerance for vx, vy in via_positions
            )
            end_at_via = any(
                math.sqrt((ex - vx) ** 2 + (ey - vy) ** 2) <= tolerance for vx, vy in via_positions
            )

            if start_at_via and end_at_via:
                # Both endpoints at vias -- moving either end disconnects a net.
                # This is logged as infeasible by the caller; we do nothing here.
                return

            if start_at_via:
                # Only move the end (free) endpoint, keep start pinned to via
                end_node.set_value(0, round(ex + dx, 4))
                end_node.set_value(1, round(ey + dy, 4))
                if result is not None:
                    result.endpoint_nudges += 1
            elif end_at_via:
                # Only move the start (free) endpoint, keep end pinned to via
                start_node.set_value(0, round(sx + dx, 4))
                start_node.set_value(1, round(sy + dy, 4))
                if result is not None:
                    result.endpoint_nudges += 1
            else:
                # Neither endpoint at a via -- full-segment nudge (original behavior)
                start_node.set_value(0, round(sx + dx, 4))
                start_node.set_value(1, round(sy + dy, 4))
                end_node.set_value(0, round(ex + dx, 4))
                end_node.set_value(1, round(ey + dy, 4))

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

    def save(self, output_path: str | Path | None = None) -> None:
        """Save the modified PCB.

        Args:
            output_path: Path to save to. If None, overwrites the input file.
        """
        from ..core.sexp_file import save_pcb

        path = Path(output_path) if output_path else self.path
        save_pcb(self.doc, path)

"""Reconciliation engine for schematic-to-PCB synchronization.

Wraps the consistency checker's analysis output and bridges it to the
PCB mutation primitives in pcb_modify.py.

The reconciler works in two phases:
  1. Analyze: Detect mismatches and propose mappings with confidence levels.
  2. Apply: Execute proposed changes on the PCB file.

Example:
    >>> from kicad_tools.sync.reconciler import Reconciler
    >>> r = Reconciler("project.kicad_pro")
    >>> analysis = r.analyze()
    >>> print(analysis.summary())
    >>> # Apply with dry-run first
    >>> changes = r.apply(analysis, dry_run=True)
    >>> # Then apply for real
    >>> changes = r.apply(analysis, dry_run=False)
"""

from __future__ import annotations

import json
import math
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.schema.bom import extract_bom


@dataclass(frozen=True)
class SyncMatch:
    """A proposed mapping between a schematic component and a PCB footprint.

    Attributes:
        schematic_ref: Reference designator in the schematic.
        pcb_ref: Reference designator in the PCB (may differ from schematic_ref).
        confidence: Match confidence - "high", "medium", or "low".
        match_type: How the match was determined:
            - "exact": References match exactly.
            - "value_footprint": Matched by value + footprint combination.
            - "footprint_only": Matched by footprint only (ambiguous).
        actions: List of actions needed to reconcile this match.
            Each action is a dict with "type" and relevant fields.
    """

    schematic_ref: str
    pcb_ref: str
    confidence: str  # "high", "medium", "low"
    match_type: str  # "exact", "value_footprint", "footprint_only"
    actions: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        """Validate field values."""
        if self.confidence not in ("high", "medium", "low"):
            raise ValueError(
                f"confidence must be 'high', 'medium', or 'low', got {self.confidence!r}"
            )
        valid_types = ("exact", "value_footprint", "footprint_only")
        if self.match_type not in valid_types:
            raise ValueError(f"match_type must be one of {valid_types}, got {self.match_type!r}")

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "schematic_ref": self.schematic_ref,
            "pcb_ref": self.pcb_ref,
            "confidence": self.confidence,
            "match_type": self.match_type,
            "actions": list(self.actions),
        }


@dataclass
class SyncChange:
    """Record of a change applied (or to be applied) to the PCB.

    Attributes:
        reference: Component reference affected.
        change_type: Type of change - "rename", "update_value", "update_footprint".
        old_value: Previous value.
        new_value: New value.
        applied: Whether the change was actually applied (False for dry-run).
    """

    reference: str
    change_type: str  # "rename", "update_value", "update_footprint", "add_footprint", "remove_orphan"
    old_value: str
    new_value: str
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "reference": self.reference,
            "change_type": self.change_type,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "applied": self.applied,
        }


@dataclass
class SyncAnalysis:
    """Result of analyzing schematic/PCB mismatches.

    Groups results into categories by confidence level and orphan status.

    Attributes:
        matches: All proposed matches between schematic and PCB components.
        schematic_orphans: References in schematic with no PCB match.
        pcb_orphans: References in PCB with no schematic match.
        value_mismatches: Components matched by reference but with different values.
        footprint_mismatches: Components matched by reference but with different footprints.
        add_footprint_actions: Proposed add_footprint actions for schematic orphans
            that have a footprint assigned and could be placed on the PCB.
    """

    matches: list[SyncMatch] = field(default_factory=list)
    schematic_orphans: list[str] = field(default_factory=list)
    pcb_orphans: list[str] = field(default_factory=list)
    value_mismatches: list[dict[str, str]] = field(default_factory=list)
    footprint_mismatches: list[dict[str, str]] = field(default_factory=list)
    add_footprint_actions: list[dict[str, str]] = field(default_factory=list)

    @property
    def high_confidence_matches(self) -> list[SyncMatch]:
        """Matches with high confidence (safe to apply automatically)."""
        return [m for m in self.matches if m.confidence == "high"]

    @property
    def medium_confidence_matches(self) -> list[SyncMatch]:
        """Matches with medium confidence (likely correct, review recommended)."""
        return [m for m in self.matches if m.confidence == "medium"]

    @property
    def low_confidence_matches(self) -> list[SyncMatch]:
        """Matches with low confidence (ambiguous, manual review needed)."""
        return [m for m in self.matches if m.confidence == "low"]

    @property
    def has_actionable_items(self) -> bool:
        """True if there are any matches with actions to apply."""
        return any(m.actions for m in self.matches) or bool(
            self.value_mismatches or self.footprint_mismatches or self.add_footprint_actions
        )

    @property
    def is_in_sync(self) -> bool:
        """True if schematic and PCB are fully synchronized."""
        return (
            not self.schematic_orphans
            and not self.pcb_orphans
            and not self.value_mismatches
            and not self.footprint_mismatches
            and not any(m.actions for m in self.matches)
        )

    def summary(self) -> str:
        """Generate a human-readable summary of the analysis."""
        lines = []
        if self.is_in_sync:
            lines.append("Schematic and PCB are in sync. No changes needed.")
            return "\n".join(lines)

        lines.append("Sync Analysis Results:")
        lines.append(f"  Matched components: {len(self.matches)}")
        lines.append(f"    High confidence:  {len(self.high_confidence_matches)}")
        lines.append(f"    Medium confidence: {len(self.medium_confidence_matches)}")
        lines.append(f"    Low confidence:    {len(self.low_confidence_matches)}")

        if self.value_mismatches:
            lines.append(f"  Value mismatches:     {len(self.value_mismatches)}")
        if self.footprint_mismatches:
            lines.append(f"  Footprint mismatches: {len(self.footprint_mismatches)}")
        if self.schematic_orphans:
            lines.append(f"  Schematic-only:       {len(self.schematic_orphans)}")
        if self.pcb_orphans:
            lines.append(f"  PCB-only:             {len(self.pcb_orphans)}")
        if self.add_footprint_actions:
            lines.append(f"  Add footprint:        {len(self.add_footprint_actions)}")

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "is_in_sync": self.is_in_sync,
            "matches": [m.to_dict() for m in self.matches],
            "schematic_orphans": self.schematic_orphans,
            "pcb_orphans": self.pcb_orphans,
            "value_mismatches": self.value_mismatches,
            "footprint_mismatches": self.footprint_mismatches,
            "add_footprint_actions": self.add_footprint_actions,
            "summary": {
                "total_matches": len(self.matches),
                "high_confidence": len(self.high_confidence_matches),
                "medium_confidence": len(self.medium_confidence_matches),
                "low_confidence": len(self.low_confidence_matches),
                "add_footprint": len(self.add_footprint_actions),
            },
        }


class Reconciler:
    """Orchestrates schematic-to-PCB reconciliation.

    Wraps the consistency checker and PCB mutation primitives to provide
    a unified analyze-then-apply workflow.

    Args:
        project: Path to .kicad_pro file.
        schematic: Path to .kicad_sch file (optional, derived from project).
        pcb: Path to .kicad_pcb file (optional, derived from project).
    """

    def __init__(
        self,
        project: str | Path | None = None,
        schematic: str | Path | None = None,
        pcb: str | Path | None = None,
    ) -> None:
        """Initialize the reconciler.

        Must provide either a project path or both schematic and pcb paths.

        Raises:
            ValueError: If neither project nor both schematic+pcb are provided.
            FileNotFoundError: If specified files do not exist.
        """
        self._schematic_path: Path | None = None
        self._pcb_path: Path | None = None

        if project:
            project_path = Path(project)
            if not project_path.exists():
                raise FileNotFoundError(f"Project file not found: {project_path}")
            self._resolve_from_project(project_path)

        # Allow explicit overrides
        if schematic:
            self._schematic_path = Path(schematic)
        if pcb:
            self._pcb_path = Path(pcb)

        if not self._schematic_path or not self._pcb_path:
            raise ValueError(
                "Must provide either a project file or both --schematic and --pcb paths"
            )

        if not self._schematic_path.exists():
            raise FileNotFoundError(f"Schematic not found: {self._schematic_path}")
        if not self._pcb_path.exists():
            raise FileNotFoundError(f"PCB not found: {self._pcb_path}")

    def _resolve_from_project(self, project_path: Path) -> None:
        """Resolve schematic and PCB paths from a project file."""
        from kicad_tools.project import Project

        proj = Project.load(project_path)
        if proj._schematic_path:
            self._schematic_path = proj._schematic_path
        if proj._pcb_path:
            self._pcb_path = proj._pcb_path

    def analyze(self) -> SyncAnalysis:
        """Analyze mismatches between schematic and PCB.

        Uses the consistency checker to detect issues, then categorizes
        them into match types with confidence levels.

        Returns:
            SyncAnalysis with categorized results.
        """
        from kicad_tools.validate.consistency import SchematicPCBChecker

        checker = SchematicPCBChecker(self._schematic_path, self._pcb_path)
        result = checker.check()

        analysis = SyncAnalysis()

        # Build component lookup maps
        sch_components = self._get_schematic_components(checker)
        pcb_components = self._get_pcb_components(checker)

        sch_refs = set(sch_components.keys())
        pcb_refs = set(pcb_components.keys())

        # Exact matches (same reference in both)
        common_refs = sch_refs & pcb_refs
        for ref in sorted(common_refs):
            sch = sch_components[ref]
            pcb = pcb_components[ref]
            actions = []

            # Check value mismatch
            sch_value = sch.get("value", "")
            pcb_value = pcb.get("value", "")
            if sch_value and pcb_value and sch_value != pcb_value:
                actions.append(
                    {
                        "type": "update_value",
                        "reference": ref,
                        "old_value": pcb_value,
                        "new_value": sch_value,
                    }
                )
                analysis.value_mismatches.append(
                    {
                        "reference": ref,
                        "schematic_value": sch_value,
                        "pcb_value": pcb_value,
                    }
                )

            # Check footprint mismatch
            sch_fp = sch.get("footprint", "")
            pcb_fp = pcb.get("footprint", "")
            if sch_fp and pcb_fp:
                sch_fp_name = sch_fp.split(":")[-1] if ":" in sch_fp else sch_fp
                pcb_fp_name = pcb_fp.split(":")[-1] if ":" in pcb_fp else pcb_fp
                if sch_fp_name != pcb_fp_name:
                    actions.append(
                        {
                            "type": "update_footprint",
                            "reference": ref,
                            "old_value": pcb_fp,
                            "new_value": sch_fp,
                        }
                    )
                    analysis.footprint_mismatches.append(
                        {
                            "reference": ref,
                            "schematic_footprint": sch_fp,
                            "pcb_footprint": pcb_fp,
                        }
                    )

            analysis.matches.append(
                SyncMatch(
                    schematic_ref=ref,
                    pcb_ref=ref,
                    confidence="high",
                    match_type="exact",
                    actions=tuple(actions),
                )
            )

        # Orphans: components only in schematic
        analysis.schematic_orphans = sorted(sch_refs - pcb_refs)

        # Orphans: components only in PCB
        analysis.pcb_orphans = sorted(pcb_refs - sch_refs)

        # Try to match orphans by value+footprint (medium confidence)
        unmatched_sch = list(analysis.schematic_orphans)
        unmatched_pcb = list(analysis.pcb_orphans)
        matched_sch: set[str] = set()
        matched_pcb: set[str] = set()

        for sch_ref in unmatched_sch:
            sch = sch_components[sch_ref]
            sch_value = sch.get("value", "")
            sch_fp = sch.get("footprint", "")
            if not sch_value or not sch_fp:
                continue

            sch_fp_name = sch_fp.split(":")[-1] if ":" in sch_fp else sch_fp

            # Find PCB candidates with same value+footprint
            candidates = []
            for pcb_ref in unmatched_pcb:
                if pcb_ref in matched_pcb:
                    continue
                pcb = pcb_components[pcb_ref]
                pcb_value = pcb.get("value", "")
                pcb_fp = pcb.get("footprint", "")
                pcb_fp_name = pcb_fp.split(":")[-1] if ":" in pcb_fp else pcb_fp

                if sch_value == pcb_value and sch_fp_name == pcb_fp_name:
                    candidates.append(pcb_ref)

            if len(candidates) == 1:
                # Unique match by value+footprint -> medium confidence
                pcb_ref = candidates[0]
                actions = (
                    {
                        "type": "rename",
                        "reference": pcb_ref,
                        "old_value": pcb_ref,
                        "new_value": sch_ref,
                    },
                )
                analysis.matches.append(
                    SyncMatch(
                        schematic_ref=sch_ref,
                        pcb_ref=pcb_ref,
                        confidence="medium",
                        match_type="value_footprint",
                        actions=actions,
                    )
                )
                matched_sch.add(sch_ref)
                matched_pcb.add(pcb_ref)

        # Try footprint-only matching for remaining orphans (low confidence)
        for sch_ref in unmatched_sch:
            if sch_ref in matched_sch:
                continue
            sch = sch_components[sch_ref]
            sch_fp = sch.get("footprint", "")
            if not sch_fp:
                continue

            sch_fp_name = sch_fp.split(":")[-1] if ":" in sch_fp else sch_fp
            candidates = []
            for pcb_ref in unmatched_pcb:
                if pcb_ref in matched_pcb:
                    continue
                pcb = pcb_components[pcb_ref]
                pcb_fp = pcb.get("footprint", "")
                pcb_fp_name = pcb_fp.split(":")[-1] if ":" in pcb_fp else pcb_fp

                if sch_fp_name == pcb_fp_name:
                    candidates.append(pcb_ref)

            if len(candidates) == 1:
                pcb_ref = candidates[0]
                actions_list = [
                    {
                        "type": "rename",
                        "reference": pcb_ref,
                        "old_value": pcb_ref,
                        "new_value": sch_ref,
                    },
                ]
                # Also update value if different
                pcb = pcb_components[pcb_ref]
                sch_value = sch.get("value", "")
                pcb_value = pcb.get("value", "")
                if sch_value and pcb_value and sch_value != pcb_value:
                    actions_list.append(
                        {
                            "type": "update_value",
                            "reference": sch_ref,
                            "old_value": pcb_value,
                            "new_value": sch_value,
                        }
                    )

                analysis.matches.append(
                    SyncMatch(
                        schematic_ref=sch_ref,
                        pcb_ref=pcb_ref,
                        confidence="low",
                        match_type="footprint_only",
                        actions=tuple(actions_list),
                    )
                )
                matched_sch.add(sch_ref)
                matched_pcb.add(pcb_ref)

        # Update orphan lists to exclude matched items
        analysis.schematic_orphans = sorted(set(analysis.schematic_orphans) - matched_sch)
        analysis.pcb_orphans = sorted(set(analysis.pcb_orphans) - matched_pcb)

        # Generate add_footprint actions for remaining schematic orphans
        for ref in analysis.schematic_orphans:
            sch = sch_components[ref]
            footprint = sch.get("footprint", "")
            value = sch.get("value", "")
            lib_id = sch.get("lib_id", "")
            if footprint:
                action = {
                    "type": "add_footprint",
                    "reference": ref,
                    "value": value,
                    "footprint": footprint,
                    "lib_id": lib_id,
                }
                analysis.add_footprint_actions.append(action)

        return analysis

    def _get_schematic_components(self, checker) -> dict[str, dict[str, str]]:
        """Extract component info from schematic using hierarchical BOM extraction.

        Uses extract_bom() with hierarchical=True to traverse all sub-sheets,
        mirroring the approach in SchematicPCBChecker.check_lvs(). This ensures
        components in hierarchical sub-sheets are included in the analysis.

        Skips virtual components, power symbols, and DNP components, consistent
        with the LVS checker's filtering logic.
        """
        components: dict[str, dict[str, str]] = {}
        sch_path = str(self._schematic_path)
        bom = extract_bom(sch_path, hierarchical=True)

        for item in bom.items:
            if item.is_virtual or item.is_power_symbol:
                continue
            if item.dnp:
                continue
            if item.reference and not item.reference.startswith("#"):
                components[item.reference] = {
                    "value": item.value or "",
                    "footprint": item.footprint or "",
                    "lib_id": item.lib_id or "",
                }
        return components

    def _get_pcb_components(self, checker) -> dict[str, dict[str, str]]:
        """Extract component info from PCB via the checker."""
        components: dict[str, dict[str, str]] = {}
        for fp in checker.pcb.footprints:
            if fp.reference and not fp.reference.startswith("#"):
                components[fp.reference] = {
                    "value": fp.value or "",
                    "footprint": fp.name or "",
                }
        return components

    def apply(
        self,
        analysis: SyncAnalysis,
        dry_run: bool = True,
        min_confidence: str = "high",
        output: str | Path | None = None,
        remove_orphans: bool = False,
        force: bool = False,
    ) -> list[SyncChange]:
        """Apply proposed changes from an analysis to the PCB.

        Args:
            analysis: The SyncAnalysis to apply.
            dry_run: If True, report what would change without modifying files.
            min_confidence: Minimum confidence level to apply.
                "high" = only high confidence, "medium" = high+medium,
                "low" = all matches.
            output: Path to write the modified PCB (default: overwrite original).
            remove_orphans: If True, remove PCB footprints not present in schematic.
            force: If True, remove orphans even when they have routed traces.

        Returns:
            List of SyncChange records documenting what was (or would be) changed.
        """
        from kicad_tools.schema.pcb import PCB

        confidence_order = {"high": 0, "medium": 1, "low": 2}
        min_level = confidence_order.get(min_confidence, 0)

        changes: list[SyncChange] = []

        # Collect all actions from matches meeting confidence threshold
        actions_to_apply = []
        for match in analysis.matches:
            match_level = confidence_order.get(match.confidence, 2)
            if match_level <= min_level and match.actions:
                actions_to_apply.extend(match.actions)

        # Include add_footprint actions from analysis
        actions_to_apply.extend(analysis.add_footprint_actions)

        has_orphans_to_remove = remove_orphans and bool(analysis.pcb_orphans)

        if not actions_to_apply and not has_orphans_to_remove:
            return changes

        # Load PCB as a PCB object for full API access (add_footprint, add_net, etc.)
        pcb = PCB.load(str(self._pcb_path))

        # Compute grid fallback position for new footprints (below board outline)
        placement_x, placement_y, placement_col = self._compute_placement_start(pcb)
        placement_start_x = placement_x
        placement_spacing = 15.0
        placement_columns = 10

        # Compute smart placement positions for new footprints based on net adjacency
        add_refs = [a["reference"] for a in actions_to_apply if a["type"] == "add_footprint"]
        smart_positions = self._compute_smart_placement(pcb, add_refs) if add_refs else {}

        has_add_footprint = any(a["type"] == "add_footprint" for a in actions_to_apply)
        has_update_footprint = any(a["type"] == "update_footprint" for a in actions_to_apply)

        for action in actions_to_apply:
            if action["type"] == "add_footprint":
                ref = action["reference"]
                if ref in smart_positions:
                    pos_x, pos_y = smart_positions[ref]
                else:
                    pos_x, pos_y = placement_x, placement_y
                    # Advance grid position for next grid-placed footprint
                    placement_col += 1
                    if placement_col >= placement_columns:
                        placement_col = 0
                        placement_x = placement_start_x
                        placement_y += placement_spacing
                    else:
                        placement_x += placement_spacing

                change = self._apply_add_footprint(
                    pcb,
                    action,
                    dry_run,
                    pos_x,
                    pos_y,
                )
                if change:
                    changes.append(change)
            elif action["type"] == "update_footprint":
                change = self._apply_update_footprint(pcb, action, dry_run)
                if change:
                    changes.append(change)
            elif action["type"] == "update_value":
                change = self._apply_update_value(pcb, action, dry_run)
                if change:
                    changes.append(change)
            elif action["type"] == "rename":
                change = self._apply_rename(pcb, action, dry_run)
                if change:
                    changes.append(change)

        # Remove PCB orphans if requested
        if has_orphans_to_remove:
            for ref in analysis.pcb_orphans:
                change = self._apply_remove_orphan(pcb, ref, dry_run, force)
                if change:
                    changes.append(change)

        # Assign nets from schematic netlist after all footprints are added/swapped
        needs_net_assignment = (
            has_add_footprint
            and any(c.applied and c.change_type == "add_footprint" for c in changes)
        ) or (
            has_update_footprint
            and any(c.applied and c.change_type == "update_footprint" for c in changes)
        )
        if not dry_run and needs_net_assignment:
            self._assign_nets(pcb)

        # Save if not dry-run and there were changes
        if not dry_run and any(c.applied for c in changes):
            output_path = str(output) if output else str(self._pcb_path)

            # Create backup before modifying
            if not output and self._pcb_path:
                backup_path = self._pcb_path.with_suffix(".kicad_pcb.bak")
                shutil.copy2(self._pcb_path, backup_path)

            pcb.save(output_path)

        return changes

    def _compute_placement_start(self, pcb) -> tuple[float, float, int]:
        """Compute starting position for placing new footprints.

        Places footprints below the board outline with a margin. If no board
        outline is detected, starts at a reasonable default position.

        Returns:
            Tuple of (x, y, col) where col is the starting column index (0).
        """
        outline = pcb.get_board_outline()
        if outline:
            # Place below the board outline with 10mm margin
            max_y = max(pt[1] for pt in outline)
            min_x = min(pt[0] for pt in outline)
            # get_board_outline() already returns board-relative coordinates
            start_x = min_x
            start_y = max_y + 10.0
        else:
            start_x = 10.0
            start_y = 10.0
        return start_x, start_y, 0

    def _build_net_adjacency(
        self,
        new_refs: set[str],
    ) -> dict[str, set[str]]:
        """Build a map from each new component to its net-neighbor references.

        A net-neighbor is any component that shares at least one net with the
        given component. Only neighbors that are NOT in new_refs (i.e., already
        placed on the PCB) are included.

        Args:
            new_refs: Set of reference designators for new (unplaced) components.

        Returns:
            Dict mapping each new ref to a set of existing (placed) neighbor refs.
        """
        try:
            from kicad_tools.operations.netlist import export_netlist

            netlist = export_netlist(str(self._schematic_path))
        except Exception:
            return {}

        # For each net, collect all component references connected to it
        adjacency: dict[str, set[str]] = defaultdict(set)
        for net in netlist.nets:
            if not net.name:
                continue
            refs_in_net = {node.reference for node in net.nodes if node.reference}
            for ref in refs_in_net:
                if ref in new_refs:
                    # Add all non-new refs from this net as neighbors
                    placed_neighbors = refs_in_net - new_refs - {ref}
                    adjacency[ref].update(placed_neighbors)

        return dict(adjacency)

    def _compute_smart_placement(
        self,
        pcb,
        new_refs: list[str],
    ) -> dict[str, tuple[float, float]]:
        """Compute placement positions for new components near their net neighbors.

        For each new component, finds existing components that share nets with it,
        computes the centroid of those neighbors' positions, and offsets the new
        component to avoid overlap using a spiral search.

        Components with no placed net-neighbors are omitted from the result;
        the caller should fall back to grid placement for those.

        Args:
            pcb: The loaded PCB object.
            new_refs: List of reference designators for components to place.

        Returns:
            Dict mapping ref to (x, y) board-relative placement position.
            Only contains entries for components that have placed net-neighbors.
        """
        new_ref_set = set(new_refs)
        adjacency = self._build_net_adjacency(new_ref_set)

        if not adjacency:
            return {}

        # Build position lookup for existing footprints (sheet-absolute coords)
        fp_positions: dict[str, tuple[float, float]] = {}
        for fp in pcb.footprints:
            if fp.reference and not fp.reference.startswith("#"):
                fp_positions[fp.reference] = fp.position

        origin_x, origin_y = pcb.board_origin

        # Track all occupied positions (existing + newly assigned) for overlap avoidance
        occupied: list[tuple[float, float]] = []
        for pos in fp_positions.values():
            # Convert to board-relative
            occupied.append((pos[0] - origin_x, pos[1] - origin_y))

        result: dict[str, tuple[float, float]] = {}
        min_spacing = 5.0  # minimum distance between footprint centers (mm)

        for ref in new_refs:
            neighbors = adjacency.get(ref, set())
            if not neighbors:
                continue

            # Compute centroid of neighbor positions (in board-relative coords)
            neighbor_positions = []
            for n_ref in neighbors:
                if n_ref in fp_positions:
                    abs_pos = fp_positions[n_ref]
                    neighbor_positions.append(
                        (abs_pos[0] - origin_x, abs_pos[1] - origin_y)
                    )

            if not neighbor_positions:
                continue

            cx = sum(p[0] for p in neighbor_positions) / len(neighbor_positions)
            cy = sum(p[1] for p in neighbor_positions) / len(neighbor_positions)

            # Find a non-overlapping position near the centroid using spiral search
            x, y = self._find_non_overlapping_position(cx, cy, occupied, min_spacing)
            result[ref] = (x, y)
            occupied.append((x, y))

        return result

    @staticmethod
    def _find_non_overlapping_position(
        cx: float,
        cy: float,
        occupied: list[tuple[float, float]],
        min_spacing: float,
    ) -> tuple[float, float]:
        """Find the nearest non-overlapping position to (cx, cy).

        Uses a spiral search pattern, checking concentric rings of positions
        at increasing distance from the target centroid.

        Args:
            cx: Target X position (board-relative).
            cy: Target Y position (board-relative).
            occupied: List of already-occupied (x, y) positions.
            min_spacing: Minimum distance between footprint centers.

        Returns:
            (x, y) position that does not overlap with any occupied position.
        """

        def _is_clear(x: float, y: float) -> bool:
            for ox, oy in occupied:
                if math.sqrt((x - ox) ** 2 + (y - oy) ** 2) < min_spacing:
                    return False
            return True

        # Try the centroid itself first
        if _is_clear(cx, cy):
            return cx, cy

        # Spiral outward in concentric rings
        step = min_spacing
        for ring in range(1, 20):
            radius = ring * step
            # Check 8 * ring points around the ring for good coverage
            num_points = 8 * ring
            for i in range(num_points):
                angle = 2.0 * math.pi * i / num_points
                x = cx + radius * math.cos(angle)
                y = cy + radius * math.sin(angle)
                if _is_clear(x, y):
                    return x, y

        # Fallback: offset far enough that overlap is impossible
        return cx + 20 * min_spacing, cy

    def _apply_add_footprint(
        self,
        pcb,
        action: dict[str, Any],
        dry_run: bool,
        x: float,
        y: float,
    ) -> SyncChange | None:
        """Apply an add_footprint action to the PCB.

        Args:
            pcb: The PCB object.
            action: Action dict with footprint details.
            dry_run: If True, don't modify the PCB.
            x: X position for placement (board-relative).
            y: Y position for placement (board-relative).

        Returns:
            SyncChange record, or None if the action failed.
        """
        ref = action["reference"]
        footprint = action.get("footprint", "")
        value = action.get("value", "")

        if dry_run:
            return SyncChange(
                reference=ref,
                change_type="add_footprint",
                old_value="",
                new_value=f"{footprint} ({value})",
                applied=False,
            )

        try:
            pcb.add_footprint(
                library_id=footprint,
                reference=ref,
                x=x,
                y=y,
                rotation=0.0,
                layer="F.Cu",
                value=value,
            )
            return SyncChange(
                reference=ref,
                change_type="add_footprint",
                old_value="",
                new_value=f"{footprint} ({value})",
                applied=True,
            )
        except (FileNotFoundError, ValueError) as e:
            # Library not found or footprint not resolvable -- record but continue
            return SyncChange(
                reference=ref,
                change_type="add_footprint",
                old_value="",
                new_value=f"{footprint} ({value}) [error: {e}]",
                applied=False,
            )

    def _apply_update_footprint(
        self,
        pcb,
        action: dict[str, Any],
        dry_run: bool,
    ) -> SyncChange | None:
        """Apply an update_footprint action: swap an existing footprint for a new one.

        Removes the old footprint, loads the new one at the same position/rotation/layer,
        and re-assigns nets from the old pads to the new pads by pad number. Traces
        connected to old pad positions are removed since pad geometry has changed.

        Args:
            pcb: The PCB object.
            action: Action dict with update_footprint details.
            dry_run: If True, don't modify the PCB.

        Returns:
            SyncChange record, or None if the action failed.
        """
        ref = action["reference"]
        old_fp_name = action["old_value"]
        new_fp_name = action["new_value"]

        if dry_run:
            return SyncChange(
                reference=ref,
                change_type="update_footprint",
                old_value=old_fp_name,
                new_value=new_fp_name,
                applied=False,
            )

        # Step 1: Capture state from the old footprint
        old_fp = pcb.get_footprint(ref)
        if not old_fp:
            return None

        old_position = old_fp.position
        old_rotation = old_fp.rotation
        old_layer = old_fp.layer

        # Capture pad-to-net mapping: {pad_number: (net_number, net_name)}
        pad_net_map: dict[str, tuple[int, str]] = {}
        for pad in old_fp.pads:
            if pad.net_number != 0 or pad.net_name:
                pad_net_map[pad.number] = (pad.net_number, pad.net_name)

        # Step 2: Find trace segments connected to old pad positions
        old_pad_positions: list[tuple[float, float]] = []
        connected_segments = []
        affected_net_names: set[str] = set()

        for pad in old_fp.pads:
            pos = pcb.get_pad_position(ref, pad.number)
            if pos:
                old_pad_positions.append(pos)
                # Find segments in this pad's net that touch this pad position
                if pad.net_number != 0:
                    for seg in pcb.segments_in_net(pad.net_number):
                        tolerance = 0.01  # mm
                        start_dist = math.sqrt(
                            (seg.start[0] - pos[0]) ** 2 + (seg.start[1] - pos[1]) ** 2
                        )
                        end_dist = math.sqrt(
                            (seg.end[0] - pos[0]) ** 2 + (seg.end[1] - pos[1]) ** 2
                        )
                        if start_dist < tolerance or end_dist < tolerance:
                            connected_segments.append(seg)
                            if pad.net_name:
                                affected_net_names.add(pad.net_name)

        # De-duplicate segments by UUID
        seen_uuids: set[str] = set()
        unique_segments = []
        for seg in connected_segments:
            key = seg.uuid if seg.uuid else id(seg)
            if key not in seen_uuids:
                seen_uuids.add(key)
                unique_segments.append(seg)
        connected_segments = unique_segments

        # Step 3: Add the new footprint BEFORE removing the old one.
        # This ensures we never lose the old footprint if add_footprint fails.
        # Use a temporary reference to avoid duplicate references in _footprints,
        # which would cause remove_footprint(ref) to delete BOTH old and new.
        temp_ref = f"__SWAP_TEMP__{ref}"
        try:
            new_fp = pcb.add_footprint(
                library_id=new_fp_name,
                reference=temp_ref,
                x=old_position[0],
                y=old_position[1],
                rotation=old_rotation,
                layer=old_layer,
                value=old_fp.value or "",
            )
        except (FileNotFoundError, ValueError) as e:
            return SyncChange(
                reference=ref,
                change_type="update_footprint",
                old_value=old_fp_name,
                new_value=f"{new_fp_name} [error: {e}]",
                applied=False,
            )

        # Step 4: Remove the old footprint only after the new one was added successfully.
        # The old footprint has ref; the new one has temp_ref, so only the old is removed.
        pcb.remove_footprint(ref)

        # Rename the new footprint from the temporary reference to the real one
        pcb.update_footprint_reference(temp_ref, ref)

        # Step 5: Re-assign nets from old pads to new pads by pad number
        new_pad_count = len(new_fp.pads)
        old_pad_count = len(pad_net_map)

        if old_pad_count > 0 and new_pad_count > 0:
            # Check if we can map by pad number (same-family swap)
            new_pad_numbers = {p.number for p in new_fp.pads}
            mappable = all(pn in new_pad_numbers for pn in pad_net_map)

            if mappable:
                # Same-family swap: map old pad nets to new pads by number
                for pad_num, (net_num, net_name) in pad_net_map.items():
                    if net_name:
                        pcb.assign_net_to_footprint_pad(ref, pad_num, net_name)
            # If pad numbers don't match, we rely on netlist assignment
            # which is called after all footprint operations complete

        # Step 6: Remove invalidated trace segments
        if connected_segments:
            pcb.remove_segments(connected_segments)

        # Build informative new_value with affected nets info
        new_value = new_fp_name
        if affected_net_names:
            nets_str = ", ".join(sorted(affected_net_names))
            new_value = f"{new_fp_name} [re-route: {nets_str}]"

        return SyncChange(
            reference=ref,
            change_type="update_footprint",
            old_value=old_fp_name,
            new_value=new_value,
            applied=True,
        )

    def _assign_nets(self, pcb) -> None:
        """Export netlist from schematic and assign nets to PCB pads.

        This wires up pad-to-net mappings for both existing and newly added
        footprints based on schematic connectivity.
        """
        try:
            from kicad_tools.operations.netlist import export_netlist

            netlist = export_netlist(str(self._schematic_path))

            # Add all nets from the netlist to the PCB
            for net in netlist.nets:
                if net.name:
                    pcb.add_net(net.name)

            # Assign nets to pads
            pcb.assign_nets_from_netlist(netlist)
        except Exception:
            # Net assignment is best-effort; failures are not fatal.
            # The user can run assign_nets_from_netlist() separately.
            pass

    def _apply_update_value(
        self,
        pcb,
        action: dict[str, Any],
        dry_run: bool,
    ) -> SyncChange | None:
        """Apply an update_value action using the PCB API.

        Uses pcb.update_footprint_value() which handles both KiCad 7 (fp_text)
        and KiCad 8+ (property) formats.

        Args:
            pcb: The PCB object.
            action: Action dict with update_value details.
            dry_run: If True, don't modify the PCB.

        Returns:
            SyncChange record, or None if the footprint was not found.
        """
        ref = action["reference"]
        old_val = action["old_value"]
        new_val = action["new_value"]

        if dry_run:
            return SyncChange(
                reference=ref,
                change_type="update_value",
                old_value=old_val,
                new_value=new_val,
                applied=False,
            )

        success = pcb.update_footprint_value(ref, new_val)
        if not success:
            return None

        return SyncChange(
            reference=ref,
            change_type="update_value",
            old_value=old_val,
            new_value=new_val,
            applied=True,
        )

    def _apply_rename(
        self,
        pcb,
        action: dict[str, Any],
        dry_run: bool,
    ) -> SyncChange | None:
        """Apply a rename action using the PCB API.

        Uses pcb.update_footprint_reference() which handles both KiCad 7 (fp_text)
        and KiCad 8+ (property) formats.

        Args:
            pcb: The PCB object.
            action: Action dict with rename details.
            dry_run: If True, don't modify the PCB.

        Returns:
            SyncChange record, or None if the footprint was not found.
        """
        old_ref = action["old_value"]
        new_ref = action["new_value"]

        if dry_run:
            return SyncChange(
                reference=old_ref,
                change_type="rename",
                old_value=old_ref,
                new_value=new_ref,
                applied=False,
            )

        success = pcb.update_footprint_reference(old_ref, new_ref)
        if not success:
            return None

        return SyncChange(
            reference=old_ref,
            change_type="rename",
            old_value=old_ref,
            new_value=new_ref,
            applied=True,
        )

    def _apply_remove_orphan(
        self,
        pcb,
        ref: str,
        dry_run: bool,
        force: bool,
    ) -> SyncChange:
        """Remove an orphan footprint from the PCB.

        Checks for routed traces before removal. If the footprint has traces
        and force is False, the removal is skipped.

        Args:
            pcb: The PCB object.
            ref: Reference designator of the orphan footprint.
            dry_run: If True, don't modify the PCB.
            force: If True, remove even if the footprint has routed traces.

        Returns:
            SyncChange record documenting the removal (or skip).
        """
        has_traces = pcb.footprint_has_traces(ref)

        if dry_run:
            new_value = "removed"
            if has_traces and not force:
                new_value = "skipped (has traces, use --force)"
            elif has_traces and force:
                new_value = "removed (forced, had traces)"
            return SyncChange(
                reference=ref,
                change_type="remove_orphan",
                old_value=ref,
                new_value=new_value,
                applied=False,
            )

        if has_traces and not force:
            return SyncChange(
                reference=ref,
                change_type="remove_orphan",
                old_value=ref,
                new_value="skipped (has traces, use --force)",
                applied=False,
            )

        success = pcb.remove_footprint(ref)
        new_value = "removed"
        if has_traces:
            new_value = "removed (forced, had traces)"

        return SyncChange(
            reference=ref,
            change_type="remove_orphan",
            old_value=ref,
            new_value=new_value,
            applied=success,
        )

    def _apply_action(self, sexp, action: dict[str, Any], dry_run: bool) -> SyncChange | None:
        """Apply a single action to the PCB s-expression.

        Args:
            sexp: The PCB s-expression tree.
            action: Action dict with "type" and relevant fields.
            dry_run: If True, don't actually modify the s-expression.

        Returns:
            SyncChange record, or None if the action failed.
        """
        from kicad_tools.cli.pcb_modify import find_footprint_sexp

        action_type = action["type"]

        if action_type == "rename":
            old_ref = action["old_value"]
            new_ref = action["new_value"]

            fp = find_footprint_sexp(sexp, old_ref)
            if not fp:
                return None

            if not dry_run:
                for fp_text in fp.find_children("fp_text"):
                    if fp_text.get_string(0) == "reference":
                        fp_text.set_value(1, new_ref)
                        break

            return SyncChange(
                reference=old_ref,
                change_type="rename",
                old_value=old_ref,
                new_value=new_ref,
                applied=not dry_run,
            )

        elif action_type == "update_value":
            ref = action["reference"]
            old_val = action["old_value"]
            new_val = action["new_value"]

            fp = find_footprint_sexp(sexp, ref)
            if not fp:
                return None

            if not dry_run:
                for fp_text in fp.find_children("fp_text"):
                    if fp_text.get_string(0) == "value":
                        fp_text.set_value(1, new_val)
                        break

            return SyncChange(
                reference=ref,
                change_type="update_value",
                old_value=old_val,
                new_value=new_val,
                applied=not dry_run,
            )

        elif action_type == "update_footprint":
            # update_footprint is handled by _apply_update_footprint() with
            # full PCB object access. If we reach here, something is wrong.
            return None

        return None

    def save_mapping(self, analysis: SyncAnalysis, output: str | Path) -> None:
        """Save the analysis as a JSON mapping file.

        Args:
            analysis: The analysis to save.
            output: Path to write the JSON file.
        """
        data = analysis.to_dict()
        data["schematic"] = str(self._schematic_path)
        data["pcb"] = str(self._pcb_path)

        with open(output, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

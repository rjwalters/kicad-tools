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
import shutil
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
    change_type: str  # "rename", "update_value", "update_footprint", "add_footprint"
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
    ) -> list[SyncChange]:
        """Apply proposed changes from an analysis to the PCB.

        Args:
            analysis: The SyncAnalysis to apply.
            dry_run: If True, report what would change without modifying files.
            min_confidence: Minimum confidence level to apply.
                "high" = only high confidence, "medium" = high+medium,
                "low" = all matches.
            output: Path to write the modified PCB (default: overwrite original).

        Returns:
            List of SyncChange records documenting what was (or would be) changed.
        """
        from kicad_tools.core.sexp_file import load_pcb, save_pcb

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

        if not actions_to_apply:
            return changes

        # Load PCB s-expression for modification
        sexp = load_pcb(str(self._pcb_path))

        for action in actions_to_apply:
            change = self._apply_action(sexp, action, dry_run)
            if change:
                changes.append(change)

        # Save if not dry-run and there were changes
        if not dry_run and changes:
            output_path = str(output) if output else str(self._pcb_path)

            # Create backup before modifying
            if not output and self._pcb_path:
                backup_path = self._pcb_path.with_suffix(".kicad_pcb.bak")
                shutil.copy2(self._pcb_path, backup_path)

            save_pcb(sexp, output_path)

        return changes

    def _apply_action(
        self, sexp, action: dict[str, Any], dry_run: bool
    ) -> SyncChange | None:
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
            ref = action["reference"]
            old_fp = action["old_value"]
            new_fp = action["new_value"]

            # Footprint updates are recorded but not applied automatically
            # because changing the footprint invalidates pad layout and routing.
            # This is logged as a warning for manual resolution.
            return SyncChange(
                reference=ref,
                change_type="update_footprint",
                old_value=old_fp,
                new_value=new_fp,
                applied=False,  # Never auto-applied
            )

        elif action_type == "add_footprint":
            ref = action["reference"]
            footprint = action.get("footprint", "")
            value = action.get("value", "")

            # add_footprint actions are recorded but not applied automatically
            # because placing a new footprint requires KiCad standard libraries
            # and net assignment from the schematic netlist. Use PCB.add_footprint()
            # directly for programmatic placement.
            return SyncChange(
                reference=ref,
                change_type="add_footprint",
                old_value="",
                new_value=f"{footprint} ({value})",
                applied=False,  # Never auto-applied
            )

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

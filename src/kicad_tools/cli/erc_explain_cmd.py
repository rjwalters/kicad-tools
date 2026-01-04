"""
ERC explain command - detailed root cause analysis and fix suggestions.

Provides in-depth analysis of ERC violations with:
- Root cause analysis showing what was checked and found
- Actual vs expected values
- Fuzzy matching for similar label names
- Actionable fix suggestions
- Cross-referenced related errors

Usage:
    kct erc explain design.kicad_sch              # Analyze schematic
    kct erc explain design-erc.json               # Analyze existing report
    kct erc explain design.kicad_sch --format json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from ..erc import (
    ERCReport,
    ERCViolation,
    ERCViolationType,
)
from .runner import find_kicad_cli, run_erc


@dataclass
class DiagnosisItem:
    """A single diagnostic check result."""

    check: str
    expected: str | None = None
    actual: str | None = None
    status: str = "info"  # "ok", "warning", "error", "info"


@dataclass
class SimilarLabel:
    """A similar label that might be a typo."""

    name: str
    similarity: float
    location: str = ""
    direction: str = ""


@dataclass
class FixSuggestion:
    """An actionable fix suggestion."""

    description: str
    command: str | None = None  # CLI command if applicable
    priority: int = 1  # Lower is higher priority


@dataclass
class ViolationExplanation:
    """Detailed explanation of an ERC violation."""

    violation: ERCViolation
    summary: str
    diagnosis: list[DiagnosisItem] = field(default_factory=list)
    possible_causes: list[str] = field(default_factory=list)
    similar_labels: list[SimilarLabel] = field(default_factory=list)
    fixes: list[FixSuggestion] = field(default_factory=list)
    related_violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON output."""
        return {
            "type": self.violation.type_str,
            "type_description": self.violation.type_description,
            "severity": self.violation.severity.value,
            "description": self.violation.description,
            "location": {
                "sheet": self.violation.sheet or "/",
                "x": self.violation.pos_x,
                "y": self.violation.pos_y,
            },
            "items": self.violation.items,
            "summary": self.summary,
            "diagnosis": [
                {
                    "check": d.check,
                    "expected": d.expected,
                    "actual": d.actual,
                    "status": d.status,
                }
                for d in self.diagnosis
            ],
            "possible_causes": self.possible_causes,
            "similar_labels": [
                {
                    "name": s.name,
                    "similarity": round(s.similarity, 2),
                    "location": s.location,
                    "direction": s.direction,
                }
                for s in self.similar_labels
            ],
            "fixes": [
                {
                    "description": f.description,
                    "command": f.command,
                    "priority": f.priority,
                }
                for f in self.fixes
            ],
            "related_violations": self.related_violations,
        }


class ERCExplainer:
    """Generates detailed explanations for ERC violations."""

    def __init__(
        self,
        schematic_path: Path | None = None,
        all_labels: list[str] | None = None,
    ):
        """Initialize the explainer.

        Args:
            schematic_path: Path to the schematic (for gathering context).
            all_labels: List of all labels in the schematic for fuzzy matching.
        """
        self.schematic_path = schematic_path
        self.all_labels = all_labels or []
        self._hierarchy_info: dict | None = None

    def explain(self, violation: ERCViolation) -> ViolationExplanation:
        """Generate a detailed explanation for a violation."""
        handlers = {
            ERCViolationType.HIER_LABEL_MISMATCH: self._explain_hier_label_mismatch,
            ERCViolationType.PIN_NOT_CONNECTED: self._explain_pin_not_connected,
            ERCViolationType.PIN_NOT_DRIVEN: self._explain_pin_not_driven,
            ERCViolationType.POWER_PIN_NOT_DRIVEN: self._explain_power_not_driven,
            ERCViolationType.LABEL_DANGLING: self._explain_label_dangling,
            ERCViolationType.GLOBAL_LABEL_DANGLING: self._explain_global_label_dangling,
            ERCViolationType.SIMILAR_LABELS: self._explain_similar_labels,
            ERCViolationType.DUPLICATE_REFERENCE: self._explain_duplicate_reference,
            ERCViolationType.WIRE_DANGLING: self._explain_wire_dangling,
            ERCViolationType.NO_CONNECT_CONNECTED: self._explain_nc_connected,
            ERCViolationType.NO_CONNECT_DANGLING: self._explain_nc_dangling,
            ERCViolationType.DIFFERENT_UNIT_NET: self._explain_different_unit_net,
            ERCViolationType.MISSING_UNIT: self._explain_missing_unit,
            ERCViolationType.UNANNOTATED: self._explain_unannotated,
            ERCViolationType.MULTIPLE_NET_NAMES: self._explain_multiple_net_names,
            ERCViolationType.ENDPOINT_OFF_GRID: self._explain_off_grid,
        }

        handler = handlers.get(violation.type, self._explain_generic)
        return handler(violation)

    def _find_similar_labels(self, target: str, threshold: float = 0.6) -> list[SimilarLabel]:
        """Find labels similar to the target using fuzzy matching."""
        similar = []
        target_lower = target.lower()

        for label in self.all_labels:
            if label == target:
                continue

            # Calculate similarity ratio
            ratio = SequenceMatcher(None, target_lower, label.lower()).ratio()

            if ratio >= threshold:
                similar.append(SimilarLabel(name=label, similarity=ratio))

        # Sort by similarity (highest first)
        similar.sort(key=lambda x: x.similarity, reverse=True)
        return similar[:5]  # Return top 5 matches

    def _extract_label_name(self, violation: ERCViolation) -> str | None:
        """Extract the label name from violation items or description."""
        # Try to extract from items
        for item in violation.items:
            item_lower = item.lower()
            if "label" in item_lower:
                # Pattern: "Hierarchical label 'NAME'" or "Label 'NAME'"
                if "'" in item:
                    start = item.find("'") + 1
                    end = item.find("'", start)
                    if end > start:
                        return item[start:end]
                # Pattern: "Label NAME on ..."
                parts = item.split()
                for i, part in enumerate(parts):
                    if part.lower() == "label" and i + 1 < len(parts):
                        return parts[i + 1].strip("'\"")

        # Try to extract from description
        if "'" in violation.description:
            start = violation.description.find("'") + 1
            end = violation.description.find("'", start)
            if end > start:
                return violation.description[start:end]

        return None

    def _extract_pin_info(self, violation: ERCViolation) -> dict | None:
        """Extract pin information from violation items."""
        for item in violation.items:
            item_lower = item.lower()
            if "pin" in item_lower:
                info = {"raw": item}
                # Pattern: "Pin NAME (TYPE) of COMPONENT"
                if " of " in item:
                    parts = item.split(" of ")
                    if len(parts) >= 2:
                        info["component"] = parts[-1].strip()
                    pin_part = parts[0]
                    if "(" in pin_part and ")" in pin_part:
                        start = pin_part.find("(") + 1
                        end = pin_part.find(")")
                        info["type"] = pin_part[start:end]
                    # Extract pin name
                    pin_name = pin_part.replace("Pin ", "").strip()
                    if "(" in pin_name:
                        pin_name = pin_name[: pin_name.find("(")].strip()
                    info["name"] = pin_name
                return info
        return None

    def _explain_hier_label_mismatch(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain hierarchical label mismatch errors."""
        label_name = self._extract_label_name(violation)

        explanation = ViolationExplanation(
            violation=violation,
            summary="Sheet pin has no matching hierarchical label in sub-schematic",
        )

        # Diagnosis
        explanation.diagnosis = [
            DiagnosisItem(
                check="Sheet pin exists in parent",
                expected=f"Pin '{label_name}' defined",
                actual="Defined",
                status="ok",
            ),
            DiagnosisItem(
                check="Hierarchical label in sub-schematic",
                expected=f"Label '{label_name}' exists",
                actual="Not found",
                status="error",
            ),
        ]

        # Possible causes
        explanation.possible_causes = [
            f"Hierarchical label '{label_name}' was deleted or never created",
            "Label name mismatch (typo or case difference)",
            "Sub-schematic file was replaced with different version",
            "Sheet instance UUID doesn't match sub-schematic",
        ]

        # Find similar labels
        if label_name:
            similar = self._find_similar_labels(label_name)
            if similar:
                explanation.similar_labels = similar
                best_match = similar[0]
                explanation.possible_causes.insert(
                    0, f"Label name mismatch: found '{best_match.name}' (similar)"
                )

        # Fixes
        explanation.fixes = [
            FixSuggestion(
                description=f"Add hierarchical label '{label_name}' in the sub-schematic",
                priority=1,
            ),
        ]

        if label_name and explanation.similar_labels:
            best = explanation.similar_labels[0]
            explanation.fixes.append(
                FixSuggestion(
                    description=f"Rename label '{best.name}' to '{label_name}' in sub-schematic",
                    priority=2,
                )
            )
            explanation.fixes.append(
                FixSuggestion(
                    description=f"Rename sheet pin '{label_name}' to '{best.name}' in parent",
                    priority=3,
                )
            )

        explanation.fixes.append(
            FixSuggestion(
                description="Delete the sheet pin if no longer needed",
                priority=4,
            )
        )

        # Related violations
        explanation.related_violations = [
            "pin_not_connected (often accompanies this error)",
        ]

        return explanation

    def _explain_pin_not_connected(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain unconnected pin errors."""
        pin_info = self._extract_pin_info(violation)
        component = pin_info.get("component", "unknown") if pin_info else "unknown"
        pin_name = pin_info.get("name", "unknown") if pin_info else "unknown"
        pin_type = pin_info.get("type", "unknown") if pin_info else "unknown"

        explanation = ViolationExplanation(
            violation=violation,
            summary=f"Pin '{pin_name}' on {component} is not connected to any net",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Pin connection status",
                expected="Connected to wire or label",
                actual="No connection",
                status="error",
            ),
            DiagnosisItem(
                check="Pin electrical type",
                actual=pin_type,
                status="info",
            ),
        ]

        explanation.possible_causes = [
            "Wire endpoint doesn't reach the pin",
            "Pin intentionally left unconnected (needs no-connect flag)",
            "Connection was accidentally deleted",
            "Symbol placed but not wired",
        ]

        explanation.fixes = [
            FixSuggestion(
                description=f"Connect a wire to pin '{pin_name}' of {component}",
                priority=1,
            ),
            FixSuggestion(
                description="Add a No-Connect (X) flag if pin should be unconnected",
                priority=2,
            ),
            FixSuggestion(
                description="Check wire endpoint alignment with pin",
                priority=3,
            ),
        ]

        # Related violations
        if pin_type == "power_in":
            explanation.related_violations = [
                "power_pin_not_driven (if this is a power input)",
            ]

        return explanation

    def _explain_pin_not_driven(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain input pin not driven errors."""
        explanation = ViolationExplanation(
            violation=violation,
            summary="Input pin is connected but not driven by any output",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Input pin connection",
                expected="Driven by output/bidirectional pin",
                actual="No driver found on net",
                status="error",
            ),
        ]

        explanation.possible_causes = [
            "Output pin not connected to the same net",
            "Net has only input pins (no driver)",
            "Missing pull-up/pull-down resistor for floating inputs",
            "Symbol pin type incorrectly set to 'input'",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Connect an output or bidirectional pin to drive this input",
                priority=1,
            ),
            FixSuggestion(
                description="Add a pull-up or pull-down resistor if floating is acceptable",
                priority=2,
            ),
            FixSuggestion(
                description="Check symbol pin electrical type in symbol editor",
                priority=3,
            ),
        ]

        return explanation

    def _explain_power_not_driven(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain power input not driven errors."""
        pin_info = self._extract_pin_info(violation)
        component = pin_info.get("component", "unknown") if pin_info else "unknown"
        pin_name = pin_info.get("name", "unknown") if pin_info else "unknown"

        explanation = ViolationExplanation(
            violation=violation,
            summary=f"Power pin '{pin_name}' on {component} not connected to power source",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Power pin connection",
                expected="Connected to power output (VCC, +3V3, etc.)",
                actual="No power source on net",
                status="error",
            ),
            DiagnosisItem(
                check="Pin electrical type",
                actual="power_in",
                status="info",
            ),
        ]

        explanation.possible_causes = [
            "Power symbol (VCC, GND, etc.) not placed on net",
            "Power symbol net name doesn't match (e.g., '+3V3' vs 'VCC')",
            "Power is supplied from PCB (needs PWR_FLAG)",
            "Wire not connected to power pin",
        ]

        explanation.fixes = [
            FixSuggestion(
                description=f"Add power symbol (VCC, +3V3, GND) to drive '{pin_name}'",
                priority=1,
            ),
            FixSuggestion(
                description="Add PWR_FLAG if power comes from external source (PCB, connector)",
                priority=2,
            ),
            FixSuggestion(
                description="Check that power symbol net name matches pin net",
                priority=3,
            ),
        ]

        return explanation

    def _explain_label_dangling(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain dangling label errors."""
        label_name = self._extract_label_name(violation)

        explanation = ViolationExplanation(
            violation=violation,
            summary=f"Label '{label_name}' is not connected to anything",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Label connection",
                expected="Connected to wire or pin",
                actual="No connection found",
                status="warning",
            ),
        ]

        explanation.possible_causes = [
            "Label placed but wire not connected",
            "Wire was deleted leaving orphaned label",
            "Label endpoint doesn't touch wire (grid alignment)",
            "Label intended for future connection",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Connect a wire to the label",
                priority=1,
            ),
            FixSuggestion(
                description="Delete the label if not needed",
                priority=2,
            ),
            FixSuggestion(
                description="Move label to connect to existing wire",
                priority=3,
            ),
        ]

        return explanation

    def _explain_global_label_dangling(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain dangling global label errors."""
        label_name = self._extract_label_name(violation)

        explanation = ViolationExplanation(
            violation=violation,
            summary=f"Global label '{label_name}' is not connected to anything",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Global label connection",
                expected="Connected to wire and used on other sheets",
                actual="No connection found",
                status="error",
            ),
        ]

        explanation.possible_causes = [
            "Global label not connected to wire",
            "Label only exists on one sheet (no other sheets use it)",
            "Spelling mismatch with labels on other sheets",
        ]

        if label_name:
            similar = self._find_similar_labels(label_name)
            if similar:
                explanation.similar_labels = similar
                explanation.possible_causes.insert(
                    0, f"Possible typo: found similar label '{similar[0].name}'"
                )

        explanation.fixes = [
            FixSuggestion(
                description="Connect a wire to the global label",
                priority=1,
            ),
            FixSuggestion(
                description="Verify label spelling matches other sheets",
                priority=2,
            ),
            FixSuggestion(
                description="Delete if this global net is no longer needed",
                priority=3,
            ),
        ]

        return explanation

    def _explain_similar_labels(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain similar labels warning."""
        labels = []
        for item in violation.items:
            if "label" in item.lower() and "'" in item:
                start = item.find("'") + 1
                end = item.find("'", start)
                if end > start:
                    labels.append(item[start:end])

        explanation = ViolationExplanation(
            violation=violation,
            summary=f"Labels '{labels[0]}' and '{labels[1]}' are similar (possible typo)"
            if len(labels) >= 2
            else "Similar labels detected",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Label similarity",
                expected="Unique, clearly different label names",
                actual=f"Found similar: {', '.join(labels)}",
                status="warning",
            ),
        ]

        explanation.possible_causes = [
            "Typo in one of the label names",
            "Intentional similar names for related signals",
            "Case sensitivity issue (SIG vs sig)",
        ]

        if len(labels) >= 2:
            explanation.fixes = [
                FixSuggestion(
                    description=f"Rename '{labels[0]}' to '{labels[1]}' if they should be same net",
                    priority=1,
                ),
                FixSuggestion(
                    description="Make names more distinct if intentionally different",
                    priority=2,
                ),
                FixSuggestion(
                    description="Add comments to clarify intentional similar names",
                    priority=3,
                ),
            ]

        return explanation

    def _explain_duplicate_reference(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain duplicate reference designator errors."""
        # Extract references from items
        refs = []
        for item in violation.items:
            if "symbol" in item.lower():
                parts = item.split()
                for i, part in enumerate(parts):
                    if part.lower() == "symbol" and i + 1 < len(parts):
                        refs.append(parts[i + 1])

        ref = refs[0] if refs else "unknown"

        explanation = ViolationExplanation(
            violation=violation,
            summary=f"Reference designator '{ref}' is used by multiple symbols",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Reference uniqueness",
                expected="Unique reference per symbol",
                actual=f"'{ref}' used multiple times",
                status="error",
            ),
        ]

        explanation.possible_causes = [
            "Copy-paste created duplicate symbols",
            "Manual annotation conflict",
            "Multi-unit symbol with wrong unit assignment",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Run 'Annotate Schematic' to reassign references",
                command="kct sch annotate <schematic>",
                priority=1,
            ),
            FixSuggestion(
                description="Manually edit one symbol's reference to be unique",
                priority=2,
            ),
            FixSuggestion(
                description="Delete duplicate symbol if unintended",
                priority=3,
            ),
        ]

        return explanation

    def _explain_wire_dangling(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain dangling wire errors."""
        explanation = ViolationExplanation(
            violation=violation,
            summary="Wire endpoint is not connected to anything",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Wire endpoint connection",
                expected="Connected to pin, junction, or label",
                actual="Dangling endpoint",
                status="warning",
            ),
        ]

        explanation.possible_causes = [
            "Wire drawn but not completed to destination",
            "Symbol moved leaving wire behind",
            "Wire endpoint off-grid (doesn't touch pin)",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Extend wire to connect to destination pin",
                priority=1,
            ),
            FixSuggestion(
                description="Delete the dangling wire segment",
                priority=2,
            ),
            FixSuggestion(
                description="Add No-Connect flag if intentionally unconnected",
                priority=3,
            ),
        ]

        return explanation

    def _explain_nc_connected(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain no-connect flag connected errors."""
        explanation = ViolationExplanation(
            violation=violation,
            summary="No-connect flag is connected to something",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="No-connect usage",
                expected="NC flag on unconnected pin only",
                actual="NC flag has connection",
                status="error",
            ),
        ]

        explanation.possible_causes = [
            "Wire accidentally connected to NC pin",
            "NC flag placed on wrong pin",
            "Design changed but NC flag not removed",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Remove the wire from the no-connect pin",
                priority=1,
            ),
            FixSuggestion(
                description="Remove the NC flag if connection is intentional",
                priority=2,
            ),
        ]

        return explanation

    def _explain_nc_dangling(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain no-connect flag dangling errors."""
        explanation = ViolationExplanation(
            violation=violation,
            summary="No-connect flag is not on a pin",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="No-connect placement",
                expected="NC flag directly on unconnected pin",
                actual="NC flag floating/not on pin",
                status="warning",
            ),
        ]

        explanation.possible_causes = [
            "NC flag placed near but not on pin",
            "Symbol moved leaving NC flag behind",
            "NC flag off-grid (doesn't touch pin)",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Move NC flag directly onto the pin endpoint",
                priority=1,
            ),
            FixSuggestion(
                description="Delete NC flag if not needed",
                priority=2,
            ),
        ]

        return explanation

    def _explain_different_unit_net(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain different unit net errors."""
        explanation = ViolationExplanation(
            violation=violation,
            summary="Same pin on different units connected to different nets",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Multi-unit pin consistency",
                expected="Same pin connected to same net across units",
                actual="Different nets on same pin",
                status="error",
            ),
        ]

        explanation.possible_causes = [
            "Different units accidentally connected to wrong nets",
            "Symbol definition error (pin should be per-unit)",
            "Wiring mistake in one unit",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Connect all units' pins to the same net",
                priority=1,
            ),
            FixSuggestion(
                description="Check symbol definition if pins should be different",
                priority=2,
            ),
        ]

        return explanation

    def _explain_missing_unit(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain missing unit errors."""
        explanation = ViolationExplanation(
            violation=violation,
            summary="Multi-unit symbol is missing one or more units",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Unit completeness",
                expected="All units of multi-unit symbol placed",
                actual="Some units missing",
                status="error",
            ),
        ]

        explanation.possible_causes = [
            "Not all units were placed from symbol",
            "Unit was deleted accidentally",
            "Design doesn't need all units (may be OK)",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Add missing unit(s) from the symbol library",
                priority=1,
            ),
            FixSuggestion(
                description="Verify all required units are present for your design",
                priority=2,
            ),
        ]

        return explanation

    def _explain_unannotated(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain unannotated symbol errors."""
        explanation = ViolationExplanation(
            violation=violation,
            summary="Symbol has no reference designator assigned",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Reference annotation",
                expected="Unique reference like R1, C2, U3",
                actual="Reference is '?' or empty",
                status="error",
            ),
        ]

        explanation.possible_causes = [
            "Symbol newly placed and not yet annotated",
            "Annotation was skipped or failed",
            "Reference field cleared accidentally",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Run 'Annotate Schematic' from Tools menu",
                command="kct sch annotate <schematic>",
                priority=1,
            ),
            FixSuggestion(
                description="Manually enter reference designator (press E to edit)",
                priority=2,
            ),
        ]

        return explanation

    def _explain_multiple_net_names(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain multiple net names error."""
        explanation = ViolationExplanation(
            violation=violation,
            summary="Wire has multiple conflicting net names assigned",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Net name uniqueness",
                expected="Single net name per wire segment",
                actual="Multiple labels on same wire",
                status="error",
            ),
        ]

        explanation.possible_causes = [
            "Multiple labels placed on same wire",
            "Hierarchical connection creates name conflict",
            "Net tie needed but not placed",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Remove duplicate labels, keep only one",
                priority=1,
            ),
            FixSuggestion(
                description="Use net tie symbol if nets should be connected",
                priority=2,
            ),
            FixSuggestion(
                description="Review hierarchical connections for conflicts",
                priority=3,
            ),
        ]

        return explanation

    def _explain_off_grid(self, violation: ERCViolation) -> ViolationExplanation:
        """Explain off-grid endpoint errors."""
        explanation = ViolationExplanation(
            violation=violation,
            summary="Wire or pin endpoint is not aligned to the grid",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Grid alignment",
                expected="Endpoint on grid intersection",
                actual="Off-grid position",
                status="warning",
            ),
        ]

        explanation.possible_causes = [
            "Wire drawn with grid snap disabled",
            "Symbol with non-standard pin grid",
            "Imported schematic with different grid",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Move endpoint to snap to grid",
                priority=1,
            ),
            FixSuggestion(
                description="Use Edit > Cleanup Graphics to fix all off-grid items",
                priority=2,
            ),
        ]

        return explanation

    def _explain_generic(self, violation: ERCViolation) -> ViolationExplanation:
        """Generic explanation for unknown violation types."""
        explanation = ViolationExplanation(
            violation=violation,
            summary=f"{violation.type_description}: {violation.description}",
        )

        explanation.diagnosis = [
            DiagnosisItem(
                check="Violation detected",
                actual=violation.description,
                status="error" if violation.is_error else "warning",
            ),
        ]

        explanation.possible_causes = [
            "Review the specific error message for details",
            "Check KiCad ERC documentation for this error type",
        ]

        explanation.fixes = [
            FixSuggestion(
                description="Review and fix the reported issue",
                priority=1,
            ),
            FixSuggestion(
                description=f"Check ERC settings for '{violation.type_str}' if this is a false positive",
                priority=2,
            ),
        ]

        # Use existing suggestions if available
        if violation.suggestions:
            explanation.fixes = [
                FixSuggestion(description=s, priority=i + 1)
                for i, s in enumerate(violation.suggestions)
            ]

        return explanation


def gather_labels_from_schematic(schematic_path: Path) -> list[str]:
    """Gather all label names from a schematic (including hierarchy)."""
    labels = []

    try:
        from kicad_tools.schema.hierarchy import build_hierarchy
        from kicad_tools.schema.schematic import Schematic

        # Try to get labels from hierarchy
        try:
            root = build_hierarchy(str(schematic_path))
            for node in root.all_nodes():
                labels.extend(node.hierarchical_labels)
        except Exception:
            pass

        # Also get labels from main schematic
        try:
            sch = Schematic.load(schematic_path)
            for lbl in sch.labels:
                if hasattr(lbl, "name"):
                    labels.append(lbl.name)
            for glbl in sch.global_labels:
                if hasattr(glbl, "name"):
                    labels.append(glbl.name)
        except Exception:
            pass

    except ImportError:
        pass

    return list(set(labels))  # Remove duplicates


def run_erc_explain(
    input_path: Path,
    output_path: Path | None = None,
    keep_report: bool = False,
) -> tuple[ERCReport | None, Path | None]:
    """Run ERC on a schematic and return the report."""
    if input_path.suffix == ".kicad_sch":
        # Run ERC
        kicad_cli = find_kicad_cli()
        if not kicad_cli:
            print("Error: kicad-cli not found", file=sys.stderr)
            return None, None

        result = run_erc(input_path, output_path)
        if not result.success:
            print(f"Error running ERC: {result.stderr}", file=sys.stderr)
            return None, None

        if result.output_path is None:
            print("Error: ERC did not produce output", file=sys.stderr)
            return None, None

        try:
            report = ERCReport.load(result.output_path)
            return report, input_path
        except Exception as e:
            print(f"Error parsing ERC report: {e}", file=sys.stderr)
            return None, None
        finally:
            if not keep_report and output_path is None:
                result.output_path.unlink(missing_ok=True)

    elif input_path.suffix in (".json", ".rpt"):
        # Load existing report
        try:
            report = ERCReport.load(input_path)
            # Try to find schematic from report
            schematic_path = None
            if report.source_file:
                candidate = input_path.parent / report.source_file
                if candidate.exists():
                    schematic_path = candidate
            return report, schematic_path
        except Exception as e:
            print(f"Error loading report: {e}", file=sys.stderr)
            return None, None

    else:
        print(f"Error: Unsupported file type: {input_path.suffix}", file=sys.stderr)
        return None, None


def output_text(explanations: list[ViolationExplanation], report: ERCReport) -> None:
    """Output explanations as formatted text."""
    error_count = sum(1 for e in explanations if e.violation.is_error)
    warning_count = len(explanations) - error_count

    print("\n" + "=" * 70)
    print("ERC ERROR ANALYSIS")
    print("=" * 70)

    if report.source_file:
        print(f"File: {Path(report.source_file).name}")
    print(f"Total: {error_count} errors, {warning_count} warnings\n")

    for i, exp in enumerate(explanations, 1):
        v = exp.violation
        severity = "ERROR" if v.is_error else "WARNING"
        icon = "X" if v.is_error else "!"

        print("-" * 70)
        print(f"\n[{icon}] {severity} #{i}: {v.type_description}")
        print(f"    {v.description}")

        if v.sheet or v.pos_x or v.pos_y:
            loc_parts = []
            if v.sheet:
                loc_parts.append(f"Sheet: {v.sheet}")
            if v.pos_x or v.pos_y:
                loc_parts.append(f"@ ({v.pos_x:.2f}, {v.pos_y:.2f})")
            print(f"    Location: {' '.join(loc_parts)}")

        print(f"\n    Summary: {exp.summary}")

        # Diagnosis
        if exp.diagnosis:
            print("\n    Diagnosis:")
            for d in exp.diagnosis:
                status_icon = {
                    "ok": "✓",
                    "error": "✗",
                    "warning": "!",
                    "info": "•",
                }.get(d.status, "•")
                print(f"      {status_icon} {d.check}")
                if d.expected:
                    print(f"        Expected: {d.expected}")
                if d.actual:
                    print(f"        Actual:   {d.actual}")

        # Similar labels (fuzzy matches)
        if exp.similar_labels:
            print("\n    Similar labels found:")
            for s in exp.similar_labels:
                similarity_pct = int(s.similarity * 100)
                print(f"      - '{s.name}' ({similarity_pct}% similar)")

        # Possible causes
        if exp.possible_causes:
            print("\n    Possible causes:")
            for cause in exp.possible_causes:
                print(f"      • {cause}")

        # Suggested fixes
        if exp.fixes:
            print("\n    Suggested fixes:")
            for j, fix in enumerate(exp.fixes, 1):
                print(f"      {j}. {fix.description}")
                if fix.command:
                    print(f"         Command: {fix.command}")

        # Related violations
        if exp.related_violations:
            print("\n    Related violations:")
            for rel in exp.related_violations:
                print(f"      → {rel}")

    print("\n" + "=" * 70)
    if error_count > 0:
        print("Fix errors before proceeding with design.")
    else:
        print("Review warnings and fix if needed.")


def output_json(explanations: list[ViolationExplanation], report: ERCReport) -> None:
    """Output explanations as JSON."""
    data = {
        "source": report.source_file,
        "kicad_version": report.kicad_version,
        "summary": {
            "errors": sum(1 for e in explanations if e.violation.is_error),
            "warnings": sum(1 for e in explanations if not e.violation.is_error),
        },
        "explanations": [e.to_dict() for e in explanations],
    }
    print(json.dumps(data, indent=2))


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct erc explain command."""
    parser = argparse.ArgumentParser(
        prog="kct erc explain",
        description="Detailed ERC error analysis with root cause and fixes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input",
        help="Schematic (.kicad_sch) to check or ERC report (.json/.rpt) to analyze",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--errors-only",
        action="store_true",
        help="Show only errors, not warnings",
    )
    parser.add_argument(
        "--type",
        "-t",
        dest="filter_type",
        help="Filter by violation type",
    )
    parser.add_argument(
        "--keep-report",
        action="store_true",
        help="Keep the ERC report file after running",
    )

    args = parser.parse_args(argv)
    input_path = Path(args.input)

    if not input_path.exists():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return 1

    # Run ERC or load report
    report, schematic_path = run_erc_explain(input_path, keep_report=args.keep_report)
    if report is None:
        return 1

    # Gather labels for fuzzy matching
    all_labels = []
    if schematic_path:
        all_labels = gather_labels_from_schematic(schematic_path)

    # Filter violations
    violations = [v for v in report.violations if not v.excluded]

    if args.errors_only:
        violations = [v for v in violations if v.is_error]

    if args.filter_type:
        filter_lower = args.filter_type.lower()
        violations = [
            v
            for v in violations
            if filter_lower in v.type_str.lower() or filter_lower in v.description.lower()
        ]

    if not violations:
        print("No ERC violations to explain.")
        return 0

    # Generate explanations
    explainer = ERCExplainer(schematic_path=schematic_path, all_labels=all_labels)
    explanations = [explainer.explain(v) for v in violations]

    # Output
    if args.format == "json":
        output_json(explanations, report)
    else:
        output_text(explanations, report)

    # Return error code
    error_count = sum(1 for e in explanations if e.violation.is_error)
    return 1 if error_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

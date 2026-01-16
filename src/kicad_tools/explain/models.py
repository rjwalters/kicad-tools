"""Data models for the explain system.

This module defines the data structures used to represent rule explanations,
spec references, and explanation results.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SpecReference:
    """Reference to an external specification.

    Attributes:
        name: Name of the specification (e.g., "JLCPCB Manufacturing Capabilities")
        section: Section within the spec (e.g., "PCB Specifications > Minimum Clearance")
        url: URL to the specification document
        version: Version or date of the specification
    """

    name: str
    section: str = ""
    url: str = ""
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "section": self.section,
            "url": self.url,
            "version": self.version,
        }


@dataclass
class RuleExplanation:
    """Explanation for a design rule.

    Attributes:
        rule_id: Unique identifier for the rule (e.g., "trace_clearance")
        title: Human-readable title
        explanation: Detailed explanation of why the rule exists
        spec_references: List of specification references
        fix_templates: Templates for fix suggestions with {placeholders}
        related_rules: List of related rule IDs
        learn_more: Optional path to additional documentation
        severity: Default severity level ("error", "warning", "info")
    """

    rule_id: str
    title: str
    explanation: str
    spec_references: list[SpecReference] = field(default_factory=list)
    fix_templates: list[str] = field(default_factory=list)
    related_rules: list[str] = field(default_factory=list)
    learn_more: str | None = None
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "explanation": self.explanation,
            "spec_references": [ref.to_dict() for ref in self.spec_references],
            "fix_templates": self.fix_templates,
            "related_rules": self.related_rules,
            "learn_more": self.learn_more,
            "severity": self.severity,
        }


@dataclass
class ExplanationResult:
    """Result of explaining a rule or violation.

    Attributes:
        rule: The rule ID that was explained
        title: Human-readable title
        explanation: Detailed explanation
        spec_reference: Primary spec reference (if any)
        current_value: Current/actual value (if applicable)
        required_value: Required/minimum value (if applicable)
        unit: Unit of measurement (e.g., "mm", "Ω")
        severity: Severity level
        fix_suggestions: Contextualized fix suggestions
        related_rules: Related rule IDs
        context: Additional context provided in the query
    """

    rule: str
    title: str
    explanation: str
    spec_reference: SpecReference | None = None
    current_value: float | None = None
    required_value: float | None = None
    unit: str = ""
    severity: str = "error"
    fix_suggestions: list[str] = field(default_factory=list)
    related_rules: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "rule": self.rule,
            "title": self.title,
            "explanation": self.explanation,
            "spec_reference": self.spec_reference.to_dict() if self.spec_reference else None,
            "current_value": self.current_value,
            "required_value": self.required_value,
            "unit": self.unit,
            "severity": self.severity,
            "fix_suggestions": self.fix_suggestions,
            "related_rules": self.related_rules,
            "context": self.context,
        }

    def format_tree(self) -> str:
        """Format as a tree structure for terminal output."""
        lines = [f"{self.title}"]

        if self.spec_reference:
            lines.append(f"├─ Spec: {self.spec_reference.name}")
            if self.spec_reference.section:
                lines.append(f"│  Section: {self.spec_reference.section}")
            if self.spec_reference.version:
                lines.append(f"│  Version: {self.spec_reference.version}")

        lines.append(f"├─ Rationale: {self.explanation}")

        if self.current_value is not None and self.required_value is not None:
            lines.append(f"├─ Current: {self.current_value}{self.unit}")
            lines.append(f"├─ Required: {self.required_value}{self.unit}")

        if self.fix_suggestions:
            lines.append(f"├─ Fix: {self.fix_suggestions[0]}")
            for suggestion in self.fix_suggestions[1:]:
                lines.append(f"│  Or: {suggestion}")

        if self.related_rules:
            lines.append(f"└─ Related: {', '.join(self.related_rules)}")

        return "\n".join(lines)


@dataclass
class ExplainedViolation:
    """A DRC violation with its explanation attached.

    Attributes:
        violation: The original DRC violation
        explanation: The explanation for this violation type
    """

    violation: Any  # DRCViolation from kicad_tools.drc
    explanation: ExplanationResult

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        violation_dict = (
            self.violation.to_dict() if hasattr(self.violation, "to_dict") else {}
        )
        return {
            "violation": violation_dict,
            "explanation": self.explanation.to_dict(),
        }


@dataclass
class InterfaceSpec:
    """Specification for a communication interface.

    Attributes:
        interface: Interface name (e.g., "USB 2.0 High Speed")
        spec_document: Official specification document name
        spec_url: URL to the specification
        constraints: Dictionary of constraint definitions
    """

    interface: str
    spec_document: str
    spec_url: str = ""
    constraints: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "interface": self.interface,
            "spec_document": self.spec_document,
            "spec_url": self.spec_url,
            "constraints": self.constraints,
        }
